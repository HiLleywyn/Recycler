"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

interface CoinflipResult {
  game_id: string;
  game_type: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    choice: "heads" | "tails";
    result: "heads" | "tails";
    won: boolean;
  };
}

export default function CoinflipGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const user = useAuthStore((s) => s.user);

  const [choice, setChoice] = useState<"heads" | "tails" | null>(null);
  const [betAmount, setBetAmount] = useState("");
  const [lastResult, setLastResult] = useState<CoinflipResult | null>(null);
  const [history, setHistory] = useState<CoinflipResult[]>([]);

  const { mutate, loading, error } = useApiMutation<CoinflipResult>(
    "/games/coinflip/play"
  );

  if (!isAuthenticated) {
    return (
      <div className="flex h-48 items-center justify-center rounded-lg bg-muted/50">
        <p className="text-sm text-muted-foreground">Login to play</p>
      </div>
    );
  }

  const bet = parseFloat(betAmount);
  const canPlay = choice !== null && !isNaN(bet) && bet > 0 && !loading;

  async function handlePlay() {
    if (!choice || isNaN(bet) || bet <= 0) return;

    const result = await mutate({
      bet_amount: bet,
      options: { choice },
    });

    if (result) {
      setLastResult(result);
      setHistory((prev) => [result, ...prev].slice(0, 5));
    }
  }

  function handleHalf() {
    const current = parseFloat(betAmount);
    if (!isNaN(current) && current > 0) {
      setBetAmount(fmt(current / 2));
    }
  }

  function handleMax() {
    const balance = user?.balance;
    if (balance != null && balance > 0) {
      setBetAmount(fmt(balance));
    }
  }

  return (
    <div className="space-y-4">
      {/* Choice buttons */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Pick a side</label>
        <div className="grid grid-cols-2 gap-3">
          <Button
            variant={choice === "heads" ? "default" : "outline"}
            size="lg"
            className="h-16 text-lg"
            onClick={() => setChoice("heads")}
            disabled={loading}
          >
            Heads
          </Button>
          <Button
            variant={choice === "tails" ? "default" : "outline"}
            size="lg"
            className="h-16 text-lg"
            onClick={() => setChoice("tails")}
            disabled={loading}
          >
            Tails
          </Button>
        </div>
      </div>

      {/* Bet input */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Bet Amount</label>
        <div className="flex gap-2">
          <Input
            type="number"
            placeholder="0.00"
            min={0}
            step="0.01"
            value={betAmount}
            onChange={(e) => setBetAmount(e.target.value)}
            disabled={loading}
          />
          <Button
            variant="secondary"
            size="sm"
            onClick={handleHalf}
            disabled={loading}
          >
            Half
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={handleMax}
            disabled={loading}
          >
            Max
          </Button>
        </div>
      </div>

      {/* Play button */}
      <Button
        className="w-full"
        size="lg"
        disabled={!canPlay}
        onClick={handlePlay}
      >
        {loading ? "Flipping..." : "Flip Coin"}
      </Button>

      {/* Error message */}
      {error && (
        <p className="text-sm text-destructive">{error}</p>
      )}

      {/* Result display */}
      {lastResult && (
        <>
          <Separator />
          <div
            className={`flex flex-col items-center gap-2 rounded-lg p-4 ${
              lastResult.result_data.won
                ? "bg-chart-green/10 text-chart-green"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            <span className="text-3xl font-bold">
              {lastResult.result_data.result === "heads" ? "Heads" : "Tails"}
            </span>
            <span className="text-sm font-medium">
              {lastResult.result_data.won ? "You won!" : "You lost!"}
            </span>
            <span className="text-lg font-semibold">
              {lastResult.result_data.won ? "+" : ""}
              {fmt(lastResult.profit)} coins
            </span>
            <span className="text-xs text-muted-foreground">
              {fmt(lastResult.multiplier, 1)}x multiplier
            </span>
          </div>
        </>
      )}

      {/* Past results */}
      <div className="rounded-lg border border-border p-3">
        <p className="mb-2 text-xs font-medium text-muted-foreground">
          Recent Results
        </p>
        {history.length === 0 ? (
          <div className="flex h-16 items-center justify-center">
            <p className="text-xs text-muted-foreground">No games played yet</p>
          </div>
        ) : (
          <div className="space-y-2">
            {history.map((r) => (
              <div
                key={r.game_id}
                className="flex items-center justify-between text-sm"
              >
                <div className="flex items-center gap-2">
                  <Badge
                    variant={r.result_data.won ? "default" : "destructive"}
                    className="text-[10px]"
                  >
                    {r.result_data.result}
                  </Badge>
                  <span className="text-muted-foreground">
                    Bet {fmt(r.bet_amount)}
                  </span>
                </div>
                <span
                  className={
                    r.result_data.won ? "text-chart-green" : "text-destructive"
                  }
                >
                  {r.result_data.won ? "+" : ""}
                  {fmt(r.profit)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
