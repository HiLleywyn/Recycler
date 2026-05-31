"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  ArrowRight,
  LogIn,
  Wallet,
  ArrowLeftRight,
  Droplets,
  Pickaxe,
  Gamepad2,
  Image as ImageIcon,
  Trophy,
  Rocket,
  Bot,
  Zap,
  Github,
  BookOpen,
  MessageCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Button, buttonVariants } from "@/components/ui/button";
import { Surface } from "@/components/ui/surface";
import { Ticker } from "@/components/ui/ticker";
import { BrandMark } from "@/components/ui/brand";
import { useAuthStore } from "@/stores/auth";
import { useQuery } from "@tanstack/react-query";

// ───────────────────────────────────────────────────────────────────
// Types
interface ServerStats {
  total_users?: number;
  total_tokens?: number;
  total_pools?: number;
  total_trades?: number;
  total_volume_usd?: number;
  total_market_cap?: number;
}

// ───────────────────────────────────────────────────────────────────
// Motion variants
const fadeUp = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { duration: 0.6, ease: "easeOut" as const } },
};

const stagger = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06 } },
};

// ───────────────────────────────────────────────────────────────────
// Landing page

export default function LandingPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const handleLogin = async () => {
    const fallback = () => {
      window.location.href = "/api/auth/discord";
    };
    try {
      const res = await fetch("/api/v2/auth/discord", {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) return fallback();
      const data = (await res.json().catch(() => null)) as { url?: string } | null;
      if (data?.url) {
        window.location.href = data.url;
      } else {
        fallback();
      }
    } catch {
      fallback();
    }
  };

  return (
    <main className="relative min-h-screen overflow-hidden">
      <Aurora />
      <LandingNav isAuthenticated={isAuthenticated} onLogin={handleLogin} />
      <Hero isAuthenticated={isAuthenticated} onLogin={handleLogin} />
      <LiveTicker />
      <StatsStrip />
      <Features />
      <HowItWorks />
      <CTA isAuthenticated={isAuthenticated} onLogin={handleLogin} />
      <Footer />
    </main>
  );
}

// ───────────────────────────────────────────────────────────────────
// Background - aurora blobs + grid + noise

