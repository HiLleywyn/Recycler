"use client";

import { useState, useCallback } from "react";
import RuleBanner from "@/components/RuleBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTable, type ColumnDef } from "@/components/ui/sortable-table";
import { UserLink } from "@/components/ui/user-link";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Pickaxe,
  Zap,
  Cpu,
  Activity,
  Boxes,
  Users,
  DollarSign,
  Gauge,
  Trophy,
  Loader2,
  AlertCircle,
  ShoppingCart,
  Trash2,
  Globe,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmt } from "@/lib/format";

interface NetworkStats {
  symbol: string;
  block_height: number;
  difficulty: number;
  total_hashrate: number;
  current_reward: number;
  last_block_ts: string | null;
}

interface MyRig {
  rig_id: string;
  quantity: number;
  total_hashrate: number;
}

interface UserMiningConfig {
  total_hashrate: number;
  total_rigs: number;
  assignments: { rig_id: string; chain_symbol: string; quantity: number }[];
  group_id: string | null;
}

interface RigType {
  rig_id: string;
  name: string;
  hashrate: number;
  power: number;
  price: number;
}

interface MinerInfo {
  user_id: number;
  username: string;
  total_hashrate: number;
  rig_count: number;
  blocks_mined: number;
}

interface MiningBlock {
  id: number;
  block_height: number;
  block_ts: string | null;
  miner_id: number | null;
  reward: number;
  total_hashrate: number;
}

interface MiningResult {
  success: boolean;
  message?: string;
}

function formatHashrate(h: number | null | undefined): string {
  const v = h ?? 0;
  if (v >= 1_000_000_000) return `${fmt(v / 1_000_000_000, 1)} GH/s`;
  if (v >= 1_000_000) return `${fmt(v / 1_000_000, 1)} MH/s`;
  if (v >= 1_000) return `${fmt(v / 1_000, 1)} KH/s`;
  return `${fmt(v, 0)} H/s`;
}

