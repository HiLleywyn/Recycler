"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { Surface } from "@/components/ui/surface";
import { TrendingUp, TrendingDown } from "lucide-react";

export interface StatProps {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  delta?: number | null;
  deltaLabel?: string;
  icon?: React.ReactNode;
  loading?: boolean;
  error?: boolean;
  className?: string;
  accent?: "default" | "success" | "destructive" | "gold";
}

function formatDelta(delta: number | null | undefined) {
  if (delta == null || Number.isNaN(delta)) return null;
  const sign = delta >= 0 ? "+" : "";
  return `${sign}${delta.toFixed(2)}%`;
}

export function Stat({
  label,
  value,
  hint,
  delta,
  deltaLabel,
  icon,
  loading,
  error,
  className,
  accent = "default",
}: StatProps) {
  const deltaDir = delta == null ? null : delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  const deltaStr = formatDelta(delta);

  const accentRing =
    accent === "success"
      ? "before:bg-[var(--success)]"
      : accent === "destructive"
        ? "before:bg-[var(--destructive)]"
        : accent === "gold"
          ? "before:bg-[var(--chart-gold)]"
          : "before:bg-gradient-brand";

  return (
    <Surface
      variant="glass"
      interactive
      className={cn(
        "group p-5",
        // Left accent bar
        "before:absolute before:inset-y-4 before:left-0 before:w-[3px] before:rounded-full before:opacity-70",
        accentRing,
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="text-eyebrow text-muted-foreground">{label}</div>
        {icon ? (
          <div className="flex size-8 items-center justify-center rounded-lg bg-gradient-brand/10 text-primary ring-1 ring-primary/20">
            {icon}
          </div>
        ) : null}
      </div>

      <div className="mt-3">
        {loading ? (
          <div className="h-8 w-32 rounded-md bg-muted shimmer" />
        ) : error ? (
          <p className="text-sm text-muted-foreground">Unable to load</p>
        ) : (
          <div className="font-display text-3xl font-semibold tabular-nums tracking-tight">
            {value}
          </div>
        )}
      </div>

      {(hint || deltaStr) && !loading && !error ? (
        <div className="mt-2 flex items-center gap-2 text-xs">
          {deltaStr ? (
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium tabular-nums",
                deltaDir === "up"
                  ? "bg-[color-mix(in_oklch,var(--success)_15%,transparent)] text-[var(--success)]"
                  : deltaDir === "down"
                    ? "bg-[color-mix(in_oklch,var(--destructive)_15%,transparent)] text-[var(--destructive)]"
                    : "bg-muted text-muted-foreground",
              )}
            >
              {deltaDir === "up" ? (
                <TrendingUp className="size-3" />
              ) : deltaDir === "down" ? (
                <TrendingDown className="size-3" />
              ) : null}
              {deltaStr}
              {deltaLabel ? <span className="opacity-70">{deltaLabel}</span> : null}
            </span>
          ) : null}
          {hint ? (
            <span className="text-muted-foreground">{hint}</span>
          ) : null}
        </div>
      ) : null}
    </Surface>
  );
}

export function StatGrid({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "grid gap-4 sm:grid-cols-2 lg:grid-cols-4",
        className,
      )}
    >
      {children}
    </div>
  );
}
