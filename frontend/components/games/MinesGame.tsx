"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Loader2, AlertCircle, Bomb, Gem, Target } from "lucide-react";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

// ----- Types -----

interface RevealedCell {
  row: number;
  col: number;
  is_mine: boolean;
}

interface MinesRawState {
  grid_size?: number;
  mine_count?: number;
  revealed: number[];
  mine_positions?: number[];
  hit_mine?: boolean;
  current_multiplier?: number;
  payout?: number;
  safe_revealed?: number;
  remaining_safe?: number;
  auto_cashout?: boolean;
}

interface MinesState {
  grid_size: number;
  mine_count: number;
  revealed: RevealedCell[];
  mines_hit: boolean;
  current_multiplier: number;
}

interface MinesSessionResponse {
  session_id: string;
  game_type: string;
  bet_amount: number;
  state: MinesRawState;
  status: string;
}

interface MinesCashoutResponse {
  game_id: number;
  game_type: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: Record<string, unknown>;
}

// ----- Helpers -----

const GRID_SIZE = 5;
const MINE_COUNTS = [1, 3, 5, 7, 10, 15, 20, 24];

function cellKey(row: number, col: number): string {
  return `${row}-${col}`;
}

/** Convert backend raw state (integer indices) to UI state (row/col objects). */
function mapMinesState(raw: MinesRawState, prevState: MinesState | null): MinesState {
  const minePositions = new Set(raw.mine_positions ?? []);
  const revealed: RevealedCell[] = (raw.revealed ?? []).map((idx: number) => ({
    row: Math.floor(idx / GRID_SIZE),
    col: idx % GRID_SIZE,
    is_mine: minePositions.has(idx),
  }));
  return {
    grid_size: raw.grid_size ?? prevState?.grid_size ?? GRID_SIZE,
    mine_count: raw.mine_count ?? prevState?.mine_count ?? 5,
    revealed,
    mines_hit: raw.hit_mine === true,
    current_multiplier: raw.current_multiplier ?? prevState?.current_multiplier ?? 1.0,
  };
}

// ----- Main Component -----

