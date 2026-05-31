"use client";

import { useState, useMemo, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { BarChart3, TrendingUp } from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { PriceChart } from "@/components/charts/PriceChart";
import { fmt as safeFmt } from "@/lib/format";

// --- API response types ---

interface Ticker {
  symbol: string;
  name?: string;
  price?: number;
}

interface TokenPrice {
  symbol: string;
  name: string;
  price: number;
  change_24h_pct: number;
  volume_24h: number;
  market_cap: number;
  circulating_supply?: number;
  max_supply?: number;
  high_24h?: number;
  low_24h?: number;
}

// --- Helpers ---

function fmt(n: number | null | undefined, decimals = 2): string {
  const v = n ?? 0;
  if (v < 0.01 && v > 0) {
    return "$" + safeFmt(v, 6);
  }
  return (
    "$" +
    v.toLocaleString(undefined, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  );
}

function fmtCompact(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000_000) return "$" + safeFmt(v / 1_000_000_000, 2) + "B";
  if (v >= 1_000_000) return "$" + safeFmt(v / 1_000_000, 2) + "M";
  if (v >= 1_000) return "$" + safeFmt(v / 1_000, 2) + "K";
  return "$" + safeFmt(v, 2);
}

function fmtSupply(n: number | null | undefined): string {
  if (n == null) return "N/A";
  if (n >= 1_000_000_000) return safeFmt(n / 1_000_000_000, 2) + "B";
  if (n >= 1_000_000) return safeFmt(n / 1_000_000, 2) + "M";
  if (n >= 1_000) return safeFmt(n / 1_000, 2) + "K";
  return safeFmt(n, 2);
}

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;

// --- Component ---

export default function ChartsPage() {
  return (
    <Suspense fallback={<div className="flex h-64 items-center justify-center"><Skeleton className="h-8 w-48" /></div>}>
      <ChartsContent />
    </Suspense>
  );
}

function ChartsContent() {
  const searchParams = useSearchParams();
  const router = useRouter();

  const paramSymbol = searchParams.get("symbol") ?? "";

  const [symbol, setSymbol] = useState(paramSymbol);
  const [timeframe, setTimeframe] = useState<string>("1h");

  // Fetch available tickers
  const { data: tickers, loading: tickersLoading } = useApi<Ticker[]>("/market/tickers");

  // Auto-select first ticker if none selected
  const activeSymbol = useMemo(() => {
    if (symbol) return symbol;
    if (tickers && tickers.length > 0) return tickers[0].symbol;
    return "";
  }, [symbol, tickers]);

  // Fetch price details for selected symbol
  const { data: priceData, loading: priceLoading } = useApi<TokenPrice>(
    activeSymbol ? `/market/prices/${activeSymbol}` : null,
    [activeSymbol]
  );

  function handleSymbolChange(newSymbol: string) {
    setSymbol(newSymbol);
    router.replace(`/dashboard/charts?symbol=${encodeURIComponent(newSymbol)}`, {
      scroll: false,
    });
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Charts</h1>
        <p className="text-sm text-muted-foreground">
          Interactive candlestick charts with volume
        </p>
      </div>

      {/* Controls */}
      <div className="flex flex-wrap items-center gap-4">
        {/* Token selector */}
        <div className="flex items-center gap-2">
          <label htmlFor="symbol-select" className="text-sm font-medium text-muted-foreground">
            Token
          </label>
          {tickersLoading ? (
            <Skeleton className="h-9 w-32" />
          ) : (
            <select
              id="symbol-select"
              value={activeSymbol}
              onChange={(e) => handleSymbolChange(e.target.value)}
              className="h-9 rounded-md border border-border bg-card px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            >
              {tickers?.map((t) => (
                <option key={t.symbol} value={t.symbol}>
                  {t.symbol} {t.name ? `- ${t.name}` : ""}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Timeframe selector */}
        <div className="flex items-center gap-1">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                timeframe === tf
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground"
              }`}
            >
              {tf.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Chart */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="size-4" />
            {activeSymbol || "Select a token"}
            {activeSymbol && (
              <span className="text-sm font-normal text-muted-foreground">
                {timeframe.toUpperCase()}
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {activeSymbol ? (
            <PriceChart symbol={activeSymbol} timeframe={timeframe} />
          ) : (
            <div className="flex h-[400px] items-center justify-center">
              <p className="text-sm text-muted-foreground">Select a token to view its chart</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Stats */}
      {activeSymbol && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TrendingUp className="size-4" />
              Market Stats
            </CardTitle>
          </CardHeader>
          <CardContent>
            {priceLoading ? (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
                {Array.from({ length: 6 }).map((_, i) => (
                  <Skeleton key={i} className="h-16 w-full" />
                ))}
              </div>
            ) : priceData ? (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
                <StatItem label="Price" value={fmt(priceData.price)} />
                <StatItem
                  label="24h Change"
                  value={`${priceData.change_24h_pct >= 0 ? "+" : ""}${safeFmt(priceData.change_24h_pct, 2)}%`}
                  valueClass={
                    priceData.change_24h_pct >= 0 ? "text-chart-green" : "text-chart-red"
                  }
                />
                <StatItem label="24h Volume" value={fmtCompact(priceData.volume_24h)} />
                <StatItem label="Market Cap" value={fmtCompact(priceData.market_cap)} />
                <StatItem
                  label="Circulating Supply"
                  value={fmtSupply(priceData.circulating_supply)}
                />
                <StatItem
                  label="Max Supply"
                  value={priceData.max_supply ? fmtSupply(priceData.max_supply) : "N/A"}
                />
              </div>
            ) : (
              <div className="flex h-16 items-center justify-center">
                <p className="text-sm text-muted-foreground">No market data available</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function StatItem({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`font-mono text-sm font-medium ${valueClass ?? ""}`}>{value}</p>
    </div>
  );
}
