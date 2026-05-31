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

type Risk = "low" | "medium" | "high";

interface PlinkoResult {
  game_id: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    path: number[];
    slot: number;
    multiplier: number;
  };
}

const ROW_OPTIONS = [8, 10, 12, 14, 16];

// Approximate multiplier tables per risk/row for display
const MULTIPLIER_TABLES: Record<Risk, Record<number, number[]>> = {
  low: {
    8: [5.6, 2.1, 1.1, 1.0, 0.5, 1.0, 1.1, 2.1, 5.6],
    10: [8.9, 3.0, 1.4, 1.1, 1.0, 0.5, 1.0, 1.1, 1.4, 3.0, 8.9],
    12: [10, 3.0, 1.6, 1.4, 1.1, 1.0, 0.5, 1.0, 1.1, 1.4, 1.6, 3.0, 10],
    14: [7.1, 4.0, 1.9, 1.4, 1.3, 1.1, 1.0, 0.5, 1.0, 1.1, 1.3, 1.4, 1.9, 4.0, 7.1],
    16: [16, 9.0, 2.0, 1.4, 1.4, 1.2, 1.1, 1.0, 0.5, 1.0, 1.1, 1.2, 1.4, 1.4, 2.0, 9.0, 16],
  },
  medium: {
    8: [13, 3.0, 1.3, 0.7, 0.4, 0.7, 1.3, 3.0, 13],
    10: [22, 5.0, 2.0, 1.4, 0.6, 0.4, 0.6, 1.4, 2.0, 5.0, 22],
    12: [33, 11, 4.0, 2.0, 1.1, 0.6, 0.3, 0.6, 1.1, 2.0, 4.0, 11, 33],
    14: [43, 13, 6.0, 3.0, 1.3, 1.0, 0.7, 0.3, 0.7, 1.0, 1.3, 3.0, 6.0, 13, 43],
    16: [110, 41, 10, 5.0, 3.0, 1.5, 1.0, 0.5, 0.3, 0.5, 1.0, 1.5, 3.0, 5.0, 10, 41, 110],
  },
  high: {
    8: [29, 4.0, 1.5, 0.3, 0.2, 0.3, 1.5, 4.0, 29],
    10: [76, 10, 3.0, 0.9, 0.3, 0.2, 0.3, 0.9, 3.0, 10, 76],
    12: [170, 24, 8.1, 2.0, 0.7, 0.2, 0.2, 0.2, 0.7, 2.0, 8.1, 24, 170],
    14: [420, 56, 18, 5.0, 1.9, 0.3, 0.2, 0.2, 0.2, 0.3, 1.9, 5.0, 18, 56, 420],
    16: [1000, 130, 26, 9.0, 4.0, 2.0, 0.2, 0.2, 0.2, 0.2, 0.2, 2.0, 4.0, 9.0, 26, 130, 1000],
  },
};

function multiplierBadgeColor(m: number): string {
  if (m >= 10) return "bg-red-500/20 text-red-400 hover:bg-red-500/20";
  if (m >= 3) return "bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500/20";
  if (m >= 1) return "bg-green-500/20 text-green-400 hover:bg-green-500/20";
  return "bg-zinc-500/20 text-zinc-400 hover:bg-zinc-500/20";
}

export default function PlinkoGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { mutate, loading, error } = useApiMutation<PlinkoResult>(
    "/games/plinko/play"
  );

  const [risk, setRisk] = useState<Risk>("medium");
  const [rows, setRows] = useState(16);
  const [betAmount, setBetAmount] = useState("");
  const [result, setResult] = useState<PlinkoResult | null>(null);
  const [pastResults, setPastResults] = useState<
    { slot: number; multiplier: number; profit: number }[]
  >([]);

  const handleDrop = async () => {
    const amount = parseFloat(betAmount);
    if (!amount || amount <= 0) return;

    const res = await mutate({
      bet_amount: amount,
      options: { risk, rows },
    });

    if (res) {
      setResult(res);
      setPastResults((prev) =>
        [
          {
            slot: res.result_data.slot,
            multiplier: res.result_data.multiplier,
            profit: res.profit,
          },
          ...prev,
        ].slice(0, 5)
      );
    }
  };

  const currentMultipliers = MULTIPLIER_TABLES[risk][rows] ?? [];

  if (!isAuthenticated) {
    return (
      <div className="flex h-48 items-center justify-center rounded-lg bg-muted/50">
        <p className="text-sm text-muted-foreground">Login to play</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Risk Selector */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Risk Level</label>
        <div className="flex gap-2">
          {(["low", "medium", "high"] as Risk[]).map((r) => (
            <Button
              key={r}
              variant={risk === r ? "default" : "outline"}
              size="sm"
              className="flex-1 capitalize"
              onClick={() => setRisk(r)}
              disabled={loading}
            >
              {r}
            </Button>
          ))}
        </div>
      </div>

      {/* Row Count */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Rows</label>
        <Select
          value={String(rows)}
          onValueChange={(val) => val && setRows(Number(val))}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ROW_OPTIONS.map((r) => (
              <SelectItem key={r} value={String(r)}>
                {r} rows
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
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

      {/* Drop Button */}
      <Button
        className="w-full"
        size="lg"
        onClick={handleDrop}
        disabled={loading || !betAmount || parseFloat(betAmount) <= 0}
      >
        {loading ? (
          <>
            <Loader2 className="mr-2 size-4 animate-spin" />
            Dropping...
          </>
        ) : (
          "Drop Ball"
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
              result.profit >= 0
                ? "bg-chart-green/10 text-chart-green"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            <div className="flex items-center gap-4">
              <div className="flex flex-col items-center">
                <span className="text-xs text-muted-foreground">Slot</span>
                <span className="text-3xl font-bold">
                  {result.result_data.slot}
                </span>
              </div>
              <div className="h-10 w-px bg-border" />
              <div className="flex flex-col items-center">
                <span className="text-xs text-muted-foreground">
                  Multiplier
                </span>
                <span className="text-3xl font-bold">
                  {fmt(result.result_data.multiplier)}x
                </span>
              </div>
            </div>
            <span className="text-lg font-semibold">
              {result.profit >= 0 ? "+" : ""}
              {fmt(result.profit)} coins
            </span>
          </div>
        </>
      )}

      {/* Multiplier Row */}
      <div className="space-y-2">
        <p className="text-xs font-medium text-muted-foreground">
          Multipliers ({risk} risk, {rows} rows)
        </p>
        <div className="flex flex-wrap justify-center gap-1">
          {currentMultipliers.map((m, i) => (
            <Badge
              key={i}
              className={`text-[10px] font-mono ${multiplierBadgeColor(m)}`}
            >
              {fmt(m, 1)}x
            </Badge>
          ))}
        </div>
      </div>

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
          <div className="space-y-2">
            {pastResults.map((r, i) => (
              <div
                key={i}
                className="flex items-center justify-between text-sm"
              >
                <div className="flex items-center gap-2">
                  <Badge
                    className={`text-[10px] font-mono ${multiplierBadgeColor(r.multiplier)}`}
                  >
                    {fmt(r.multiplier)}x
                  </Badge>
                  <span className="text-muted-foreground">
                    Slot {r.slot}
                  </span>
                </div>
                <span
                  className={
                    r.profit >= 0 ? "text-chart-green" : "text-destructive"
                  }
                >
                  {r.profit >= 0 ? "+" : ""}
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