function Aurora() {
  const reduced = useReducedMotion();
  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      <div className="absolute inset-0 bg-grid opacity-40 [mask-image:radial-gradient(ellipse_at_center,black_40%,transparent_75%)]" />
      <motion.div
        className="absolute -left-[20vw] -top-[30vh] size-[70vw] rounded-full blur-[120px]"
        style={{ background: "radial-gradient(circle, oklch(0.68 0.22 262 / 0.35), transparent 60%)" }}
        animate={reduced ? undefined : { x: [0, 40, 0], y: [0, 20, 0] }}
        transition={{ duration: 18, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="absolute -right-[15vw] top-[20vh] size-[55vw] rounded-full blur-[120px]"
        style={{ background: "radial-gradient(circle, oklch(0.82 0.16 205 / 0.30), transparent 60%)" }}
        animate={reduced ? undefined : { x: [0, -30, 0], y: [0, -20, 0] }}
        transition={{ duration: 22, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="absolute left-[30vw] bottom-[-20vh] size-[60vw] rounded-full blur-[120px]"
        style={{ background: "radial-gradient(circle, oklch(0.70 0.22 320 / 0.25), transparent 60%)" }}
        animate={reduced ? undefined : { x: [0, 20, 0], y: [0, -30, 0] }}
        transition={{ duration: 26, repeat: Infinity, ease: "easeInOut" }}
      />
      <div className="absolute inset-0 bg-noise opacity-[0.5] mix-blend-overlay" />
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────
// Floating top nav

function LandingNav({
  isAuthenticated,
  onLogin,
}: {
  isAuthenticated: boolean;
  onLogin: () => void;
}) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 8);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);

  return (
    <header
      className={cn(
        "sticky top-4 z-40 mx-auto flex w-[min(92%,1200px)] items-center justify-between rounded-full px-4 py-2 transition-all",
        scrolled ? "glass-strong" : "glass",
      )}
    >
      <Link href="/" className="flex items-center gap-2">
        <BrandMark size={30} glow />
        <span className="font-display text-lg font-semibold tracking-tight text-gradient-brand">
          Discoin
        </span>
      </Link>

      <nav className="hidden items-center gap-6 text-sm text-muted-foreground md:flex">
        <a href="#features" className="transition-colors hover:text-foreground">Features</a>
        <a href="#stats" className="transition-colors hover:text-foreground">Live stats</a>
        <a href="#how" className="transition-colors hover:text-foreground">How it works</a>
        <Link href="/dashboard" className="transition-colors hover:text-foreground">Dashboard</Link>
      </nav>

      <div className="flex items-center gap-2">
        {isAuthenticated ? (
          <Link
            href="/dashboard"
            className={cn(buttonVariants({ variant: "glow", size: "sm" }), "gap-1.5 rounded-full px-4")}
          >
            Open dashboard
            <ArrowRight className="size-3.5" />
          </Link>
        ) : (
          <Button
            variant="glow"
            size="sm"
            className="gap-1.5 rounded-full px-4"
            onClick={onLogin}
          >
            <LogIn className="size-3.5" />
            Login
          </Button>
        )}
      </div>
    </header>
  );
}

// ───────────────────────────────────────────────────────────────────
// Hero

function Hero({
  isAuthenticated,
  onLogin,
}: {
  isAuthenticated: boolean;
  onLogin: () => void;
}) {
  return (
    <section className="relative mx-auto flex w-[min(92%,1200px)] flex-col items-center pt-20 pb-16 text-center sm:pt-28">
      <motion.div
        initial="hidden"
        animate="show"
        variants={stagger}
        className="flex flex-col items-center"
      >
        <motion.div variants={fadeUp}>
          <Surface
            variant="subtle"
            className="mb-6 flex items-center gap-2 rounded-full px-3 py-1 text-xs"
          >
            <span className="flex size-1.5 rounded-full bg-[var(--success)] shadow-[0_0_10px_var(--success)]" />
            <span className="text-muted-foreground">Live on Discord</span>
            <span className="h-3 w-px bg-border" />
            <span className="font-mono text-foreground">v2.0</span>
          </Surface>
        </motion.div>

        <motion.h1
          variants={fadeUp}
          className="text-display max-w-4xl"
        >
          <span className="block">Discord economy,</span>
          <span className="block text-gradient-brand">reimagined.</span>
        </motion.h1>

        <motion.p
          variants={fadeUp}
          className="mt-6 max-w-2xl text-balance text-base text-muted-foreground sm:text-lg"
        >
          Trade tokens, provide liquidity, stake rewards, mine blocks, predict
          markets, and play games - a full DeFi experience built for the
          servers you already live in.
        </motion.p>

        <motion.div variants={fadeUp} className="mt-8 flex flex-col gap-3 sm:flex-row">
          {isAuthenticated ? (
            <Link
              href="/dashboard"
              className={cn(
                buttonVariants({ variant: "glow", size: "lg" }),
                "gap-2 rounded-full px-6",
              )}
            >
              Launch dashboard
              <ArrowRight className="size-4" />
            </Link>
          ) : (
            <Button
              variant="glow"
              size="lg"
              onClick={onLogin}
              className="gap-2 rounded-full px-6"
            >
              <LogIn className="size-4" />
              Login with Discord
            </Button>
          )}
          <Link
            href="/dashboard"
            className={cn(
              buttonVariants({ variant: "outline", size: "lg" }),
              "gap-2 rounded-full px-6 backdrop-blur-md",
            )}
          >
            Browse as guest
            <ArrowRight className="size-4" />
          </Link>
        </motion.div>

        <motion.div
          variants={fadeUp}
          className="mt-10 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs text-muted-foreground"
        >
          <span className="flex items-center gap-1.5">
            <Bot className="size-3.5 text-primary" /> One-click install
          </span>
          <span className="flex items-center gap-1.5">
            <Zap className="size-3.5 text-primary" /> Real-time WebSocket
          </span>
          <span className="flex items-center gap-1.5">
            <Rocket className="size-3.5 text-primary" /> Free to play
          </span>
        </motion.div>
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// Live ticker

function LiveTicker() {
  return (
    <section className="mx-auto w-[min(96%,1400px)] pb-12">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-80px" }}
        transition={{ duration: 0.5 }}
      >
        <Ticker />
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// Live stats strip

function StatsStrip() {
  // React Query caches across navigations (60s) so bouncing between the
  // landing page and the dashboard doesn't spam the stats endpoint.
  const { data } = useQuery<ServerStats>({
    queryKey: ["landing-stats"],
    queryFn: async () => {
      const res = await fetch("/api/v2/stats/stats", {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    },
    staleTime: 60 * 1000,
    retry: 1,
  });

  const stats = [
    { label: "Users", value: data?.total_users, format: "num" as const },
    { label: "Tokens", value: data?.total_tokens, format: "num" as const },
    { label: "Liquidity pools", value: data?.total_pools, format: "num" as const },
    { label: "Total trades", value: data?.total_trades, format: "num" as const },
  ];

  return (
    <section id="stats" className="mx-auto w-[min(92%,1200px)] pb-24">
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-100px" }}
        transition={{ duration: 0.7 }}
      >
        <Surface
          variant="glass"
          elevation={2}
          className="grid grid-cols-2 gap-6 p-8 sm:grid-cols-4"
        >
          {stats.map((s) => (
            <div key={s.label} className="text-center sm:text-left">
              <div className="text-eyebrow text-muted-foreground">{s.label}</div>
              <div className="mt-2 font-display text-3xl font-semibold tabular-nums tracking-tight sm:text-4xl">
                {s.value == null ? (
                  <span className="inline-block h-9 w-24 animate-pulse rounded-md bg-muted" />
                ) : (
                  s.value.toLocaleString()
                )}
              </div>
            </div>
          ))}
        </Surface>
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// Feature grid

const FEATURES: Array<{
  title: string;
  icon: React.ReactNode;
  desc: string;
  accent: string;
}> = [
  {
    title: "CeFi banking",
    icon: <Wallet className="size-5" />,
    desc: "Wallet, bank, savings, and lending - the familiar rails, inside Discord.",
    accent: "from-indigo-500/30 to-indigo-500/5",
  },
  {
    title: "Token swaps",
    icon: <ArrowLeftRight className="size-5" />,
    desc: "Instant conversion between supported tokens with live on-chain pricing.",
    accent: "from-cyan-500/30 to-cyan-500/5",
  },
  {
    title: "Liquidity pools",
    icon: <Droplets className="size-5" />,
    desc: "Add liquidity, earn fees, track yield - AMM-style pools, zero friction.",
    accent: "from-teal-500/30 to-teal-500/5",
  },
  {
    title: "Staking & mining",
    icon: <Pickaxe className="size-5" />,
    desc: "Validators, stonestakes, hash rigs. Passive yield through every cycle.",
    accent: "from-violet-500/30 to-violet-500/5",
  },
  {
    title: "Games",
    icon: <Gamepad2 className="size-5" />,
    desc: "Blackjack, crash, plinko, slots, wheel - 10+ games, one wallet.",
    accent: "from-pink-500/30 to-pink-500/5",
  },
  {
    title: "NFTs & shop",
    icon: <ImageIcon className="size-5" />,
    desc: "Mint, trade, list. Consumables and stones to level up your stack.",
    accent: "from-amber-500/30 to-amber-500/5",
  },
];

function Features() {
  return (
    <section id="features" className="mx-auto w-[min(92%,1200px)] pb-24">
      <SectionHeading
        eyebrow="Features"
        title="Everything a modern crypto stack needs"
        subtitle="All the primitives you'd expect from a DeFi platform, unified under one Discord bot and one dashboard."
      />

      <motion.div
        initial="hidden"
        whileInView="show"
        viewport={{ once: true, margin: "-80px" }}
        variants={stagger}
        className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
      >
        {FEATURES.map((f) => (
          <motion.div key={f.title} variants={fadeUp}>
            <Surface variant="glass" interactive className="group h-full p-6">
              <div
                aria-hidden
                className={cn(
                  "pointer-events-none absolute -right-16 -top-16 size-48 rounded-full blur-3xl opacity-60 transition-opacity duration-500 group-hover:opacity-100 bg-gradient-to-br",
                  f.accent,
                )}
              />
              <div className="relative z-10">
                <div className="mb-4 flex size-11 items-center justify-center rounded-xl bg-gradient-brand text-white shadow-[0_6px_20px_-6px_oklch(0.68_0.22_262/0.5)]">
                  {f.icon}
                </div>
                <h3 className="font-display text-lg font-semibold tracking-tight">
                  {f.title}
                </h3>
                <p className="mt-2 text-sm text-muted-foreground">
                  {f.desc}
                </p>
              </div>
            </Surface>
          </motion.div>
        ))}
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// How it works

function HowItWorks() {
  const steps = [
    {
      n: "01",
      title: "Invite the bot",
      desc: "Add Discoin to your Discord server with one click. No keys, no setup.",
      icon: <Bot className="size-5" />,
    },
    {
      n: "02",
      title: "Connect your account",
      desc: "Log in with Discord OAuth to unlock the web dashboard and live data.",
      icon: <LogIn className="size-5" />,
    },
    {
      n: "03",
      title: "Start earning",
      desc: "Trade, stake, mine, lend, predict, and play across any supported network.",
      icon: <Trophy className="size-5" />,
    },
  ];

  return (
    <section id="how" className="mx-auto w-[min(92%,1200px)] pb-24">
      <SectionHeading
        eyebrow="How it works"
        title="From invite to first trade in under a minute"
        subtitle="Built for communities that don't want to read a manual."
      />

      <motion.div
        initial="hidden"
        whileInView="show"
        viewport={{ once: true, margin: "-80px" }}
        variants={stagger}
        className="relative mt-12 grid gap-4 md:grid-cols-3"
      >
        {/* connecting line */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-6 top-8 hidden h-px md:block"
          style={{ background: "linear-gradient(90deg, transparent, var(--primary), transparent)" }}
        />
        {steps.map((s) => (
          <motion.div key={s.n} variants={fadeUp}>
            <Surface variant="glass" className="relative h-full p-6">
              <div className="mb-4 flex items-center justify-between">
                <span className="font-mono text-xs text-muted-foreground">{s.n}</span>
                <div className="flex size-9 items-center justify-center rounded-xl bg-gradient-brand text-white">
                  {s.icon}
                </div>
              </div>
              <h3 className="font-display text-lg font-semibold tracking-tight">{s.title}</h3>
              <p className="mt-2 text-sm text-muted-foreground">{s.desc}</p>
            </Surface>
          </motion.div>
        ))}
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// Final CTA

function CTA({
  isAuthenticated,
  onLogin,
}: {
  isAuthenticated: boolean;
  onLogin: () => void;
}) {
  return (
    <section className="mx-auto w-[min(92%,1200px)] pb-24">
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-100px" }}
        transition={{ duration: 0.7 }}
      >
        <Surface
          variant="glass"
          elevation={3}
          className="relative overflow-hidden p-10 text-center sm:p-16"
        >
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 opacity-40 bg-gradient-aurora"
          />
          <div className="relative z-10 flex flex-col items-center">
            <h2 className="text-hero max-w-3xl text-balance">
              Ship your first trade{" "}
              <span className="text-gradient-brand">tonight.</span>
            </h2>
            <p className="mt-4 max-w-lg text-balance text-muted-foreground">
              It takes about 30 seconds to add the bot and another minute to
              make your first swap. No downloads.
            </p>
            <div className="mt-8 flex flex-col gap-3 sm:flex-row">
              {isAuthenticated ? (
                <Link
                  href="/dashboard"
                  className={cn(
                    buttonVariants({ variant: "glow", size: "lg" }),
                    "gap-2 rounded-full px-6",
                  )}
                >
                  Launch dashboard
                  <ArrowRight className="size-4" />
                </Link>
              ) : (
                <Button
                  variant="glow"
                  size="lg"
                  onClick={onLogin}
                  className="gap-2 rounded-full px-6"
                >
                  <LogIn className="size-4" />
                  Login with Discord
                </Button>
              )}
            </div>
          </div>
        </Surface>
      </motion.div>
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────
// Footer

function Footer() {
  return (
    <footer className="border-t border-border/50">
      <div className="mx-auto flex w-[min(92%,1200px)] flex-col gap-8 py-10 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2">
          <BrandMark size={28} />
          <span className="font-display text-base font-semibold tracking-tight">
            Discoin
          </span>
          <span className="ml-2 text-xs text-muted-foreground">
            &copy; {new Date().getFullYear()}
          </span>
        </div>
        <nav className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-muted-foreground">
          <a href="#features" className="transition-colors hover:text-foreground">Features</a>
          <a href="#how" className="transition-colors hover:text-foreground">How it works</a>
          <Link href="/dashboard" className="transition-colors hover:text-foreground">Dashboard</Link>
          <Link href="/dashboard/commands" className="inline-flex items-center gap-1.5 transition-colors hover:text-foreground">
            <BookOpen className="size-3.5" /> Commands
          </Link>
          <a
            href="https://github.com/HiLleywyn/Discoin"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 transition-colors hover:text-foreground"
          >
            <Github className="size-3.5" /> GitHub
          </a>
          <a
            href="https://discord.com"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 transition-colors hover:text-foreground"
          >
            <MessageCircle className="size-3.5" /> Discord
          </a>
        </nav>
      </div>
    </footer>
  );
}

// ───────────────────────────────────────────────────────────────────
// Section heading helper

function SectionHeading({
  eyebrow,
  title,
  subtitle,
}: {
  eyebrow: string;
  title: string;
  subtitle?: string;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-100px" }}
      transition={{ duration: 0.6 }}
      className="mx-auto max-w-2xl text-center"
    >
      <div className="text-eyebrow text-primary">{eyebrow}</div>
      <h2 className="mt-3 text-hero text-balance">{title}</h2>
      {subtitle ? (
        <p className="mt-3 text-balance text-sm text-muted-foreground sm:text-base">
          {subtitle}
        </p>
      ) : null}
    </motion.div>
  );
}
