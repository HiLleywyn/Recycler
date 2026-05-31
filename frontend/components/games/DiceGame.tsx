"use client";

import { useState, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

interface DiceResult {
  game_id: string;
  game_type: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    target: number;
    roll: number;
    over_under: "over" | "under";
    won: boolean;
  };
}

export default function DiceGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const user = useAuthStore((s) => s.user);

  const [target, setTarget] = useState(50);
  const [overUnder, setOverUnder] = useState<"over" | "under">("over");
  const [betAmount, setBetAmount] = useState("");
  const [lastResult, setLastResult] = useState<DiceResult | null>(null);
  const [history, setHistory] = useState<DiceResult[]>([]);

  const { mutate, loading, error } = useApiMutation<DiceResult>(
    "/games/dice/play"
  );

  const winChance = useMemo(() => {
    if (overUnder === "over") return 100 - target;
    return target - 1;
  }, [target, overUnder]);

  const multiplier = useMemo(() => {
    if (winChance <= 0 || winChance >= 100) return 0;
    // ~99% RTP (1% house edge)
    return (99 / winChance);
  }, [winChance]);

  const bet = parseFloat(betAmount);
  const potentialPayout = useMemo(() => {
    if (isNaN(bet) || bet <= 0) return 0;
    return bet * multiplier;
  }, [bet, multiplier]);

  if (!isAuthenticated) {
    return (
      <div className="flex h-48 items-center justify-center rounded-lg bg-muted/50">
        <p className="text-sm text-muted-foreground">Login to play</p>
      </div>
    );
  }

  const canPlay = !isNaN(bet) && bet > 0 && !loading && winChance > 0 && winChance < 100;

  async function handlePlay() {
    if (!canPlay) return;

    const result = await mutate({
      bet_amount: bet,
      options: { target, over_under: overUnder },
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
      {/* Target slider */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <label className="text-sm font-medium">Target</label>
          <span className="text-sm font-semibold tabular-nums">{target}</span>
        </div>
        <input
          type="range"
          min={1}
          max={99}
          value={target}
          onChange={(e) => setTarget(parseInt(e.target.value))}
          disabled={loading}
          className="w-full accent-primary"
        />
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>1</span>
          <span>50</span>
          <span>99</span>
        </div>
      </div>

      {/* Over / Under toggle */}
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant={overUnder === "over" ? "default" : "outline"}
          size="lg"
          onClick={() => setOverUnder("over")}
          disabled={loading}
        >
          Roll Over {target}
        </Button>
        <Button
          variant={overUnder === "under" ? "default" : "outline"}
          size="lg"
          onClick={() => setOverUnder("under")}
          disabled={loading}
        >
          Roll Under {target}
        </Button>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 rounded-lg bg-muted/50 p-3">
        <div className="text-center">
          <p className="text-xs text-muted-foreground">Win Chance</p>
          <p className="text-sm font-semibold">{fmt(winChance, 1)}%</p>
        </div>
        <div className="text-center">
          <p className="text-xs text-muted-foreground">Multiplier</p>
          <p className="text-sm font-semibold">{fmt(multiplier, 4)}x</p>
        </div>
        <div className="text-center">
          <p className="text-xs text-muted-foreground">Payout</p>
          <p className="text-sm font-semibold">{fmt(potentialPayout)}</p>
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

      {/* Roll button */}
      <Button
        className="w-full"
        size="lg"
        disabled={!canPlay}
        onClick={handlePlay}
      >
        {loading ? "Rolling..." : "Roll Dice"}
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
            <span className="text-5xl font-bold tabular-nums">
              {fmt(lastResult.result_data.roll, 2)}
            </span>
            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">
                {lastResult.result_data.over_under === "over" ? "Over" : "Under"}{" "}
                {lastResult.result_data.target}
              </span>
              <span className="font-medium">
                {lastResult.result_data.won ? "Win!" : "Loss"}
              </span>
            </div>
            <span className="text-lg font-semibold">
              {lastResult.result_data.won ? "+" : ""}
              {fmt(lastResult.profit)} coins
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
                    className="text-[10px] tabular-nums"
                  >
                    {fmt(r.result_data.roll, 2)}
                  </Badge>
                  <span className="text-muted-foreground">
                    {r.result_data.over_under === "over" ? ">" : "<"}{" "}
                    {r.result_data.target} | Bet {fmt(r.bet_amount)}
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
