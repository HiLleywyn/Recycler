"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import {
  BarChart3,
  Clock,
  DollarSign,
  AlertCircle,
  CheckCircle,
  XCircle,
  TrendingUp,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import ModuleGate from "@/components/ModuleGate";

interface OptionStats {
  bets: number;
  amount: number;
}

interface Market {
  id: number;
  question: string;
  description: string | null;
  options: string[];
  option_stats: Record<string, OptionStats>;
  pool_amount: number;
  status: string;
  created_by: string;
  created_at: string;
  closes_at: string | null;
  resolved_at: string | null;
  winning_option: number | null;
}

interface MarketsResponse {
  markets: Market[];
  total: number;
}

interface Bet {
  id: number;
  market_id: number;
  question: string;
  option_index: number;
  option_label: string;
  amount: number;
  placed_at: string;
  market_status: string;
  won: boolean;
}

interface BetsResponse {
  bets: Bet[];
  total: number;
}

interface PredictionSummary {
  active_markets: number;
  total_pool: number;
  user_active_bets: number;
}

function fmt(n: number | undefined | null): string {
  const v = n ?? 0;
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function MarketCard({
  market,
  isAuthenticated,
}: {
  market: Market;
  isAuthenticated: boolean;
}) {
  const [betAmount, setBetAmount] = useState("");
  const [selectedOption, setSelectedOption] = useState<number | null>(null);
  const { mutate: placeBet, loading: placing } = useApiMutation<{ success: boolean }>(
    `/predictions/market/${market.id}/bet`
  );

  const totalBetAmount = Object.values(market.option_stats).reduce(
    (sum, s) => sum + s.amount,
    0
  );

  async function handleBet() {
    if (selectedOption === null || !betAmount) return;
    const result = await placeBet({
      option_index: selectedOption,
      amount: parseFloat(betAmount),
    });
    if (result) {
      toast.success("Bet placed successfully!");
      setBetAmount("");
      setSelectedOption(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <CardTitle className="text-base">{market.question}</CardTitle>
          <Badge
            variant={market.status === "open" ? "default" : "secondary"}
            className="shrink-0 capitalize"
          >
            {market.status}
          </Badge>
        </div>
        {market.description && (
          <p className="text-sm text-muted-foreground">{market.description}</p>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          {market.options.map((option, idx) => {
            const stats = market.option_stats[String(idx)];
            const amount = stats?.amount ?? 0;
            const pct = totalBetAmount > 0 ? (amount / totalBetAmount) * 100 : 0;
            const isWinner = market.winning_option === idx;
            const isSelected = selectedOption === idx;

            return (
              <button
                key={idx}
                className={`relative w-full overflow-hidden rounded-lg border p-3 text-left transition-all ${
                  isWinner
                    ? "border-green-500/50 bg-green-500/10"
                    : isSelected
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/30"
                }`}
                onClick={() => market.status === "open" && setSelectedOption(idx)}
                disabled={market.status !== "open"}
              >
                <div
                  className="absolute inset-y-0 left-0 bg-primary/10"
                  style={{ width: `${pct}%` }}
                />
                <div className="relative flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {isWinner && <CheckCircle className="size-4 text-green-500" />}
                    <span className="text-sm font-medium">{option}</span>
                  </div>
                  <div className="flex items-center gap-3 text-sm text-muted-foreground">
                    <span>{stats?.bets ?? 0} bets</span>
                    <span>{fmt(amount)}</span>
                    <span className="font-medium">{pct.toFixed(1)}%</span>
                  </div>
                </div>
              </button>
            );
          })}
        </div>

        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>Pool: {fmt(market.pool_amount)}</span>
          {market.closes_at && (
            <span className="flex items-center gap-1">
              <Clock className="size-3" />
              Closes {new Date(market.closes_at).toLocaleDateString()}
            </span>
          )}
        </div>

        {isAuthenticated && market.status === "open" && selectedOption !== null && (
          <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/30 p-3">
            <span className="text-sm text-muted-foreground">
              Bet on: <span className="font-medium text-foreground">{market.options[selectedOption]}</span>
            </span>
            <Input
              type="number"
              placeholder="Amount"
              className="w-28"
              value={betAmount}
              onChange={(e) => setBetAmount(e.target.value)}
              min="1"
              step="1"
            />
            <Button size="sm" onClick={handleBet} disabled={placing || !betAmount}>
              Place Bet
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function PredictionsPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const {
    data: summary,
    loading: summaryLoading,
  } = useApi<PredictionSummary>("/predictions/summary");

  const {
    data: openData,
    loading: openLoading,
    error: openError,
  } = useApi<MarketsResponse>("/predictions/markets");

  const {
    data: closedData,
    loading: closedLoading,
    error: closedError,
  } = useApi<MarketsResponse>("/predictions/markets?status=resolved");

  const {
    data: myBetsData,
    loading: myBetsLoading,
    error: myBetsError,
  } = useApi<BetsResponse>(isAuthenticated ? "/predictions/my" : null);

  return (
    <ModuleGate modules={["predictions"]}>
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Predictions</h1>
          <p className="text-sm text-muted-foreground">
            Bet on outcomes and win from the pool
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          {summaryLoading ? (
            <>
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
            </>
          ) : (
            <>
              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    Active Markets
                  </CardTitle>
                  <BarChart3 className="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold">
                    {summary?.active_markets ?? 0}
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    Total Pool
                  </CardTitle>
                  <DollarSign className="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold">
                    {fmt(summary?.total_pool)}
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    My Active Bets
                  </CardTitle>
                  <TrendingUp className="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold">
                    {summary?.user_active_bets ?? 0}
                  </div>
                </CardContent>
              </Card>
            </>
          )}
        </div>

        <Tabs defaultValue="open">
          <TabsList>
            <TabsTrigger value="open">Open Markets</TabsTrigger>
            <TabsTrigger value="resolved">Resolved</TabsTrigger>
            {isAuthenticated && (
              <TabsTrigger value="my-bets">My Bets</TabsTrigger>
            )}
          </TabsList>

          <TabsContent value="open">
            <div className="mt-4 space-y-4">
              {openLoading ? (
                <>
                  <Skeleton className="h-48" />
                  <Skeleton className="h-48" />
                </>
              ) : openError ? (
                <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {openError}
                </div>
              ) : openData?.markets && openData.markets.length > 0 ? (
                openData.markets.map((m) => (
                  <MarketCard
                    key={m.id}
                    market={m}
                    isAuthenticated={isAuthenticated}
                  />
                ))
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No open prediction markets
                </p>
              )}
            </div>
          </TabsContent>

          <TabsContent value="resolved">
            <div className="mt-4 space-y-4">
              {closedLoading ? (
                <>
                  <Skeleton className="h-48" />
                  <Skeleton className="h-48" />
                </>
              ) : closedError ? (
                <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {closedError}
                </div>
              ) : closedData?.markets && closedData.markets.length > 0 ? (
                closedData.markets.map((m) => (
                  <MarketCard
                    key={m.id}
                    market={m}
                    isAuthenticated={isAuthenticated}
                  />
                ))
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No resolved markets
                </p>
              )}
            </div>
          </TabsContent>

          {isAuthenticated && (
            <TabsContent value="my-bets">
              <div className="mt-4">
                {myBetsLoading ? (
                  <div className="space-y-3">
                    {Array.from({ length: 3 }).map((_, i) => (
                      <Skeleton key={i} className="h-16" />
                    ))}
                  </div>
                ) : myBetsError ? (
                  <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                    <AlertCircle className="size-4" />
                    {myBetsError}
                  </div>
                ) : myBetsData?.bets && myBetsData.bets.length > 0 ? (
                  <div className="space-y-2">
                    {myBetsData.bets.map((bet) => (
                      <div
                        key={bet.id}
                        className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">
                            {bet.question}
                          </p>
                          <div className="flex items-center gap-2 text-xs text-muted-foreground">
                            <span>Picked: {bet.option_label}</span>
                            <span>{fmt(bet.amount)}</span>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          {bet.market_status === "resolved" ? (
                            bet.won ? (
                              <Badge className="bg-green-500/15 text-green-400 border-none">
                                <CheckCircle className="mr-1 size-3" />
                                Won
                              </Badge>
                            ) : (
                              <Badge className="bg-red-500/15 text-red-400 border-none">
                                <XCircle className="mr-1 size-3" />
                                Lost
                              </Badge>
                            )
                          ) : (
                            <Badge variant="secondary" className="capitalize">
                              {bet.market_status}
                            </Badge>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="py-8 text-center text-sm text-muted-foreground">
                    You haven't placed any bets yet
                  </p>
                )}
              </div>
            </TabsContent>
          )}
        </Tabs>
      </div>
    </ModuleGate>
  );
}
