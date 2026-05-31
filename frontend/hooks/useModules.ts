"use client";

import { useApi } from "@/hooks/useApi";

interface ModulesResponse {
  modules: Record<string, boolean>;
}

/**
 * Fetches enabled/disabled module status for the current guild.
 * Returns a lookup like { gambling: true, mining: false, ... }.
 * Modules default to true if not present (legacy guilds).
 */
export function useModules() {
  const { data, loading, error, refetch } = useApi<ModulesResponse>(
    "/users/guild-modules"
  );

  const modules = data?.modules ?? {};

  /** Check if at least one of the given module keys is enabled. */
  const isEnabled = (...keys: string[]): boolean => {
    if (!data) return true; // still loading → assume enabled
    return keys.some((k) => modules[k] !== false);
  };

  return { modules, loading, error, isEnabled, refetch };
}
