"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2 } from "lucide-react";
import { useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { fmt } from "@/lib/format";

type BetType = "color" | "number" | "dozen" | "column" | "half" | "parity";

interface RouletteResult {
  game_id: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    spin: number;
    color: string;
    won: boolean;
  };
}

const BET_TYPE_OPTIONS: { value: BetType; label: string }[] = [
  { value: "color", label: "Color" },
  { value: "number", label: "Number" },
  { value: "dozen", label: "Dozen" },
  { value: "column", label: "Column" },
  { value: "half", label: "Half" },
  { value: "parity", label: "Parity" },
];

const BET_VALUE_OPTIONS: Record<BetType, { value: string; label: string }[]> = {
  color: [
    { value: "red", label: "Red" },
    { value: "black", label: "Black" },
  ],
  number: Array.from({ length: 36 }, (_, i) => ({
    value: String(i + 1),
    label: String(i + 1),
  })),
  dozen: [
    { value: "1st", label: "1st (1-12)" },
    { value: "2nd", label: "2nd (13-24)" },
    { value: "3rd", label: "3rd (25-36)" },
  ],
  column: [
    { value: "1st", label: "1st Column" },
    { value: "2nd", label: "2nd Column" },
    { value: "3rd", label: "3rd Column" },
  ],
  half: [
    { value: "1-18", label: "1-18 (Low)" },
    { value: "19-36", label: "19-36 (High)" },
  ],
  parity: [
    { value: "odd", label: "Odd" },
    { value: "even", label: "Even" },
  ],
};

const ROULETTE_REDS = new Set([
  1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36,
]);

function colorBadgeClasses(color: string): string {
  switch (color) {
    case "red":
      return "bg-red-600 text-white hover:bg-red-600";
    case "black":
      return "bg-zinc-900 text-white hover:bg-zinc-900";
    case "green":
      return "bg-green-600 text-white hover:bg-green-600";
    default:
      return "";
  }
}

export default function RouletteGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { mutate, loading, error } = useApiMutation<RouletteResult>(
    "/games/roulette/play"
  );

  const [betType, setBetType] = useState<BetType>("color");
  const [betValue, setBetValue] = useState("red");
  const [betAmount, setBetAmount] = useState("");
  const [result, setResult] = useState<RouletteResult | null>(null);
  const [pastResults, setPastResults] = useState<
    { spin: number; color: string; won: boolean }[]
  >([]);

  const handleBetTypeChange = (val: string | null) => {
    if (!val) return;
    const newType = val as BetType;
    setBetType(newType);
    setBetValue(BET_VALUE_OPTIONS[newType][0].value);
  };

  const handleSpin = async () => {
    const amount = parseFloat(betAmount);
    if (!amount || amount <= 0) return;

    const res = await mutate({
      bet_amount: amount,
      options: { bet_type: betType, bet_value: betValue },
    });

    if (res) {
      setResult(res);
      setPastResults((prev) => [res.result_data, ...prev].slice(0, 10));
    }
  };

  if (!isAuthenticated) {
    return (
      <div className="flex h-48 items-center justify-center rounded-lg bg-muted/50">
        <p className="text-sm text-muted-foreground">Login to play</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Bet Type Selector */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Bet Type</label>
        <Select value={betType} onValueChange={handleBetTypeChange}>
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {BET_TYPE_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Bet Value Selector */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Bet Value</label>
        {betType === "number" ? (
          <Select value={betValue} onValueChange={(v) => v && setBetValue(v)}>
            <SelectTrigger className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {BET_VALUE_OPTIONS.number.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <div className="flex flex-wrap gap-2">
            {BET_VALUE_OPTIONS[betType].map((opt) => (
              <Button
                key={opt.value}
                variant={betValue === opt.value ? "default" : "outline"}
                size="sm"
                onClick={() => setBetValue(opt.value)}
                disabled={loading}
              >
                {opt.label}
              </Button>
            ))}
          </div>
        )}
      </div>

      {/* Bet Amount */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Bet Amount</label>
        <Input
          type="number"
          placeholder="0.00"
          min={0}
          step="0.01"
          value={betAmount}
          onChange={(e) => setBetAmount(e.target.value)}
          disabled={loading}
        />
      </div>

      {/* Spin Button */}
      <Button
        className="w-full"
        size="lg"
        onClick={handleSpin}
        disabled={loading || !betAmount || parseFloat(betAmount) <= 0}
      >
        {loading ? (
          <>
            <Loader2 className="mr-2 size-4 animate-spin" />
            Spinning...
          </>
        ) : (
          "Spin"
        )}
      </Button>

      {/* Error */}
      {error && (
        <p className="text-sm text-destructive">{error}</p>
      )}

      {/* Result Display */}
      {result && (
        <>
          <Separator />
          <div
            className={`flex flex-col items-center gap-2 rounded-lg p-4 ${
              result.result_data.won
                ? "bg-chart-green/10 text-chart-green"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            <div
              className={`flex size-16 items-center justify-center rounded-full text-2xl font-bold ${colorBadgeClasses(result.result_data.color)}`}
            >
              {result.result_data.spin}
            </div>
            <span className="text-sm capitalize text-muted-foreground">
              {result.result_data.color} {result.result_data.spin}
            </span>
            <span className="text-lg font-bold">
              {result.result_data.won ? "You won!" : "You lost!"}
            </span>
            <span className="text-lg font-semibold">
              {result.profit >= 0 ? "+" : ""}
              {fmt(result.profit)} coins
            </span>
            <span className="text-xs text-muted-foreground">
              Payout: {fmt(result.payout)} | {fmt(result.multiplier, 1)}x
              multiplier
            </span>
          </div>
        </>
      )}

      {/* Past Results */}
      <div className="rounded-lg border border-border p-3">
        <p className="mb-2 text-xs font-medium text-muted-foreground">
          Recent Results
        </p>
        {pastResults.length === 0 ? (
          <div className="flex h-16 items-center justify-center">
            <p className="text-xs text-muted-foreground">
              No games played yet
            </p>
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {pastResults.map((r, i) => (
              <Badge
                key={i}
                className={`text-xs font-mono ${colorBadgeClasses(r.color)}`}
              >
                {r.spin}
              </Badge>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
