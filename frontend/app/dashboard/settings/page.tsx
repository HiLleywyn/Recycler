"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Bell, Palette, Shield, Globe, CheckCircle, XCircle, Gamepad2 } from "lucide-react";
import { useTheme } from "@/components/providers/theme-provider";
import { useAuthStore } from "@/stores/auth";

interface GameplaySettings {
  auto_levelup: boolean;
}

interface NotificationPrefs {
  dm_mining: boolean;
  dm_transfer: boolean;
  dm_validator: boolean;
  dm_staking: boolean;
  dm_2fa: boolean;
}

const NOTIF_ITEMS: { key: keyof NotificationPrefs; label: string; desc: string }[] = [
  { key: "dm_mining", label: "Mining Notifications", desc: "Get a DM when your mining rig finds a block" },
  { key: "dm_transfer", label: "Transfer Notifications", desc: "Get a DM when you receive a token transfer" },
  { key: "dm_validator", label: "Validator Notifications", desc: "Get a DM for validator events and rewards" },
  { key: "dm_staking", label: "Staking Notifications", desc: "Get a DM when staking rewards are distributed" },
  { key: "dm_2fa", label: "2FA Notifications", desc: "Get a DM for two-factor authentication events" },
];

export default function SettingsPage() {
  const { theme, setTheme } = useTheme();
  const user = useAuthStore((s) => s.user);
  const token = useAuthStore((s) => s.token);

  // Notification preferences state
  const [notifPrefs, setNotifPrefs] = useState<NotificationPrefs | null>(null);
  const [notifLoading, setNotifLoading] = useState(false);
  const [notifSaving, setNotifSaving] = useState<keyof NotificationPrefs | null>(null);

  useEffect(() => {
    if (!token) return;
    setNotifLoading(true);
    fetch("/api/v2/notifications/preferences", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => setNotifPrefs(data))
      .catch(() => {})
      .finally(() => setNotifLoading(false));
  }, [token]);

  const [notifError, setNotifError] = useState<string | null>(null);

  const handleNotifToggle = async (key: keyof NotificationPrefs) => {
    if (!token || !notifPrefs) return;
    const newVal = !notifPrefs[key];
    setNotifPrefs((prev) => prev ? { ...prev, [key]: newVal } : prev);
    setNotifSaving(key);
    setNotifError(null);
    try {
      const res = await fetch("/api/v2/notifications/preferences", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ [key]: newVal }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to save preference.");
      }
    } catch (e: unknown) {
      // revert on failure and show error
      setNotifPrefs((prev) => prev ? { ...prev, [key]: !newVal } : prev);
      setNotifError(e instanceof Error ? e.message : "Failed to save preference.");
    } finally {
      setNotifSaving(null);
    }
  };

  // Gameplay settings state
  const [gameplaySettings, setGameplaySettings] = useState<GameplaySettings | null>(null);
  const [gameplayLoading, setGameplayLoading] = useState(false);
  const [gameplaySaving, setGameplaySaving] = useState(false);
  const [gameplayError, setGameplayError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setGameplayLoading(true);
    fetch("/api/v2/users/me/settings", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => setGameplaySettings({ auto_levelup: data.auto_levelup ?? false }))
      .catch(() => {})
      .finally(() => setGameplayLoading(false));
  }, [token]);

  const handleAutoLevelupToggle = async () => {
    if (!token || !gameplaySettings) return;
    const newVal = !gameplaySettings.auto_levelup;
    setGameplaySettings((prev) => prev ? { ...prev, auto_levelup: newVal } : prev);
    setGameplaySaving(true);
    setGameplayError(null);
    try {
      const res = await fetch("/api/v2/users/me/settings", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ auto_levelup: newVal }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to save setting.");
      }
    } catch (e: unknown) {
      setGameplaySettings((prev) => prev ? { ...prev, auto_levelup: !newVal } : prev);
      setGameplayError(e instanceof Error ? e.message : "Failed to save setting.");
    } finally {
      setGameplaySaving(false);
    }
  };

  // 2FA state
  const [tfaEnabled, setTfaEnabled] = useState(false);
  const [tfaLoading, setTfaLoading] = useState(false);
  const [tfaSetup, setTfaSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [tfaCode, setTfaCode] = useState("");
  const [tfaMessage, setTfaMessage] = useState("");

  useEffect(() => {
    if (!token) return;
    fetch("/api/v2/auth/2fa/status", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => setTfaEnabled(data.enabled))
      .catch(() => {});
  }, [token]);

  const handle2FASetup = async () => {
    if (!token) return;
    setTfaLoading(true);
    setTfaMessage("");
    try {
      const res = await fetch("/api/v2/auth/2fa/setup", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
      });
      const data = await res.json();
      if (!res.ok) {
        setTfaMessage(data.detail || "Failed to start 2FA setup.");
        return;
      }
      setTfaSetup({ secret: data.secret, uri: data.uri });
    } catch {
      setTfaMessage("Failed to start 2FA setup.");
    } finally {
      setTfaLoading(false);
    }
  };

  const handleVerify2FA = async () => {
    if (!token || tfaCode.length !== 6) return;
    setTfaLoading(true);
    setTfaMessage("");
    try {
      const res = await fetch("/api/v2/auth/2fa/verify-setup", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ code: tfaCode }),
      });
      const data = await res.json();
      if (!res.ok) {
        setTfaMessage(data.detail || "Invalid code.");
        return;
      }
      setTfaEnabled(true);
      setTfaSetup(null);
      setTfaCode("");
      setTfaMessage("2FA enabled successfully!");
    } catch {
      setTfaMessage("Verification failed.");
    } finally {
      setTfaLoading(false);
    }
  };

  const handleDisable2FA = async () => {
    if (!token) return;
    const code = prompt("Enter your 6-digit authenticator code to disable 2FA:");
    if (!code || code.length !== 6) return;
    setTfaLoading(true);
    setTfaMessage("");
    try {
      const res = await fetch("/api/v2/auth/2fa/disable", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ code }),
      });
      const data = await res.json();
      if (!res.ok) {
        setTfaMessage(data.detail || "Failed to disable 2FA.");
        return;
      }
      setTfaEnabled(false);
      setTfaMessage("2FA disabled.");
    } catch {
      setTfaMessage("Failed to disable 2FA.");
    } finally {
      setTfaLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Manage your account preferences
        </p>
      </div>

      {/* Appearance */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Palette className="size-4" />
            Appearance
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label>Theme</Label>
              <p className="text-xs text-muted-foreground">
                Choose between light and dark mode
              </p>
            </div>
            <div className="flex gap-2">
              {(["light", "dark", "system"] as const).map((t) => (
                <Button
                  key={t}
                  variant={theme === t ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTheme(t)}
                >
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </Button>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Notifications */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Bell className="size-4" />
            Notifications
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {!user ? (
            <p className="text-sm text-muted-foreground">Log in to manage notification preferences.</p>
          ) : notifLoading ? (
            <div className="space-y-4">
              {NOTIF_ITEMS.map((item) => (
                <div key={item.key}>
                  <div className="flex items-center justify-between">
                    <div className="space-y-1">
                      <Skeleton className="h-4 w-36" />
                      <Skeleton className="h-3 w-56" />
                    </div>
                    <Skeleton className="h-5 w-9 rounded-full" />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <>
              {NOTIF_ITEMS.map((item, i) => (
                <div key={item.key}>
                  <div className="flex items-center justify-between">
                    <div>
                      <Label>{item.label}</Label>
                      <p className="text-xs text-muted-foreground">{item.desc}</p>
                    </div>
                    <Switch
                      checked={notifPrefs ? notifPrefs[item.key] : false}
                      disabled={notifSaving === item.key}
                      onCheckedChange={() => handleNotifToggle(item.key)}
                    />
                  </div>
                  {i < NOTIF_ITEMS.length - 1 && <Separator className="mt-4" />}
                </div>
              ))}
              {notifError && (
                <p className="text-xs text-destructive">{notifError}</p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Gameplay */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Gamepad2 className="size-4" />
            Gameplay
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {!user ? (
            <p className="text-sm text-muted-foreground">Log in to manage gameplay settings.</p>
          ) : gameplayLoading ? (
            <div className="flex items-center justify-between">
              <div className="space-y-1">
                <Skeleton className="h-4 w-36" />
                <Skeleton className="h-3 w-56" />
              </div>
              <Skeleton className="h-5 w-9 rounded-full" />
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <div>
                  <Label>Auto Level-up Items</Label>
                  <p className="text-xs text-muted-foreground">
                    Automatically pay SUN and level up your stones when XP threshold is met
                  </p>
                </div>
                <Switch
                  checked={gameplaySettings ? gameplaySettings.auto_levelup : false}
                  disabled={gameplaySaving}
                  onCheckedChange={() => handleAutoLevelupToggle()}
                />
              </div>
              {gameplayError && (
                <p className="text-xs text-destructive">{gameplayError}</p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Security / 2FA */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Shield className="size-4" />
            Security
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label>Two-Factor Authentication</Label>
              <p className="text-xs text-muted-foreground">
                Add an extra layer of security to your account
              </p>
            </div>
            <div className="flex items-center gap-2">
              {tfaEnabled ? (
                <span className="flex items-center gap-1 text-xs text-green-500">
                  <CheckCircle className="size-3" /> Enabled
                </span>
              ) : (
                <span className="flex items-center gap-1 text-xs text-muted-foreground">
                  <XCircle className="size-3" /> Disabled
                </span>
              )}
            </div>
          </div>

          {!user && (
            <p className="text-xs text-muted-foreground">
              Log in to manage two-factor authentication.
            </p>
          )}

          {user && !tfaEnabled && !tfaSetup && (
            <Button
              variant="outline"
              size="sm"
              onClick={handle2FASetup}
              disabled={tfaLoading}
            >
              {tfaLoading ? "Setting up..." : "Enable 2FA"}
            </Button>
          )}

          {user && tfaEnabled && (
            <Button
              variant="destructive"
              size="sm"
              onClick={handleDisable2FA}
              disabled={tfaLoading}
            >
              {tfaLoading ? "Disabling..." : "Disable 2FA"}
            </Button>
          )}

          {tfaSetup && (
            <div className="space-y-3 rounded-lg border border-border p-4">
              <p className="text-sm font-medium">
                Add this secret to your authenticator app:
              </p>
              <code className="block rounded bg-muted px-3 py-2 text-xs font-mono break-all">
                {tfaSetup.secret}
              </code>
              <p className="text-xs text-muted-foreground">
                Or scan the QR code with your authenticator app using this URI.
              </p>
              <div className="flex gap-2">
                <Input
                  type="text"
                  inputMode="numeric"
                  maxLength={6}
                  placeholder="Enter 6-digit code"
                  value={tfaCode}
                  onChange={(e) => setTfaCode(e.target.value.replace(/\D/g, ""))}
                  onKeyDown={(e) => e.key === "Enter" && handleVerify2FA()}
                  className="font-mono"
                />
                <Button
                  size="sm"
                  onClick={handleVerify2FA}
                  disabled={tfaCode.length !== 6 || tfaLoading}
                >
                  Verify
                </Button>
              </div>
            </div>
          )}

          {tfaMessage && (
            <p className="text-xs text-muted-foreground">{tfaMessage}</p>
          )}
        </CardContent>
      </Card>

      {/* Server/Guild */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe className="size-4" />
            Server
          </CardTitle>
        </CardHeader>
        <CardContent>
          {user?.guildId ? (
            <p className="text-sm text-muted-foreground">
              Connected to server <span className="font-medium text-foreground">{user.guildId}</span>
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              No server selected. Log in to select a server.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
