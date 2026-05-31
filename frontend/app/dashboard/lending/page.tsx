"use client";

import { useState, useCallback } from "react";
import RuleBanner from "@/components/RuleBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  HandCoins,
  ArrowUpFromLine,
  ArrowDownToLine,
  Activity,
  ShieldCheck,
  Percent,
  AlertCircle,
  Plus,
  Loader2,
  LogIn,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { fmtPct as safeFmtPct } from "@/lib/format";

// API response types — matches the actual /lending/my-loans flat list
interface MyLoan {
  loan_type: "usd";
  principal: number;
  outstanding: number;
  collateral: number;
  collateral_ratio: number;
  last_interest?: string | null;
  created_at?: string | null;
}

interface LendingStats {
  active_loans: number;
  total_borrowed: number;
  total_collateral: number;
  avg_collateral_ratio: number;
}

interface LoanResult {
  success: boolean;
  message: string;
}

type DialogMode = "borrow" | "repay" | "add-collateral" | null;

function fmt(n: number | undefined | null, decimals = 2): string {
  const v = n ?? 0;
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtNum(n: number | undefined | null): string {
  return (n ?? 0).toLocaleString();
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
      </CardContent>
    </Card>
  );
}

export default function LendingPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: stats, loading: statsLoading, error: statsError } =
    useApi<LendingStats>("/lending/stats");

  const { data: myLoans, loading: loansLoading, error: loansError, refetch: refetchLoans } =
    useApi<MyLoan[]>(isAuthenticated ? "/lending/my-loans" : null);

  const { mutate: borrowAction, loading: borrowLoading, error: borrowError } =
    useApiMutation<LoanResult>("/lending/borrow");
  const { mutate: repayAction, loading: repayLoading, error: repayError } =
    useApiMutation<LoanResult>("/lending/repay");
  const { mutate: addCollateralAction, loading: addColLoading, error: addColError } =
    useApiMutation<LoanResult>("/lending/add-collateral");

  const [dialogMode, setDialogMode] = useState<DialogMode>(null);
  const [borrowAmount, setBorrowAmount] = useState("");
  const [collateralAmount, setCollateralAmount] = useState("");
  const [actionAmount, setActionAmount] = useState("");

  const usdLoans = myLoans ?? [];
  const hasUsdLoan = usdLoans.length > 0;

  const openDialog = useCallback((mode: DialogMode) => {
    setDialogMode(mode);
    setBorrowAmount("");
    setCollateralAmount("");
    setActionAmount("");
  }, []);

  const closeDialog = useCallback(() => {
    setDialogMode(null);
    setBorrowAmount("");
    setCollateralAmount("");
    setActionAmount("");
  }, []);

  const handleBorrow = useCallback(async () => {
    if (!borrowAmount || !collateralAmount || Number(borrowAmount) <= 0 || Number(collateralAmount) <= 0) return;
    const result = await borrowAction({ amount: Number(borrowAmount), collateral: Number(collateralAmount) });
    if (result) {
      toast.success(result.message || "Loan created successfully");
      closeDialog();
      refetchLoans();
    }
  }, [borrowAmount, collateralAmount, borrowAction, closeDialog, refetchLoans]);

  const handleRepay = useCallback(async () => {
    if (!actionAmount || Number(actionAmount) <= 0) return;
    const result = await repayAction({ amount: Number(actionAmount) });
    if (result) {
      toast.success(result.message || "Repayment successful");
      closeDialog();
      refetchLoans();
    }
  }, [actionAmount, repayAction, closeDialog, refetchLoans]);

  const handleAddCollateral = useCallback(async () => {
    if (!actionAmount || Number(actionAmount) <= 0) return;
    const result = await addCollateralAction({ amount: Number(actionAmount) });
    if (result) {
      toast.success(result.message || "Collateral added");
      closeDialog();
      refetchLoans();
    }
  }, [actionAmount, addCollateralAction, closeDialog, refetchLoans]);

  const actionLoading = borrowLoading || repayLoading || addColLoading;
  const currentError = borrowError || repayError || addColError;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Lending</h1>
          <p className="text-sm text-muted-foreground">
            Borrow against collateral at 75% LTV — 5% annual interest
          </p>
        </div>
        {isAuthenticated && !hasUsdLoan && (
          <Button onClick={() => openDialog("borrow")} className="gap-2">
            <Plus className="size-4" />
            New Loan
          </Button>
        )}
      </div>

      <RuleBanner title="Lending Rules" rules={[
        "Max 65% LTV — borrow up to 65% of your collateral value",
        "2% daily interest — accrues every 30 minutes",
        "Liquidation at 80% LTV — 5% penalty burned on liquidation",
        "1h collateral seasoning — must hold collateral 1h before borrowing",
      ]} />

      {/* Protocol stats */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {statsLoading ? (
          <><CardSkeleton /><CardSkeleton /><CardSkeleton /><CardSkeleton /><CardSkeleton /></>
        ) : statsError ? (
          <Card className="sm:col-span-2 lg:col-span-5">
            <CardContent className="pt-6">
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{statsError}
              </div>
            </CardContent>
          </Card>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Active Loans</CardTitle>
                <Activity className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{fmtNum(stats?.active_loans)}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Total Borrowed</CardTitle>
                <ArrowDownToLine className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{fmt(stats?.total_borrowed)}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Total Collateral</CardTitle>
                <ArrowUpFromLine className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{fmt(stats?.total_collateral)}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Avg Collateral Ratio</CardTitle>
                <ShieldCheck className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {stats?.avg_collateral_ratio != null ? fmtPct(stats.avg_collateral_ratio) : "--%"}
                </div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* User loans */}
      {isAuthenticated && (
        <Card>
          <CardHeader className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <HandCoins className="size-4" />
              Your Loans
            </CardTitle>
            {hasUsdLoan && (
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => openDialog("add-collateral")}>
                  Add Collateral
                </Button>
                <Button size="sm" onClick={() => openDialog("repay")}>
                  Repay
                </Button>
              </div>
            )}
          </CardHeader>
          <CardContent>
            {loansLoading ? (
              <div className="space-y-3">
                {[0,1,2].map(i => <Skeleton key={i} className="h-16 w-full" />)}
              </div>
            ) : loansError ? (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{loansError}
              </div>
            ) : (
              <Tabs defaultValue="usd">
                <TabsList>
                  <TabsTrigger value="usd">USD Loans ({usdLoans.length})</TabsTrigger>
                </TabsList>

                <TabsContent value="usd">
                  {usdLoans.length > 0 ? (
                    <div className="space-y-3 pt-2">
                      {usdLoans.map((loan, i) => (
                        <div key={i} className="rounded-lg border border-border p-4 space-y-2">
                          <div className="flex items-center justify-between">
                            <span className="font-medium">USD Loan</span>
                            <Badge variant={loan.collateral_ratio >= 1.5 ? "secondary" : "destructive"}>
                              {fmtPct(loan.collateral_ratio * 100)} CR
                            </Badge>
                          </div>
                          <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground">
                            <div>
                              <span className="block text-xs">Principal</span>
                              <span className="text-foreground">{fmt(loan.principal)}</span>
                            </div>
                            <div>
                              <span className="block text-xs">Outstanding</span>
                              <span className="text-foreground">{fmt(loan.outstanding)}</span>
                            </div>
                            <div>
                              <span className="block text-xs">Collateral Locked</span>
                              <span className="text-foreground">{loan.collateral.toLocaleString()}</span>
                            </div>
                            <div>
                              <span className="block text-xs">Interest Rate</span>
                              <span className="text-foreground">
                                <Percent className="mr-1 inline size-3" />5% / yr
                              </span>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="py-4 space-y-3">
                      <p className="text-sm text-muted-foreground">No active USD loans.</p>
                      <Button onClick={() => openDialog("borrow")} className="gap-2">
                        <Plus className="size-4" />Take a Loan
                      </Button>
                    </div>
                  )}
                </TabsContent>

              </Tabs>
            )}
          </CardContent>
        </Card>
      )}

      {!isAuthenticated && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-border py-6">
          <LogIn className="size-4 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">Log in to view or create loans</p>
        </div>
      )}

      {/* Borrow Dialog */}
      <Dialog open={dialogMode === "borrow"} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Borrow USD</DialogTitle>
            <DialogDescription>
              Lock collateral at 1.33× minimum ratio (75% LTV). Liquidated at 90% LTV. 5% annual interest.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Borrow Amount (USD)</label>
              <Input
                type="number"
                placeholder="e.g. 1000"
                min="0"
                step="any"
                value={borrowAmount}
                onChange={(e) => {
                  setBorrowAmount(e.target.value);
                  // Auto-suggest min collateral (133%)
                  if (Number(e.target.value) > 0) {
                    setCollateralAmount((Number(e.target.value) * 1.33).toFixed(2));
                  }
                }}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Collateral (USD value locked from wallet)</label>
              <Input
                type="number"
                placeholder="e.g. 1330"
                min="0"
                step="any"
                value={collateralAmount}
                onChange={(e) => setCollateralAmount(e.target.value)}
              />
              {borrowAmount && collateralAmount && Number(collateralAmount) > 0 && Number(borrowAmount) > 0 && (
                <p className="text-xs text-muted-foreground">
                  Collateral ratio: {((Number(collateralAmount) / Number(borrowAmount)) * 100).toFixed(0)}%
                  {Number(collateralAmount) / Number(borrowAmount) < 1.33 && (
                    <span className="ml-1 text-destructive">(min 133%)</span>
                  )}
                </p>
              )}
            </div>
            {currentError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{currentError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>Cancel</Button>
            <Button
              disabled={actionLoading || !borrowAmount || !collateralAmount || Number(borrowAmount) <= 0 || Number(collateralAmount) <= 0}
              onClick={handleBorrow}
            >
              {actionLoading ? (
                <><Loader2 className="size-4 animate-spin" />Borrowing...</>
              ) : "Confirm Borrow"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Repay Dialog */}
      <Dialog open={dialogMode === "repay"} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Repay Loan</DialogTitle>
            <DialogDescription>
              Outstanding: {fmt(usdLoans[0]?.outstanding ?? 0)}. Partial repayments are accepted.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Repay Amount (USD)</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  placeholder="0.0"
                  min="0"
                  step="any"
                  value={actionAmount}
                  onChange={(e) => setActionAmount(e.target.value)}
                  className="flex-1"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setActionAmount(String(usdLoans[0]?.outstanding ?? 0))}
                >
                  Full
                </Button>
              </div>
            </div>
            {repayError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{repayError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>Cancel</Button>
            <Button
              disabled={repayLoading || !actionAmount || Number(actionAmount) <= 0}
              onClick={handleRepay}
            >
              {repayLoading ? (
                <><Loader2 className="size-4 animate-spin" />Repaying...</>
              ) : "Confirm Repay"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Add Collateral Dialog */}
      <Dialog open={dialogMode === "add-collateral"} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Collateral</DialogTitle>
            <DialogDescription>
              Current collateral: {(usdLoans[0]?.collateral ?? 0).toLocaleString()} USD.
              Adding more collateral lowers your liquidation risk.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Additional Collateral (USD)</label>
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={actionAmount}
                onChange={(e) => setActionAmount(e.target.value)}
              />
            </div>
            {addColError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{addColError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>Cancel</Button>
            <Button
              disabled={addColLoading || !actionAmount || Number(actionAmount) <= 0}
              onClick={handleAddCollateral}
            >
              {addColLoading ? (
                <><Loader2 className="size-4 animate-spin" />Adding...</>
              ) : "Add Collateral"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
