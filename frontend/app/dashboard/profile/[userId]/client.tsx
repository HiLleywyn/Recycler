"use client";

import { useParams, useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SortableTable, type ColumnDef } from "@/components/ui/sortable-table";
import { UserLink } from "@/components/ui/user-link";
import { useAuthStore } from "@/stores/auth";
import { User, Trophy, Activity, Gamepad2, Star, TrendingUp, AlertCircle, Pickaxe, DollarSign, LogIn } from "lucide-react";
import { useApi } from "@/hooks/useApi";

// --- API response types ---

interface UserProfile {
  user_id: string;
  username?: string;
  avatar?: string;
  total_trades: number;
  total_trade_volume: number;
  realized_pnl: number;
  best_trade_pnl: number;
  worst_trade_pnl: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  total_games: number;
  total_wagered: number;
  total_game_profit: number;
  badges: UserBadge[];
  member_since?: string;
}

interface UserBadge {
  badge_id: string;
  name: string;
  description?: string;
  icon?: string;
  category?: string;
  earned_at?: string;
}

interface HoldingsResponse {
  user_id: string;
  holdings: HoldingItem[];
}

interface HoldingItem {
  symbol: string;
  amount: number;
  value_usd: number;
  price: number;
}

interface GameStats {
  game_type: string;
  games_played: number;
  total_wagered: number;
  total_profit: number;
  best_win: number;
  avg_bet: number;
}

interface MiningRig {
  rig_id: string;
  quantity: number;
  total_hashrate: number;
}

interface MiningConfig {
  total_hashrate: number;
  total_rigs: number;
  assignments: { rig_id: string; chain_symbol: string; quantity: number }[];
  group_id: string | null;
}

interface PnLEntry {
  period: string;
  realized: number;
  unrealized: number;
  total: number;
}

// --- Helpers ---

