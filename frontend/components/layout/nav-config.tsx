import {
  LayoutDashboard,
  Wallet,
  ArrowLeftRight,
  TrendingUp,
  Droplets,
  Landmark,
  Pickaxe,
  Search,
  PiggyBank,
  HandCoins,
  FileCode,
  ShoppingBag,
  Trophy,
  Settings,
  Shield,
  ShieldAlert,
  Terminal,
  Users,
  Lock,
  BarChart3,
  Image as ImageIcon,
  Gamepad2,
} from "lucide-react";

export interface NavItem {
  label: string;
  href: string;
  icon: React.ReactNode;
  badge?: string;
  /** Module key(s) - item hidden when ALL listed modules are disabled. */
  modules?: string[];
  /** Hint used by the command palette search. */
  keywords?: string[];
}

export interface NavSection {
  title: string;
  items: NavItem[];
}

export const overviewItem: NavItem = {
  label: "Overview",
  href: "/dashboard",
  icon: <LayoutDashboard className="size-4" />,
  keywords: ["home", "dashboard", "summary"],
};

export const navSections: NavSection[] = [
  {
    title: "CeFi",
    items: [
      { label: "Bank", href: "/dashboard/bank", icon: <Landmark className="size-4" />, badge: "Trade", keywords: ["wallet", "balance"] },
      { label: "Savings", href: "/dashboard/savings", icon: <PiggyBank className="size-4" />, modules: ["savings"], keywords: ["interest", "yield"] },
      { label: "Lending", href: "/dashboard/lending", icon: <HandCoins className="size-4" />, modules: ["lending"], keywords: ["loan", "borrow"] },
    ],
  },
  {
    title: "DeFi",
    items: [
      { label: "Swap", href: "/dashboard/swap", icon: <ArrowLeftRight className="size-4" />, modules: ["crypto"], keywords: ["trade", "exchange"] },
      { label: "Pools", href: "/dashboard/pools", icon: <Droplets className="size-4" />, modules: ["pools"], keywords: ["liquidity", "lp"] },
      { label: "Staking", href: "/dashboard/staking", icon: <Lock className="size-4" />, modules: ["staking", "validators"], keywords: ["stake", "validator"] },
      { label: "Mining", href: "/dashboard/mining", icon: <Pickaxe className="size-4" />, modules: ["chain"], keywords: ["hash", "rig"] },
    ],
  },
  {
    title: "Explore",
    items: [
      { label: "Portfolio", href: "/dashboard/portfolio", icon: <Wallet className="size-4" />, keywords: ["holdings", "positions"] },
      { label: "Prices", href: "/dashboard/prices", icon: <TrendingUp className="size-4" />, keywords: ["market", "tokens"] },
      { label: "Charts", href: "/dashboard/charts", icon: <BarChart3 className="size-4" />, keywords: ["candlesticks", "trading"] },
      { label: "Explorer", href: "/dashboard/explorer", icon: <Search className="size-4" />, keywords: ["transactions", "blockchain"] },
      { label: "NFTs", href: "/dashboard/nfts", icon: <ImageIcon className="size-4" />, modules: ["nft"], keywords: ["collectibles"] },
      { label: "Predictions", href: "/dashboard/predictions", icon: <BarChart3 className="size-4" />, modules: ["predictions"], keywords: ["bets", "markets"] },
      { label: "Contracts", href: "/dashboard/contracts", icon: <FileCode className="size-4" />, modules: ["validators"], keywords: ["smart", "code"] },
    ],
  },
  {
    title: "Play",
    items: [
      { label: "Games", href: "/dashboard/games", icon: <Gamepad2 className="size-4" />, keywords: ["blackjack", "crash", "slots", "plinko"] },
    ],
  },
  {
    title: "Social",
    items: [
      { label: "Groups", href: "/dashboard/groups", icon: <Users className="size-4" />, modules: ["groups"], keywords: ["clan", "team"] },
      { label: "Shop", href: "/dashboard/shop", icon: <ShoppingBag className="size-4" />, modules: ["shop"], keywords: ["items", "buy"] },
      { label: "Leaderboard", href: "/dashboard/leaderboard", icon: <Trophy className="size-4" />, keywords: ["rank", "top"] },
    ],
  },
  {
    title: "System",
    items: [
      { label: "Commands", href: "/dashboard/commands", icon: <Terminal className="size-4" />, keywords: ["cli", "help"] },
      { label: "Settings", href: "/dashboard/settings", icon: <Settings className="size-4" />, keywords: ["preferences"] },
      { label: "Admin", href: "/dashboard/admin", icon: <Shield className="size-4" />, keywords: ["moderator"] },
      { label: "Security Logs", href: "/dashboard/security-logs", icon: <ShieldAlert className="size-4" />, keywords: ["audit"] },
    ],
  },
];
