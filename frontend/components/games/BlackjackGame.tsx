"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Loader2, AlertCircle, Spade } from "lucide-react";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

// ----- Types -----

interface CardData {
  value: string;
  suit: string;
}

interface BlackjackState {
  player_hand: CardData[];
  dealer_hand?: CardData[];
  dealer_showing?: CardData[];
  player_value: number;
  dealer_value?: number;
  dealer_value_showing?: number;
  outcome?: string;
  status:
    | "playing"
    | "player_bust"
    | "dealer_bust"
    | "player_win"
    | "dealer_win"
    | "push"
    | "blackjack";
}

interface BlackjackStartResponse {
  session_id: string;
  game_type: string;
  bet_amount: number;
  state: Record<string, unknown>;
  status: string;
}

interface BlackjackActionResponse {
  session_id: string;
  game_type: string;
  bet_amount: number;
  state: Record<string, unknown>;
  status: string;
}

// ----- Helpers -----

const SUIT_EMOJI: Record<string, string> = {
  spades: "\u2660",
  hearts: "\u2665",
  diamonds: "\u2666",
  clubs: "\u2663",
  S: "\u2660",
  H: "\u2665",
  D: "\u2666",
  C: "\u2663",
};

const RED_SUITS = new Set(["hearts", "diamonds", "H", "D"]);

function suitEmoji(suit: string): string {
  return SUIT_EMOJI[suit] ?? suit;
}

function isRedSuit(suit: string): boolean {
  return RED_SUITS.has(suit);
}

type GameOutcome = "win" | "lose" | "push";

function outcomeOf(status: BlackjackState["status"]): GameOutcome {
  switch (status) {
    case "player_win":
    case "dealer_bust":
    case "blackjack":
      return "win";
    case "push":
      return "push";
    default:
      return "lose";
  }
}

function outcomeLabel(status: BlackjackState["status"]): string {
  switch (status) {
    case "player_win":
      return "YOU WIN";
    case "dealer_bust":
      return "DEALER BUST - YOU WIN";
    case "blackjack":
      return "BLACKJACK!";
    case "player_bust":
      return "BUST - YOU LOSE";
    case "dealer_win":
      return "DEALER WINS";
    case "push":
      return "PUSH";
    default:
      return "";
  }
}

function outcomeColor(outcome: GameOutcome): string {
  switch (outcome) {
    case "win":
      return "text-emerald-400";
    case "lose":
      return "text-red-400";
    case "push":
      return "text-yellow-400";
  }
}

function payoutMultiplier(status: BlackjackState["status"]): number {
  switch (status) {
    case "blackjack":
      return 2.5;
    case "player_win":
    case "dealer_bust":
      return 2;
    case "push":
      return 1;
    default:
      return 0;
  }
}

/** Map the raw API response (GameSession) into a BlackjackState the UI expects. */
function mapToBlackjackState(raw: Record<string, unknown>, sessionStatus: string): BlackjackState {
  const s = raw as Record<string, unknown>;
  const outcome = s.outcome as string | undefined;

  // Determine display status
  let status: BlackjackState["status"] = "playing";
  if (sessionStatus === "completed" || outcome) {
    switch (outcome) {
      case "bust": status = "player_bust"; break;
      case "win": status = "player_win"; break;
      case "lose": status = "dealer_win"; break;
      case "push": status = "push"; break;
      case "blackjack": status = "blackjack"; break;
      default: status = outcome ? "dealer_win" : "playing";
    }
  }

  return {
    player_hand: (s.player_hand ?? []) as CardData[],
    dealer_hand: (s.dealer_hand ?? s.dealer_showing ?? []) as CardData[],
    dealer_showing: (s.dealer_showing ?? []) as CardData[],
    player_value: (s.player_value ?? 0) as number,
    dealer_value: (s.dealer_value ?? s.dealer_value_showing ?? 0) as number,
    dealer_value_showing: s.dealer_value_showing as number | undefined,
    outcome,
    status,
  };
}

// ----- Sub-components -----

function PlayingCard({
  card,
  faceDown = false,
}: {
  card: CardData;
  faceDown?: boolean;
}) {
  if (faceDown) {
    return (
      <div className="flex h-20 w-14 items-center justify-center rounded-lg border-2 border-primary/30 bg-primary/10 text-lg font-bold text-primary/40 shadow-sm">
        ?
      </div>
    );
  }

  const red = isRedSuit(card.suit);

  return (
    <div
      className={`flex h-20 w-14 flex-col items-center justify-center rounded-lg border bg-card shadow-sm ${
        red ? "text-red-500" : "text-foreground"
      }`}
    >
      <span className="text-base font-bold leading-none">{card.value}</span>
      <span className="text-lg leading-none">{suitEmoji(card.suit)}</span>
    </div>
  );
}

function Hand({
  label,
  cards,
  value,
  hideSecond = false,
}: {
  label: string;
  cards: CardData[];
  value: number;
  hideSecond?: boolean;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-sm font-semibold">
          {hideSecond ? "?" : fmt(value, 0)}
        </span>
      </div>
      <div className="flex gap-2">
        {cards.map((card, i) => (
          <PlayingCard
            key={`${card.value}${card.suit}-${i}`}
            card={card}
            faceDown={hideSecond && i === 1}
          />
        ))}
      </div>
    </div>
  );
}

