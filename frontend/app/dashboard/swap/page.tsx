"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowLeftRight, ArrowDown, Loader2, AlertCircle } from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import RuleBanner from "@/components/RuleBanner";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmt } from "@/lib/format";

interface Ticker {
  symbol: string;
  price: number;
  change_24h_pct: number;
  swappable?: boolean;
}

interface SwapQuote {
  amount_out: number;
  price_impact: number;
  fee: number;
}

export default function SwapPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: tickers, loading: tickersLoading } = useApi<Ticker[]>("/market/tickers");

  const {
    mutate: fetchQuote,
    loading: quoteLoading,
    error: quoteError,
  } = useApiMutation<SwapQuote>("/trading/swap/quote");

  const {
    mutate: executeSwap,
    loading: swapLoading,
    error: swapError,
  } = useApiMutation<{ success: boolean; message?: string }>("/trading/swap/execute");

  const [tokenIn, setTokenIn] = useState<string>("");
  const [tokenOut, setTokenOut] = useState<string>("");
  const [amountIn, setAmountIn] = useState<string>("");
  const [quote, setQuote] = useState<SwapQuote | null>(null);

  const symbols = tickers?.filter((t) => t.swappable !== false).map((t) => t.symbol) ?? [];

  const handleGetQuote = useCallback(async () => {
    if (!tokenIn || !tokenOut || !amountIn || Number(amountIn) <= 0) return;
    setQuote(null);
    const result = await fetchQuote({
      token_in: tokenIn,
      token_out: tokenOut,
      amount_in: Number(amountIn),
    });
    if (result) {
      setQuote(result);
    }
  }, [tokenIn, tokenOut, amountIn, fetchQuote]);

  const handleSwap = useCallback(async () => {
    if (!quote || !tokenIn || !tokenOut || !amountIn) return;
    const result = await executeSwap({
      token_in: tokenIn,
      token_out: tokenOut,
      amount_in: Number(amountIn),
      min_amount_out: quote.amount_out * 0.99, // 1% slippage tolerance
    });
    if (result) {
      toast.success(`Swapped ${amountIn} ${tokenIn} for ${fmt(quote.amount_out, 4)} ${tokenOut}`);
      setAmountIn("");
      setQuote(null);
    }
  }, [quote, tokenIn, tokenOut, amountIn, executeSwap]);

  const handleFlip = useCallback(() => {
    setTokenIn(tokenOut);
    setTokenOut(tokenIn);
    setQuote(null);
  }, [tokenIn, tokenOut]);

  const canQuote = tokenIn && tokenOut && tokenIn !== tokenOut && Number(amountIn) > 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Swap</h1>
        <p className="text-sm text-muted-foreground">
          Trade tokens instantly at the best rates
        </p>
      </div>

      <RuleBanner title="Swap Rules" rules={[
        "0.3% swap fee — 25% of fees are permanently burned",
        "Max 15% of pool reserves per swap (5% for thin pools)",
        "Circuit breaker halts pool if reserves drop 20% in 10 min",
        "Slippage protection — swaps rejected if output below tolerance",
        "PoW tokens (MTA, SUN) are not swappable — use Buy/Sell instead",
      ]} />

      <div className="mx-auto max-w-md">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ArrowLeftRight className="size-4" />
              Swap Tokens
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {tickersLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-24 w-full" />
                <Skeleton className="h-24 w-full" />
              </div>
            ) : (
              <>
                {/* From token */}
                <div className="rounded-lg bg-muted/50 p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">From</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      type="number"
                      placeholder="0.0"
                      min="0"
                      step="any"
                      value={amountIn}
                      onChange={(e) => {
                        setAmountIn(e.target.value);
                        setQuote(null);
                      }}
                      className="border-0 bg-transparent text-xl font-bold focus-visible:ring-0"
                    />
                    <Select value={tokenIn} onValueChange={(val) => { setTokenIn(val as string); setQuote(null); }}>
                      <SelectTrigger className="w-28 shrink-0">
                        <SelectValue placeholder="Token" />
                      </SelectTrigger>
                      <SelectContent>
                        {symbols.map((s) => (
                          <SelectItem key={s} value={s}>
                            {s}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                {/* Swap direction button */}
                <div className="flex justify-center">
                  <Button
                    variant="outline"
                    size="icon"
                    className="rounded-full"
                    onClick={handleFlip}
                  >
                    <ArrowDown className="size-4" />
                  </Button>
                </div>

                {/* To token */}
                <div className="rounded-lg bg-muted/50 p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">To</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      type="number"
                      placeholder="0.0"
                      value={quote ? fmt(quote.amount_out, 4) : ""}
                      readOnly
                      className="border-0 bg-transparent text-xl font-bold focus-visible:ring-0"
                    />
                    <Select value={tokenOut} onValueChange={(val) => { setTokenOut(val as string); setQuote(null); }}>
                      <SelectTrigger className="w-28 shrink-0">
                        <SelectValue placeholder="Token" />
                      </SelectTrigger>
                      <SelectContent>
                        {symbols.map((s) => (
                          <SelectItem key={s} value={s}>
                            {s}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                {/* Quote / Execute buttons */}
                {!isAuthenticated ? (
                  <Button className="w-full" size="lg" disabled>
                    Login to Swap
                  </Button>
                ) : !quote ? (
                  <Button
                    className="w-full"
                    size="lg"
                    disabled={!canQuote || quoteLoading}
                    onClick={handleGetQuote}
                  >
                    {quoteLoading ? (
                      <>
                        <Loader2 className="size-4 animate-spin" />
                        Getting Quote...
                      </>
                    ) : (
                      "Get Quote"
                    )}
                  </Button>
                ) : (
                  <Button
                    className="w-full"
                    size="lg"
                    disabled={swapLoading}
                    onClick={handleSwap}
                  >
                    {swapLoading ? (
                      <>
                        <Loader2 className="size-4 animate-spin" />
                        Swapping...
                      </>
                    ) : (
                      `Swap ${amountIn} ${tokenIn}`
                    )}
                  </Button>
                )}

                {/* Error display */}
                {(quoteError || swapError) && (
                  <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-xs text-destructive">
                    <AlertCircle className="size-3.5 shrink-0" />
                    {quoteError || swapError}
                  </div>
                )}

                {/* Quote details */}
                {quote && (
                  <div className="rounded-lg border border-border p-3 text-xs text-muted-foreground">
                    <div className="flex justify-between">
                      <span>Estimated Output</span>
                      <span className="font-mono">
                        {fmt(quote.amount_out, 4)} {tokenOut}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span>Price Impact</span>
                      <span className={quote.price_impact > 3 ? "text-destructive" : ""}>
                        {fmt(quote.price_impact, 2)}%
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span>Fee</span>
                      <span>{fmt(quote.fee, 4)}</span>
                    </div>
                  </div>
                )}

                {/* Current rate info when no quote */}
                {!quote && tokenIn && tokenOut && tokenIn !== tokenOut && tickers && (
                  <div className="rounded-lg border border-border p-3 text-xs text-muted-foreground">
                    <div className="flex justify-between">
                      <span>Rate</span>
                      <span className="font-mono">
                        {(() => {
                          const priceIn = tickers.find((t) => t.symbol === tokenIn)?.price;
                          const priceOut = tickers.find((t) => t.symbol === tokenOut)?.price;
                          if (priceIn && priceOut) {
                            return `1 ${tokenIn} = ${fmt(priceIn / (priceOut || 1), 4)} ${tokenOut}`;
                          }
                          return "--";
                        })()}
                      </span>
                    </div>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
