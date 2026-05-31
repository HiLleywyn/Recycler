"use client";

import { useState, useCallback } from "react";
import RuleBanner from "@/components/RuleBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  PiggyBank,
  TrendingUp,
  Shield,
  Activity,
  Coins,
  AlertCircle,
  ArrowDownToLine,
  ArrowUpFromLine,
  Loader2,
  LogIn,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmtPct as safeFmtPct } from "@/lib/format";

interface SavingsPool {
  symbol: string;
  total_deposited: number;
  total_borrowed: number;
  utilization_pct: number;
  deposit_apy: number;
  borrow_apy: number;
}

interface MyDeposit {
  symbol: string;
  amount: number;
  interest_earned: number;
  apy: number;
}

interface SavingsResult {
  success: boolean;
  message: string;
  symbol: string;
  amount: number;
  new_balance: number;
}

type DialogMode = "deposit" | "withdraw" | null;

function fmt(n: number | undefined | null, decimals = 2): string {
  const v = n ?? 0;
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtPct(n: number | null | undefined): string {
  return safeFmtPct(n, 2);
}

function CardSkeleton() {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="size-4" />
      </CardHeader>
      <CardContent>
        <Skeleton className="mb-1 h-7 w-32" />
        <Skeleton className="h-3 w-16" />
      </CardContent>
    </Card>
  );
}

