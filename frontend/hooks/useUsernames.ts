"use client";

import { useState, useEffect } from "react";
import { useAuthStore } from "@/stores/auth";

const API_BASE = "/api/v2";

// Module-level cache shared across all hook instances
const usernameCache = new Map<string, string>();

export function useUsernames(userIds: string[]): {
  names: Record<string, string>;
  loading: boolean;
} {
  const [names, setNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const token = useAuthStore((s) => s.token);

  useEffect(() => {
    if (!userIds.length) return;

    // Build initial result from cache
    const cached: Record<string, string> = {};
    const uncached: string[] = [];

    for (const id of userIds) {
      const hit = usernameCache.get(id);
      if (hit) {
        cached[id] = hit;
      } else {
        uncached.push(id);
      }
    }

    // Deduplicate uncached IDs
    const uniqueUncached = [...new Set(uncached)];

    // If everything is cached, return immediately
    if (uniqueUncached.length === 0) {
      setNames(cached);
      return;
    }

    setNames(cached);
    setLoading(true);

    // Debounce: wait 50ms to collect IDs before fetching
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`${API_BASE}/users/resolve`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ user_ids: uniqueUncached }),
        });

        if (res.ok) {
          const data: Record<string, string> = await res.json();

          // Merge into module-level cache
          for (const [id, name] of Object.entries(data)) {
            usernameCache.set(id, name);
          }

          // Update state with cached + newly resolved
          setNames((prev) => ({ ...prev, ...data }));
        }
      } catch {
        // Silently fail — components fall back to truncated ID
      } finally {
        setLoading(false);
      }
    }, 50);

    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userIds.join(","), token]);

  return { names, loading };
}
