// ===== User & Auth =====
export interface User {
  id: string;
  username: string;
  discriminator?: string;
  avatar: string | null;
  guildId: string | null;
  isAdmin: boolean;
  isOwner?: boolean;
  balance?: number;
  level?: number;
  xp?: number;
  badges?: Badge[];
  createdAt?: string;
}

export interface AuthState {
  token: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
}

// ===== Tokens & Prices =====
export interface Token {
  id: string;
  symbol: string;
  name: string;
  icon?: string;
  decimals: number;
  totalSupply: number;
  circulatingSupply: number;
  price: number;
  marketCap: number;
  volume24h: number;
  change24h: number;
  change7d?: number;
  contractAddress?: string;
}

export interface PriceData {
  symbol: string;
  price: number;
  change24h: number;
  volume24h: number;
  high24h: number;
  low24h: number;
  lastUpdate: number;
  sparkline?: number[];
}

export interface PriceCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// ===== Portfolio =====
export interface PortfolioHolding {
  tokenId: string;
  symbol: string;
  name: string;
  amount: number;
  value: number;
  averageBuyPrice: number;
  currentPrice: number;
  pnl: number;
  pnlPercent: number;
  allocation: number;
}

export interface Portfolio {
  totalValue: number;
  totalPnl: number;
  totalPnlPercent: number;
  holdings: PortfolioHolding[];
}

// ===== Pools & Liquidity =====
export interface Pool {
  id: string;
  tokenA: string;
  tokenB: string;
  symbolA: string;
  symbolB: string;
  reserveA: number;
  reserveB: number;
  totalLiquidity: number;
  volume24h: number;
  fees24h: number;
  apr: number;
  userLiquidity?: number;
  userSharePercent?: number;
}

// ===== Staking =====
export interface Validator {
  id: string;
  name: string;
  address: string;
  totalStaked: number;
  commission: number;
  uptime: number;
  delegators: number;
  apr: number;
  status: "active" | "inactive" | "jailed";
}

export interface Stake {
  id: string;
  validatorId: string;
  validatorName: string;
  amount: number;
  rewards: number;
  stakedAt: string;
  lockEnd?: string;
  status: "active" | "unbonding" | "completed";
}

// ===== Transactions =====
export interface Transaction {
  id: string;
  hash: string;
  type:
    | "transfer"
    | "swap"
    | "stake"
    | "unstake"
    | "mint"
    | "burn"
    | "reward"
    | "game"
    | "shop";
  from: string;
  to: string;
  amount: number;
  symbol: string;
  fee?: number;
  status: "pending" | "confirmed" | "failed";
  timestamp: string;
  blockNumber?: number;
  metadata?: Record<string, unknown>;
}

// ===== Games =====
export interface GameResult {
  id: string;
  game: string;
  userId: string;
  bet: number;
  payout: number;
  multiplier: number;
  result: "win" | "loss" | "draw";
  details: Record<string, unknown>;
  timestamp: string;
}

export interface GameConfig {
  id: string;
  name: string;
  slug: string;
  description: string;
  icon: string;
  minBet: number;
  maxBet: number;
  houseEdge: number;
  enabled: boolean;
}

// ===== Badges & Achievements =====
export interface Badge {
  id: string;
  name: string;
  description: string;
  icon: string;
  rarity: "common" | "uncommon" | "rare" | "epic" | "legendary";
  earnedAt?: string;
}

// ===== Notifications =====
export interface Notification {
  id: string;
  type: "info" | "success" | "warning" | "error" | "trade" | "game" | "system";
  title: string;
  message: string;
  read: boolean;
  timestamp: string;
  link?: string;
  metadata?: Record<string, unknown>;
}

// ===== Leaderboard =====
export interface LeaderboardEntry {
  rank: number;
  userId: string;
  username: string;
  avatar: string | null;
  score: number;
  change?: number;
}

// ===== Savings & Lending =====
export interface SavingsAccount {
  id: string;
  tokenId: string;
  symbol: string;
  deposited: number;
  earned: number;
  apy: number;
  lockPeriod?: number;
  maturityDate?: string;
}

export interface LendingPosition {
  id: string;
  type: "lend" | "borrow";
  tokenId: string;
  symbol: string;
  amount: number;
  interestRate: number;
  collateral?: number;
  collateralSymbol?: string;
  healthFactor?: number;
  status: "active" | "liquidated" | "closed";
}

// ===== Smart Contracts =====
export interface SmartContract {
  id: string;
  name: string;
  address: string;
  creator: string;
  description: string;
  abi?: unknown[];
  verified: boolean;
  createdAt: string;
}

// ===== Mining =====
export interface MiningRig {
  id: string;
  name: string;
  hashRate: number;
  powerConsumption: number;
  efficiency: number;
  cost: number;
  dailyReward: number;
}

export interface MiningSession {
  id: string;
  rigId: string;
  rigName: string;
  startTime: string;
  endTime?: string;
  hashesCompleted: number;
  tokensEarned: number;
  status: "mining" | "idle" | "maintenance";
}

// ===== Shop =====
export interface ShopItem {
  id: string;
  name: string;
  description: string;
  icon: string;
  category: string;
  price: number;
  currency: string;
  stock?: number;
  owned?: boolean;
}

// ===== API Response Wrappers =====
export interface ApiResponse<T> {
  data: T;
  success: boolean;
  message?: string;
}

export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}

// ===== WebSocket =====
export interface WSMessage {
  type: string;
  channel: string;
  data: unknown;
  timestamp: number;
}

// ===== Guild / Server =====
export interface Guild {
  id: string;
  name: string;
  icon: string | null;
  memberCount: number;
  features: string[];
}
