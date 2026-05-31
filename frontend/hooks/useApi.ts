"use client";

import { useState, useEffect, useCallback } from "react";
import { useAuthStore } from "@/stores/auth";

const API_BASE = "/api/v2";

/**
 * Perform a fetch with automatic retry on 429 (rate limit) and
 * token refresh on 401 (expired token).
 */
async function resilientFetch(
  url: string,
  init: RequestInit,
  getToken: () => string | null,
  setToken: (t: string) => void,
  logout: () => void
): Promise<Response> {
  const maxRetries = 3;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const token = getToken();
    const headers: Record<string, string> = {
      ...(init.headers as Record<string, string>),
    };
    if (token) headers.Authorization = `Bearer ${token}`;

    const res = await fetch(url, { ...init, headers });

    // Rate limited — wait and retry
    if (res.status === 429 && attempt < maxRetries) {
      const retryAfter = res.headers.get("Retry-After");
      const waitMs = retryAfter ? parseInt(retryAfter, 10) * 1000 : 2000 * (attempt + 1);
      await new Promise((r) => setTimeout(r, waitMs));
      continue;
    }

    // Token missing or expired — try to refresh once.
    // The refresh uses the httpOnly cookie so it works even when the access
    // token is null (e.g. before hydration completes on a hard page load).
    if (res.status === 401 && attempt === 0) {
      try {
        const refreshRes = await fetch(`${API_BASE}/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (refreshRes.ok) {
          const data = await refreshRes.json();
          setToken(data.access_token);
          // Retry the original request with the new token
          continue;
        }
        // Only logout on genuine auth failures — not transient server errors.
        // 401: refresh token expired/invalid; 403: token revoked.
        // 5xx or other codes may be transient and should not end the session.
        if (refreshRes.status === 401 || refreshRes.status === 403) {
          logout();
        }
      } catch {
        // Network error on refresh — don't logout, just let the error propagate
      }
    }

    return res;
  }

  // Should not reach here, but just in case return the last response
  const token = getToken();
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string>),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  return fetch(url, { ...init, headers });
}

/**
 * Simple data-fetching hook that calls the API with auth token.
 * Refetches when `deps` change. Returns { data, loading, error, refetch }.
 *
 * Automatically retries on 429 (rate limit) and refreshes the token on 401.
 */
export function useApi<T>(
  path: string | null,
  deps: unknown[] = []
): { data: T | null; loading: boolean; error: string | null; refetch: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(!!path);
  const [error, setError] = useState<string | null>(null);
  const token = useAuthStore((s) => s.token);
  const setToken = useAuthStore((s) => s.setToken);
  const logout = useAuthStore((s) => s.logout);

  const fetchData = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      const res = await resilientFetch(
        `${API_BASE}${path}`,
        {},
        () => useAuthStore.getState().token,
        setToken,
        logout
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = body.detail;
        const message = typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d: Record<string, unknown>) => d.msg ?? JSON.stringify(d)).join("; ")
            : `HTTP ${res.status}`;
        throw new Error(message);
      }
      const json = await res.json();
      setData(json);
    } catch (e: unknown) {
      setData(null);
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, token, ...deps]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { data, loading, error, refetch: fetchData };
}

/**
 * Wrapper for POST requests.
 *
 * Automatically retries on 429 (rate limit) and refreshes the token on 401.
 */
export function useApiMutation<T>(path: string) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const token = useAuthStore((s) => s.token);
  const setToken = useAuthStore((s) => s.setToken);
  const logout = useAuthStore((s) => s.logout);

  const mutate = useCallback(
    async (body: unknown): Promise<T | null> => {
      setLoading(true);
      setError(null);
      try {
        const res = await resilientFetch(
          `${API_BASE}${path}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
          () => useAuthStore.getState().token,
          setToken,
          logout
        );
        if (!res.ok) {
          const respBody = await res.json().catch(() => ({}));
          const detail = respBody.detail;
          const message = typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d: Record<string, unknown>) => d.msg ?? JSON.stringify(d)).join("; ")
              : `HTTP ${res.status}`;
          throw new Error(message);
        }
        return await res.json();
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Request failed";
        setError(msg);
        return null;
      } finally {
        setLoading(false);
      }
    },
    [path, token, setToken, logout]
  );

  return { mutate, loading, error };
}
