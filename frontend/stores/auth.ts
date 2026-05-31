import { create } from "zustand";
import type { User } from "@/types";

interface AuthStore {
  token: string | null;
  user: User | null;
  isAuthenticated: boolean;

  login: (token: string, user: User) => void;
  logout: () => void;
  setUser: (user: Partial<User>) => void;
  setGuild: (guildId: string) => void;
  setToken: (token: string) => void;
  refreshTokenAction: () => Promise<void>;
  hydrate: () => void;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  token: null,
  user: null,
  isAuthenticated: false,

  login: (token, user) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("discoin_token", token);
      localStorage.setItem("discoin_user", JSON.stringify(user));
    }
    set({ token, user, isAuthenticated: true });
  },

  logout: () => {
    if (typeof window !== "undefined") {
      localStorage.removeItem("discoin_token");
      localStorage.removeItem("discoin_user");
    }
    // Also call the backend to revoke the refresh token cookie
    fetch(
      `${process.env.NEXT_PUBLIC_API_URL || "/api/v2"}/auth/logout`,
      { method: "POST", credentials: "include" }
    ).catch(() => {});
    set({ token: null, user: null, isAuthenticated: false });
  },

  setUser: (userData) => {
    const current = get().user;
    if (!current) return;
    const updated = { ...current, ...userData };
    if (typeof window !== "undefined") {
      localStorage.setItem("discoin_user", JSON.stringify(updated));
    }
    set({ user: updated });
  },

  setGuild: (guildId) => {
    const current = get().user;
    if (!current) return;
    const updated = { ...current, guildId };
    if (typeof window !== "undefined") {
      localStorage.setItem("discoin_user", JSON.stringify(updated));
    }
    set({ user: updated });
  },

  setToken: (token) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("discoin_token", token);
    }
    set({ token });
  },

  refreshTokenAction: async () => {
    // Refresh token is stored as an httpOnly cookie by the backend.
    // The browser sends it automatically with credentials: "include".
    // We never store or access the refresh token in JavaScript.
    try {
      const response = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "/api/v2"}/auth/refresh`,
        {
          method: "POST",
          credentials: "include",
        }
      );

      if (!response.ok) {
        // Don't logout on rate limit — the session is still valid,
        // we just need to wait and try again later.
        if (response.status === 429) return;
        throw new Error("Refresh failed");
      }

      const data = await response.json();
      get().setToken(data.access_token);
    } catch {
      get().logout();
    }
  },

  hydrate: () => {
    if (typeof window === "undefined") return;

    const token = localStorage.getItem("discoin_token");
    const userStr = localStorage.getItem("discoin_user");

    if (token && userStr) {
      try {
        const user = JSON.parse(userStr) as User;
        set({ token, user, isAuthenticated: true });
      } catch {
        set({ token: null, user: null, isAuthenticated: false });
      }
    }
  },
}));

// Synchronously hydrate from localStorage as soon as this module is imported in
// the browser.  This ensures the token is in the store before any component
// mounts and fires API requests, preventing spurious 401s on hard page loads or
// direct URL navigation where no component ever calls useAuth() / hydrate().
if (typeof window !== "undefined") {
  useAuthStore.getState().hydrate();
}
