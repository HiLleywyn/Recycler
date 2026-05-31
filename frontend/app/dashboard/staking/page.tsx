"use client";

import { useState, useCallback } from "react";
import RuleBanner from "@/components/RuleBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTable, type ColumnDef } from "@/components/ui/sortable-table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Landmark, Lock, TrendingUp, Unlock, Loader2, Users, LogIn } from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { UserLink } from "@/components/ui/user-link";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmt, fmtPct } from "@/lib/format";

interface Validator {
  validator_id: string;
  name: string;
  network: string;
  emoji: string;
  reward_rate: number;
  uptime: number;
  slash_rate: number;
  total_staked: number;
  staker_count: number;
}

interface UserStake {
  user_id: number;
  validator_id: string;
  validator_name: string;
  symbol: string;
  amount: number;
  value_usd: number;
  reward_rate: number;
  staked_at: string | null;
}

interface PosValidator {
  user_id: number;
  network: string;
  stake_token: string;
  stake_amount: number;
  is_active: boolean;
  total_blocks_validated: number;
  total_rewards_earned: number;
  slash_count: number;
  delegation_count: number;
  total_delegated: number;
}

interface UserDelegation {
  validator_user_id: number;
  network: string;
  token: string;
  amount: number;
  locked_until: string | null;
  total_earned: number;
}

type DialogMode = { type: "stake"; validator: Validator } | { type: "unstake"; stake: UserStake } | null;
type DelegateDialogMode =
  | { type: "delegate"; validator: PosValidator }
  | { type: "undelegate"; delegation: UserDelegation }
  | null;

