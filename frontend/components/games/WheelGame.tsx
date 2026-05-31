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

interface WheelResult {
  game_id: string;
  bet_amount: number;
  payout: number;
  profit: number;
  multiplier: number;
  result_data: {
    segment: number;
    multiplier: number;
  };
}

const SEGMENT_OPTIONS = [10, 20, 30, 40, 50];

function multiplierBadgeColor(m: number): string {
  if (m >= 5) return "bg-red-500/20 text-red-400 hover:bg-red-500/20";
  if (m >= 2) return "bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500/20";
  if (m >= 1) return "bg-green-500/20 text-green-400 hover:bg-green-500/20";
  return "bg-zinc-500/20 text-zinc-400 hover:bg-zinc-500/20";
}

export default function WheelGame() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { mutate, loading, error } = useApiMutation<WheelResult>(
    "/games/wheel/play"
  );

  const [segments, setSegments] = useState(20);
  const [betAmount, setBetAmount] = useState("");
  const [result, setResult] = useState<WheelResult | null>(null);
  const [pastResults, setPastResults] = useState<
    { segment: number; multiplier: number; profit: number; payout: number }[]
  >([]);

  const handleSpin = async () => {
    const amount = parseFloat(betAmount);
    if (!amount || amount <= 0) return;

    const res = await mutate({
      bet_amount: amount,
      options: { segments },
    });

    if (res) {
      setResult(res);
      setPastResults((prev) =>
        [
          {
            segment: res.result_data.segment,
            multiplier: res.result_data.multiplier,
            profit: res.profit,
            payout: res.payout,
          },
          ...prev,
        ].slice(0, 10)
      );
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
      {/* Segment Count Selector */}
      <div className="space-y-2">
        <label className="text-sm font-medium">Segments</label>
        <Select
          value={String(segments)}
          onValueChange={(val) => val && setSegments(Number(val))}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SEGMENT_OPTIONS.map((s) => (
              <SelectItem key={s} value={String(s)}>
                {s} segments
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">
          More segments = higher potential multipliers
        </p>
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
          "Spin the Wheel"
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
                <span className="text-xs text-muted-foreground">Segment</span>
                <span className="text-3xl font-bold">
                  {result.result_data.segment}
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
            <span className="text-lg font-bold">
              {result.profit >= 0 ? "You won!" : "You lost!"}
            </span>
            <span className="text-lg font-semibold">
              {result.profit >= 0 ? "+" : ""}
              {fmt(result.profit)} coins
            </span>
            <span className="text-xs text-muted-foreground">
              Payout: {fmt(result.payout)}
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
                    Seg {r.segment}
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