export default function SavingsPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const {
    data: pools,
    loading: poolsLoading,
    error: poolsError,
  } = useApi<SavingsPool[]>("/savings/pools");

  const {
    data: deposits,
    loading: depositsLoading,
    error: depositsError,
    refetch: refetchDeposits,
  } = useApi<MyDeposit[]>(isAuthenticated ? "/savings/my-positions" : null);

  const { mutate: depositAction, loading: depositLoading, error: depositError } =
    useApiMutation<SavingsResult>("/savings/deposit");
  const { mutate: withdrawAction, loading: withdrawLoading, error: withdrawError } =
    useApiMutation<SavingsResult>("/savings/withdraw");

  const [dialogMode, setDialogMode] = useState<DialogMode>(null);
  const [dialogAsset, setDialogAsset] = useState<"usd" | "sun">("usd");
  const [dialogAmount, setDialogAmount] = useState("");

  const openDeposit = useCallback((asset: "usd" | "sun" = "usd") => {
    setDialogMode("deposit");
    setDialogAsset(asset);
    setDialogAmount("");
  }, []);

  const openWithdraw = useCallback((d: MyDeposit) => {
    setDialogMode("withdraw");
    setDialogAsset(d.symbol.toLowerCase() as "usd" | "sun");
    setDialogAmount("");
  }, []);

  const closeDialog = useCallback(() => {
    setDialogMode(null);
    setDialogAmount("");
  }, []);

  const handleAction = useCallback(async () => {
    if (!dialogAmount || Number(dialogAmount) <= 0) return;
    const body = { amount: Number(dialogAmount), asset: dialogAsset };
    const action = dialogMode === "deposit" ? depositAction : withdrawAction;
    const result = await action(body);
    if (result) {
      toast.success(result.message || (dialogMode === "deposit" ? "Deposited successfully" : "Withdrawn successfully"));
      closeDialog();
      refetchDeposits();
    }
  }, [dialogAmount, dialogAsset, dialogMode, depositAction, withdrawAction, closeDialog, refetchDeposits]);

  const bestApy = pools?.reduce((max, p) => Math.max(max, p.deposit_apy), 0);
  const totalDeposited = deposits?.reduce((sum, d) => sum + d.amount, 0) ?? 0;
  const totalInterest = deposits?.reduce((sum, d) => sum + d.interest_earned, 0) ?? 0;
  const actionLoading = depositLoading || withdrawLoading;
  const actionError = dialogMode === "deposit" ? depositError : withdrawError;
  const currentDepositForAsset = deposits?.find(d => d.symbol.toLowerCase() === dialogAsset)?.amount ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Savings</h1>
          <p className="text-sm text-muted-foreground">
            Deposit tokens and earn interest over time
          </p>
        </div>
        {isAuthenticated && (
          <Button onClick={() => openDeposit("usd")} className="gap-2">
            <ArrowDownToLine className="size-4" />
            Deposit
          </Button>
        )}
      </div>

      <RuleBanner title="Savings Rules" rules={[
        "~6% APY floor — rate scales up with pool utilization (up to 620% APY at 100%)",
        "15% reserve factor — portion of interest goes to protocol reserves",
        "Minimum deposit: $1 USD or 1 SUN",
        "Withdraw anytime — no lock period on savings deposits",
      ]} />

      {/* Summary stats */}
      <div className="grid gap-4 sm:grid-cols-3">
        {poolsLoading || depositsLoading ? (
          <><CardSkeleton /><CardSkeleton /><CardSkeleton /></>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Your Deposits</CardTitle>
                <PiggyBank className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmt(totalDeposited)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Interest Earned</CardTitle>
                <TrendingUp className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmt(totalInterest)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Best APY</CardTitle>
                <Shield className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {bestApy != null ? fmtPct(bestApy) : "--%"}
                </div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Savings pools */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins className="size-4" />
            Savings Pools
          </CardTitle>
        </CardHeader>
        <CardContent>
          {poolsLoading ? (
            <div className="space-y-3">
              {[0,1,2].map(i => <Skeleton key={i} className="h-20 w-full" />)}
            </div>
          ) : poolsError ? (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" />{poolsError}
            </div>
          ) : pools && pools.length > 0 ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {pools.map((pool) => (
                <div key={pool.symbol} className="rounded-lg border border-border p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-base font-semibold">{pool.symbol}</span>
                    <Badge variant="secondary">{fmtPct(pool.deposit_apy)} APY</Badge>
                  </div>
                  <div className="space-y-1 text-sm text-muted-foreground">
                    <div className="flex justify-between">
                      <span>Total Deposited</span>
                      <span className="text-foreground">{fmt(pool.total_deposited)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Borrow APY</span>
                      <span className="text-foreground">{fmtPct(pool.borrow_apy)}</span>
                    </div>
                  </div>
                  <div>
                    <div className="mb-1 flex justify-between text-xs text-muted-foreground">
                      <span>Utilization</span>
                      <span>{fmtPct(pool.utilization_pct)}</span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary transition-all"
                        style={{ width: `${Math.min(pool.utilization_pct, 100)}%` }}
                      />
                    </div>
                  </div>
                  {isAuthenticated && (
                    <Button
                      size="sm"
                      className="w-full gap-1"
                      onClick={() => openDeposit(pool.symbol.toLowerCase() as "usd" | "sun")}
                    >
                      <ArrowDownToLine className="size-3" />
                      Deposit {pool.symbol}
                    </Button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No savings pools available</p>
          )}
        </CardContent>
      </Card>

      {/* User deposits */}
      {isAuthenticated && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="size-4" />
              Your Deposits
            </CardTitle>
          </CardHeader>
          <CardContent>
            {depositsLoading ? (
              <div className="space-y-3">
                {[0,1].map(i => <Skeleton key={i} className="h-12 w-full" />)}
              </div>
            ) : depositsError ? (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{depositsError}
              </div>
            ) : deposits && deposits.length > 0 ? (
              <div className="space-y-2">
                {deposits.map((d) => (
                  <div
                    key={d.symbol}
                    className="flex items-center justify-between rounded-lg bg-muted/50 px-4 py-3"
                  >
                    <div>
                      <span className="font-medium">{d.symbol}</span>
                      <p className="text-xs text-muted-foreground">{fmtPct(d.apy)} APY</p>
                    </div>
                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <div className="font-medium">{fmt(d.amount)}</div>
                        <p className="text-xs text-muted-foreground">+{fmt(d.interest_earned)} earned</p>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="gap-1"
                        onClick={() => openWithdraw(d)}
                      >
                        <ArrowUpFromLine className="size-3" />
                        Withdraw
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="flex h-24 items-center justify-center rounded-lg border border-dashed border-border">
                <p className="text-sm text-muted-foreground">No active deposits — choose a pool above</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {!isAuthenticated && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-border py-6">
          <LogIn className="size-4 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">Log in to deposit or withdraw</p>
        </div>
      )}

      {/* Deposit / Withdraw Dialog */}
      <Dialog open={!!dialogMode} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {dialogMode === "deposit" ? "Deposit to Savings" : "Withdraw from Savings"}
            </DialogTitle>
            <DialogDescription>
              {dialogMode === "deposit"
                ? "Earn interest on your deposited tokens."
                : `Available: ${currentDepositForAsset.toLocaleString()} ${dialogAsset.toUpperCase()}`}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Asset</label>
              <Select value={dialogAsset} onValueChange={(v) => setDialogAsset(v as "usd" | "sun")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="usd">USD</SelectItem>
                  <SelectItem value="sun">SUN</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Amount</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  placeholder="0.0"
                  min="0"
                  step="any"
                  value={dialogAmount}
                  onChange={(e) => setDialogAmount(e.target.value)}
                  className="flex-1"
                />
                {dialogMode === "withdraw" && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setDialogAmount(String(currentDepositForAsset))}
                  >
                    Max
                  </Button>
                )}
              </div>
            </div>
            {actionError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{actionError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>Cancel</Button>
            <Button
              disabled={actionLoading || !dialogAmount || Number(dialogAmount) <= 0}
              onClick={handleAction}
            >
              {actionLoading ? (
                <><Loader2 className="size-4 animate-spin" />{dialogMode === "deposit" ? "Depositing..." : "Withdrawing..."}</>
              ) : dialogMode === "deposit" ? (
                <><ArrowDownToLine className="size-4" />Confirm Deposit</>
              ) : (
                <><ArrowUpFromLine className="size-4" />Confirm Withdraw</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
