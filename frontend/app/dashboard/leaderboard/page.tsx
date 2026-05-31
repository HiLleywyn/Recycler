"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SortableTable, type ColumnDef } from "@/components/ui/sortable-table";
import { UserLink } from "@/components/ui/user-link";
import { Skeleton } from "@/components/ui/skeleton";
import { Trophy, Crown, Medal } from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { fmt } from "@/lib/format";

interface LeaderboardEntry {
  rank: number;
  user_id: string;
  value: number;
  detail: string;
}

type TabKey = "wealth" | "trading" | "mining" | "games";

const TAB_CONFIG: { key: TabKey; label: string; path: string; title: string }[] = [
  { key: "wealth", label: "Wealth", path: "/stats/leaderboard", title: "Wealthiest Players" },
  { key: "trading", label: "Trading", path: "/stats/leaderboard/traders", title: "Top Traders" },
  { key: "mining", label: "Mining", path: "/stats/leaderboard/miners", title: "Top Miners" },
  { key: "games", label: "Games", path: "/stats/leaderboard/gamblers", title: "Top Gamblers" },
];

function RankIcon({ rank }: { rank: number }) {
  if (rank === 1) return <Crown className="size-4 text-chart-gold" />;
  if (rank <= 3) return <Medal className="size-4 text-muted-foreground" />;
  return (
    <span className="flex size-4 items-center justify-center text-xs text-muted-foreground">
      {rank}
    </span>
  );
}

function formatValue(value: number | null | undefined): string {
  const v = value ?? 0;
  if (v >= 1_000_000) return `${fmt(v / 1_000_000, 2)}M`;
  if (v >= 1_000) return `${fmt(v / 1_000, 1)}K`;
  return (v).toLocaleString();
}

function LeaderboardTable({ path, title }: { path: string; title: string }) {
  const { data, loading, error } = useApi<LeaderboardEntry[]>(path);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Trophy className="size-4" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : error ? (
          <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        ) : !data || data.length === 0 ? (
          <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
            <p className="text-sm text-muted-foreground">No entries yet</p>
          </div>
        ) : (
          <SortableTable<Record<string, unknown>>
            columns={[
              { key: "rank", label: "Rank", sortable: true, className: "w-16", render: (row) => (
                <div className="flex items-center gap-2">
                  <RankIcon rank={row.rank as number} />
                  <span className="font-mono text-sm">#{row.rank as number}</span>
                </div>
              )},
              { key: "user_id", label: "User", render: (row) => (
                <UserLink userId={String(row.user_id)} username={row.detail as string | undefined} />
              )},
              { key: "value", label: "Value", sortable: true, className: "text-right", render: (row) => (
                <span className="font-mono text-sm">{formatValue(row.value as number)}</span>
              )},
            ] satisfies ColumnDef<Record<string, unknown>>[]}
            data={data as unknown as Record<string, unknown>[]}
            defaultSort={{ key: "rank", dir: "asc" }}
            emptyMessage="No entries yet"
          />
        )}
      </CardContent>
    </Card>
  );
}

export default function LeaderboardPage() {
  const [activeTab, setActiveTab] = useState<TabKey>("wealth");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Leaderboard</h1>
        <p className="text-sm text-muted-foreground">
          Top players and traders in the community
        </p>
      </div>

      <Tabs
        defaultValue="wealth"
        value={activeTab}
        onValueChange={(val) => setActiveTab(val as TabKey)}
      >
        <TabsList>
          {TAB_CONFIG.map((tab) => (
            <TabsTrigger key={tab.key} value={tab.key}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {TAB_CONFIG.map((tab) => (
          <TabsContent key={tab.key} value={tab.key}>
            <LeaderboardTable path={tab.path} title={tab.title} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
