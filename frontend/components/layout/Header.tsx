"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  Bell,
  Sun,
  Moon,
  LogIn,
  LogOut,
  User,
  Settings,
} from "lucide-react";

import { Button, buttonVariants } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTheme } from "@/components/providers/theme-provider";
import { useAuthStore } from "@/stores/auth";
import { useNotificationStore } from "@/stores/notifications";
import { cn } from "@/lib/utils";

import { MobileMenuButton, MobileSidebar } from "./Sidebar";
import { CommandPalette } from "./CommandPalette";
import { BrandMark } from "@/components/ui/brand";

export function Header() {
  const { setTheme, resolvedTheme } = useTheme();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const unreadCount = useNotificationStore((s) => s.unreadCount);
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  const avatarUrl = user?.avatar
    ? `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png`
    : undefined;
  const initials = user?.username
    ? user.username.slice(0, 2).toUpperCase()
    : "DC";

  return (
    <>
      <MobileSidebar open={mobileOpen} onClose={() => setMobileOpen(false)} />
      <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border/60 bg-background/70 px-3 backdrop-blur-xl sm:px-4">
        <MobileMenuButton onClick={() => setMobileOpen(true)} />

        {/* Brand - shown on mobile only; sidebar has it on md+ */}
        <Link href="/dashboard" className="flex items-center gap-2 md:hidden">
          <BrandMark size={26} />
          <span className="font-display text-base font-semibold tracking-tight text-gradient-brand">
            Discoin
          </span>
        </Link>

        {/* Command palette (+ trigger) */}
        <div className="ml-auto flex items-center gap-1.5 sm:ml-4 sm:mr-auto">
          <CommandPalette />
        </div>

        {/* Right cluster */}
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
            className="relative size-9"
            aria-label="Toggle theme"
          >
            <Sun
              className={cn(
                "absolute size-4 transition-all",
                resolvedTheme === "dark"
                  ? "rotate-0 scale-100 opacity-100"
                  : "-rotate-90 scale-0 opacity-0",
              )}
            />
            <Moon
              className={cn(
                "absolute size-4 transition-all",
                resolvedTheme === "dark"
                  ? "rotate-90 scale-0 opacity-0"
                  : "rotate-0 scale-100 opacity-100",
              )}
            />
          </Button>

          <Link
            href="/dashboard/settings"
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon" }),
              "relative size-9",
            )}
            aria-label="Notifications"
          >
            <Bell className="size-4" />
            {unreadCount > 0 ? (
              <Badge
                variant="destructive"
                className="absolute -right-0.5 -top-0.5 flex size-4 items-center justify-center rounded-full p-0 text-[10px]"
              >
                {unreadCount > 9 ? "9+" : unreadCount}
              </Badge>
            ) : null}
          </Link>

          {user ? (
            <DropdownMenu>
              <DropdownMenuTrigger
                className={cn(
                  buttonVariants({ variant: "ghost" }),
                  "ml-1 h-9 gap-2 rounded-full px-1.5",
                )}
              >
                <Avatar className="size-7 ring-1 ring-border">
                  <AvatarImage src={avatarUrl} alt={user.username} />
                  <AvatarFallback className="bg-gradient-brand text-[10px] text-white">
                    {initials}
                  </AvatarFallback>
                </Avatar>
                <span className="hidden pr-1 text-sm font-medium sm:inline-block">
                  {user.username}
                </span>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                className="w-52 border-border/60 glass-strong"
              >
                <div className="px-3 py-2">
                  <p className="truncate text-sm font-medium">{user.username}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    Discord connected
                  </p>
                </div>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() =>
                    window.location.assign(`/dashboard/profile/${user.id}/`)
                  }
                  className="gap-2"
                >
                  <User className="size-4" /> Profile
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => router.push("/dashboard/settings")}
                  className="gap-2"
                >
                  <Settings className="size-4" /> Settings
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={logout}
                  className="gap-2 text-destructive"
                >
                  <LogOut className="size-4" /> Log out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <Button
              variant="glow"
              size="sm"
              className="ml-1 h-9 gap-1.5 rounded-full px-4"
              onClick={async () => {
                const fallback = () => {
                  window.location.href = "/api/auth/discord";
                };
                try {
                  const res = await fetch("/api/v2/auth/discord", {
                    headers: { Accept: "application/json" },
                  });
                  if (!res.ok) return fallback();
                  const data = (await res
                    .json()
                    .catch(() => null)) as { url?: string } | null;
                  if (data?.url) {
                    window.location.href = data.url;
                  } else {
                    fallback();
                  }
                } catch {
                  fallback();
                }
              }}
            >
              <LogIn className="size-4" />
              <span className="hidden sm:inline-block">Login</span>
            </Button>
          )}
        </div>
      </header>
    </>
  );
}
