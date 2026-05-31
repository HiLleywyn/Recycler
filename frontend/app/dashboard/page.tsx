"use client";

import Link from "next/link";
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  Landmark,
  Activity,
  DollarSign,
  Users,
  Coins,
  Droplets,
  ArrowLeftRight,
  Image as ImageIcon,
  BarChart,
  ArrowRight,
  Rocket,
  Gamepad2,
  Pickaxe,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Surface } from "@/components/ui/surface";
import { Stat, StatGrid } from "@/components/ui/stat";
import { Ticker } from "@/components/ui/ticker";
import { EmptyState } from "@/components/ui/empty-state";
import { useApi } from "@/hooks/useApi";
import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/auth";
import { fmt as safeFmt } from "@/lib/format";
import { cn } from "@/lib/utils";

// ── API response types ──────────────────────────────────────────

interface PortfolioSummary {
  wallet: number;
  bank: number;
  net_worth: number;
  net_worth_change_24h: number;
  holdings_count: number;
  stakes_count: number;
  lp_count: number;
}

interface TopMover {
  symbol: string;
  price: number;
  change_24h_pct: number;
}

interface MarketOverview {
  total_market_cap: number;
  total_volume_24h: number;
  total_tokens: number;
  top_gainers: TopMover[];
  top_losers: TopMover[];
}

interface NFTSummary {
  owned_count: number;
  total_value: number;
  listed_count: number;
}

interface PredictionSummary {
  active_markets: number;
  total_pool: number;
  user_active_bets: number;
}

interface ServerStats {
  total_users: number;
  total_tokens: number;
  total_pools: number;
  total_trades: number;
  total_volume_usd: number;
  total_market_cap: number;
  treasury_balance: number;
  active_loans: number;
  active_stakes: number;
  mining_hashrate: number;
}

// ── Helpers ─────────────────────────────────────────────────────

