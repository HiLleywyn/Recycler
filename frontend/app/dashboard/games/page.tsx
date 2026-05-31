"use client";

import Link from "next/link";
import { useState } from "react";
import {
  Gamepad2,
  Spade,
  Coins,
  Rocket,
  Dice5,
  Bomb,
  Target,
  CircleDot,
  Ticket,
  Radiation,
  Gem,
  Wrench,
} from "lucide-react";

import { Surface } from "@/components/ui/surface";
import { cn } from "@/lib/utils";

type Category = "all" | "cards" | "arcade" | "chance" | "classic";

interface Game {
  slug: string;
  name: string;
  desc: string;
  category: Exclude<Category, "all">;
  icon: React.ReactNode;
  color: string;
  commandHint: string;
}

const GAMES: Game[] = [
  {
    slug: "blackjack",
    name: "Blackjack",
    desc: "Beat the dealer without busting.",
    category: "cards",
    icon: <Spade className="size-5" />,
    color: "from-emerald-500/40 via-emerald-500/10 to-transparent",
    commandHint: ",blackjack <bet>",
  },
  {
    slug: "coinflip",
    name: "Coinflip",
    desc: "Heads or tails. 50/50, no gimmicks.",
    category: "classic",
    icon: <Coins className="size-5" />,
    color: "from-amber-500/40 via-amber-500/10 to-transparent",
    commandHint: ",coinflip <bet>",
  },
  {
    slug: "crash",
    name: "Crash",
    desc: "Cash out before the rocket blows.",
    category: "arcade",
    icon: <Rocket className="size-5" />,
    color: "from-fuchsia-500/40 via-fuchsia-500/10 to-transparent",
    commandHint: ",crash <bet>",
  },
  {
    slug: "dice",
    name: "Dice",
    desc: "Roll over or under to pocket the pot.",
    category: "chance",
    icon: <Dice5 className="size-5" />,
    color: "from-sky-500/40 via-sky-500/10 to-transparent",
    commandHint: ",dice <bet>",
  },
  {
    slug: "mines",
    name: "Mines",
    desc: "Step lightly. Every tile could be your last.",
    category: "arcade",
    icon: <Bomb className="size-5" />,
    color: "from-rose-500/40 via-rose-500/10 to-transparent",
    commandHint: ",mines <bet>",
  },
  {
    slug: "plinko",
    name: "Plinko",
    desc: "Drop the chip, pray to the pegs.",
    category: "arcade",
    icon: <CircleDot className="size-5" />,
    color: "from-cyan-500/40 via-cyan-500/10 to-transparent",
    commandHint: ",plinko <bet>",
  },
  {
    slug: "roulette",
    name: "Roulette",
    desc: "Red, black, or one single number.",
    category: "classic",
    icon: <Target className="size-5" />,
    color: "from-red-500/40 via-red-500/10 to-transparent",
    commandHint: ",roulette <bet>",
  },
  {
    slug: "slots",
    name: "Slots",
    desc: "Three reels. One lucky line.",
    category: "arcade",
    icon: <Ticket className="size-5" />,
    color: "from-violet-500/40 via-violet-500/10 to-transparent",
    commandHint: ",slots <bet>",
  },
  {
    slug: "wheel",
    name: "Wheel",
    desc: "Spin to win a multiplier.",
    category: "chance",
    icon: <Radiation className="size-5" />,
    color: "from-teal-500/40 via-teal-500/10 to-transparent",
    commandHint: ",wheel <bet>",
  },
  {
    slug: "gamba",
    name: "Gamba",
    desc: "Classic all-in. All or nothing.",
    category: "chance",
    icon: <Gem className="size-5" />,
    color: "from-pink-500/40 via-pink-500/10 to-transparent",
    commandHint: ",gamba <bet>",
  },
];

const CATEGORIES: Array<{ id: Category; label: string }> = [
  { id: "all", label: "All" },
  { id: "cards", label: "Cards" },
  { id: "arcade", label: "Arcade" },
  { id: "chance", label: "Chance" },
  { id: "classic", label: "Classic" },
];

export default function GamesPage() {
  const [filter, setFilter] = useState<Category>("all");
  const filtered =
    filter === "all" ? GAMES : GAMES.filter((g) => g.category === filter);

  return (
    <div className="space-y-6">
      <Surface
        variant="glass"
        elevation={2}
        className="relative overflow-hidden p-6 md:p-8"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute -right-24 -bottom-24 size-64 rounded-full blur-3xl opacity-60 bg-gradient-to-br from-fuchsia-500/30 to-cyan-500/10"
        />
        <div className="relative z-10 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-eyebrow text-primary">
              <Gamepad2 className="size-4" /> Games
            </div>
            <h1 className="mt-2 text-hero">Test your luck and skill</h1>
            <p className="mt-2 max-w-xl text-sm text-muted-foreground">
              All games are played through the Discord bot with your wallet
              balance. Use the command shown on each card to start a round.
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-secondary/40 px-3 py-1.5 text-xs text-muted-foreground">
            <Wrench className="size-3.5" />
            Web play coming soon
          </div>
        </div>
      </Surface>

      <div className="flex flex-wrap gap-2">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => setFilter(c.id)}
            className={cn(
              "rounded-full px-3.5 py-1.5 text-xs font-medium transition-all",
              filter === c.id
                ? "bg-gradient-brand text-white shadow-[0_4px_14px_-4px_oklch(0.68_0.22_262/0.6)]"
                : "border border-border bg-secondary/40 text-muted-foreground hover:bg-secondary/70 hover:text-foreground",
            )}
            aria-pressed={filter === c.id}
          >
            {c.label}
          </button>
        ))}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {filtered.map((g) => (
          <GameCard key={g.slug} game={g} />
        ))}
      </div>
    </div>
  );
}

function GameCard({ game }: { game: Game }) {
  return (
    <Link href={`/dashboard/games/${game.slug}`} className="group block">
      <Surface
        variant="glass"
        interactive
        className="relative h-full overflow-hidden p-5"
      >
        <div
          aria-hidden
          className={cn(
            "pointer-events-none absolute -right-10 -top-10 size-40 rounded-full blur-2xl opacity-70 transition-opacity duration-300 group-hover:opacity-100 bg-gradient-to-br",
            game.color,
          )}
        />

        <div className="relative z-10">
          <div className="mb-4 flex aspect-[16/9] w-full items-center justify-center rounded-xl border border-border bg-[radial-gradient(circle_at_30%_30%,oklch(1_0_0/0.06),transparent_60%)] glass-subtle">
            <div className="flex size-14 items-center justify-center rounded-2xl bg-gradient-brand text-white shadow-[0_8px_22px_-6px_oklch(0.68_0.22_262/0.6)]">
              {game.icon}
            </div>
          </div>

          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="font-display text-lg font-semibold tracking-tight">
                {game.name}
              </h3>
              <p className="mt-1 text-sm text-muted-foreground">{game.desc}</p>
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between">
            <code className="rounded-md bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
              {game.commandHint}
            </code>
            <span className="text-[10px] font-semibold uppercase tracking-wider text-primary/80">
              {game.category}
            </span>
          </div>
        </div>
      </Surface>
    </Link>
  );
}
