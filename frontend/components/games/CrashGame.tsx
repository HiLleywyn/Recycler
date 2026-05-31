"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Loader2, AlertCircle, TrendingUp } from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

// ----- Types -----

interface CrashJoinResponse {
  session_id: string;
  bet_amount: number;
  state: {
    status: string;
    multiplier: number;
  };
}

interface CrashCashoutResponse {
  payout: number;
  profit: number;
  multiplier: number;
  crashed: boolean;
}

interface CrashCurrentResponse {
  round_id: string;
  status: string;
  multiplier: number;
  crash_point: number | null;
  players: number;
}

// ----- Main Component -----

export default function CrashGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const {
    mutate: joinRound,
    loading: joinLoading,
    error: joinError,
  } = useApiMutation<CrashJoinResponse>("/games/crash/join");

  const {
    mutate: cashoutRound,
    loading: cashoutLoading,
    error: cashoutError,
  } = useApiMutation<CrashCashoutResponse>("/games/crash/cashout");

  const {
    data: currentRound,
    loading: roundLoading,
  } = useApi<CrashCurrentResponse>("/games/crash/current");

  const [betAmount, setBetAmount] = useState<string>("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [betValue, setBetValue] = useState<number>(0);
  const [displayMultiplier, setDisplayMultiplier] = useState<number>(1.0);
  const [crashed, setCrashed] = useState<boolean>(false);
  const [cashoutResult, setCashoutResult] =
    useState<CrashCashoutResponse | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number>(0);

  const isJoined = sessionId !== null;
  const isRunning = isJoined && !crashed && !cashoutResult;
  const loading = joinLoading || cashoutLoading;
  const error = joinError || cashoutError;

  // Simulate multiplier climbing after joining
  useEffect(() => {
    if (!isRunning) return;

    startTimeRef.current = Date.now();

    intervalRef.current = setInterval(() => {
      const elapsed = (Date.now() - startTimeRef.current) / 1000;
      // Exponential growth curve: starts slow, accelerates
      // ~1.0x at 0s, ~1.5x at 3s, ~2.0x at 5s, ~3.0x at 8s, etc.
      const newMultiplier = Math.pow(Math.E, elapsed * 0.08);
      setDisplayMultiplier(newMultiplier);
    }, 50);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [isRunning]);

  // Clean up interval on unmount
  useEffect(() => {
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, []);

  const handleJoin = useCallback(async () => {
    const amount = Number(betAmount);
    if (amount <= 0) return;

    setCashoutResult(null);
    setCrashed(false);
    setDisplayMultiplier(1.0);

    const result = await joinRound({ bet_amount: amount });
    if (result) {
      setSessionId(result.session_id);
      setBetValue(result.bet_amount);
    }
  }, [betAmount, joinRound]);

  const handleCashout = useCallback(async () => {
    if (!sessionId) return;

    // Stop the multiplier animation immediately for snappy UX
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    const result = await cashoutRound({ session_id: sessionId });
    if (result) {
      if (result.crashed) {
        setCrashed(true);
        setDisplayMultiplier(result.multiplier);
      } else {
        setCashoutResult(result);
        setDisplayMultiplier(result.multiplier);
      }
    }
  }, [sessionId, cashoutRound]);

  const handleNewRound = useCallback(() => {
    setSessionId(null);
    setCashoutResult(null);
    setCrashed(false);
    setDisplayMultiplier(1.0);
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  // ---------- Multiplier Display Color ----------
  function multiplierColor(): string {
    if (crashed) return "text-red-400";
    if (cashoutResult) return "text-emerald-400";
    if (displayMultiplier >= 3) return "text-yellow-400";
    if (displayMultiplier >= 2) return "text-emerald-400";
    return "text-foreground";
  }

  // ---------- Render ----------

  if (!isAuthenticated) {
    return (
      <Card>
        <CardContent className="flex h-48 items-center justify-center">
          <p className="text-sm text-muted-foreground">Login to play Crash</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TrendingUp className="size-4" />
          Crash
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* ---------- Multiplier Display ---------- */}
        <div
          className={`flex min-h-[120px] flex-col items-center justify-center rounded-lg border ${
            crashed
              ? "border-red-500/30 bg-red-500/5"
              : isRunning
                ? "border-primary/30 bg-primary/5"
                : "border-border bg-muted/50"
          } p-6 transition-colors`}
        >
          <span
            className={`font-mono text-5xl font-bold tracking-tight ${multiplierColor()}`}
          >
            {fmt(displayMultiplier, 2)}x
          </span>

          {crashed && (
            <span className="mt-2 text-sm font-medium text-red-400">
              CRASHED
            </span>
          )}

          {cashoutResult && !crashed && (
            <span className="mt-2 text-sm font-medium text-emerald-400">
              CASHED OUT
            </span>
          )}

          {!isJoined && !crashed && !cashoutResult && (
            <span className="mt-2 text-xs text-muted-foreground">
              {roundLoading
                ? "Loading..."
                : currentRound?.status === "running"
                  ? "Round in progress"
                  : "Waiting for next round"}
            </span>
          )}

          {isRunning && (
            <span className="mt-2 text-xs text-muted-foreground">
              Multiplier climbing...
            </span>
          )}
        </div>

        {/* ---------- Bet input + Join ---------- */}
        {!isJoined && !cashoutResult && !crashed && (
          <div className="space-y-3">
            <div className="space-y-2">
              <label className="text-sm font-medium">Bet Amount</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  placeholder="0.00"
                  min={0}
                  step="any"
                  value={betAmount}
                  onChange={(e) => setBetAmount(e.target.value)}
                />
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() =>
                    setBetAmount((prev) =>
                      fmt(Math.max(Number(prev) / 2, 0), 2)
                    )
                  }
                >
                  Half
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() =>
                    setBetAmount((prev) => fmt(Number(prev) * 2, 2))
                  }
                >
                  2x
                </Button>
              </div>
            </div>

            <Button
              className="w-full"
              size="lg"
              disabled={!Number(betAmount) || loading}
              onClick={handleJoin}
            >
              {joinLoading ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Joining...
                </>
              ) : (
                "Join Round"
              )}
            </Button>
          </div>
        )}

        {/* ---------- Cashout button (while running) ---------- */}
        {isRunning && (
          <div className="space-y-2">
            <Button
              className="w-full"
              size="lg"
              onClick={handleCashout}
              disabled={cashoutLoading}
            >
              {cashoutLoading ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Cashing out...
                </>
              ) : (
                <>Cashout at {fmt(displayMultiplier, 2)}x</>
              )}
            </Button>

            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                Bet: <span className="font-mono font-semibold text-foreground">{fmt(betValue, 2)}</span>
              </span>
              <span className="text-muted-foreground">
                Potential:{" "}
                <span className="font-mono font-semibold text-foreground">
                  {fmt(betValue * displayMultiplier, 2)}
                </span>
              </span>
            </div>
          </div>
        )}

        {/* ---------- Result: Crashed ---------- */}
        {crashed && (
          <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
            <p className="text-xl font-bold text-red-400">
              CRASHED at {fmt(displayMultiplier, 2)}x
            </p>
            <Badge variant="destructive">-{fmt(betValue, 2)}</Badge>
            <Button className="w-full" size="lg" onClick={handleNewRound}>
              Play Again
            </Button>
          </div>
        )}

        {/* ---------- Result: Cashed Out ---------- */}
        {cashoutResult && !crashed && (
          <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
            <p className="text-xl font-bold text-emerald-400">
              CASHED OUT at {fmt(cashoutResult.multiplier, 2)}x
            </p>
            <div className="flex items-center justify-center gap-2">
              <Badge variant="default">+{fmt(cashoutResult.profit, 2)}</Badge>
              <span className="text-xs text-muted-foreground">
                Payout: {fmt(cashoutResult.payout, 2)}
              </span>
            </div>
            <Button className="w-full" size="lg" onClick={handleNewRound}>
              Play Again
            </Button>
          </div>
        )}

        <Separator />

        {/* ---------- Current Round Info ---------- */}
        {currentRound && (
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">
              Current Round
            </p>
            <div className="rounded-lg border border-border p-3 text-xs text-muted-foreground">
              <div className="flex justify-between">
                <span>Status</span>
                <Badge
                  variant={
                    currentRound.status === "running"
                      ? "default"
                      : "secondary"
                  }
                >
                  {currentRound.status}
                </Badge>
              </div>
              <div className="mt-1 flex justify-between">
                <span>Players</span>
                <span className="font-mono">
                  {currentRound.players ?? 0}
                </span>
              </div>
              {currentRound.crash_point !== null && (
                <div className="mt-1 flex justify-between">
                  <span>Crash Point</span>
                  <span className="font-mono text-red-400">
                    {fmt(currentRound.crash_point, 2)}x
                  </span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ---------- Error ---------- */}
        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-xs text-destructive">
            <AlertCircle className="size-3.5 shrink-0" />
            {error}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
