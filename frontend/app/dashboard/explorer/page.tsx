"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
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
  Search,
  Blocks,
  ArrowRight,
  ArrowRightLeft,
  Database,
  Users,
  Inbox,
  Hash,
  AlertCircle,
  X,
} from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { UserLink } from "@/components/ui/user-link";
import { fmt } from "@/lib/format";

interface ExplorerSummary {
  total_blocks: number;
  total_transactions: number;
  total_addresses: number;
  networks: string[];
  mempool_size: number;
}

interface Block {
  block_num: number;
  network: string;
  status: string;
  tx_count: number;
  block_hash: string;
  miner_id: number | null;
  ts: string;
}

interface Transaction {
  tx_hash: string;
  tx_type: string;
  symbol_in: string;
  amount_in: number;
  symbol_out: string;
  amount_out: number;
  gas_fee: number;
  block_num: number;
  ts: string;
}

interface SearchResult {
  type: "block" | "transaction" | "error";
  block?: Block;
  transaction?: Transaction;
  message?: string;
}

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function truncateHash(hash: string): string {
  if (hash.length <= 14) return hash;
  return `${hash.slice(0, 8)}...${hash.slice(-6)}`;
}

function formatNumber(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

export default function ExplorerPage() {
  const token = useAuthStore((s) => s.token);
  const { data: summary, loading: summaryLoading, error: summaryError } =
    useApi<ExplorerSummary>("/blockchain/explorer-summary");
  const { data: blocks, loading: blocksLoading, error: blocksError } =
    useApi<Block[]>("/blockchain/blocks?limit=10");
  const { data: transactions, loading: txLoading, error: txError } =
    useApi<Transaction[]>("/blockchain/transactions?limit=10");

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);

  const handleSearch = useCallback(async () => {
    const q = searchQuery.trim();
    if (!q) return;
    setSearchLoading(true);
    setSearchResult(null);

    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;

    try {
      // Block number?
      if (/^\d+$/.test(q)) {
        const res = await fetch(`/api/v2/blockchain/blocks/${q}`, { headers });
        if (res.ok) {
          const block: Block = await res.json();
          setSearchResult({ type: "block", block });
          return;
        }
      }
      // Hash (tx or block hash)?
      const res = await fetch(
        `/api/v2/blockchain/transactions/${encodeURIComponent(q)}`,
        { headers }
      );
      if (res.ok) {
        const tx: Transaction = await res.json();
        setSearchResult({ type: "transaction", transaction: tx });
        return;
      }
      setSearchResult({ type: "error", message: `No result found for "${q}"` });
    } catch {
      setSearchResult({ type: "error", message: "Search failed. Please try again." });
    } finally {
      setSearchLoading(false);
    }
  }, [searchQuery, token]);

  const clearSearch = useCallback(() => {
    setSearchQuery("");
    setSearchResult(null);
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Explorer</h1>
        <p className="text-sm text-muted-foreground">
          Browse transactions, blocks, and addresses
        </p>
      </div>

      {/* Search bar */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Search by block number, transaction hash..."
                className="pl-9"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              />
              {searchQuery && (
                <button
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  onClick={clearSearch}
                >
                  <X className="size-4" />
                </button>
              )}
            </div>
            <Button className="gap-2" onClick={handleSearch} disabled={searchLoading || !searchQuery.trim()}>
              {searchLoading ? "Searching..." : <><Search className="size-4" />Search</>}
            </Button>
          </div>

          {/* Search results */}
          {searchResult && (
            <div className="mt-4 rounded-lg border border-border p-4">
              {searchResult.type === "error" ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <AlertCircle className="size-4" />{searchResult.message}
                </div>
              ) : searchResult.type === "block" && searchResult.block ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <Blocks className="size-4" />
                    <span className="font-semibold">Block #{formatNumber(searchResult.block.block_num)}</span>
                    <Badge variant="secondary">{searchResult.block.network}</Badge>
                    <Badge variant={searchResult.block.status === "confirmed" ? "secondary" : "outline"}>
                      {searchResult.block.status}
                    </Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground sm:grid-cols-4">
                    <div><span className="block text-xs">Transactions</span><span className="text-foreground">{searchResult.block.tx_count}</span></div>
                    <div><span className="block text-xs">Miner</span><span className="text-foreground font-mono">{searchResult.block.miner_id != null ? <UserLink userId={String(searchResult.block.miner_id)} /> : "--"}</span></div>
                    <div><span className="block text-xs">Time</span><span className="text-foreground">{timeAgo(searchResult.block.ts)}</span></div>
                    <div><span className="block text-xs">Hash</span><span className="text-foreground font-mono text-xs" title={searchResult.block.block_hash}>{truncateHash(searchResult.block.block_hash)}</span></div>
                  </div>
                </div>
              ) : searchResult.type === "transaction" && searchResult.transaction ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <Hash className="size-4" />
                    <span className="font-semibold font-mono text-sm">{truncateHash(searchResult.transaction.tx_hash)}</span>
                    <Badge variant="outline">{searchResult.transaction.tx_type}</Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground sm:grid-cols-4">
                    <div>
                      <span className="block text-xs">Amount</span>
                      <span className="text-foreground">{searchResult.transaction.amount_in} {searchResult.transaction.symbol_in}</span>
                    </div>
                    {searchResult.transaction.symbol_out && (
                      <div>
                        <span className="block text-xs">Output</span>
                        <span className="text-foreground">{searchResult.transaction.amount_out} {searchResult.transaction.symbol_out}</span>
                      </div>
                    )}
                    <div><span className="block text-xs">Gas Fee</span><span className="text-foreground">{fmt(searchResult.transaction.gas_fee, 4)}</span></div>
                    <div><span className="block text-xs">Block</span><span className="text-foreground">#{searchResult.transaction.block_num}</span></div>
                    <div><span className="block text-xs">Time</span><span className="text-foreground">{timeAgo(searchResult.transaction.ts)}</span></div>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {[
          { label: "Total Blocks", key: "total_blocks", icon: Blocks },
          { label: "Transactions", key: "total_transactions", icon: ArrowRightLeft },
          { label: "Addresses", key: "total_addresses", icon: Users },
          { label: "Networks", key: "networks_count", icon: Database },
          { label: "Mempool", key: "mempool_size", icon: Inbox },
        ].map(({ label, key, icon: Icon }) => (
          <Card key={key}>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
              <Icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {summaryLoading ? (
                <Skeleton className="h-8 w-20" />
              ) : summaryError ? (
                <div className="text-sm text-destructive">--</div>
              ) : (
                <div className="text-2xl font-bold">
                  {key === "networks_count"
                    ? formatNumber(summary?.networks?.length ?? 0)
                    : formatNumber(summary?.[key as keyof ExplorerSummary] as number ?? 0)}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Recent Blocks */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Blocks className="size-4" />
            Recent Blocks
          </CardTitle>
        </CardHeader>
        <CardContent>
          {blocksLoading ? (
            <div className="space-y-3">
              {[0,1,2,3,4].map(i => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : blocksError ? (
            <div className="flex items-center gap-2 text-sm text-destructive py-8 justify-center">
              <AlertCircle className="size-4" />Failed to load blocks: {blocksError}
            </div>
          ) : blocks && blocks.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Block #</TableHead>
                  <TableHead>Network</TableHead>
                  <TableHead className="text-right">Txs</TableHead>
                  <TableHead>Miner</TableHead>
                  <TableHead className="text-right">Status</TableHead>
                  <TableHead className="text-right">Time</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {blocks.map((block) => (
                  <TableRow
                    key={`${block.network}-${block.block_num}`}
                    className="cursor-pointer"
                    onClick={() => {
                      setSearchQuery(String(block.block_num));
                      // trigger search inline
                    }}
                  >
                    <TableCell className="font-mono font-medium">
                      <button
                        className="text-primary hover:underline"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSearchQuery(String(block.block_num));
                          handleSearch();
                        }}
                      >
                        #{formatNumber(block.block_num)}
                      </button>
                    </TableCell>
                    <TableCell><Badge variant="secondary">{block.network}</Badge></TableCell>
                    <TableCell className="text-right font-mono">{block.tx_count}</TableCell>
                    <TableCell className="font-mono text-muted-foreground">{block.miner_id != null ? <UserLink userId={String(block.miner_id)} /> : "--"}</TableCell>
                    <TableCell className="text-right">
                      <Badge variant={block.status === "confirmed" ? "secondary" : "outline"}>
                        {block.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">{timeAgo(block.ts)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-center text-sm text-muted-foreground py-8">No blocks found</p>
          )}
        </CardContent>
      </Card>

      {/* Recent Transactions */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Hash className="size-4" />
            Recent Transactions
          </CardTitle>
        </CardHeader>
        <CardContent>
          {txLoading ? (
            <div className="space-y-3">
              {[0,1,2,3,4].map(i => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : txError ? (
            <div className="flex items-center gap-2 text-sm text-destructive py-8 justify-center">
              <AlertCircle className="size-4" />Failed to load transactions: {txError}
            </div>
          ) : transactions && transactions.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Hash</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead className="text-right">Fee</TableHead>
                  <TableHead className="text-right">Block</TableHead>
                  <TableHead className="text-right">Time</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {transactions.map((tx) => (
                  <TableRow
                    key={tx.tx_hash}
                    className="cursor-pointer"
                    onClick={() => setSearchQuery(tx.tx_hash)}
                  >
                    <TableCell className="font-mono">
                      <button
                        className="text-primary hover:underline"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSearchQuery(tx.tx_hash);
                          handleSearch();
                        }}
                        title={tx.tx_hash}
                      >
                        {truncateHash(tx.tx_hash)}
                      </button>
                    </TableCell>
                    <TableCell><Badge variant="outline">{tx.tx_type}</Badge></TableCell>
                    <TableCell className="text-right font-mono">
                      {tx.amount_in} {tx.symbol_in}
                      {tx.symbol_out && (
                        <span className="text-muted-foreground">
                          {" "}<ArrowRight className="inline size-3" />{" "}
                          {tx.amount_out} {tx.symbol_out}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right font-mono text-muted-foreground">
                      {fmt(tx.gas_fee, 4)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      <button
                        className="text-primary hover:underline"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSearchQuery(String(tx.block_num));
                          handleSearch();
                        }}
                      >
                        #{formatNumber(tx.block_num)}
                      </button>
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">{timeAgo(tx.ts)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-center text-sm text-muted-foreground py-8">No transactions found</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
