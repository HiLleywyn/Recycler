"use client";

import { useMemo } from "react";
import { usePriceStore } from "@/stores/prices";
import { cn } from "@/lib/utils";
import { TrendingUp, TrendingDown } from "lucide-react";
import type { PriceData } from "@/types";

interface TickerProps {
  className?: string;
  /** Fallback items to show when no live prices are loaded. */
  placeholders?: Array<{ symbol: string; price?: number; change24h?: number }>;
  compact?: boolean;
}

const DEFAULT_PLACEHOLDERS = [
  { symbol: "DSD", price: 1, change24h: 0 },
  { symbol: "MTA", price: 62_450, change24h: 1.24 },
  { symbol: "ARC", price: 3_180, change24h: -0.44 },
  { symbol: "SUN", price: 0.092, change24h: 3.12 },
  { symbol: "USDC", price: 1, change24h: 0 },
];

function fmtPrice(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  if (n >= 1000) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
  if (n >= 1) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return n.toFixed(Math.min(6, Math.max(2, -Math.floor(Math.log10(n)) + 2)));
}

function fmtPct(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return "";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function Ticker({ className, placeholders = DEFAULT_PLACEHOLDERS, compact = false }: TickerProps) {
  const prices = usePriceStore((s) => s.prices);

  const items = useMemo(() => {
    const arr: Array<Pick<PriceData, "symbol" | "price" | "change24h">> = [];
    if (prices.size > 0) {
      for (const p of prices.values()) {
        arr.push({ symbol: p.symbol, price: p.price, change24h: p.change24h });
      }
    } else {
      for (const p of placeholders) {
        arr.push({ symbol: p.symbol, price: p.price ?? 0, change24h: p.change24h ?? 0 });
      }
    }
    return arr;
  }, [prices, placeholders]);

  // Double the items so CSS translateX(-50%) produces a seamless loop.
  const loop = [...items, ...items];

  return (
    <div
      className={cn(
        "group relative w-full overflow-hidden",
        compact ? "h-8" : "h-11",
        "glass-subtle rounded-full",
        className,
      )}
      aria-label="Live price ticker"
    >
      {/* Edge fade */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-y-0 left-0 w-16 bg-gradient-to-r from-background to-transparent z-10"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-y-0 right-0 w-16 bg-gradient-to-l from-background to-transparent z-10"
      />
      <div
        className="flex h-full min-w-max items-center gap-6 whitespace-nowrap px-6 group-hover:[animation-play-state:paused]"
        style={{ animation: "ticker 45s linear infinite" }}
      >
        {loop.map((p, i) => {
          const up = (p.change24h ?? 0) > 0;
          const down = (p.change24h ?? 0) < 0;
          return (
            <div
              key={`${p.symbol}-${i}`}
              className={cn(
                "flex items-center gap-2 font-mono tabular-nums",
                compact ? "text-xs" : "text-sm",
              )}
            >
              <span className="font-semibold text-foreground">{p.symbol}</span>
              <span className="text-muted-foreground">${fmtPrice(p.price)}</span>
              <span
                className={cn(
                  "inline-flex items-center gap-0.5",
                  up
                    ? "text-[var(--success)]"
                    : down
                      ? "text-[var(--destructive)]"
                      : "text-muted-foreground",
                )}
              >
                {up ? (
                  <TrendingUp className="size-3" />
                ) : down ? (
                  <TrendingDown className="size-3" />
                ) : null}
                {fmtPct(p.change24h)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
