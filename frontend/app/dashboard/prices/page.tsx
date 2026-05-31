"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { SortableTable, ColumnDef } from "@/components/ui/sortable-table";
import Link from "next/link";
import { TrendingUp } from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { fmt as safeFmt } from "@/lib/format";

// --- API response types ---

interface TokenPrice {
  [key: string]: unknown;
  symbol: string;
  name?: string;
  price: number;
  change_24h_pct: number;
  volume_24h: number;
  market_cap: number;
  high_24h: number;
  low_24h: number;
  circulating_supply?: number;
  max_supply?: number;
}

// --- Helpers ---

function fmt(n: number | null | undefined, decimals = 2): string {
  const v = n ?? 0;
  if (v < 0.01 && v > 0) {
    return "$" + safeFmt(v, 6);
  }
  return "$" + (v).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtCompact(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return "$" + safeFmt(v / 1_000_000, 2) + "M";
  if (v >= 1_000) return "$" + safeFmt(v / 1_000, 2) + "K";
  return "$" + safeFmt(v, 2);
}

function fmtNumber(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return safeFmt(v / 1_000_000, 2) + "M";
  if (v >= 1_000) return safeFmt(v / 1_000, 2) + "K";
  return safeFmt(v, 2);
}

function fmtPct(n: number | null | undefined): string {
  const v = n ?? 0;
  const sign = v >= 0 ? "+" : "";
  return `${sign}${safeFmt(v, 2)}%`;
}

// --- Columns ---

const columns: ColumnDef<TokenPrice>[] = [
  {
    key: "symbol",
    label: "Token",
    sortable: false,
    render: (row) => (
      <div>
        <Link href={`/dashboard/charts?symbol=${row.symbol}`} className="font-medium text-primary hover:underline">
          {row.symbol}
        </Link>
        {row.name && <p className="text-xs text-muted-foreground">{row.name}</p>}
      </div>
    ),
  },
  {
    key: "price",
    label: "Price",
    sortable: true,
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-sm">{fmt(row.price)}</span>
    ),
  },
  {
    key: "change_24h_pct",
    label: "24h %",
    sortable: true,
    className: "text-right",
    render: (row) => (
      <span className={`text-sm font-medium ${row.change_24h_pct >= 0 ? "text-chart-green" : "text-chart-red"}`}>
        {fmtPct(row.change_24h_pct)}
      </span>
    ),
  },
  {
    key: "volume_24h",
    label: "Volume",
    sortable: true,
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-sm text-muted-foreground">{fmtCompact(row.volume_24h)}</span>
    ),
  },
  {
    key: "market_cap",
    label: "Market Cap",
    sortable: true,
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-sm text-muted-foreground">{fmtCompact(row.market_cap)}</span>
    ),
  },
  {
    key: "circulating_supply",
    label: "Circulating Supply",
    sortable: true,
    className: "text-right",
    visible: false,
    render: (row) => (
      <span className="font-mono text-sm text-muted-foreground">
        {row.circulating_supply != null ? fmtNumber(row.circulating_supply) : "—"}
      </span>
    ),
  },
  {
    key: "max_supply",
    label: "Max Supply",
    sortable: true,
    className: "text-right",
    visible: false,
    render: (row) => (
      <span className="font-mono text-sm text-muted-foreground">
        {row.max_supply != null ? fmtNumber(row.max_supply) : "—"}
      </span>
    ),
  },
];

// --- Component ---

export default function PricesPage() {
  const { data: prices, loading, error: pricesError } = useApi<TokenPrice[]>("/market/prices");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Prices</h1>
        <p className="text-sm text-muted-foreground">
          Live token prices and market data
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TrendingUp className="size-4" />
            All Tokens
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="space-y-3">
              {Array.from({ length: 8 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : pricesError ? (
            <div className="flex h-32 items-center justify-center">
              <p className="text-sm text-muted-foreground">
                No price data available
              </p>
            </div>
          ) : (
            <SortableTable<TokenPrice>
              columns={columns}
              data={prices ?? []}
              defaultSort={{ key: "market_cap", dir: "desc" }}
              searchable
              searchPlaceholder="Search tokens..."
              columnToggle
              emptyMessage="No market data available"
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
