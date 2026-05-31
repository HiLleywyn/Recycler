"use client";

import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth";
import { useRouter } from "next/navigation";

/**
 * Hook that wraps the auth store and provides convenience methods.
 * Automatically hydrates auth state from localStorage on mount.
 */
export function useAuth() {
  const store = useAuthStore();

  useEffect(() => {
    store.hydrate();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    user: store.user,
    token: store.token,
    isAuthenticated: store.isAuthenticated,
    login: store.login,
    logout: store.logout,
    setGuild: store.setGuild,
    setUser: store.setUser,
  };
}

/**
 * Hook that redirects to landing page if not authenticated.
 * Use in dashboard pages that require auth.
 */
export function useRequireAuth() {
  const { isAuthenticated, user } = useAuth();
  const router = useRouter();

  useEffect(() => {
    // Small delay to allow hydration
    const timer = setTimeout(() => {
      if (!isAuthenticated) {
        router.push("/");
      }
    }, 100);

    return () => clearTimeout(timer);
  }, [isAuthenticated, router]);

  return { isAuthenticated, user };
}
