"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Command } from "cmdk";
import {
  Search,
  Sun,
  Moon,
  LogOut,
  User,
  Settings,
  ArrowRight,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { navSections, overviewItem } from "./nav-config";
import { useModules } from "@/hooks/useModules";
import { useAuthStore } from "@/stores/auth";
import { useTheme } from "@/components/providers/theme-provider";

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const { isEnabled } = useModules();
  const { setTheme, resolvedTheme } = useTheme();
  const logout = useAuthStore((s) => s.logout);
  const user = useAuthStore((s) => s.user);

  // Global hotkey: cmd/ctrl + k
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const navigate = (href: string) => {
    setOpen(false);
    router.push(href);
  };

  const sections = navSections
    .map((s) => ({
      ...s,
      items: s.items.filter(
        (item) => !item.modules || isEnabled(...item.modules),
      ),
    }))
    .filter((s) => s.items.length > 0);

  return (
    <>
      <PaletteTrigger onOpen={() => setOpen(true)} />
      {open ? (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
          role="dialog"
          aria-modal="true"
        >
          <div
            className="absolute inset-0 bg-background/60 backdrop-blur-md"
            onClick={() => setOpen(false)}
          />
          <Command
            className="relative z-10 w-[min(92vw,640px)] overflow-hidden rounded-2xl glass-strong shadow-2xl"
            label="Global command palette"
          >
            <div className="flex items-center gap-2 border-b border-border/60 px-4">
              <Search className="size-4 text-muted-foreground" />
              <Command.Input
                autoFocus
                placeholder="Search pages, actions, settings..."
                className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
              <kbd className="hidden rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground sm:inline">
                ESC
              </kbd>
            </div>

            <Command.List className="max-h-[60vh] overflow-y-auto p-2">
              <Command.Empty className="py-8 text-center text-sm text-muted-foreground">
                No results. Try another query.
              </Command.Empty>

              {/* Quick nav */}
              <Command.Group heading="Pages" className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-muted-foreground/70">
                <PaletteItem
                  icon={overviewItem.icon}
                  label={overviewItem.label}
                  hint="Dashboard home"
                  onSelect={() => navigate(overviewItem.href)}
                />
                {sections.map((section) =>
                  section.items.map((item) => (
                    <PaletteItem
                      key={item.href}
                      icon={item.icon}
                      label={item.label}
                      hint={section.title}
                      keywords={item.keywords}
                      onSelect={() => navigate(item.href)}
                    />
                  )),
                )}
              </Command.Group>

              {/* Actions */}
              <Command.Group heading="Actions" className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-muted-foreground/70">
                <PaletteItem
                  icon={resolvedTheme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
                  label={`Switch to ${resolvedTheme === "dark" ? "light" : "dark"} mode`}
                  keywords={["theme", "dark", "light"]}
                  onSelect={() => {
                    setTheme(resolvedTheme === "dark" ? "light" : "dark");
                    setOpen(false);
                  }}
                />
                {user ? (
                  <>
                    <PaletteItem
                      icon={<User className="size-4" />}
                      label="Open your profile"
                      keywords={["profile", "me"]}
                      onSelect={() => navigate(`/dashboard/profile/${user.id}`)}
                    />
                    <PaletteItem
                      icon={<Settings className="size-4" />}
                      label="Open settings"
                      keywords={["settings", "prefs"]}
                      onSelect={() => navigate("/dashboard/settings")}
                    />
                    <PaletteItem
                      icon={<LogOut className="size-4 text-destructive" />}
                      label="Log out"
                      keywords={["signout", "logout"]}
                      onSelect={() => {
                        logout();
                        setOpen(false);
                      }}
                    />
                  </>
                ) : null}
              </Command.Group>
            </Command.List>
          </Command>
        </div>
      ) : null}
    </>
  );
}

function PaletteTrigger({ onOpen }: { onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "group hidden h-9 min-w-[220px] items-center gap-2 rounded-full border border-border bg-secondary/40 px-3 text-sm text-muted-foreground transition-colors hover:bg-secondary/70 hover:text-foreground sm:flex",
      )}
      aria-label="Open command palette"
    >
      <Search className="size-4" />
      <span className="flex-1 text-left">Search or jump to...</span>
      <kbd className="rounded bg-background px-1.5 py-0.5 font-mono text-[10px] ring-1 ring-border">
        <span className="align-middle">⌘</span>K
      </kbd>
    </button>
  );
}

function PaletteItem({
  icon,
  label,
  hint,
  keywords,
  onSelect,
}: {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  keywords?: string[];
  onSelect: () => void;
}) {
  return (
    <Command.Item
      value={`${label} ${keywords?.join(" ") ?? ""}`}
      onSelect={onSelect}
      className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm outline-none data-[selected=true]:bg-accent data-[selected=true]:text-foreground"
    >
      <span className="text-primary">{icon}</span>
      <span className="flex-1">{label}</span>
      {hint ? (
        <span className="text-xs text-muted-foreground">{hint}</span>
      ) : null}
      <ArrowRight className="size-3.5 opacity-0 transition-opacity group-data-[selected=true]:opacity-100" />
    </Command.Item>
  );
}
