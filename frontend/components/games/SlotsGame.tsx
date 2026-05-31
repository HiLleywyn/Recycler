"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

const SYMBOLS = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣"];

interface SlotsResult {
  game_id: string;
  game_type: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    reels: [string, string, string];
    won: boolean;
    jackpot: boolean;
  };
}

export default function SlotsGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const user = useAuthStore((s) => s.user);

  const [betAmount, setBetAmount] = useState("");
  const [displayReels, setDisplayReels] = useState<[string, string, string]>([
    "🍒",
    "⭐",
    "🔔",
  ]);
  const [spinning, setSpinning] = useState(false);
  const [lastResult, setLastResult] = useState<SlotsResult | null>(null);
  const [history, setHistory] = useState<SlotsResult[]>([]);

  const { mutate, loading, error } = useApiMutation<SlotsResult>(
    "/games/slots/play"
  );

  const spinIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pendingResultRef = useRef<SlotsResult | null>(null);

  const randomSymbol = () => SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)];

  const stopSpinning = useCallback(() => {
    if (spinIntervalRef.current) {
      clearInterval(spinIntervalRef.current);
      spinIntervalRef.current = null;
    }
    const result = pendingResultRef.current;
    if (result) {
      setDisplayReels(result.result_data.reels);
      setLastResult(result);
      setHistory((prev) => [result, ...prev].slice(0, 5));
      pendingResultRef.current = null;
    }
    setSpinning(false);
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (spinIntervalRef.current) {
        clearInterval(spinIntervalRef.current);
      }
    };
  }, []);

  if (!isAuthenticated) {
    return (
      <div className="flex h-48 items-center justify-center rounded-lg bg-muted/50">
        <p className="text-sm text-muted-foreground">Login to play</p>
      </div>
    );
  }

  const bet = parseFloat(betAmount);
  const canPlay = !isNaN(bet) && bet > 0 && !loading && !spinning;

  async function handleSpin() {
    if (!canPlay) return;

    setSpinning(true);
    setLastResult(null);

    // Start visual spinning animation
    spinIntervalRef.current = setInterval(() => {
      setDisplayReels([randomSymbol(), randomSymbol(), randomSymbol()]);
    }, 80);

    // Fire API request
    const result = await mutate({
      bet_amount: bet,
      options: {},
    });

    if (result) {
      pendingResultRef.current = result;
      // Let the animation run a bit then settle
      setTimeout(() => {
        stopSpinning();
      }, 600);
    } else {
      // API error - stop spinning
      stopSpinning();
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
      {/* Reels display */}
      <div className="flex items-center justify-center gap-3 rounded-lg bg-muted/50 p-6">
        {displayReels.map((symbol, i) => (
          <div
            key={i}
            className={`flex h-20 w-20 items-center justify-center rounded-lg border-2 bg-background text-4xl ${
              spinning
                ? "border-primary/50 animate-pulse"
                : lastResult
                  ? lastResult.result_data.won
                    ? "border-chart-green"
                    : "border-border"
                  : "border-border"
            }`}
          >
            {symbol}
          </div>
        ))}
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
            disabled={loading || spinning}
          />
          <Button
            variant="secondary"
            size="sm"
            onClick={handleHalf}
            disabled={loading || spinning}
          >
            Half
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={handleMax}
            disabled={loading || spinning}
          >
            Max
          </Button>
        </div>
      </div>

      {/* Spin button */}
      <Button
        className="w-full"
        size="lg"
        disabled={!canPlay}
        onClick={handleSpin}
      >
        {spinning ? "Spinning..." : "Spin"}
      </Button>

      {/* Error message */}
      {error && (
        <p className="text-sm text-destructive">{error}</p>
      )}

      {/* Result display */}
      {lastResult && !spinning && (
        <>
          <Separator />
          <div
            className={`flex flex-col items-center gap-2 rounded-lg p-4 ${
              lastResult.result_data.won
                ? lastResult.result_data.jackpot
                  ? "bg-yellow-500/10 text-yellow-500"
                  : "bg-chart-green/10 text-chart-green"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            {lastResult.result_data.jackpot && (
              <span className="text-lg font-bold tracking-wide">JACKPOT!</span>
            )}
            <span className="text-xl font-bold">
              {lastResult.result_data.won ? "You won!" : "No luck"}
            </span>
            <span className="text-lg font-semibold">
              {lastResult.result_data.won ? "+" : ""}
              {fmt(lastResult.profit)} coins
            </span>
            {lastResult.result_data.won && (
              <span className="text-xs text-muted-foreground">
                {fmt(lastResult.multiplier, 1)}x multiplier
              </span>
            )}
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
                    {r.result_data.reels.join(" ")}
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