export default function MinesGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const {
    mutate: startGame,
    loading: startLoading,
    error: startError,
  } = useApiMutation<MinesSessionResponse>("/games/mines/start");

  const {
    mutate: revealCell,
    loading: revealLoading,
    error: revealError,
  } = useApiMutation<MinesSessionResponse>("/games/mines/reveal");

  const {
    mutate: cashout,
    loading: cashoutLoading,
    error: cashoutError,
  } = useApiMutation<MinesCashoutResponse>("/games/mines/cashout");

  const [betAmount, setBetAmount] = useState<string>("");
  const [mineCount, setMineCount] = useState<number>(5);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [betValue, setBetValue] = useState<number>(0);
  const [gameState, setGameState] = useState<MinesState | null>(null);
  const [cashoutResult, setCashoutResult] =
    useState<MinesCashoutResponse | null>(null);

  const isInGame = sessionId !== null && gameState !== null;
  const isGameOver = gameState?.mines_hit === true;
  const isCashedOut = cashoutResult !== null;
  const canAct = isInGame && !isGameOver && !isCashedOut;
  const loading = startLoading || revealLoading || cashoutLoading;
  const error = startError || revealError || cashoutError;

  // Build a lookup for revealed cells
  const revealedMap = new Map<string, RevealedCell>();
  if (gameState) {
    for (const cell of gameState.revealed) {
      revealedMap.set(cellKey(cell.row, cell.col), cell);
    }
  }

  const handleStart = useCallback(async () => {
    const amount = Number(betAmount);
    if (amount <= 0) return;

    setCashoutResult(null);
    const result = await startGame({
      bet_amount: amount,
      mine_count: mineCount,
    });
    if (result) {
      setSessionId(result.session_id);
      setBetValue(result.bet_amount);
      setGameState(mapMinesState(result.state, null));
    }
  }, [betAmount, mineCount, startGame]);

  const handleReveal = useCallback(
    async (row: number, col: number) => {
      if (!sessionId || !canAct) return;
      // Already revealed
      if (revealedMap.has(cellKey(row, col))) return;

      const result = await revealCell({
        session_id: sessionId,
        row,
        col,
      });
      if (result) {
        setGameState(mapMinesState(result.state, gameState));
      }
    },
    [sessionId, canAct, revealedMap, revealCell, gameState]
  );

  const handleCashout = useCallback(async () => {
    if (!sessionId) return;

    const result = await cashout({ session_id: sessionId });
    if (result) {
      setCashoutResult(result);
    }
  }, [sessionId, cashout]);

  const handleNewGame = useCallback(() => {
    setSessionId(null);
    setGameState(null);
    setCashoutResult(null);
  }, []);

  // ---------- Render ----------

  if (!isAuthenticated) {
    return (
      <Card>
        <CardContent className="flex h-48 items-center justify-center">
          <p className="text-sm text-muted-foreground">Login to play Mines</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Bomb className="size-4" />
          Mines
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* ---------- Pre-game Setup ---------- */}
        {!isInGame && !isCashedOut && (
          <div className="space-y-3">
            {/* Bet amount */}
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

            {/* Mine count */}
            <div className="space-y-2">
              <label className="text-sm font-medium">Mines</label>
              <div className="flex flex-wrap gap-1.5">
                {MINE_COUNTS.map((count) => (
                  <Button
                    key={count}
                    variant={mineCount === count ? "default" : "outline"}
                    size="sm"
                    onClick={() => setMineCount(count)}
                  >
                    {count}
                  </Button>
                ))}
              </div>
            </div>

            <Button
              className="w-full"
              size="lg"
              disabled={!Number(betAmount) || loading}
              onClick={handleStart}
            >
              {startLoading ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Starting...
                </>
              ) : (
                "Start Game"
              )}
            </Button>
          </div>
        )}

        {/* ---------- In-game ---------- */}
        {isInGame && (
          <>
            {/* Stats bar */}
            <div className="flex items-center justify-between text-sm">
              <div className="space-x-3">
                <span className="text-muted-foreground">
                  Bet: <span className="font-mono font-semibold text-foreground">{fmt(betValue, 2)}</span>
                </span>
                <span className="text-muted-foreground">
                  Mines: <span className="font-mono font-semibold text-foreground">{gameState.mine_count}</span>
                </span>
              </div>
              <Badge variant="secondary" className="font-mono">
                {fmt(gameState.current_multiplier, 2)}x
              </Badge>
            </div>

            <Separator />

            {/* 5x5 Grid */}
            <div className="mx-auto w-fit">
              <div
                className="grid gap-1.5"
                style={{
                  gridTemplateColumns: `repeat(${GRID_SIZE}, 1fr)`,
                }}
              >
                {Array.from({ length: GRID_SIZE }, (_, row) =>
                  Array.from({ length: GRID_SIZE }, (_, col) => {
                    const key = cellKey(row, col);
                    const revealed = revealedMap.get(key);
                    const isMine = revealed?.is_mine === true;
                    const isSafe = revealed !== undefined && !isMine;

                    let cellClass =
                      "flex size-12 items-center justify-center rounded-md border text-lg font-bold transition-all sm:size-14";

                    if (isMine) {
                      cellClass +=
                        " border-red-500/50 bg-red-500/20 text-red-400";
                    } else if (isSafe) {
                      cellClass +=
                        " border-emerald-500/50 bg-emerald-500/20 text-emerald-400";
                    } else {
                      cellClass +=
                        " border-border bg-muted/50 hover:bg-muted hover:border-primary/50 cursor-pointer";
                    }

                    if (!canAct && !revealed) {
                      cellClass += " opacity-50 cursor-default";
                    }

                    return (
                      <button
                        key={key}
                        className={cellClass}
                        disabled={!canAct || revealed !== undefined}
                        onClick={() => handleReveal(row, col)}
                      >
                        {isMine && <Bomb className="size-5" />}
                        {isSafe && <Gem className="size-5" />}
                        {revealLoading && !revealed && canAct && null}
                      </button>
                    );
                  })
                )}
              </div>
            </div>

            <Separator />

            {/* Cashout / Game over / Cashed out */}
            {canAct && (
              <div className="space-y-2">
                <Button
                  className="w-full"
                  size="lg"
                  onClick={handleCashout}
                  disabled={
                    cashoutLoading || gameState.revealed.length === 0
                  }
                >
                  {cashoutLoading ? (
                    <>
                      <Loader2 className="size-4 animate-spin" />
                      Cashing out...
                    </>
                  ) : (
                    <>
                      <Target className="size-4" />
                      Cashout {fmt(betValue * gameState.current_multiplier, 2)}
                    </>
                  )}
                </Button>
                <p className="text-center text-xs text-muted-foreground">
                  Current multiplier:{" "}
                  <span className="font-mono font-semibold">
                    {fmt(gameState.current_multiplier, 2)}x
                  </span>
                </p>
              </div>
            )}

            {isGameOver && (
              <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
                <p className="text-xl font-bold text-red-400">BOOM!</p>
                <p className="text-sm text-muted-foreground">
                  You hit a mine and lost{" "}
                  <span className="font-mono font-semibold text-foreground">
                    {fmt(betValue, 2)}
                  </span>
                </p>
                <Button className="w-full" size="lg" onClick={handleNewGame}>
                  Play Again
                </Button>
              </div>
            )}

            {isCashedOut && (
              <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
                <p className="text-xl font-bold text-emerald-400">
                  CASHED OUT!
                </p>
                <div className="flex items-center justify-center gap-2">
                  <Badge variant="default">
                    +{fmt(cashoutResult.profit, 2)}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    at {fmt(cashoutResult.multiplier, 2)}x
                  </span>
                </div>
                <p className="text-sm text-muted-foreground">
                  Payout:{" "}
                  <span className="font-mono font-semibold text-foreground">
                    {fmt(cashoutResult.payout, 2)}
                  </span>
                </p>
                <Button className="w-full" size="lg" onClick={handleNewGame}>
                  Play Again
                </Button>
              </div>
            )}
          </>
        )}

        {/* Post-cashout when not in game */}
        {!isInGame && isCashedOut && (
          <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
            <p className="text-xl font-bold text-emerald-400">CASHED OUT!</p>
            <div className="flex items-center justify-center gap-2">
              <Badge variant="default">+{fmt(cashoutResult.profit, 2)}</Badge>
              <span className="text-xs text-muted-foreground">
                at {fmt(cashoutResult.multiplier, 2)}x
              </span>
            </div>
            <p className="text-sm text-muted-foreground">
              Payout:{" "}
              <span className="font-mono font-semibold text-foreground">
                {fmt(cashoutResult.payout, 2)}
              </span>
            </p>
            <Button className="w-full" size="lg" onClick={handleNewGame}>
              Play Again
            </Button>
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