function fmtUsd(n: number): string {
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDate(ts?: string | null): string {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// --- Component ---

export default function ProfilePageClient() {
  const params = useParams();
  const router = useRouter();
  const userId = params.userId as string;
  // "_" is the static-export placeholder — skip API calls until the real ID is available
  const validUserId = userId && userId !== "_" ? userId : null;
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: profile, loading: profileLoading, error: profileError } = useApi<UserProfile>(
    validUserId ? `/users/${validUserId}/profile` : null
  );
  const { data: holdingsData, loading: holdingsLoading, error: holdingsError } = useApi<HoldingsResponse>(
    validUserId ? `/users/${validUserId}/holdings` : null
  );
  const { data: gameStats, loading: gameStatsLoading, error: gameStatsError } = useApi<GameStats[]>(
    validUserId ? `/users/${validUserId}/game-stats` : null
  );
  const { data: myRigs, loading: rigsLoading, error: rigsError } = useApi<MiningRig[]>(
    "/mining/my-rigs"
  );
  const { data: myConfig, loading: configLoading } = useApi<MiningConfig>(
    "/mining/my-config"
  );
  const { data: pnlData, loading: pnlLoading, error: pnlError } = useApi<PnLEntry[]>(
    validUserId ? `/users/${validUserId}/pnl` : null
  );

  const displayName = profile?.username || (validUserId ? `User ${validUserId.slice(0, 8)}` : "Unknown");
  const netWorth = holdingsData?.holdings.reduce((sum, h) => sum + h.value_usd, 0) ?? 0;
  const totalGames = profile?.total_games ?? 0;
  const totalTrades = profile?.total_trades ?? 0;

  // Show auth gate when the user is not authenticated — profile data requires sign-in
  if (!isAuthenticated && !profileLoading) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
          <LogIn className="size-10 text-muted-foreground" />
          <div>
            <p className="font-semibold">Sign in to view profiles</p>
            <p className="mt-1 text-sm text-muted-foreground">
              You need to be signed in to view player profiles and balances.
            </p>
          </div>
          <Button onClick={() => router.push("/dashboard")}>Go to Dashboard</Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {/* Profile header */}
      <Card>
        <CardContent className="pt-6">
          {profileLoading ? (
            <div className="flex items-center gap-4">
              <Skeleton className="size-16 rounded-full" />
              <div className="space-y-2">
                <Skeleton className="h-6 w-40" />
                <Skeleton className="h-4 w-32" />
                <div className="flex gap-1">
                  <Skeleton className="h-5 w-20 rounded-full" />
                </div>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-4">
              <Avatar className="size-16">
                <AvatarImage src={profile?.avatar ?? undefined} />
                <AvatarFallback className="text-lg">
                  <User className="size-6" />
                </AvatarFallback>
              </Avatar>
              <div>
                <h1 className="text-2xl font-bold tracking-tight">{displayName}</h1>
                <p className="text-sm text-muted-foreground">
                  Member since {fmtDate(profile?.member_since)}
                </p>
                {profile && (
                  <div className="mt-1 flex gap-1 flex-wrap">
                    <Badge variant="secondary">{totalTrades} trades</Badge>
                    {profile.badges.length > 0 && (
                      <Badge variant="secondary">{profile.badges.length} badges</Badge>
                    )}
                    {profile.win_rate > 0 && (
                      <Badge variant="secondary">{profile.win_rate.toFixed(1)}% win rate</Badge>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Stats */}
      <div className="grid gap-4 sm:grid-cols-3">
        {profileLoading || holdingsLoading ? (
          <>
            {Array.from({ length: 3 }).map((_, i) => (
              <Card key={i}>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="size-4" />
                </CardHeader>
                <CardContent>
                  <Skeleton className="h-7 w-28" />
                </CardContent>
              </Card>
            ))}
          </>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Holdings Value
                </CardTitle>
                <Trophy className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmtUsd(netWorth)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Realized PnL
                </CardTitle>
                <TrendingUp className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className={`text-2xl font-bold ${(profile?.realized_pnl ?? 0) >= 0 ? "text-chart-green" : "text-chart-red"}`}>
                  {fmtUsd(profile?.realized_pnl ?? 0)}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Games Played
                </CardTitle>
                <Gamepad2 className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{totalGames.toLocaleString()}</div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Tabs */}
      <Tabs defaultValue="holdings">
        <TabsList>
          <TabsTrigger value="holdings">Holdings</TabsTrigger>
          <TabsTrigger value="badges">Badges</TabsTrigger>
          <TabsTrigger value="games">Game Stats</TabsTrigger>
          <TabsTrigger value="mining">Mining</TabsTrigger>
          <TabsTrigger value="pnl">PnL</TabsTrigger>
        </TabsList>

        {/* Holdings Tab */}
        <TabsContent value="holdings">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Activity className="size-4" />
                Token Holdings
              </CardTitle>
            </CardHeader>
            <CardContent>
              {holdingsLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : holdingsError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {holdingsError}
                </div>
              ) : holdingsData && holdingsData.holdings.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "symbol", label: "Symbol", sortable: true, render: (row) => <span className="font-medium">{row.symbol as string}</span> },
                    { key: "amount", label: "Amount", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{(row.amount as number).toLocaleString(undefined, { maximumFractionDigits: 6 })}</span> },
                    { key: "price", label: "Price", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtUsd(row.price as number)}</span> },
                    { key: "value_usd", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtUsd(row.value_usd as number)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={holdingsData.holdings as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "value_usd", dir: "desc" }}
                  emptyMessage="No holdings found"
                />
              ) : (
                <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-muted-foreground">No holdings found</p>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Badges Tab */}
        <TabsContent value="badges">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Star className="size-4" />
                Badges
              </CardTitle>
            </CardHeader>
            <CardContent>
              {profileLoading ? (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <Skeleton key={i} className="h-20 w-full rounded-lg" />
                  ))}
                </div>
              ) : profileError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {profileError}
                </div>
              ) : profile?.badges && profile.badges.length > 0 ? (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {profile.badges.map((badge) => (
                    <div
                      key={badge.badge_id}
                      className="flex items-start gap-3 rounded-lg border border-border p-3"
                    >
                      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-lg">
                        {badge.icon ?? "🏅"}
                      </div>
                      <div className="min-w-0">
                        <p className="text-sm font-medium truncate">{badge.name}</p>
                        {badge.description && (
                          <p className="text-xs text-muted-foreground line-clamp-2">
                            {badge.description}
                          </p>
                        )}
                        {badge.earned_at && (
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            {fmtDate(badge.earned_at)}
                          </p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-muted-foreground">No badges earned yet</p>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Game Stats Tab */}
        <TabsContent value="games">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Gamepad2 className="size-4" />
                Game Statistics
              </CardTitle>
            </CardHeader>
            <CardContent>
              {gameStatsLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 4 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : gameStatsError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {gameStatsError}
                </div>
              ) : gameStats && gameStats.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "game_type", label: "Game", sortable: true, render: (row) => <span className="font-medium capitalize">{row.game_type as string}</span> },
                    { key: "games_played", label: "Played", sortable: true, className: "text-right", render: (row) => <span>{(row.games_played as number).toLocaleString()}</span> },
                    { key: "total_wagered", label: "Wagered", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtUsd(row.total_wagered as number)}</span> },
                    { key: "total_profit", label: "Net Profit", sortable: true, className: "text-right", render: (row) => <span className={`font-mono text-sm ${(row.total_profit as number) >= 0 ? "text-chart-green" : "text-chart-red"}`}>{fmtUsd(row.total_profit as number)}</span> },
                    { key: "best_win", label: "Best Win", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm text-chart-green">{fmtUsd(row.best_win as number)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={gameStats as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "total_profit", dir: "desc" }}
                  emptyMessage="No game history found"
                />
              ) : (
                <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-muted-foreground">No game history found</p>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Mining Tab */}
        <TabsContent value="mining">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Pickaxe className="size-4" />
                Mining Overview
              </CardTitle>
            </CardHeader>
            <CardContent>
              {rigsLoading || configLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 3 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : rigsError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {rigsError}
                </div>
              ) : (
                <div className="space-y-4">
                  {myConfig && (
                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="rounded-lg border border-border p-3 text-center">
                        <p className="text-xs text-muted-foreground">Total Hashrate</p>
                        <p className="text-lg font-bold">{myConfig.total_hashrate.toLocaleString()} H/s</p>
                      </div>
                      <div className="rounded-lg border border-border p-3 text-center">
                        <p className="text-xs text-muted-foreground">Total Rigs</p>
                        <p className="text-lg font-bold">{myConfig.total_rigs}</p>
                      </div>
                      <div className="rounded-lg border border-border p-3 text-center">
                        <p className="text-xs text-muted-foreground">Mining Mode</p>
                        <p className="text-lg font-bold">{myConfig.group_id ? "Group" : "Solo"}</p>
                      </div>
                    </div>
                  )}
                  {myRigs && myRigs.length > 0 ? (
                    <SortableTable<Record<string, unknown>>
                      columns={[
                        { key: "rig_id", label: "Rig Type", render: (row) => <span className="font-medium">{row.rig_id as string}</span> },
                        { key: "quantity", label: "Count", sortable: true, className: "text-right", render: (row) => <span className="font-mono">{(row.quantity as number).toLocaleString()}</span> },
                        { key: "total_hashrate", label: "Total Hashrate", sortable: true, className: "text-right", render: (row) => <span className="font-mono">{(row.total_hashrate as number).toLocaleString()} H/s</span> },
                      ] satisfies ColumnDef<Record<string, unknown>>[]}
                      data={myRigs as unknown as Record<string, unknown>[]}
                      defaultSort={{ key: "total_hashrate", dir: "desc" }}
                      emptyMessage="No mining rigs"
                    />
                  ) : (
                    <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
                      <p className="text-sm text-muted-foreground">No mining rigs owned</p>
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* PnL Tab */}
        <TabsContent value="pnl">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <DollarSign className="size-4" />
                Profit &amp; Loss
              </CardTitle>
            </CardHeader>
            <CardContent>
              {pnlLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : pnlError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {pnlError}
                </div>
              ) : pnlData && pnlData.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "period", label: "Period", render: (row) => <span className="font-medium">{row.period as string}</span> },
                    { key: "realized", label: "Realized", sortable: true, className: "text-right", render: (row) => <span className={`font-mono text-sm ${(row.realized as number) >= 0 ? "text-chart-green" : "text-chart-red"}`}>{fmtUsd(row.realized as number)}</span> },
                    { key: "unrealized", label: "Unrealized", sortable: true, className: "text-right", render: (row) => <span className={`font-mono text-sm ${(row.unrealized as number) >= 0 ? "text-chart-green" : "text-chart-red"}`}>{fmtUsd(row.unrealized as number)}</span> },
                    { key: "total", label: "Total", sortable: true, className: "text-right", render: (row) => <span className={`font-mono text-sm font-semibold ${(row.total as number) >= 0 ? "text-chart-green" : "text-chart-red"}`}>{fmtUsd(row.total as number)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={pnlData as unknown as Record<string, unknown>[]}
                  emptyMessage="No PnL data available"
                />
              ) : (
                <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-muted-foreground">No PnL data available</p>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
