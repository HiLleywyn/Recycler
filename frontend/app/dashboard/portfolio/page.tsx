"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SortableTable, type ColumnDef } from "@/components/ui/sortable-table";
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
  Wallet,
  PieChart,
  History,
  Landmark,
  Pickaxe,
  Lock,
  Droplets,
  HandCoins,
  PiggyBank,
  LogIn,
  TrendingUp,
  ArrowLeftRight,
  Send,
  ArrowDownToLine,
  ArrowUpFromLine,
  Loader2,
  AlertCircle,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";

interface Holding {
  symbol: string;
  cefi_amount: number;
  defi_amount: number;
  total_amount: number;
  price: number;
  value: number;
}

interface NetWorthBreakdown {
  total: number;
  cefi: number;
  defi: number;
  staking: number;
  pos_staking: number;
  lp: number;
  mining_rigs: number;
  delegations: number;
  savings: number;
}

interface Transaction {
  tx_hash: string;
  type: string;
  symbol: string;
  amount: number;
  fee: number;
  timestamp: string;
  block_num: number;
}

interface StakeItem {
  validator_id: string;
  validator_name: string;
  symbol: string;
  amount: number;
  value_usd: number;
  apy: number;
  staked_at?: string;
}

interface LPItem {
  pool_id: string;
  token_a: string;
  token_b: string;
  lp_shares: number;
  value_usd: number;
  share_pct: number;
  added_at?: string;
}

interface SavingsItem {
  asset: string;
  amount: number;
  interest_earned: number;
  apy: number;
  deposited_at?: string;
}

interface LoanItem {
  loan_id: string;
  loan_type: "usd" | "sun";
  principal: number;
  outstanding: number;
  collateral: number;
  interest_rate: number;
  created_at?: string;
}

interface BankBalance {
  usd_balance: number;
  recent_transactions: BankTransaction[];
}

interface BankTransaction {
  id: string;
  type: string;
  amount: number;
  timestamp: string;
  description: string;
}

interface TransferResult {
  success: boolean;
  message: string;
}

type TransferDirection = "cefi-to-defi" | "defi-to-cefi";
type TransferDialogState = {
  symbol: string;
  direction: TransferDirection;
} | null;

type SendDialogState = {
  symbol: string;
} | null;

type BankDialogMode = "deposit" | "withdraw";
type BankDialogState = {
  mode: BankDialogMode;
} | null;

type MoveAllDialogState = {
  symbol: string;
  amount: number;
  price: number;
  direction: TransferDirection;
} | null;

