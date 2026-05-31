"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Menu, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { BrandMark } from "@/components/ui/brand";
import { useModules } from "@/hooks/useModules";
import {
  navSections,
  overviewItem,
  type NavItem,
  type NavSection,
} from "./nav-config";

const COLLAPSE_KEY = "discoin-sidebar-collapsed";

function useCollapse(): [boolean, (v: boolean) => void] {
  // Lazy initializer reads localStorage once, before first paint -
  // no post-mount setState, so no cascade render.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const set = (v: boolean) => {
    setCollapsed(v);
    try {
      localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
    } catch {
      // ignore
    }
  };
  return [collapsed, set];
}

function useVisibleSections() {
  const { isEnabled } = useModules();
  return navSections
    .map((section) => ({
      ...section,
      items: section.items.filter(
        (item) => !item.modules || isEnabled(...item.modules),
      ),
    }))
    .filter((section) => section.items.length > 0);
}

// ─────────────────────────────────────────────────────────────
// Desktop collapsible sidebar

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useCollapse();
  const sections = useVisibleSections();

  return (
    <aside
      data-collapsed={collapsed}
      className={cn(
        "hidden shrink-0 md:flex md:flex-col",
        "h-dvh sticky top-0 z-20",
        "border-r border-border/60 bg-sidebar/70 backdrop-blur-xl",
        "transition-[width] duration-250 ease-out",
        collapsed ? "w-[72px]" : "w-[248px]",
      )}
      aria-label="Primary navigation"
    >
      {/* Brand */}
      <div className="flex h-14 items-center justify-between px-3">
        <Link href="/dashboard" className="flex items-center gap-2 overflow-hidden">
          <BrandMark size={28} />
          {!collapsed ? (
            <span className="font-display text-base font-semibold tracking-tight text-gradient-brand">
              Discoin
            </span>
          ) : null}
        </Link>
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0"
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? (
            <ChevronRight className="size-4" />
          ) : (
            <ChevronLeft className="size-4" />
          )}
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden px-2 pb-4">
        <NavLink item={overviewItem} pathname={pathname} collapsed={collapsed} exact />
        {sections.map((section) => (
          <NavSectionBlock
            key={section.title}
            section={section}
            pathname={pathname}
            collapsed={collapsed}
          />
        ))}
      </div>
    </aside>
  );
}

function NavSectionBlock({
  section,
  pathname,
  collapsed,
}: {
  section: NavSection;
  pathname: string;
  collapsed: boolean;
}) {
  return (
    <div className="mt-5">
      {!collapsed ? (
        <div className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
          {section.title}
        </div>
      ) : (
        <div className="mx-2 mb-1 h-px bg-border/50" />
      )}
      {section.items.map((item) => (
        <NavLink
          key={item.href}
          item={item}
          pathname={pathname}
          collapsed={collapsed}
        />
      ))}
    </div>
  );
}

function NavLink({
  item,
  pathname,
  collapsed,
  exact = false,
}: {
  item: NavItem;
  pathname: string;
  collapsed: boolean;
  exact?: boolean;
}) {
  const isActive = exact
    ? pathname === item.href
    : pathname === item.href || pathname.startsWith(item.href + "/");

  return (
    <Link
      href={item.href}
      aria-current={isActive ? "page" : undefined}
      title={collapsed ? item.label : undefined}
      className={cn(
        "group relative my-0.5 flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium transition-all outline-none",
        "focus-visible:ring-2 focus-visible:ring-ring",
        isActive
          ? "text-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
        collapsed && "justify-center px-2",
      )}
    >
      {/* Active accent bar + glow */}
      {isActive ? (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-xl ring-1 ring-primary/40 glass-subtle"
          style={{ boxShadow: "var(--ring-glow)" }}
        />
      ) : null}
      <span className={cn("relative z-10 shrink-0", isActive && "text-primary")}>
        {item.icon}
      </span>
      {!collapsed ? (
        <>
          <span className="relative z-10 flex-1 truncate">{item.label}</span>
          {item.badge ? (
            <span className="relative z-10 rounded-full bg-gradient-brand/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary ring-1 ring-primary/20">
              {item.badge}
            </span>
          ) : null}
        </>
      ) : null}
    </Link>
  );
}

// ─────────────────────────────────────────────────────────────
// Mobile: slide-in drawer + header button to toggle

export function MobileSidebar({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const pathname = usePathname();
  const sections = useVisibleSections();

  // Lock body scroll while open
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  return (
    <div
      className={cn(
        "fixed inset-0 z-50 md:hidden",
        open ? "pointer-events-auto" : "pointer-events-none",
      )}
      aria-hidden={!open}
    >
      {/* Backdrop */}
      <div
        className={cn(
          "absolute inset-0 bg-background/70 backdrop-blur-md transition-opacity duration-200",
          open ? "opacity-100" : "opacity-0",
        )}
        onClick={onClose}
      />

      {/* Panel */}
      <aside
        role="dialog"
        aria-modal="true"
        className={cn(
          "absolute inset-y-0 left-0 flex w-[280px] flex-col border-r border-border/60 bg-sidebar/95 backdrop-blur-xl shadow-2xl transition-transform duration-250 ease-out",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-14 items-center justify-between px-4">
          <Link
            href="/dashboard"
            className="flex items-center gap-2"
            onClick={onClose}
          >
            <BrandMark size={28} />
            <span className="font-display text-base font-semibold tracking-tight text-gradient-brand">
              Discoin
            </span>
          </Link>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close menu"
          >
            <X className="size-4" />
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-6">
          <MobileLink item={overviewItem} pathname={pathname} onClick={onClose} exact />
          {sections.map((section) => (
            <div key={section.title} className="mt-5">
              <div className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                {section.title}
              </div>
              {section.items.map((item) => (
                <MobileLink
                  key={item.href}
                  item={item}
                  pathname={pathname}
                  onClick={onClose}
                />
              ))}
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}

function MobileLink({
  item,
  pathname,
  onClick,
  exact = false,
}: {
  item: NavItem;
  pathname: string;
  onClick: () => void;
  exact?: boolean;
}) {
  const isActive = exact
    ? pathname === item.href
    : pathname === item.href || pathname.startsWith(item.href + "/");

  return (
    <Link
      href={item.href}
      onClick={onClick}
      aria-current={isActive ? "page" : undefined}
      className={cn(
        "my-0.5 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
        isActive
          ? "bg-gradient-brand/10 text-primary ring-1 ring-primary/20"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      {item.icon}
      <span className="flex-1 truncate">{item.label}</span>
      {item.badge ? (
        <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
          {item.badge}
        </span>
      ) : null}
    </Link>
  );
}

export function MobileMenuButton({ onClick }: { onClick: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={onClick}
      className="md:hidden"
      aria-label="Open menu"
    >
      <Menu className="size-5" />
    </Button>
  );
}

// Back-compat: previous TopNav export is no longer rendered by Header.
export function TopNav() {
  return null;
}