function formatNumber(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${fmt(v / 1_000_000, 2)}M`;
  if (v >= 1_000) return `${fmt(v / 1_000, 1)}K`;
  return v.toLocaleString();
}

function SlashBadge({ count }: { count: number }) {
  if (count === 0) return <Badge variant="outline" className="text-green-600 border-green-600">✓ Clean</Badge>;
  if (count <= 1) return <Badge variant="outline" className="text-green-500 border-green-500">{count}/5</Badge>;
  if (count <= 3) return <Badge variant="outline" className="text-yellow-500 border-yellow-500">⚠ {count}/5</Badge>;
  if (count === 4) return <Badge variant="outline" className="text-orange-500 border-orange-500">🔴 {count}/5</Badge>;
  return <Badge variant="destructive">⛔ {count}/5 DEACTIVATED</Badge>;
}

function ValidatorStatusBadge({ isActive }: { isActive: boolean }) {
  return isActive
    ? <Badge className="bg-green-600">Active</Badge>
    : <Badge variant="destructive">Inactive</Badge>;
}

export default function StakingPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: validators, loading: validatorsLoading, error: validatorsError } =
    useApi<Validator[]>("/staking/validators");

  const {
    data: myStakes,
    loading: stakesLoading,
    refetch: refetchStakes,
  } = useApi<UserStake[]>(isAuthenticated ? "/staking/my-stakes" : null);

  const { data: posValidators, loading: posLoading } =
    useApi<PosValidator[]>("/staking/pos-validators");

  const {
    data: myDelegations,
    loading: delegationsLoading,
    refetch: refetchDelegations,
  } = useApi<UserDelegation[]>(isAuthenticated ? "/staking/my-delegations" : null);

  const { mutate: stakeAction, loading: stakeLoading } =
    useApiMutation<{ success: boolean }>("/staking/stake");
  const { mutate: unstakeAction, loading: unstakeLoading } =
    useApiMutation<{ success: boolean }>("/staking/unstake");
  const { mutate: delegateAction, loading: delegateLoading } =
    useApiMutation<{ success: boolean }>("/staking/delegate");
  const { mutate: undelegateAction, loading: undelegateLoading } =
    useApiMutation<{ success: boolean }>("/staking/undelegate");

  const [dialog, setDialog] = useState<DialogMode>(null);
  const [amount, setAmount] = useState("");
  const [stakeSymbol, setStakeSymbol] = useState("");
  const [delegateDialog, setDelegateDialog] = useState<DelegateDialogMode>(null);
  const [delegateAmount, setDelegateAmount] = useState("");

  const totalStaked = myStakes?.reduce((sum, s) => sum + (s.value_usd ?? 0), 0) ?? 0;
  const avgApy =
    myStakes && myStakes.length > 0
      ? (myStakes.reduce((sum, s) => sum + s.reward_rate, 0) / myStakes.length) * 365
      : 0;

  const openStake = useCallback((v: Validator) => {
    setDialog({ type: "stake", validator: v });
    setAmount("");
    setStakeSymbol(v.network || "DSC");
  }, []);

  const openUnstake = useCallback((s: UserStake) => {
    setDialog({ type: "unstake", stake: s });
    setAmount("");
  }, []);

  const closeDialog = useCallback(() => {
    setDialog(null);
    setAmount("");
  }, []);

  const handleStake = useCallback(async () => {
    if (dialog?.type !== "stake" || !amount || Number(amount) <= 0 || !stakeSymbol) return;
    const result = await stakeAction({
      validator_id: dialog.validator.validator_id,
      symbol: stakeSymbol,
      amount: Number(amount),
    });
    if (result) {
      toast.success(`Staked ${amount} ${stakeSymbol} with ${dialog.validator.name}`);
      closeDialog();
      refetchStakes();
    }
  }, [dialog, amount, stakeSymbol, stakeAction, closeDialog, refetchStakes]);

  const handleUnstake = useCallback(async () => {
    if (dialog?.type !== "unstake" || !amount || Number(amount) <= 0) return;
    const result = await unstakeAction({
      validator_id: dialog.stake.validator_id,
      symbol: dialog.stake.symbol,
      amount: Number(amount),
    });
    if (result) {
      toast.success(`Unstaked ${amount} ${dialog.stake.symbol}`);
      closeDialog();
      refetchStakes();
    }
  }, [dialog, amount, unstakeAction, closeDialog, refetchStakes]);

  const handleDelegate = useCallback(async () => {
    if (!delegateDialog) return;
    const amt = parseFloat(delegateAmount);
    if (isNaN(amt) || amt <= 0) { toast.error("Enter a valid amount."); return; }

    if (delegateDialog.type === "delegate") {
      const res = await delegateAction({
        validator_user_id: delegateDialog.validator.user_id,
        network: delegateDialog.validator.network,
        amount: amt,
      });
      if (res?.success) {
        toast.success("Delegation submitted.");
        refetchDelegations();
      } else {
        toast.error("Delegation failed.");
      }
    } else {
      const res = await undelegateAction({
        validator_user_id: delegateDialog.delegation.validator_user_id,
        network: delegateDialog.delegation.network,
        amount: amt,
      });
      if (res?.success) {
        toast.success("Undelegation submitted.");
        refetchDelegations();
      } else {
        toast.error("Undelegation failed.");
      }
    }
    setDelegateDialog(null);
    setDelegateAmount("");
  }, [delegateDialog, delegateAmount, delegateAction, undelegateAction, refetchDelegations]);

  const actionLoading = stakeLoading || unstakeLoading;
  const isStake = dialog?.type === "stake";

  type PosRow = PosValidator & Record<string, unknown>;
  const posColumns: ColumnDef<PosRow>[] = [
    { key: "user_id",                label: "Validator",  sortable: false, render: (v) => <UserLink userId={String(v.user_id)} /> },
    { key: "network",                label: "Network",    sortable: true,  render: (v) => <Badge variant="outline">{v.network}</Badge> },
    { key: "stake_amount",           label: "Stake",      sortable: true,  render: (v) => `${formatNumber(v.stake_amount)} ${v.stake_token}` },
    { key: "is_active",              label: "Status",     sortable: false, render: (v) => <ValidatorStatusBadge isActive={v.is_active} /> },
    { key: "total_blocks_validated", label: "Blocks",     sortable: true,  render: (v) => formatNumber(v.total_blocks_validated) },
    { key: "total_rewards_earned",   label: "Earned",     sortable: true,  render: (v) => formatNumber(v.total_rewards_earned) },
    { key: "slash_count",            label: "Health",     sortable: true,  render: (v) => <SlashBadge count={v.slash_count} /> },
    { key: "delegation_count",       label: "Delegators", sortable: true,  render: (v) => v.delegation_count },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Staking</h1>
        <p className="text-sm text-muted-foreground">
          Stake tokens with validators to earn rewards and secure the network
        </p>
      </div>

      <RuleBanner title="Staking Rules" rules={[
        "24h lock — cannot unstake for 24 hours after staking",
        "5% early unstake penalty — burned if unstaked within 48 hours",
        "12h warmup — rewards ramp linearly to full over first 12 hours",
        "ARC stakes on Arcadia validators, DSC on Discoin validators only",
        "Validators can be slashed — 5 slashes = auto-deactivated, delegators refunded",
      ]} />

      {/* Summary cards */}
      {isAuthenticated && (
        <div className="grid gap-4 sm:grid-cols-3">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Total Staked</CardTitle>
              <Lock className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {stakesLoading ? <Skeleton className="h-8 w-24" /> : (
                <div className="text-2xl font-bold">${formatNumber(totalStaked)}</div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Active Stakes</CardTitle>
              <TrendingUp className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {stakesLoading ? <Skeleton className="h-8 w-12" /> : (
                <div className="text-2xl font-bold">{myStakes?.length ?? 0}</div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Avg APY</CardTitle>
              <Unlock className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {stakesLoading ? <Skeleton className="h-8 w-16" /> : (
                <div className="text-2xl font-bold">
                  {avgApy > 0 ? fmtPct(avgApy, 1) : "--%"}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Your Stakes */}
      {isAuthenticated && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Lock className="size-4" />
              Your Stakes
            </CardTitle>
          </CardHeader>
          <CardContent>
            {stakesLoading ? (
              <div className="space-y-2">
                {[0,1,2].map(i => <Skeleton key={i} className="h-10 w-full" />)}
              </div>
            ) : myStakes && myStakes.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Validator</TableHead>
                    <TableHead>Token</TableHead>
                    <TableHead className="text-right">Amount</TableHead>
                    <TableHead className="text-right">Value</TableHead>
                    <TableHead className="text-right">APY</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {myStakes.map((stake) => (
                    <TableRow key={`${stake.validator_id}-${stake.symbol}`}>
                      <TableCell className="font-medium">{stake.validator_name || stake.validator_id}</TableCell>
                      <TableCell><Badge variant="secondary">{stake.symbol}</Badge></TableCell>
                      <TableCell className="text-right font-mono text-sm">{formatNumber(stake.amount)}</TableCell>
                      <TableCell className="text-right font-mono text-sm">${formatNumber(stake.value_usd)}</TableCell>
                      <TableCell className="text-right text-chart-green">{fmtPct(stake.reward_rate * 365, 1)}</TableCell>
                      <TableCell className="text-right">
                        <Button size="sm" variant="outline" onClick={() => openUnstake(stake)}>
                          Unstake
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
                <p className="text-sm text-muted-foreground">No active stakes — choose a validator below</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* My Delegations */}
      {isAuthenticated && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold">My Delegations</h2>
          {delegationsLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : !myDelegations?.length ? (
            <p className="text-muted-foreground text-sm">No active delegations.</p>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Validator</TableHead>
                    <TableHead>Network</TableHead>
                    <TableHead>Amount</TableHead>
                    <TableHead>Lock Status</TableHead>
                    <TableHead>Earned</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {myDelegations.map((d, i) => {
                    const locked = d.locked_until ? new Date(d.locked_until) > new Date() : false;
                    return (
                      <TableRow key={`${d.validator_user_id}-${d.network}-${d.token}`}>
                        <TableCell><UserLink userId={String(d.validator_user_id)} /></TableCell>
                        <TableCell><Badge variant="outline">{d.network}</Badge></TableCell>
                        <TableCell>{formatNumber(d.amount)} {d.token}</TableCell>
                        <TableCell>
                          {locked
                            ? <Badge variant="outline" className="text-yellow-500">🔒 Locked</Badge>
                            : <Badge variant="outline" className="text-green-500">Unlocked</Badge>}
                        </TableCell>
                        <TableCell>{formatNumber(d.total_earned)} {d.token}</TableCell>
                        <TableCell>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={locked}
                            onClick={() => setDelegateDialog({ type: "undelegate", delegation: d })}
                          >
                            <Unlock className="h-3 w-3 mr-1" /> Undelegate
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      )}

      {/* Protocol Nodes */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Landmark className="size-4" />
            Protocol Nodes
          </CardTitle>
        </CardHeader>
        <CardContent>
          {validatorsLoading ? (
            <div className="space-y-2">
              {[0,1,2,3].map(i => <Skeleton key={i} className="h-12 w-full" />)}
            </div>
          ) : validatorsError ? (
            <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
              <p className="text-sm text-destructive">{validatorsError}</p>
            </div>
          ) : !validators || validators.length === 0 ? (
            <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
              <p className="text-sm text-muted-foreground">No validators available</p>
            </div>
          ) : (
            <SortableTable<Record<string, unknown>>
              columns={[
                { key: "name", label: "Name", sortable: true, render: (row) => <span className="font-medium">{row.emoji as string} {row.name as string}</span> },
                { key: "network", label: "Network", render: (row) => <Badge variant="secondary">{(row.network as string) || "--"}</Badge> },
                { key: "reward_rate", label: "APY", sortable: true, className: "text-right", render: (row) => <span className="text-chart-green">{fmtPct((row.reward_rate as number) * 365, 1)}</span> },
                { key: "uptime", label: "Uptime", sortable: true, className: "text-right", render: (row) => {
                  const up = row.uptime as number;
                  return <span className={up >= 99 ? "text-chart-green" : up >= 95 ? "" : "text-destructive"}>{fmtPct(up, 1)}</span>;
                }},
                { key: "total_staked", label: "Total Staked", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{formatNumber(row.total_staked as number)}</span> },
                { key: "staker_count", label: "Stakers", sortable: true, className: "text-right", render: (row) => (
                  <span className="inline-flex items-center gap-1 text-sm text-muted-foreground">
                    <Users className="size-3" />{row.staker_count as number}
                  </span>
                )},
                ...(isAuthenticated ? [{
                  key: "_action",
                  label: "Action",
                  sortable: false,
                  className: "text-right",
                  render: (row: Record<string, unknown>) => (
                    <Button size="sm" onClick={() => openStake(row as unknown as Validator)}>Stake</Button>
                  ),
                } satisfies ColumnDef<Record<string, unknown>>] : []),
              ]}
              data={validators as unknown as Record<string, unknown>[]}
              defaultSort={{ key: "reward_rate", dir: "desc" }}
              emptyMessage="No validators available"
            />
          )}
          {!isAuthenticated && (
            <div className="mt-4 flex items-center justify-center gap-2 rounded-lg border border-dashed border-border py-4">
              <LogIn className="size-4 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">Log in to stake tokens</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Player Validators — Active PoS */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <span>⚡ Player Validators — Active PoS</span>
        </h2>
        {posLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : !posValidators?.length ? (
          <p className="text-muted-foreground text-sm">No player validators registered yet.</p>
        ) : (
          <SortableTable<PosRow>
            data={posValidators as PosRow[]}
            columns={[
              ...posColumns,
              ...(isAuthenticated ? [{
                key: "_delegate",
                label: "",
                sortable: false,
                render: (v: PosRow) => (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!v.is_active}
                    onClick={() => setDelegateDialog({ type: "delegate", validator: v as PosValidator })}
                  >
                    Delegate
                  </Button>
                ),
              } satisfies ColumnDef<PosRow>] : []),
            ]}
            defaultSort={{ key: "stake_amount", dir: "desc" }}
            emptyMessage="No player validators"
          />
        )}
        {!isAuthenticated && posValidators && posValidators.length > 0 && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <LogIn className="size-4" />
            <span>Log in to delegate to player validators</span>
          </div>
        )}
      </div>

      {/* Stake / Unstake Dialog */}
      <Dialog open={!!dialog} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {isStake
                ? `Stake with ${(dialog as { type: "stake"; validator: Validator })?.validator?.name}`
                : `Unstake ${(dialog as { type: "unstake"; stake: UserStake })?.stake?.symbol}`}
            </DialogTitle>
            <DialogDescription>
              {isStake
                ? `APY: ${fmtPct(((dialog as { type: "stake"; validator: Validator })?.validator?.reward_rate ?? 0) * 365, 1)} · Slash risk: ${fmtPct((dialog as { type: "stake"; validator: Validator })?.validator?.slash_rate ?? 0, 2)}`
                : `Available: ${formatNumber((dialog as { type: "unstake"; stake: UserStake })?.stake?.amount ?? 0)} ${(dialog as { type: "unstake"; stake: UserStake })?.stake?.symbol}`}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            {isStake && (
              <div>
                <label className="text-sm font-medium">Token Symbol</label>
                <Input
                  placeholder="e.g. DSC, ARC"
                  value={stakeSymbol}
                  onChange={(e) => setStakeSymbol(e.target.value.toUpperCase())}
                  className="mt-1"
                />
              </div>
            )}
            <label className="text-sm font-medium">
              Amount to {isStake ? "stake" : "unstake"}
            </label>
            <div className="flex gap-2">
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="flex-1"
              />
              {!isStake && dialog?.type === "unstake" && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setAmount(String(dialog.stake.amount))}
                >
                  Max
                </Button>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>Cancel</Button>
            <Button
              disabled={actionLoading || !amount || Number(amount) <= 0}
              onClick={isStake ? handleStake : handleUnstake}
            >
              {actionLoading ? (
                <><Loader2 className="size-4 animate-spin" />{isStake ? "Staking..." : "Unstaking..."}</>
              ) : isStake ? "Confirm Stake" : "Confirm Unstake"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delegate / Undelegate Dialog */}
      <Dialog open={!!delegateDialog} onOpenChange={(o) => !o && setDelegateDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {delegateDialog?.type === "delegate" ? "Delegate to Validator" : "Undelegate"}
            </DialogTitle>
            <DialogDescription>
              {delegateDialog?.type === "delegate" ? (
                <>
                  Delegate tokens to validator <strong><UserLink userId={String(delegateDialog.validator.user_id)} /></strong> on{" "}
                  <strong>{delegateDialog.validator.network}</strong>.<br />
                  24-hour lock applies. Minimum 50 tokens.
                </>
              ) : (
                <>
                  Undelegate from validator{" "}
                  <strong><UserLink userId={String(delegateDialog?.delegation.validator_user_id ?? "")} /></strong>.<br />
                  <span className="text-yellow-600 font-medium">
                    ⚠️ Early unstake within 48h of lock carries a 5% burn penalty.
                  </span>
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Input
              type="number"
              placeholder="Amount"
              value={delegateAmount}
              onChange={(e) => setDelegateAmount(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDelegateDialog(null)}>Cancel</Button>
            <Button
              onClick={handleDelegate}
              disabled={delegateLoading || undelegateLoading}
            >
              {delegateLoading || undelegateLoading ? (
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
              ) : null}
              Confirm
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