function formatNumber(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function MiningPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: networks, loading: networksLoading } = useApi<NetworkStats[]>(
    isAuthenticated ? "/mining/networks" : null
  );
  const { data: myRigs, loading: myRigsLoading, refetch: refetchRigs } = useApi<MyRig[]>(
    isAuthenticated ? "/mining/my-rigs" : null
  );
  const { data: myConfig, refetch: refetchConfig } = useApi<UserMiningConfig>(
    isAuthenticated ? "/mining/my-config" : null
  );
  const { data: rigTypes, loading: rigTypesLoading } = useApi<RigType[]>("/mining/rigs");
  const { data: miners, loading: minersLoading } = useApi<MinerInfo[]>(
    isAuthenticated ? "/mining/miners" : null
  );
  const { data: blocks, loading: blocksLoading } = useApi<MiningBlock[]>(
    isAuthenticated ? "/mining/blocks?limit=10" : null
  );

  // Mutations
  const { mutate: buyRig, loading: buyRigLoading, error: buyRigError } =
    useApiMutation<MiningResult>("/mining/buy-rig");
  const { mutate: sellRig, loading: sellRigLoading, error: sellRigError } =
    useApiMutation<MiningResult>("/mining/sell-rig");
  // Buy dialog state
  const [buyDialog, setBuyDialog] = useState<RigType | null>(null);
  const [buyQuantity, setBuyQuantity] = useState("1");

  // Sell dialog state
  const [sellDialog, setSellDialog] = useState<MyRig | null>(null);
  const [sellQuantity, setSellQuantity] = useState("1");

  const totalHashrate = myRigs?.reduce((sum, r) => sum + r.total_hashrate, 0) ?? 0;
  const totalRigs = myRigs?.reduce((sum, r) => sum + r.quantity, 0) ?? 0;
  const totalNetworkHashrate = networks?.reduce((sum, n) => sum + n.total_hashrate, 0) ?? 0;
  const myNetworkShare = totalNetworkHashrate > 0 ? (totalHashrate / totalNetworkHashrate * 100) : 0;

  const handleBuyRig = useCallback(async () => {
    if (!buyDialog || Number(buyQuantity) <= 0) return;
    const result = await buyRig({ rig_type: buyDialog.rig_id, quantity: Number(buyQuantity) });
    if (result) {
      toast.success(result.message || `Purchased ${buyQuantity}x ${buyDialog.name}`);
      setBuyDialog(null);
      setBuyQuantity("1");
      refetchRigs();
    }
  }, [buyDialog, buyQuantity, buyRig, refetchRigs]);

  const handleSellRig = useCallback(async () => {
    if (!sellDialog || Number(sellQuantity) <= 0) return;
    const result = await sellRig({ rig_type: sellDialog.rig_id, quantity: Number(sellQuantity) });
    if (result) {
      toast.success(result.message || `Sold ${sellQuantity}x ${sellDialog.rig_id}`);
      setSellDialog(null);
      setSellQuantity("1");
      refetchRigs();
    }
  }, [sellDialog, sellQuantity, sellRig, refetchRigs]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Mining</h1>
        <p className="text-sm text-muted-foreground">
          Mine tokens with virtual rigs on PoW networks — SUN and MTA
        </p>
      </div>

      <RuleBanner title="Mining Rules" rules={[
        "Solo share cap — max 20% (SUN) or 15% (MTA) of block reward per miner",
        "Electricity costs — $0.16/kWh (SUN), $0.22/kWh (MTA), +8% per extra rig",
        "Warmup — block rewards ramp 0-100% over first 50 (SUN) / 100 (MTA) blocks",
        "Rig slots limited by job tier (2 for Homeless, up to 128 for Exploiter)",
        "Halving — block reward halves every 210,000 blocks per network",
      ]} />

      {/* Overview cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Your Hashrate</CardTitle>
            <Zap className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {myRigsLoading ? <Skeleton className="h-8 w-24" /> : (
              <>
                <div className="text-2xl font-bold">
                  {isAuthenticated ? formatHashrate(totalHashrate) : "--"}
                </div>
                {isAuthenticated && totalNetworkHashrate > 0 && (
                  <p className="text-xs text-muted-foreground">{myNetworkShare.toFixed(3)}% of network</p>
                )}
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Your Rigs</CardTitle>
            <Cpu className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {myRigsLoading ? <Skeleton className="h-8 w-16" /> : (
              <>
                <div className="text-2xl font-bold">{isAuthenticated ? totalRigs : "--"}</div>
                {isAuthenticated && myConfig && (
                  <p className="text-xs text-muted-foreground capitalize">
                    {myConfig.assignments?.length ? myConfig.assignments.map(a => a.chain_symbol).join(", ") : "No network"} · {myConfig.group_id ? "Group" : "Solo"}
                  </p>
                )}
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Network Hashrate</CardTitle>
            <Activity className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {networksLoading ? <Skeleton className="h-8 w-28" /> : (
              <div className="text-2xl font-bold">{formatHashrate(totalNetworkHashrate)}</div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">PoW Networks</CardTitle>
            <Boxes className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {networksLoading ? <Skeleton className="h-8 w-12" /> : (
              <div className="text-2xl font-bold">{networks?.length ?? 0}</div>
            )}
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="rigs">
        <TabsList>
          <TabsTrigger value="rigs">My Rigs</TabsTrigger>
          <TabsTrigger value="buy">Buy Rigs</TabsTrigger>
          <TabsTrigger value="networks">Network Stats</TabsTrigger>
          <TabsTrigger value="leaderboard">Miners</TabsTrigger>
          <TabsTrigger value="blocks">Recent Blocks</TabsTrigger>
        </TabsList>

        {/* My Rigs — with full web management */}
        <TabsContent value="rigs">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Pickaxe className="size-4" />
                Your Mining Rigs
              </CardTitle>
            </CardHeader>
            <CardContent>
              {!isAuthenticated ? (
                <p className="text-sm text-muted-foreground py-4">Log in to view your rigs.</p>
              ) : myRigsLoading ? (
                <div className="space-y-3">
                  {[0,1].map(i => <Skeleton key={i} className="h-12 w-full" />)}
                </div>
              ) : (
                <div className="space-y-4">
                  {/* Mining config summary */}
                  <div className="flex flex-wrap items-center gap-4 rounded-lg bg-muted/50 px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Globe className="size-4 text-muted-foreground" />
                      <span className="text-sm text-muted-foreground">Total Hashrate:</span>
                      <span className="text-sm font-medium">{formatHashrate(myConfig?.total_hashrate ?? 0)}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Cpu className="size-4 text-muted-foreground" />
                      <span className="text-sm text-muted-foreground">Rigs:</span>
                      <span className="text-sm font-medium">{myConfig?.total_rigs ?? 0}</span>
                    </div>
                    {myConfig?.group_id && (
                      <div className="flex items-center gap-2">
                        <Users className="size-4 text-muted-foreground" />
                        <span className="text-sm text-muted-foreground">Group:</span>
                        <Badge variant="outline">{myConfig.group_id}</Badge>
                      </div>
                    )}
                  </div>

                  {myRigs && myRigs.length > 0 ? (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Rig Type</TableHead>
                          <TableHead className="text-right">Count</TableHead>
                          <TableHead className="text-right">Total Hashrate</TableHead>
                          <TableHead className="text-right">Actions</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {myRigs.map((rig) => (
                          <TableRow key={rig.rig_id}>
                            <TableCell className="font-medium">{rig.rig_id}</TableCell>
                            <TableCell className="text-right font-mono">{rig.quantity}</TableCell>
                            <TableCell className="text-right font-mono">{formatHashrate(rig.total_hashrate)}</TableCell>
                            <TableCell className="text-right">
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1"
                                onClick={() => { setSellDialog(rig); setSellQuantity("1"); }}
                              >
                                <Trash2 className="size-3" />Sell
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                        <TableRow className="border-t-2">
                          <TableCell className="font-semibold">Total</TableCell>
                          <TableCell className="text-right font-semibold">{totalRigs}</TableCell>
                          <TableCell className="text-right font-semibold">{formatHashrate(totalHashrate)}</TableCell>
                          <TableCell />
                        </TableRow>
                      </TableBody>
                    </Table>
                  ) : (
                    <div className="rounded-lg border border-dashed border-border p-6 text-center">
                      <p className="text-sm text-muted-foreground mb-2">You don&apos;t own any mining rigs yet.</p>
                      <p className="text-xs text-muted-foreground">Go to the &quot;Buy Rigs&quot; tab to get started.</p>
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Buy Rigs — with actual buy buttons */}
        <TabsContent value="buy">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShoppingCart className="size-4" />
                Available Rig Types
              </CardTitle>
            </CardHeader>
            <CardContent>
              {rigTypesLoading ? (
                <div className="space-y-3">
                  {[0,1,2,3].map(i => <Skeleton key={i} className="h-12 w-full" />)}
                </div>
              ) : rigTypes && rigTypes.length > 0 ? (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {rigTypes.map((rig) => (
                    <div key={rig.rig_id} className="rounded-lg border border-border p-4 space-y-3">
                      <div className="flex items-center justify-between">
                        <h3 className="font-semibold">{rig.name}</h3>
                        <Badge variant="outline">
                          <DollarSign className="size-3" />{formatNumber(rig.price)}
                        </Badge>
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground">
                        <div className="flex items-center gap-1.5">
                          <Gauge className="size-3.5" />
                          <span>{formatHashrate(rig.hashrate)}</span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          <Zap className="size-3.5" />
                          <span>{rig.power}W</span>
                        </div>
                      </div>
                      {isAuthenticated && (
                        <Button
                          size="sm"
                          className="w-full gap-1"
                          onClick={() => { setBuyDialog(rig); setBuyQuantity("1"); }}
                        >
                          <ShoppingCart className="size-3" />Buy
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-center text-sm text-muted-foreground py-4">No rig types available</p>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Network Stats */}
        <TabsContent value="networks">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Activity className="size-4" />
                PoW Network Stats
              </CardTitle>
            </CardHeader>
            <CardContent>
              {networksLoading ? (
                <div className="space-y-3">
                  {[0,1].map(i => <Skeleton key={i} className="h-12 w-full" />)}
                </div>
              ) : networks && networks.length > 0 ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Network</TableHead>
                      <TableHead className="text-right">Block Height</TableHead>
                      <TableHead className="text-right">Difficulty</TableHead>
                      <TableHead className="text-right">Network Hashrate</TableHead>
                      <TableHead className="text-right">Block Reward</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {networks.map((net) => (
                      <TableRow key={net.symbol}>
                        <TableCell className="font-medium">
                          <Badge variant="secondary">{net.symbol}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono">{formatNumber(net.block_height)}</TableCell>
                        <TableCell className="text-right font-mono">{formatNumber(net.difficulty)}</TableCell>
                        <TableCell className="text-right font-mono">{formatHashrate(net.total_hashrate)}</TableCell>
                        <TableCell className="text-right font-mono">{net.current_reward} {net.symbol}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <p className="text-center text-sm text-muted-foreground py-8">No network data available</p>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Miners Leaderboard */}
        <TabsContent value="leaderboard">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Trophy className="size-4" />
                Top Miners
              </CardTitle>
            </CardHeader>
            <CardContent>
              {minersLoading ? (
                <div className="space-y-3">
                  {[0,1,2,3,4].map(i => <Skeleton key={i} className="h-10 w-full" />)}
                </div>
              ) : miners && miners.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "_rank", label: "Rank", sortable: false, render: (_row, ) => {
                      const idx = miners.findIndex((m) => m.user_id === (_row.user_id as number));
                      return <span className="font-mono text-muted-foreground">#{idx + 1}</span>;
                    }},
                    { key: "user_id", label: "Miner", render: (row) => (
                      <UserLink userId={String(row.user_id)} username={row.username as string | undefined} />
                    )},
                    { key: "total_hashrate", label: "Hashrate", sortable: true, className: "text-right", render: (row) => <span className="font-mono">{formatHashrate(row.total_hashrate as number)}</span> },
                    { key: "rig_count", label: "Rigs", sortable: true, className: "text-right", render: (row) => <span className="font-mono">{row.rig_count as number}</span> },
                    { key: "blocks_mined", label: "Blocks Mined", sortable: true, className: "text-right", render: (row) => <span className="font-mono">{row.blocks_mined as number}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={miners as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "total_hashrate", dir: "desc" }}
                  emptyMessage="No miners yet"
                />
              ) : (
                <p className="text-center text-sm text-muted-foreground py-8">No miners yet</p>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Recent Blocks */}
        <TabsContent value="blocks">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Boxes className="size-4" />
                Recently Mined Blocks
              </CardTitle>
            </CardHeader>
            <CardContent>
              {blocksLoading ? (
                <div className="space-y-3">
                  {[0,1,2,3,4].map(i => <Skeleton key={i} className="h-10 w-full" />)}
                </div>
              ) : blocks && blocks.length > 0 ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Block</TableHead>
                      <TableHead>Miner</TableHead>
                      <TableHead className="text-right">Reward</TableHead>
                      <TableHead className="text-right">Time</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {blocks.map((b) => (
                      <TableRow key={b.id}>
                        <TableCell className="font-mono">#{formatNumber(b.block_height)}</TableCell>
                        <TableCell className="font-mono text-muted-foreground text-sm">
                          {b.miner_id != null ? String(b.miner_id).slice(0, 10) + "\u2026" : "Pool"}
                        </TableCell>
                        <TableCell className="text-right font-mono">{b.reward}</TableCell>
                        <TableCell className="text-right text-muted-foreground">{b.block_ts ? timeAgo(b.block_ts) : "--"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <p className="text-center text-sm text-muted-foreground py-8">No blocks mined yet</p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Buy Rig Dialog */}
      <Dialog open={!!buyDialog} onOpenChange={(open) => !open && setBuyDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Buy {buyDialog?.name}</DialogTitle>
            <DialogDescription>
              Cost: ${formatNumber(buyDialog?.price ?? 0)} each. Hashrate: {formatHashrate(buyDialog?.hashrate)}. Power: {buyDialog?.power}W.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Quantity</label>
              <Input
                type="number"
                min="1"
                step="1"
                value={buyQuantity}
                onChange={(e) => setBuyQuantity(e.target.value)}
              />
            </div>
            {Number(buyQuantity) > 0 && buyDialog && (
              <div className="rounded-lg bg-muted/50 p-3 text-sm space-y-1">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Total Cost</span>
                  <span className="font-mono font-semibold">${formatNumber(buyDialog.price * Number(buyQuantity))}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Total Hashrate</span>
                  <span className="font-mono">{formatHashrate(buyDialog.hashrate * Number(buyQuantity))}</span>
                </div>
              </div>
            )}
            {buyRigError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{buyRigError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBuyDialog(null)}>Cancel</Button>
            <Button disabled={buyRigLoading || Number(buyQuantity) <= 0} onClick={handleBuyRig}>
              {buyRigLoading ? (
                <><Loader2 className="size-4 animate-spin" />Buying...</>
              ) : (
                <><ShoppingCart className="size-4" />Buy {buyQuantity}x</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Sell Rig Dialog */}
      <Dialog open={!!sellDialog} onOpenChange={(open) => !open && setSellDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Sell {sellDialog?.rig_id}</DialogTitle>
            <DialogDescription>
              You own {sellDialog?.quantity ?? 0}x {sellDialog?.rig_id}. Rigs sell at 50% of purchase price.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Quantity to Sell</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  min="1"
                  max={sellDialog?.quantity ?? 1}
                  step="1"
                  value={sellQuantity}
                  onChange={(e) => setSellQuantity(e.target.value)}
                  className="flex-1"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSellQuantity(String(sellDialog?.quantity ?? 1))}
                >
                  Max
                </Button>
              </div>
            </div>
            {sellRigError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{sellRigError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSellDialog(null)}>Cancel</Button>
            <Button
              variant="destructive"
              disabled={sellRigLoading || Number(sellQuantity) <= 0}
              onClick={handleSellRig}
            >
              {sellRigLoading ? (
                <><Loader2 className="size-4 animate-spin" />Selling...</>
              ) : (
                <><Trash2 className="size-4" />Sell {sellQuantity}x</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
