"use client";

import { Header } from "@/components/layout/Header";
import { Sidebar } from "@/components/layout/Sidebar";
import { PageShell } from "@/components/layout/PageShell";
import { useWebSocketConnection } from "@/hooks/useWebSocket";
import { useAuthStore } from "@/stores/auth";
import { useCallback, useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import type { User } from "@/types";

interface AuthTokenPayload {
  sub: string;
  username?: string;
  avatar?: string | null;
  guild_id?: string | null;
  is_admin?: boolean;
  is_owner?: boolean;
  tfa_pending?: boolean;
}

function decodeAuthToken(accessToken: string): AuthTokenPayload | null {
  const tokenParts = accessToken.split(".");
  if (tokenParts.length < 2) return null;

  try {
    // JWT payloads use base64url encoding and must be converted back to
    // standard base64 before browser decoding.
    const base64Payload = tokenParts[1].replace(/-/g, "+").replace(/_/g, "/");
    const paddingLength = (4 - (base64Payload.length % 4)) % 4;
    const paddedPayload = base64Payload.padEnd(
      base64Payload.length + paddingLength,
      "="
    );
    return JSON.parse(atob(paddedPayload)) as AuthTokenPayload;
  } catch {
    return null;
  }
}

function OAuthHandler() {
  const searchParams = useSearchParams();
  const login = useAuthStore((s) => s.login);
  const [showGuildPicker, setShowGuildPicker] = useState(false);
  const [guilds, setGuilds] = useState<Array<{ id: string; name: string; icon: string | null }>>([]);
  const [partialToken, setPartialToken] = useState<string | null>(null);
  const [show2FA, setShow2FA] = useState(false);
  const [tfaCode, setTfaCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectGuild = useCallback(async (guildId: string, authToken?: string) => {
    const tkn = authToken || partialToken;
    if (!tkn) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch("/api/v2/auth/select-guild", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${tkn}`,
        },
        body: JSON.stringify({ guild_id: guildId }),
      });

      const data = await res.json();

      if (!res.ok) {
        setError(data.detail || "Guild selection failed.");
        setLoading(false);
        return;
      }

      const accessToken = data.access_token;
      const payload = decodeAuthToken(accessToken);

      if (!payload) {
        setError("Invalid authentication token received.");
        setLoading(false);
        return;
      }

      if (payload.tfa_pending) {
        // 2FA required — show 2FA form
        setPartialToken(accessToken);
        setShowGuildPicker(false);
        setShow2FA(true);
        setLoading(false);
        return;
      }

      const user: User = {
        id: payload.sub,
        username: payload.username || "",
        avatar: payload.avatar || null,
        guildId: payload.guild_id || null,
        isAdmin: payload.is_admin || false,
        isOwner: payload.is_owner || false,
      };

      login(accessToken, user);
      setShowGuildPicker(false);
      setLoading(false);
    } catch {
      setError("Failed to select server.");
      setLoading(false);
    }
  }, [login, partialToken]);

  const handleOAuthRedirect = useCallback(async (urlToken: string | null, authError: string | null) => {
    if (authError) {
      setError(`Login failed: ${authError.replace(/_/g, " ")}`);
      // Clean URL
      window.history.replaceState({}, "", "/dashboard");
      return;
    }

    if (!urlToken) return;

    // Clean URL immediately
    window.history.replaceState({}, "", "/dashboard");

    // This is a partial token from OAuth callback — fetch guilds
    setPartialToken(urlToken);
    setLoading(true);

    fetch("/api/v2/auth/guilds", {
      headers: { Authorization: `Bearer ${urlToken}` },
    })
      .then((res) => res.json())
      .then((data) => {
        const guildList = data.guilds || [];
        if (guildList.length === 1) {
          // Auto-select if only one guild
          selectGuild(guildList[0].id, urlToken);
        } else if (guildList.length === 0) {
          setError("No mutual servers found. Make sure the bot is in your server.");
          setLoading(false);
        } else {
          setGuilds(guildList);
          setShowGuildPicker(true);
          setLoading(false);
        }
      })
      .catch(() => {
        setError("Failed to fetch server list.");
        setLoading(false);
      });
  }, [selectGuild]);

  useEffect(() => {
    const urlToken = searchParams.get("token");
    const authError = searchParams.get("auth_error");

    // Defer the OAuth bootstrap by one tick because this mount-time URL parser
    // immediately updates React state, and React's current hook lint rules
    // reject doing that synchronously inside the first effect execution.
    const timer = window.setTimeout(() => {
      void handleOAuthRedirect(urlToken, authError);
    }, 0);

    return () => window.clearTimeout(timer);
  }, [handleOAuthRedirect, searchParams]);

  const verify2FA = async () => {
    if (!partialToken || tfaCode.length !== 6) return;
    setLoading(true);
    setError(null);

    try {
      const res = await fetch("/api/v2/auth/2fa/verify", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${partialToken}`,
        },
        body: JSON.stringify({ code: tfaCode }),
      });

      const data = await res.json();
      if (!res.ok) {
        setError(data.detail || "Invalid 2FA code.");
        setLoading(false);
        return;
      }

      const accessToken = data.access_token;
      const payload = decodeAuthToken(accessToken);

      if (!payload) {
        setError("Invalid authentication token received.");
        setLoading(false);
        return;
      }

      const user: User = {
        id: payload.sub,
        username: payload.username || "",
        avatar: payload.avatar || null,
        guildId: payload.guild_id || null,
        isAdmin: payload.is_admin || false,
        isOwner: payload.is_owner || false,
      };

      login(accessToken, user);
      setShow2FA(false);
      setLoading(false);
    } catch {
      setError("2FA verification failed.");
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
        <div className="rounded-lg border border-border bg-card p-6 shadow-lg">
          <p className="text-sm text-muted-foreground">Logging in...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
        <div className="mx-4 max-w-sm rounded-lg border border-border bg-card p-6 shadow-lg">
          <p className="mb-4 text-sm text-destructive">{error}</p>
          <button
            onClick={() => setError(null)}
            className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
          >
            Dismiss
          </button>
        </div>
      </div>
    );
  }

  if (showGuildPicker) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
        <div className="mx-4 w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-lg">
          <h2 className="mb-4 text-lg font-semibold">Select a Server</h2>
          <div className="space-y-2">
            {guilds.map((g) => (
              <button
                key={g.id}
                onClick={() => selectGuild(g.id)}
                className="flex w-full items-center gap-3 rounded-md border border-border p-3 text-left transition-colors hover:bg-accent"
              >
                {g.icon ? (
                  <img
                    src={`https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=40`}
                    alt=""
                    className="size-8 rounded-full"
                  />
                ) : (
                  <div className="flex size-8 items-center justify-center rounded-full bg-muted text-xs font-bold">
                    {g.name.charAt(0)}
                  </div>
                )}
                <span className="text-sm font-medium">{g.name}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (show2FA) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
        <div className="mx-4 w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-lg">
          <h2 className="mb-2 text-lg font-semibold">Two-Factor Authentication</h2>
          <p className="mb-4 text-sm text-muted-foreground">
            Enter your 6-digit authenticator code
          </p>
          <input
            type="text"
            inputMode="numeric"
            maxLength={6}
            value={tfaCode}
            onChange={(e) => setTfaCode(e.target.value.replace(/\D/g, ""))}
            onKeyDown={(e) => e.key === "Enter" && verify2FA()}
            className="mb-4 w-full rounded-md border border-input bg-background px-3 py-2 text-center text-lg font-mono tracking-widest"
            placeholder="000000"
            autoFocus
          />
          <button
            onClick={verify2FA}
            disabled={tfaCode.length !== 6}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            Verify
          </button>
        </div>
      </div>
    );
  }

  return null;
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Establish WebSocket connection for the dashboard
  useWebSocketConnection();

  return (
    <div className="flex min-h-dvh">
      {/* OAuth + guild selection handler */}
      <Suspense fallback={null}>
        <OAuthHandler />
      </Suspense>

      <Sidebar />

      <div className="flex min-w-0 flex-1 flex-col">
        <Header />
        <main className="flex-1 overflow-x-hidden">
          <div className="mx-auto w-full max-w-7xl p-4 md:p-6">
            <PageShell>{children}</PageShell>
          </div>
        </main>
      </div>
    </div>
  );
}