function fmtUsd(n: number | undefined | null, decimals = 2): string {
  const v = n ?? 0;
  return (
    "$" +
    v.toLocaleString(undefined, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  );
}

function fmtNum(n: number | undefined | null): string {
  return (n ?? 0).toLocaleString();
}

function fmtPct(n: number | null | undefined): string {
  const v = n ?? 0;
  const sign = v >= 0 ? "+" : "";
  return `${sign}${safeFmt(v, 2)}%`;
}

// ── Component ───────────────────────────────────────────────────

export default function DashboardOverview() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const user = useAuthStore((s) => s.user);

  const { data: portfolio, loading: portfolioLoading, error: portfolioError } =
    useApi<PortfolioSummary>(isAuthenticated ? "/portfolio" : null);

  // Shared stats + market queries use React Query so bouncing between the
  // landing page and the dashboard hits the cache instead of the network.
  const { data: stats, isLoading: statsLoading } = useQuery<ServerStats>({
    queryKey: ["landing-stats"],
    queryFn: async () => {
      const res = await fetch("/api/v2/stats/stats", {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    },
    staleTime: 60 * 1000,
    retry: 1,
  });
  const { data: market, isLoading: marketLoading, isError: marketError } =
    useQuery<MarketOverview>({
      queryKey: ["market-overview"],
      queryFn: async () => {
        const res = await fetch("/api/v2/market/overview", {
          headers: { Accept: "application/json" },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      },
      staleTime: 30 * 1000,
      retry: 1,
    });

  const { data: nftSummary } =
    useApi<NFTSummary>(isAuthenticated ? "/nfts/summary" : null);
  const { data: predictSummary } =
    useApi<PredictionSummary>("/predictions/summary");

  return (
    <div className="space-y-6">
      {/* Welcome + ticker hero */}
      <Surface
        variant="glass"
        elevation={2}
        className="relative overflow-hidden p-6 md:p-8"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute -right-32 -top-32 size-72 rounded-full blur-3xl opacity-60 bg-gradient-to-br from-indigo-500/30 to-cyan-500/10"
        />
        <div className="relative z-10 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="text-eyebrow text-primary">Overview</div>
            <h1 className="mt-2 text-hero">
              {isAuthenticated && user?.username
                ? `Welcome back, ${user.username}`
                : "Your Discoin economy at a glance"}
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Live markets, positions, and network-wide stats.
            </p>
          </div>
          <div className="flex gap-2">
            <Link
              href="/dashboard/swap"
              className={cn(
                buttonVariants({ variant: "glow", size: "sm" }),
                "gap-1.5 rounded-full px-4",
              )}
            >
              <ArrowLeftRight className="size-3.5" /> Quick swap
            </Link>
            <Link
              href="/dashboard/prices"
              className={cn(
                buttonVariants({ variant: "outline", size: "sm" }),
                "gap-1.5 rounded-full px-4",
              )}
            >
              Markets <ArrowRight className="size-3.5" />
            </Link>
          </div>
        </div>
        <div className="mt-6">
          <Ticker compact />
        </div>
      </Surface>

      {/* Main portfolio stats */}
      <StatGrid>
        <Stat
          label="Net Worth"
          value={fmtUsd(portfolio?.net_worth)}
          delta={portfolio?.net_worth_change_24h ?? null}
          deltaLabel="24h"
          icon={<DollarSign className="size-4" />}
          hint={
            portfolio
              ? `${portfolio.holdings_count} holdings · ${portfolio.stakes_count} stakes`
              : undefined
          }
          loading={portfolioLoading}
          error={!!portfolioError}
          accent="default"
        />
        <Stat
          label="Wallet"
          value={fmtUsd(portfolio?.wallet)}
          icon={<Wallet className="size-4" />}
          loading={portfolioLoading}
          error={!!portfolioError}
        />
        <Stat
          label="Bank"
          value={fmtUsd(portfolio?.bank)}
          icon={<Landmark className="size-4" />}
          loading={portfolioLoading}
          error={!!portfolioError}
        />
        <Stat
          label="Volume (24h)"
          value={fmtUsd(market?.total_volume_24h)}
          icon={<Activity className="size-4" />}
          loading={marketLoading}
          error={!!marketError}
          accent="gold"
        />
      </StatGrid>

      {/* Network stats strip */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <Stat
          label="Users"
          value={fmtNum(stats?.total_users)}
          icon={<Users className="size-4" />}
          loading={statsLoading}
        />
        <Stat
          label="Tokens"
          value={fmtNum(stats?.total_tokens)}
          icon={<Coins className="size-4" />}
          loading={statsLoading}
        />
        <Stat
          label="Pools"
          value={fmtNum(stats?.total_pools)}
          icon={<Droplets className="size-4" />}
          loading={statsLoading}
        />
        <Stat
          label="Trades"
          value={fmtNum(stats?.total_trades)}
          icon={<ArrowLeftRight className="size-4" />}
          loading={statsLoading}
        />
        <Stat
          label="NFTs owned"
          value={fmtNum(nftSummary?.owned_count)}
          hint={nftSummary ? `${fmtUsd(nftSummary.total_value)} value` : undefined}
          icon={<ImageIcon className="size-4" />}
        />
        <Stat
          label="Prediction markets"
          value={fmtNum(predictSummary?.active_markets)}
          hint={predictSummary ? "active" : undefined}
          icon={<BarChart className="size-4" />}
        />
      </div>

      {/* Top movers */}
      <div className="grid gap-4 md:grid-cols-2">
        <MoversPanel
          title="Top Gainers"
          icon={<TrendingUp className="size-4 text-[var(--success)]" />}
          movers={market?.top_gainers}
          direction="up"
          loading={marketLoading}
          error={!!marketError}
        />
        <MoversPanel
          title="Top Losers"
          icon={<TrendingDown className="size-4 text-[var(--destructive)]" />}
          movers={market?.top_losers}
          direction="down"
          loading={marketLoading}
          error={!!marketError}
        />
      </div>

      {/* Shortcuts row */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Shortcut
          href="/dashboard/pools"
          icon={<Droplets className="size-5" />}
          title="Liquidity Pools"
          desc="Provide liquidity, earn fees"
        />
        <Shortcut
          href="/dashboard/staking"
          icon={<Rocket className="size-5" />}
          title="Staking"
          desc="Validators & stonestakes"
        />
        <Shortcut
          href="/dashboard/mining"
          icon={<Pickaxe className="size-5" />}
          title="Mining"
          desc="Run a rig, earn block rewards"
        />
        <Shortcut
          href="/dashboard/games"
          icon={<Gamepad2 className="size-5" />}
          title="Games"
          desc="Blackjack, crash, slots, more"
        />
      </div>
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────

function MoversPanel({
  title,
  icon,
  movers,
  direction,
  loading,
  error,
}: {
  title: string;
  icon: React.ReactNode;
  movers: TopMover[] | undefined;
  direction: "up" | "down";
  loading: boolean;
  error: boolean;
}) {
  return (
    <Surface variant="glass" className="p-5">
      <div className="mb-4 flex items-center gap-2">
        {icon}
        <h3 className="font-display text-sm font-semibold tracking-tight">
          {title}
        </h3>
      </div>
      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-9 rounded-lg bg-muted shimmer" />
          ))}
        </div>
      ) : error ? (
        <p className="text-sm text-muted-foreground">Market data unavailable</p>
      ) : movers && movers.length > 0 ? (
        <div className="space-y-1">
          {movers.map((t) => (
            <Link
              key={t.symbol}
              href={`/dashboard/prices#${t.symbol}`}
              className="flex items-center justify-between rounded-lg px-3 py-2 text-sm transition-colors hover:bg-accent/60"
            >
              <div className="flex items-center gap-3">
                <div className="flex size-7 items-center justify-center rounded-lg bg-gradient-brand/10 text-xs font-semibold text-primary ring-1 ring-primary/20">
                  {t.symbol.slice(0, 2)}
                </div>
                <span className="font-mono font-medium">{t.symbol}</span>
                <span className="text-xs tabular-nums text-muted-foreground">
                  {fmtUsd(t.price, t.price < 1 ? 4 : 2)}
                </span>
              </div>
              <Badge
                variant="secondary"
                className={cn(
                  "tabular-nums",
                  direction === "up"
                    ? "text-[var(--success)]"
                    : "text-[var(--destructive)]",
                )}
              >
                {fmtPct(t.change_24h_pct)}
              </Badge>
            </Link>
          ))}
        </div>
      ) : (
        <EmptyState
          compact
          title="No movers yet"
          description="Trading data will appear here as soon as markets open."
        />
      )}
    </Surface>
  );
}

function Shortcut({
  href,
  icon,
  title,
  desc,
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <Link href={href} className="group block">
      <Surface variant="glass" interactive className="p-4">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-xl bg-gradient-brand text-white shadow-[0_6px_18px_-8px_oklch(0.68_0.22_262/0.6)]">
            {icon}
          </div>
          <div className="min-w-0 flex-1">
            <div className="font-display text-sm font-semibold tracking-tight">
              {title}
            </div>
            <div className="truncate text-xs text-muted-foreground">{desc}</div>
          </div>
          <ArrowRight className="size-4 translate-x-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
        </div>
      </Surface>
    </Link>
  );
}