// ----- Main Component -----

export default function BlackjackGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const {
    mutate: startGame,
    loading: startLoading,
    error: startError,
  } = useApiMutation<BlackjackStartResponse>("/games/blackjack/start");

  const {
    mutate: sendAction,
    loading: actionLoading,
    error: actionError,
  } = useApiMutation<BlackjackActionResponse>("/games/blackjack/action");

  const [betAmount, setBetAmount] = useState<string>("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [betValue, setBetValue] = useState<number>(0);
  const [gameState, setGameState] = useState<BlackjackState | null>(null);

  const isPlaying = gameState?.status === "playing";
  const isGameOver = gameState !== null && !isPlaying;
  const loading = startLoading || actionLoading;
  const error = startError || actionError;

  const handleDeal = useCallback(async () => {
    const amount = Number(betAmount);
    if (amount <= 0) return;

    const result = await startGame({ bet_amount: amount });
    if (result) {
      setSessionId(result.session_id);
      setBetValue(result.bet_amount);
      setGameState(mapToBlackjackState(result.state, result.status));
    }
  }, [betAmount, startGame]);

  const handleAction = useCallback(
    async (action: "hit" | "stand" | "double") => {
      if (!sessionId) return;

      const result = await sendAction({ session_id: sessionId, action });
      if (result) {
        setBetValue(result.bet_amount);
        setGameState(mapToBlackjackState(result.state, result.status));
      }
    },
    [sessionId, sendAction]
  );

  const handlePlayAgain = useCallback(() => {
    setSessionId(null);
    setGameState(null);
    setBetValue(0);
  }, []);

  // ---------- Render ----------

  if (!isAuthenticated) {
    return (
      <Card>
        <CardContent className="flex h-48 items-center justify-center">
          <p className="text-sm text-muted-foreground">
            Login to play Blackjack
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Spade className="size-4" />
          Blackjack
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* ---------- Pre-game: Bet Input ---------- */}
        {!gameState && (
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
                      String(Math.max(Number(prev) / 2, 0).toFixed(2))
                    )
                  }
                >
                  Half
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() =>
                    setBetAmount((prev) => String((Number(prev) * 2).toFixed(2)))
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
              onClick={handleDeal}
            >
              {startLoading ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Dealing...
                </>
              ) : (
                "Deal"
              )}
            </Button>
          </div>
        )}

        {/* ---------- In-game: Hands ---------- */}
        {gameState && (
          <>
            {/* Dealer hand */}
            <Hand
              label="Dealer"
              cards={isPlaying && gameState.dealer_hand && gameState.dealer_hand.length === 1
                ? [...gameState.dealer_hand, { value: "?", suit: "?" }]
                : gameState.dealer_hand ?? []}
              value={gameState.dealer_value ?? 0}
              hideSecond={isPlaying}
            />

            <Separator />

            {/* Player hand */}
            <Hand
              label="You"
              cards={gameState.player_hand}
              value={gameState.player_value}
            />

            {/* Bet indicator */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Bet</span>
              <span className="font-mono font-semibold">
                {fmt(betValue, 2)}
              </span>
            </div>

            {/* ---------- Actions ---------- */}
            {isPlaying && (
              <div className="flex gap-2">
                <Button
                  className="flex-1"
                  onClick={() => handleAction("hit")}
                  disabled={loading}
                >
                  {actionLoading ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    "Hit"
                  )}
                </Button>
                <Button
                  className="flex-1"
                  variant="secondary"
                  onClick={() => handleAction("stand")}
                  disabled={loading}
                >
                  Stand
                </Button>
                <Button
                  className="flex-1"
                  variant="outline"
                  onClick={() => handleAction("double")}
                  disabled={loading}
                >
                  Double
                </Button>
              </div>
            )}

            {/* ---------- Result overlay ---------- */}
            {isGameOver && (
              <div className="space-y-3 rounded-lg border bg-muted/50 p-4 text-center">
                {(() => {
                  const outcome = outcomeOf(gameState.status);
                  const payout = betValue * payoutMultiplier(gameState.status);
                  const profit = payout - betValue;

                  return (
                    <>
                      <p
                        className={`text-xl font-bold ${outcomeColor(outcome)}`}
                      >
                        {outcomeLabel(gameState.status)}
                      </p>

                      <div className="flex items-center justify-center gap-2">
                        <Badge
                          variant={
                            outcome === "win"
                              ? "default"
                              : outcome === "lose"
                                ? "destructive"
                                : "secondary"
                          }
                        >
                          {outcome === "win"
                            ? `+${fmt(profit, 2)}`
                            : outcome === "lose"
                              ? `-${fmt(betValue, 2)}`
                              : "0.00"}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          Payout: {fmt(payout, 2)}
                        </span>
                      </div>

                      <Button
                        className="w-full"
                        size="lg"
                        onClick={handlePlayAgain}
                      >
                        Play Again
                      </Button>
                    </>
                  );
                })()}
              </div>
            )}
          </>
        )}

        {/* ---------- Error ---------- */}
        {error && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-xs text-destructive">
              <AlertCircle className="size-3.5 shrink-0" />
              {error}
            </div>
            {!isPlaying && gameState && (
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={handlePlayAgain}
              >
                Try Again
              </Button>
            )}
            {!gameState && (
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={() => setBetAmount("")}
              >
                Reset
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