function fmt(n: number | undefined | null, decimals = 2): string {
  const v = n ?? 0;
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtAmount(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

function fmtDate(ts?: string | null): string {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function SkeletonTable({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-3 pt-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
      <p className="text-sm text-muted-foreground">{text}</p>
    </div>
  );
}

export default function PortfolioPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data: holdings, loading: holdingsLoading, error: holdingsError, refetch: refetchHoldings } = useApi<Holding[]>(
    isAuthenticated ? "/portfolio/holdings" : null
  );
  const { data: netWorth, loading: netWorthLoading } = useApi<NetWorthBreakdown>(
    isAuthenticated ? "/portfolio/net-worth" : null
  );
  const { data: txHistory, loading: txLoading, error: txError } = useApi<Transaction[]>(
    isAuthenticated ? "/portfolio/tx-history?limit=25" : null
  );
  const { data: stakes, loading: stakesLoading } = useApi<StakeItem[]>(
    isAuthenticated ? "/portfolio/stakes" : null
  );
  const { data: lpPositions, loading: lpLoading } = useApi<LPItem[]>(
    isAuthenticated ? "/portfolio/lp-positions" : null
  );
  const { data: savings, loading: savingsLoading } = useApi<SavingsItem[]>(
    isAuthenticated ? "/portfolio/savings" : null
  );
  const { data: loans, loading: loansLoading } = useApi<LoanItem[]>(
    isAuthenticated ? "/portfolio/loans" : null
  );
  const { data: bankData, loading: bankLoading, refetch: refetchBank } = useApi<BankBalance>(
    isAuthenticated ? "/portfolio/bank" : null
  );

  // Transfer mutations
  const { mutate: cefiToDefi, loading: cefiToDefiLoading, error: cefiToDefiError } =
    useApiMutation<TransferResult>("/trading/cefi-to-defi");
  const { mutate: defiToCefi, loading: defiToCefiLoading, error: defiToCefiError } =
    useApiMutation<TransferResult>("/trading/defi-to-cefi");
  const { mutate: sendTransfer, loading: sendLoading, error: sendError } =
    useApiMutation<TransferResult>("/trading/transfer");
  const { mutate: bankDeposit, loading: bankDepositLoading } =
    useApiMutation<TransferResult>("/trading/bank-deposit");
  const { mutate: bankWithdraw, loading: bankWithdrawLoading } =
    useApiMutation<TransferResult>("/trading/bank-withdraw");

  // Dialog states
  const [transferDialog, setTransferDialog] = useState<TransferDialogState>(null);
  const [transferAmount, setTransferAmount] = useState("");
  const [transferNetwork, setTransferNetwork] = useState("sun");

  const [sendDialog, setSendDialog] = useState<SendDialogState>(null);
  const [sendAmount, setSendAmount] = useState("");
  const [sendRecipient, setSendRecipient] = useState("");

  const [bankDialog, setBankDialog] = useState<BankDialogState>(null);
  const [bankAmount, setBankAmount] = useState("");

  const [moveAllDialog, setMoveAllDialog] = useState<MoveAllDialogState>(null);

  const [holdingsSubTab, setHoldingsSubTab] = useState("all");

  const closeTransfer = useCallback(() => {
    setTransferDialog(null);
    setTransferAmount("");
    setTransferNetwork("sun");
  }, []);

  const closeSend = useCallback(() => {
    setSendDialog(null);
    setSendAmount("");
    setSendRecipient("");
  }, []);

  const closeBankDialog = useCallback(() => {
    setBankDialog(null);
    setBankAmount("");
  }, []);

  const closeMoveAllDialog = useCallback(() => {
    setMoveAllDialog(null);
  }, []);

  const handleTransfer = useCallback(async () => {
    if (!transferDialog || !transferAmount || Number(transferAmount) <= 0) return;
    const action = transferDialog.direction === "cefi-to-defi" ? cefiToDefi : defiToCefi;
    const result = await action({
      symbol: transferDialog.symbol,
      amount: Number(transferAmount),
      network: transferNetwork,
    });
    if (result) {
      toast.success(result.message || "Transfer successful");
      closeTransfer();
      refetchHoldings();
    }
  }, [transferDialog, transferAmount, transferNetwork, cefiToDefi, defiToCefi, closeTransfer, refetchHoldings]);

  const handleSend = useCallback(async () => {
    if (!sendDialog || !sendAmount || !sendRecipient || Number(sendAmount) <= 0) return;
    const result = await sendTransfer({
      symbol: sendDialog.symbol,
      amount: Number(sendAmount),
      recipient: sendRecipient,
    });
    if (result) {
      toast.success(result.message || "Transfer sent");
      closeSend();
      refetchHoldings();
    }
  }, [sendDialog, sendAmount, sendRecipient, sendTransfer, closeSend, refetchHoldings]);

  const handleBankAction = useCallback(async () => {
    if (!bankDialog || !bankAmount || Number(bankAmount) <= 0) return;
    const action = bankDialog.mode === "deposit" ? bankDeposit : bankWithdraw;
    const result = await action({ amount: Number(bankAmount) });
    if (result) {
      toast.success(result.message || (bankDialog.mode === "deposit" ? "Deposited to bank" : "Withdrawn from bank"));
      closeBankDialog();
      refetchBank();
      refetchHoldings();
    }
  }, [bankDialog, bankAmount, bankDeposit, bankWithdraw, closeBankDialog, refetchBank, refetchHoldings]);

  const transferLoading = cefiToDefiLoading || defiToCefiLoading;
  const transferError = cefiToDefiError || defiToCefiError;
  const handleMoveAll = useCallback(async () => {
    if (!moveAllDialog) return;
    const action = moveAllDialog.direction === "cefi-to-defi" ? cefiToDefi : defiToCefi;
    const result = await action({
      symbol: moveAllDialog.symbol,
      amount: moveAllDialog.amount,
      network: "sun",
    });
    if (result) {
      toast.success(result.message || "Transfer successful");
      closeMoveAllDialog();
      refetchHoldings();
    }
  }, [moveAllDialog, cefiToDefi, defiToCefi, closeMoveAllDialog, refetchHoldings]);

  const bankActionLoading = bankDepositLoading || bankWithdrawLoading;

  if (!isAuthenticated) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Portfolio</h1>
          <p className="text-sm text-muted-foreground">Track your holdings and performance</p>
        </div>
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-3 py-16">
            <LogIn className="size-10 text-muted-foreground" />
            <p className="text-lg font-medium">Log in to view portfolio</p>
            <p className="text-sm text-muted-foreground">
              Sign in with Discord to see your holdings, balances, and transaction history.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  const breakdownCards: { label: string; key: keyof NetWorthBreakdown; icon: React.ElementType }[] = [
    { label: "Total", key: "total", icon: Wallet },
    { label: "CeFi", key: "cefi", icon: Landmark },
    { label: "DeFi", key: "defi", icon: PieChart },
    { label: "Staking", key: "staking", icon: Lock },
    { label: "LP", key: "lp", icon: Droplets },
    { label: "Mining Rigs", key: "mining_rigs", icon: Pickaxe },
    { label: "Savings", key: "savings", icon: PiggyBank },
    { label: "Delegations", key: "delegations", icon: HandCoins },
  ];

  const cefiHoldings = holdings?.filter((h) => h.cefi_amount > 0) ?? [];
  const defiHoldings = holdings?.filter((h) => h.defi_amount > 0) ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Portfolio</h1>
        <p className="text-sm text-muted-foreground">Track your holdings and performance</p>
      </div>

      {/* Net worth breakdown */}
      <div className="grid gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
        {netWorthLoading
          ? Array.from({ length: 4 }).map((_, i) => (
              <Card key={i}>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="size-4" />
                </CardHeader>
                <CardContent><Skeleton className="h-7 w-24" /></CardContent>
              </Card>
            ))
          : breakdownCards.map(({ label, key, icon: Icon }) => (
              <Card key={key}>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
                  <Icon className="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className={`font-bold ${key === "total" ? "text-2xl" : "text-lg"}`}>
                    {fmt(netWorth?.[key])}
                  </div>
                </CardContent>
              </Card>
            ))}
      </div>

      <Tabs defaultValue="holdings">
        <TabsList className="flex-wrap">
          <TabsTrigger value="holdings">Holdings</TabsTrigger>
          <TabsTrigger value="bank">Bank</TabsTrigger>
          <TabsTrigger value="stakes">Stakes</TabsTrigger>
          <TabsTrigger value="lp">LP Positions</TabsTrigger>
          <TabsTrigger value="savings">Savings</TabsTrigger>
          <TabsTrigger value="loans">Loans</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>

        {/* Holdings with CeFi / DeFi sub-tabs */}
        <TabsContent value="holdings">
          <Card>
            <CardContent className="pt-6">
              <div className="mb-4 flex gap-2">
                <Button
                  size="sm"
                  variant={holdingsSubTab === "all" ? "default" : "outline"}
                  onClick={() => setHoldingsSubTab("all")}
                >
                  All Holdings
                </Button>
                <Button
                  size="sm"
                  variant={holdingsSubTab === "cefi" ? "default" : "outline"}
                  onClick={() => setHoldingsSubTab("cefi")}
                >
                  CeFi Holdings
                </Button>
                <Button
                  size="sm"
                  variant={holdingsSubTab === "defi" ? "default" : "outline"}
                  onClick={() => setHoldingsSubTab("defi")}
                >
                  DeFi Holdings
                </Button>
              </div>

              {holdingsLoading ? (
                <SkeletonTable />
              ) : holdingsError ? (
                <EmptyState text="Unable to load holdings" />
              ) : (
                <>
                  {/* All Holdings */}
                  {holdingsSubTab === "all" && (
                    holdings && holdings.length > 0 ? (
                      <SortableTable<Record<string, unknown>>
                        columns={[
                          { key: "symbol", label: "Token", sortable: true, render: (row) => <span className="font-medium">{row.symbol as string}</span> },
                          { key: "cefi_amount", label: "CeFi", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.cefi_amount as number)}</span> },
                          { key: "defi_amount", label: "DeFi", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.defi_amount as number)}</span> },
                          { key: "price", label: "Price", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.price as number)}</span> },
                          { key: "value", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm font-semibold">{fmt(row.value as number)}</span> },
                          { key: "_actions", label: "Actions", sortable: false, className: "text-right", render: (row) => {
                            const h = row as unknown as Holding;
                            return (
                              <div className="flex justify-end gap-1">
                                {h.cefi_amount > 0 && (
                                  <>
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1 text-xs"
                                      onClick={() => setTransferDialog({ symbol: h.symbol, direction: "cefi-to-defi" })}
                                    >
                                      <ArrowLeftRight className="size-3" />CeFi&rarr;DeFi
                                    </Button>
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1 text-xs"
                                      onClick={() => setMoveAllDialog({ symbol: h.symbol, amount: h.cefi_amount, price: h.price, direction: "cefi-to-defi" })}
                                    >
                                      Move All
                                    </Button>
                                  </>
                                )}
                                {h.defi_amount > 0 && (
                                  <>
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1 text-xs"
                                      onClick={() => setTransferDialog({ symbol: h.symbol, direction: "defi-to-cefi" })}
                                    >
                                      <ArrowLeftRight className="size-3" />DeFi&rarr;CeFi
                                    </Button>
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1 text-xs"
                                      onClick={() => setMoveAllDialog({ symbol: h.symbol, amount: h.defi_amount, price: h.price, direction: "defi-to-cefi" })}
                                    >
                                      Move All
                                    </Button>
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1 text-xs"
                                      onClick={() => setSendDialog({ symbol: h.symbol })}
                                    >
                                      <Send className="size-3" />Send
                                    </Button>
                                  </>
                                )}
                              </div>
                            );
                          }},
                        ] satisfies ColumnDef<Record<string, unknown>>[]}
                        data={holdings as unknown as Record<string, unknown>[]}
                        defaultSort={{ key: "value", dir: "desc" }}
                        emptyMessage="No holdings found"
                      />
                    ) : (
                      <EmptyState text="No holdings found" />
                    )
                  )}

                  {/* CeFi Holdings */}
                  {holdingsSubTab === "cefi" && (
                    cefiHoldings.length > 0 ? (
                      <SortableTable<Record<string, unknown>>
                        columns={[
                          { key: "symbol", label: "Token", sortable: true, render: (row) => <span className="font-medium">{row.symbol as string}</span> },
                          { key: "cefi_amount", label: "CeFi Balance", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.cefi_amount as number)}</span> },
                          { key: "price", label: "Price", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.price as number)}</span> },
                          { key: "_value", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm font-semibold">{fmt((row.cefi_amount as number) * (row.price as number))}</span> },
                          { key: "_actions", label: "Actions", sortable: false, className: "text-right", render: (row) => (
                            <div className="flex justify-end gap-1">
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1 text-xs"
                                onClick={() => setTransferDialog({ symbol: row.symbol as string, direction: "cefi-to-defi" })}
                              >
                                <ArrowLeftRight className="size-3" />Transfer to DeFi
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1 text-xs"
                                onClick={() => setMoveAllDialog({ symbol: row.symbol as string, amount: row.cefi_amount as number, price: row.price as number, direction: "cefi-to-defi" })}
                              >
                                Move All
                              </Button>
                            </div>
                          )},
                        ] satisfies ColumnDef<Record<string, unknown>>[]}
                        data={cefiHoldings as unknown as Record<string, unknown>[]}
                        defaultSort={{ key: "cefi_amount", dir: "desc" }}
                        emptyMessage="No CeFi holdings"
                      />
                    ) : (
                      <EmptyState text="No CeFi holdings" />
                    )
                  )}

                  {/* DeFi Holdings */}
                  {holdingsSubTab === "defi" && (
                    defiHoldings.length > 0 ? (
                      <SortableTable<Record<string, unknown>>
                        columns={[
                          { key: "symbol", label: "Token", sortable: true, render: (row) => <span className="font-medium">{row.symbol as string}</span> },
                          { key: "defi_amount", label: "DeFi Balance", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.defi_amount as number)}</span> },
                          { key: "price", label: "Price", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.price as number)}</span> },
                          { key: "_value", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm font-semibold">{fmt((row.defi_amount as number) * (row.price as number))}</span> },
                          { key: "_actions", label: "Actions", sortable: false, className: "text-right", render: (row) => (
                            <div className="flex justify-end gap-1">
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1 text-xs"
                                onClick={() => setTransferDialog({ symbol: row.symbol as string, direction: "defi-to-cefi" })}
                              >
                                <ArrowLeftRight className="size-3" />Transfer to CeFi
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1 text-xs"
                                onClick={() => setMoveAllDialog({ symbol: row.symbol as string, amount: row.defi_amount as number, price: row.price as number, direction: "defi-to-cefi" })}
                              >
                                Move All
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                className="gap-1 text-xs"
                                onClick={() => setSendDialog({ symbol: row.symbol as string })}
                              >
                                <Send className="size-3" />Send
                              </Button>
                            </div>
                          )},
                        ] satisfies ColumnDef<Record<string, unknown>>[]}
                        data={defiHoldings as unknown as Record<string, unknown>[]}
                        defaultSort={{ key: "defi_amount", dir: "desc" }}
                        emptyMessage="No DeFi holdings"
                      />
                    ) : (
                      <EmptyState text="No DeFi holdings" />
                    )
                  )}
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Bank Tab */}
        <TabsContent value="bank">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2">
                  <Landmark className="size-4" />
                  Bank Account
                </CardTitle>
                <div className="flex gap-2">
                  <Button size="sm" className="gap-1" onClick={() => setBankDialog({ mode: "deposit" })}>
                    <ArrowDownToLine className="size-3" />Deposit
                  </Button>
                  <Button size="sm" variant="outline" className="gap-1" onClick={() => setBankDialog({ mode: "withdraw" })}>
                    <ArrowUpFromLine className="size-3" />Withdraw
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {bankLoading ? (
                <SkeletonTable rows={3} />
              ) : (
                <div className="space-y-6">
                  <div className="rounded-lg bg-muted/50 p-6 text-center">
                    <p className="text-sm text-muted-foreground">Bank Balance</p>
                    <p className="text-3xl font-bold">{fmt(bankData?.usd_balance)}</p>
                  </div>

                  {bankData?.recent_transactions && bankData.recent_transactions.length > 0 ? (
                    <>
                      <h3 className="text-sm font-semibold">Recent Bank Transactions</h3>
                      <SortableTable<Record<string, unknown>>
                        columns={[
                          { key: "type", label: "Type", render: (row) => <Badge variant="secondary">{row.type as string}</Badge> },
                          { key: "description", label: "Description", render: (row) => <span className="text-sm text-muted-foreground">{row.description as string}</span> },
                          { key: "amount", label: "Amount", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.amount as number)}</span> },
                          { key: "timestamp", label: "Time", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.timestamp as string)}</span> },
                        ] satisfies ColumnDef<Record<string, unknown>>[]}
                        data={bankData.recent_transactions as unknown as Record<string, unknown>[]}
                        emptyMessage="No recent bank transactions"
                      />
                    </>
                  ) : (
                    <EmptyState text="No recent bank transactions" />
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Stakes */}
        <TabsContent value="stakes">
          <Card>
            <CardContent className="pt-6">
              {stakesLoading ? (
                <SkeletonTable />
              ) : stakes && stakes.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "validator_name", label: "Validator", sortable: true, render: (row) => <span className="font-medium">{(row.validator_name as string) || (row.validator_id as string)}</span> },
                    { key: "symbol", label: "Token", render: (row) => <Badge variant="secondary">{row.symbol as string}</Badge> },
                    { key: "amount", label: "Amount", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.amount as number)}</span> },
                    { key: "value_usd", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.value_usd as number)}</span> },
                    { key: "apy", label: "APY", sortable: true, className: "text-right", render: (row) => <span className="text-chart-green">{(row.apy as number).toFixed(1)}%</span> },
                    { key: "staked_at", label: "Since", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.staked_at as string)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={stakes as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "value_usd", dir: "desc" }}
                  emptyMessage="No active stakes"
                />
              ) : (
                <EmptyState text="No active stakes" />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* LP Positions */}
        <TabsContent value="lp">
          <Card>
            <CardContent className="pt-6">
              {lpLoading ? (
                <SkeletonTable />
              ) : lpPositions && lpPositions.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "pool_id", label: "Pair", render: (row) => <span className="font-medium">{row.token_a as string} / {row.token_b as string}</span> },
                    { key: "lp_shares", label: "LP Shares", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{(row.lp_shares as number).toFixed(6)}</span> },
                    { key: "value_usd", label: "Value", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.value_usd as number)}</span> },
                    { key: "share_pct", label: "Pool Share", sortable: true, className: "text-right", render: (row) => <Badge variant="secondary">{((row.share_pct as number) ?? 0).toFixed(2)}%</Badge> },
                    { key: "added_at", label: "Since", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.added_at as string)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={lpPositions as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "value_usd", dir: "desc" }}
                  emptyMessage="No LP positions"
                />
              ) : (
                <EmptyState text="No LP positions" />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Savings */}
        <TabsContent value="savings">
          <Card>
            <CardContent className="pt-6">
              {savingsLoading ? (
                <SkeletonTable />
              ) : savings && savings.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "asset", label: "Asset", render: (row) => <Badge variant="secondary">{row.asset as string}</Badge> },
                    { key: "amount", label: "Amount", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.amount as number)}</span> },
                    { key: "interest_earned", label: "Interest Earned", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm text-chart-green">+{fmtAmount(row.interest_earned as number)}</span> },
                    { key: "apy", label: "APY", sortable: true, className: "text-right", render: (row) => <span>{(row.apy as number).toFixed(1)}%</span> },
                    { key: "deposited_at", label: "Since", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.deposited_at as string)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={savings as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "amount", dir: "desc" }}
                  emptyMessage="No savings deposits"
                />
              ) : (
                <EmptyState text="No savings deposits" />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Loans */}
        <TabsContent value="loans">
          <Card>
            <CardContent className="pt-6">
              {loansLoading ? (
                <SkeletonTable />
              ) : loans && loans.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "loan_type", label: "Type", render: (row) => <Badge variant={(row.loan_type as string) === "usd" ? "secondary" : "outline"}>{(row.loan_type as string).toUpperCase()}</Badge> },
                    { key: "principal", label: "Principal", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.principal as number)}</span> },
                    { key: "outstanding", label: "Outstanding", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmt(row.outstanding as number)}</span> },
                    { key: "collateral", label: "Collateral", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.collateral as number)}</span> },
                    { key: "interest_rate", label: "Rate", sortable: true, className: "text-right", render: (row) => <span>{(row.interest_rate as number).toFixed(1)}%</span> },
                    { key: "created_at", label: "Since", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.created_at as string)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={loans as unknown as Record<string, unknown>[]}
                  defaultSort={{ key: "outstanding", dir: "desc" }}
                  emptyMessage="No active loans"
                />
              ) : (
                <EmptyState text="No active loans" />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* History */}
        <TabsContent value="history">
          <Card>
            <CardContent className="pt-6">
              {txLoading ? (
                <SkeletonTable />
              ) : txError ? (
                <EmptyState text="Unable to load transaction history" />
              ) : txHistory && txHistory.length > 0 ? (
                <SortableTable<Record<string, unknown>>
                  columns={[
                    { key: "type", label: "Type", render: (row) => <span className="rounded bg-muted px-1.5 py-0.5 text-xs font-medium uppercase">{row.type as string}</span> },
                    { key: "symbol", label: "Token", render: (row) => <span className="font-medium">{row.symbol as string}</span> },
                    { key: "amount", label: "Amount", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm">{fmtAmount(row.amount as number)}</span> },
                    { key: "fee", label: "Fee", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-sm text-muted-foreground">{fmtAmount(row.fee as number)}</span> },
                    { key: "block_num", label: "Block", sortable: true, className: "text-right", render: (row) => <span className="font-mono text-xs text-muted-foreground">#{row.block_num as number}</span> },
                    { key: "timestamp", label: "Time", className: "text-right", render: (row) => <span className="text-xs text-muted-foreground">{fmtDate(row.timestamp as string)}</span> },
                  ] satisfies ColumnDef<Record<string, unknown>>[]}
                  data={txHistory as unknown as Record<string, unknown>[]}
                  searchable
                  searchPlaceholder="Search transactions..."
                  emptyMessage="No transactions found"
                />
              ) : (
                <EmptyState text="No transactions found" />
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Transfer Dialog */}
      <Dialog open={!!transferDialog} onOpenChange={(open) => !open && closeTransfer()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Transfer {transferDialog?.symbol} — {transferDialog?.direction === "cefi-to-defi" ? "CeFi to DeFi" : "DeFi to CeFi"}
            </DialogTitle>
            <DialogDescription>
              Move tokens between your CeFi and DeFi wallets.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Direction</label>
              <Select
                value={transferDialog?.direction ?? "cefi-to-defi"}
                onValueChange={(v) =>
                  setTransferDialog((prev) => prev ? { ...prev, direction: v as TransferDirection } : prev)
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cefi-to-defi">CeFi &rarr; DeFi</SelectItem>
                  <SelectItem value="defi-to-cefi">DeFi &rarr; CeFi</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Amount</label>
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={transferAmount}
                onChange={(e) => setTransferAmount(e.target.value)}
              />
            </div>
            {transferDialog?.direction === "cefi-to-defi" && (
              <div className="space-y-2">
                <label className="text-sm font-medium">Network</label>
                <Select value={transferNetwork} onValueChange={(v) => setTransferNetwork(v ?? "")}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="sun">SUN Network</SelectItem>
                    <SelectItem value="mta">MTA Network</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
            {transferError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{transferError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeTransfer}>Cancel</Button>
            <Button
              disabled={transferLoading || !transferAmount || Number(transferAmount) <= 0}
              onClick={handleTransfer}
            >
              {transferLoading ? (
                <><Loader2 className="size-4 animate-spin" />Transferring...</>
              ) : (
                <><ArrowLeftRight className="size-4" />Transfer</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Send Dialog */}
      <Dialog open={!!sendDialog} onOpenChange={(open) => !open && closeSend()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Send {sendDialog?.symbol}</DialogTitle>
            <DialogDescription>
              Send tokens from your DeFi wallet to another user.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Recipient (Username or ID)</label>
              <Input
                placeholder="username or user ID"
                value={sendRecipient}
                onChange={(e) => setSendRecipient(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Amount</label>
              <Input
                type="number"
                placeholder="0.0"
                min="0"
                step="any"
                value={sendAmount}
                onChange={(e) => setSendAmount(e.target.value)}
              />
            </div>
            {sendError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{sendError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeSend}>Cancel</Button>
            <Button
              disabled={sendLoading || !sendAmount || !sendRecipient || Number(sendAmount) <= 0}
              onClick={handleSend}
            >
              {sendLoading ? (
                <><Loader2 className="size-4 animate-spin" />Sending...</>
              ) : (
                <><Send className="size-4" />Send</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Move All Confirmation Dialog */}
      <Dialog open={!!moveAllDialog} onOpenChange={(open) => !open && closeMoveAllDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Move All {moveAllDialog?.symbol}
            </DialogTitle>
            <DialogDescription>
              Transfer your entire {moveAllDialog?.symbol} balance from{" "}
              {moveAllDialog?.direction === "cefi-to-defi" ? "CeFi to your DeFi wallet" : "your DeFi wallet to CeFi"}.
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-lg border bg-muted/40 p-4 space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Token</span>
              <span className="font-medium">{moveAllDialog?.symbol}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Amount</span>
              <span className="font-mono font-medium">{fmtAmount(moveAllDialog?.amount)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Value</span>
              <span className="font-mono">{fmt((moveAllDialog?.amount ?? 0) * (moveAllDialog?.price ?? 0))}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">From</span>
              <span>{moveAllDialog?.direction === "cefi-to-defi" ? "CeFi (Bank)" : "DeFi Wallet"}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">To</span>
              <span>{moveAllDialog?.direction === "cefi-to-defi" ? "DeFi Wallet" : "CeFi (Bank)"}</span>
            </div>
          </div>
          {transferError && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" />{transferError}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={closeMoveAllDialog}>Cancel</Button>
            <Button
              disabled={transferLoading || !moveAllDialog || moveAllDialog.amount <= 0}
              onClick={handleMoveAll}
            >
              {transferLoading ? (
                <><Loader2 className="size-4 animate-spin" />Moving...</>
              ) : (
                <><ArrowLeftRight className="size-4" />Move All {moveAllDialog?.symbol}</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Bank Deposit/Withdraw Dialog */}
      <Dialog open={!!bankDialog} onOpenChange={(open) => !open && closeBankDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {bankDialog?.mode === "deposit" ? "Deposit to Bank" : "Withdraw from Bank"}
            </DialogTitle>
            <DialogDescription>
              {bankDialog?.mode === "deposit"
                ? "Move USD from your wallet into the bank."
                : `Bank balance: ${fmt(bankData?.usd_balance)}`}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Amount (USD)</label>
              <div className="flex gap-2">
                <Input
                  type="number"
                  placeholder="0.00"
                  min="0"
                  step="any"
                  value={bankAmount}
                  onChange={(e) => setBankAmount(e.target.value)}
                  className="flex-1"
                />
                {bankDialog?.mode === "withdraw" && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setBankAmount(String(bankData?.usd_balance ?? 0))}
                  >
                    Max
                  </Button>
                )}
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeBankDialog}>Cancel</Button>
            <Button
              disabled={bankActionLoading || !bankAmount || Number(bankAmount) <= 0}
              onClick={handleBankAction}
            >
              {bankActionLoading ? (
                <><Loader2 className="size-4 animate-spin" />{bankDialog?.mode === "deposit" ? "Depositing..." : "Withdrawing..."}</>
              ) : bankDialog?.mode === "deposit" ? (
                <><ArrowDownToLine className="size-4" />Deposit</>
              ) : (
                <><ArrowUpFromLine className="size-4" />Withdraw</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
