"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Landmark,
  TrendingUp,
  TrendingDown,
  DollarSign,
  Loader2,
  AlertCircle,
  LogIn,
  ShoppingCart,
  History,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import RuleBanner from "@/components/RuleBanner";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmtUsd } from "@/lib/format";

interface PriceItem {
  symbol: string;
  price: number;
  change_24h_pct: number;
  buyable_usd?: boolean;
}

interface BankBalance {
  usd_balance: number;
}

interface TradeResult {
  success: boolean;
  message: string;
  symbol: string;
  amount: number;
  total_cost: number;
  price: number;
}

interface RecentTrade {
  id: string;
  type: "buy" | "sell";
  symbol: string;
  amount: number;
  price: number;
  total: number;
  timestamp: string;
}

function fmtDate(ts?: string | null): string {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export default function BankPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: prices, loading: pricesLoading } = useApi<PriceItem[]>("/market/tickers");
  const { data: bankBalance, loading: balanceLoading, refetch: refetchBalance } = useApi<BankBalance>(
    isAuthenticated ? "/portfolio/bank" : null
  );
  const { data: recentTrades, loading: tradesLoading, refetch: refetchTrades } = useApi<RecentTrade[]>(
    isAuthenticated ? "/trading/recent-trades?limit=15" : null
  );

  const { mutate: buyToken, loading: buyLoading, error: buyError } =
    useApiMutation<TradeResult>("/trading/buy");
  const { mutate: sellToken, loading: sellLoading, error: sellError } =
    useApiMutation<TradeResult>("/trading/sell");

  const [selectedToken, setSelectedToken] = useState("");
  const [amount, setAmount] = useState("");
  const [inputMode, setInputMode] = useState<"token" | "usd">("usd");

  const selectedPrice = prices?.find((p) => p.symbol === selectedToken)?.price ?? 0;

  const parsedAmount = (() => {
    const raw = amount.replace(/^\$/, "").trim();
    const num = Number(raw);
    if (isNaN(num) || num <= 0) return 0;
    return num;
  })();

  const tokenQty = inputMode === "usd" && selectedPrice > 0
    ? parsedAmount / selectedPrice
    : parsedAmount;

  const usdTotal = inputMode === "token" && selectedPrice > 0
    ? parsedAmount * selectedPrice
    : parsedAmount;

  const handleTrade = useCallback(async (side: "buy" | "sell") => {
    if (!selectedToken || parsedAmount <= 0) return;
    const action = side === "buy" ? buyToken : sellToken;
    const result = await action({
      symbol: selectedToken,
      amount: tokenQty,
      input_mode: inputMode,
      input_amount: parsedAmount,
    });
    if (result) {
      toast.success(result.message || `${side === "buy" ? "Bought" : "Sold"} ${result.amount} ${result.symbol}`);
      setAmount("");
      refetchBalance();
      refetchTrades();
    }
  }, [selectedToken, parsedAmount, tokenQty, inputMode, buyToken, sellToken, refetchBalance, refetchTrades]);

  const actionLoading = buyLoading || sellLoading;
  const actionError = buyError || sellError;

  if (!isAuthenticated) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Bank</h1>
          <p className="text-sm text-muted-foreground">Buy and sell tokens with USD</p>
        </div>
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-3 py-16">
            <LogIn className="size-10 text-muted-foreground" />
            <p className="text-lg font-medium">Log in to access the bank</p>
            <p className="text-sm text-muted-foreground">
              Sign in with Discord to buy and sell tokens.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Bank</h1>
        <p className="text-sm text-muted-foreground">Buy and sell tokens with USD</p>
      </div>

      <RuleBanner title="Trading Rules" rules={[
        "0.2% platform fee ($0.10 min, $20 max) on buy/sell",
        "Only MTA, SUN, ARC, DSC, USDC, DSD buyable with USD",
        "VTR and DSY must be acquired via Swap",
        "Price impact scales with trade size relative to pool reserves",
      ]} />

      {/* Bank balance */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">Bank Balance</CardTitle>
          <Landmark className="size-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          {balanceLoading ? (
            <Skeleton className="h-9 w-32" />
          ) : (
            <div className="text-3xl font-bold">{fmtUsd(bankBalance?.usd_balance)}</div>
          )}
        </CardContent>
      </Card>

      {/* Trade interface */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShoppingCart className="size-4" />
            Market Order
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Token selector */}
          <div className="space-y-2">
            <label className="text-sm font-medium">Token</label>
            <Select value={selectedToken} onValueChange={(v) => setSelectedToken(v ?? "")}>
              <SelectTrigger>
                <SelectValue placeholder="Select a token" />
              </SelectTrigger>
              <SelectContent>
                {pricesLoading ? (
                  <SelectItem value="_loading" disabled>Loading...</SelectItem>
                ) : prices && prices.length > 0 ? (
                  prices.filter((p) => p.buyable_usd !== false).map((p) => (
                    <SelectItem key={p.symbol} value={p.symbol}>
                      {p.symbol} &mdash; {fmtUsd(p.price)}
                    </SelectItem>
                  ))
                ) : (
                  <SelectItem value="_none" disabled>No tokens available</SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>

          {/* Amount input */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">Amount</label>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant={inputMode === "usd" ? "default" : "outline"}
                  className="h-6 px-2 text-xs"
                  onClick={() => setInputMode("usd")}
                >
                  USD
                </Button>
                <Button
                  size="sm"
                  variant={inputMode === "token" ? "default" : "outline"}
                  className="h-6 px-2 text-xs"
                  onClick={() => setInputMode("token")}
                >
                  Token
                </Button>
              </div>
            </div>
            <Input
              type="text"
              placeholder={inputMode === "usd" ? "$0.00" : "0.0"}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>

          {/* Price display */}
          {selectedToken && selectedPrice > 0 && parsedAmount > 0 && (
            <div className="rounded-lg bg-muted/50 p-3 text-sm space-y-1">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Price</span>
                <span className="font-mono">{fmtUsd(selectedPrice)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Token Quantity</span>
                <span className="font-mono">{tokenQty.toLocaleString(undefined, { maximumFractionDigits: 6 })} {selectedToken}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">USD Total</span>
                <span className="font-mono font-semibold">{fmtUsd(usdTotal)}</span>
              </div>
            </div>
          )}

          {actionError && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" />{actionError}
            </div>
          )}

          {/* Buy/Sell buttons */}
          <div className="flex gap-3">
            <Button
              className="flex-1 gap-2"
              disabled={actionLoading || !selectedToken || parsedAmount <= 0}
              onClick={() => handleTrade("buy")}
            >
              {buyLoading ? (
                <><Loader2 className="size-4 animate-spin" />Buying...</>
              ) : (
                <><TrendingUp className="size-4" />Buy</>
              )}
            </Button>
            <Button
              className="flex-1 gap-2"
              variant="outline"
              disabled={actionLoading || !selectedToken || parsedAmount <= 0}
              onClick={() => handleTrade("sell")}
            >
              {sellLoading ? (
                <><Loader2 className="size-4 animate-spin" />Selling...</>
              ) : (
                <><TrendingDown className="size-4" />Sell</>
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Recent trades */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <History className="size-4" />
            Recent Trades
          </CardTitle>
        </CardHeader>
        <CardContent>
          {tradesLoading ? (
            <div className="space-y-3">
              {[0, 1, 2, 3, 4].map((i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : recentTrades && recentTrades.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Side</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Time</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recentTrades.map((trade) => (
                  <TableRow key={trade.id}>
                    <TableCell>
                      <Badge variant={trade.type === "buy" ? "default" : "secondary"}>
                        {trade.type.toUpperCase()}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-medium">{trade.symbol}</TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {trade.amount.toLocaleString(undefined, { maximumFractionDigits: 6 })}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">{fmtUsd(trade.price)}</TableCell>
                    <TableCell className="text-right font-mono text-sm font-semibold">{fmtUsd(trade.total)}</TableCell>
                    <TableCell className="text-right text-xs text-muted-foreground">{fmtDate(trade.timestamp)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
              <p className="text-sm text-muted-foreground">No recent trades</p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
