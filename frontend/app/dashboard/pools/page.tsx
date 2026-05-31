"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Droplets, Wallet, DollarSign, Percent, Plus, Minus, Loader2, LogIn, AlertCircle } from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmt, fmtUsd, fmtNumDecimals } from "@/lib/format";

interface Pool {
  pool_id: string;
  token_a: string;
  token_b: string;
  reserve_a: number;
  reserve_b: number;
  total_lp: number;
  tvl: number;
  fee_rate: number;
}

interface LPPosition {
  pool_id: string;
  token_a: string;
  token_b: string;
  lp_shares: number;
  value_usd: number;
  share_pct: number;
}

interface AddLiquidityResult {
  success: boolean;
  message: string;
  lp_shares_minted: number;
}

interface RemoveLiquidityResult {
  success: boolean;
  message: string;
  amount_a: number;
  amount_b: number;
}

type AddDialog = { pool: Pool } | null;
type RemoveDialog = { position: LPPosition } | null;

function formatUsd(n: number | null | undefined): string {
  return fmtUsd(n, 2);
}

function formatPct(n: number | null | undefined): string {
  return `${fmt((n ?? 0) * 100, 2)}%`;
}

export default function PoolsPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: pools, loading: poolsLoading, error: poolsError, refetch: refetchPools } = useApi<Pool[]>(isAuthenticated ? "/pools" : null);
  const { data: positions, loading: positionsLoading, error: positionsError, refetch: refetchPositions } = useApi<LPPosition[]>(
    isAuthenticated ? "/pools/my-positions" : null
  );

  const { mutate: addLiquidity, loading: addLoading, error: addError } =
    useApiMutation<AddLiquidityResult>("/pools/add-liquidity");
  const { mutate: removeLiquidity, loading: removeLoading, error: removeError } =
    useApiMutation<RemoveLiquidityResult>("/pools/remove-liquidity");

  const [addDialog, setAddDialog] = useState<AddDialog>(null);
  const [removeDialog, setRemoveDialog] = useState<RemoveDialog>(null);
  const [amountA, setAmountA] = useState("");
  const [amountB, setAmountB] = useState("");
  const [lpShares, setLpShares] = useState("");

  const openAdd = useCallback((pool: Pool) => {
    setAddDialog({ pool });
    setAmountA("");
    setAmountB("");
  }, []);

  const openRemove = useCallback((position: LPPosition) => {
    setRemoveDialog({ position });
    setLpShares("");
  }, []);

  const closeAll = useCallback(() => {
    setAddDialog(null);
    setRemoveDialog(null);
    setAmountA("");
    setAmountB("");
    setLpShares("");
  }, []);

  const handleAdd = useCallback(async () => {
    if (!addDialog || !amountA || !amountB || Number(amountA) <= 0 || Number(amountB) <= 0) return;
    const result = await addLiquidity({
      pool_id: addDialog.pool.pool_id,
      amount_a: Number(amountA),
      amount_b: Number(amountB),
    });
    if (result) {
      toast.success(`Added liquidity — minted ${result.lp_shares_minted?.toFixed(6)} LP shares`);
      closeAll();
      refetchPools();
      refetchPositions();
    }
  }, [addDialog, amountA, amountB, addLiquidity, closeAll, refetchPools, refetchPositions]);

  const handleRemove = useCallback(async () => {
    if (!removeDialog || !lpShares || Number(lpShares) <= 0) return;
    const result = await removeLiquidity({
      pool_id: removeDialog.position.pool_id,
      lp_shares: Number(lpShares),
    });
    if (result) {
      toast.success(
        `Removed liquidity — received ${result.amount_a?.toFixed(4)} ${removeDialog.position.token_a} + ${result.amount_b?.toFixed(4)} ${removeDialog.position.token_b}`
      );
      closeAll();
      refetchPools();
      refetchPositions();
    }
  }, [removeDialog, lpShares, removeLiquidity, closeAll, refetchPools, refetchPositions]);

  const totalTvl = pools?.reduce((sum, p) => sum + p.tvl, 0) ?? 0;
  const totalPositionValue = positions?.reduce((sum, p) => sum + p.value_usd, 0) ?? 0;
  const positionMap = new Map<string, LPPosition>();
  positions?.forEach((p) => positionMap.set(p.pool_id, p));

  // Auto-fill amount_b when amount_a changes based on pool ratio
  const handleAmountAChange = useCallback((val: string) => {
    setAmountA(val);
    if (addDialog && val && Number(val) > 0) {
      const pool = addDialog.pool;
      if (pool.reserve_a > 0 && pool.reserve_b > 0) {
        const ratio = pool.reserve_b / pool.reserve_a;
        setAmountB((Number(val) * ratio).toFixed(6));
      }
    }
  }, [addDialog]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Liquidity Pools</h1>
        <p className="text-sm text-muted-foreground">Provide liquidity and earn trading fees</p>
      </div>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Total TVL</CardTitle>
            <DollarSign className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {poolsLoading ? <Skeleton className="h-8 w-28" /> : (
              <div className="text-2xl font-bold">{formatUsd(totalTvl)}</div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Active Pools</CardTitle>
            <Droplets className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {poolsLoading ? <Skeleton className="h-8 w-12" /> : (
              <div className="text-2xl font-bold">{pools?.length ?? 0}</div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Your LP Value</CardTitle>
            <Wallet className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {positionsLoading ? <Skeleton className="h-8 w-24" /> : (
              <div className="text-2xl font-bold">
                {isAuthenticated ? formatUsd(totalPositionValue) : "--"}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Your Positions */}
      {isAuthenticated && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Wallet className="size-4" />
              Your Positions
            </CardTitle>
          </CardHeader>
          <CardContent>
            {positionsLoading ? (
              <div className="space-y-3">
                {[0,1].map(i => <Skeleton key={i} className="h-12 w-full" />)}
              </div>
            ) : positionsError ? (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />Failed to load positions: {positionsError}
              </div>
            ) : positions && positions.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Pair</TableHead>
                    <TableHead className="text-right">LP Shares</TableHead>
                    <TableHead className="text-right">Value</TableHead>
                    <TableHead className="text-right">Pool Share</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {positions.map((pos) => (
                    <TableRow key={pos.pool_id}>
                      <TableCell className="font-medium">{pos.token_a} / {pos.token_b}</TableCell>
                      <TableCell className="text-right font-mono">
                        {fmtNumDecimals(pos.lp_shares, { max: 4 })}
                      </TableCell>
                      <TableCell className="text-right font-mono">{formatUsd(pos.value_usd)}</TableCell>
                      <TableCell className="text-right">
                        <Badge variant="secondary">{fmt((pos.share_pct ?? 0) * 100, 2)}%</Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <Button size="sm" variant="outline" className="gap-1" onClick={() => openRemove(pos)}>
                          <Minus className="size-3" />Remove
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
                <p className="text-sm text-muted-foreground">No LP positions yet — add liquidity to a pool below</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* All Pools */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Droplets className="size-4" />
            All Pools
          </CardTitle>
        </CardHeader>
        <CardContent>
          {poolsLoading ? (
            <div className="space-y-3">
              {[0,1,2,3].map(i => <Skeleton key={i} className="h-12 w-full" />)}
            </div>
          ) : poolsError ? (
            <div className="flex items-center gap-2 text-sm text-destructive py-8 justify-center">
              <AlertCircle className="size-4" />Failed to load pools: {poolsError}
            </div>
          ) : pools && pools.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Pair</TableHead>
                  <TableHead className="text-right">TVL</TableHead>
                  <TableHead className="text-right">Fee Rate</TableHead>
                  <TableHead className="text-right">Reserve A</TableHead>
                  <TableHead className="text-right">Reserve B</TableHead>
                  {isAuthenticated && <TableHead className="text-right">Your Position</TableHead>}
                  {isAuthenticated && <TableHead className="text-right">Action</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {pools.map((pool) => {
                  const userPos = positionMap.get(pool.pool_id);
                  return (
                    <TableRow key={pool.pool_id}>
                      <TableCell className="font-medium">{pool.token_a} / {pool.token_b}</TableCell>
                      <TableCell className="text-right font-mono">{formatUsd(pool.tvl)}</TableCell>
                      <TableCell className="text-right">
                        <Badge variant="outline">
                          <Percent className="size-3" />
                          {fmt((pool.fee_rate ?? 0) * 100, 2)}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono text-muted-foreground">
                        {fmtNumDecimals(pool.reserve_a, { max: 2 })}
                      </TableCell>
                      <TableCell className="text-right font-mono text-muted-foreground">
                        {fmtNumDecimals(pool.reserve_b, { max: 2 })}
                      </TableCell>
                      {isAuthenticated && (
                        <TableCell className="text-right font-mono">
                          {userPos ? formatUsd(userPos.value_usd) : "--"}
                        </TableCell>
                      )}
                      {isAuthenticated && (
                        <TableCell className="text-right">
                          <Button size="sm" className="gap-1" onClick={() => openAdd(pool)}>
                            <Plus className="size-3" />Add
                          </Button>
                        </TableCell>
                      )}
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          ) : (
            <p className="text-center text-sm text-muted-foreground py-8">No pools available</p>
          )}
        </CardContent>
      </Card>

      {!isAuthenticated && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-border py-4">
          <LogIn className="size-4 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">Log in to add or remove liquidity</p>
        </div>
      )}

      {/* Add Liquidity Dialog */}
      <Dialog open={!!addDialog} onOpenChange={(open) => !open && closeAll()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Liquidity — {addDialog?.pool.token_a} / {addDialog?.pool.token_b}</DialogTitle>
            <DialogDescription>
              Amounts are automatically balanced to the current pool ratio.
              You will receive LP shares proportional to your contribution.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">{addDialog?.pool.token_a} Amount</label>
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={amountA}
                onChange={(e) => handleAmountAChange(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">{addDialog?.pool.token_b} Amount</label>
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={amountB}
                onChange={(e) => setAmountB(e.target.value)}
              />
            </div>
            {addError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{addError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeAll}>Cancel</Button>
            <Button
              disabled={addLoading || !amountA || !amountB || Number(amountA) <= 0 || Number(amountB) <= 0}
              onClick={handleAdd}
            >
              {addLoading ? (
                <><Loader2 className="size-4 animate-spin" />Adding...</>
              ) : (
                <><Plus className="size-4" />Add Liquidity</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Remove Liquidity Dialog */}
      <Dialog open={!!removeDialog} onOpenChange={(open) => !open && closeAll()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Remove Liquidity — {removeDialog?.position.token_a} / {removeDialog?.position.token_b}
            </DialogTitle>
            <DialogDescription>
              You hold {fmtNumDecimals(removeDialog?.position.lp_shares ?? 0, { max: 6 })} LP shares (
              {fmt((removeDialog?.position.share_pct ?? 0) * 100, 2)}% of pool).
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">LP Shares to Redeem</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  placeholder="0.0"
                  min="0"
                  step="any"
                  value={lpShares}
                  onChange={(e) => setLpShares(e.target.value)}
                  className="flex-1"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setLpShares(String(removeDialog?.position.lp_shares ?? 0))}
                >
                  Max
                </Button>
              </div>
            </div>
            {removeError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{removeError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeAll}>Cancel</Button>
            <Button
              disabled={removeLoading || !lpShares || Number(lpShares) <= 0}
              onClick={handleRemove}
            >
              {removeLoading ? (
                <><Loader2 className="size-4 animate-spin" />Removing...</>
              ) : (
                <><Minus className="size-4" />Remove Liquidity</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
