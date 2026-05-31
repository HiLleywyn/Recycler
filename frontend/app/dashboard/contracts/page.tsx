"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  FileCode,
  Hash,
  Globe,
  Activity,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Pause,
  CheckCircle,
  User,
  Calendar,
} from "lucide-react";
import { useApi } from "@/hooks/useApi";

// --- API response types ---

interface Contract {
  address: string;
  name: string;
  network: string;
  type: string;
  owner: string;
  paused: boolean;
  call_count: number;
  deployed_at: string;
}

// --- Helpers ---

function fmtDate(ts: string): string {
  const d = new Date(ts);
  return (
    d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) +
    " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  );
}

function truncateAddress(addr: string): string {
  if (addr.length <= 14) return addr;
  return addr.slice(0, 8) + "..." + addr.slice(-6);
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

// --- Component ---

export default function ContractsPage() {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const {
    data: contracts,
    loading,
    error,
  } = useApi<Contract[]>("/contracts");

  const totalCalls = contracts?.reduce((sum, c) => sum + c.call_count, 0) ?? 0;
  const pausedCount = contracts?.filter((c) => c.paused).length ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Smart Contracts</h1>
        <p className="text-sm text-muted-foreground">
          View deployed contracts and their activity
        </p>
      </div>

      {/* Summary stats */}
      <div className="grid gap-4 sm:grid-cols-3">
        {loading ? (
          <>
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
          </>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Deployed Contracts
                </CardTitle>
                <FileCode className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {contracts?.length ?? 0}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Total Calls
                </CardTitle>
                <Activity className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {(totalCalls ?? 0).toLocaleString()}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Paused
                </CardTitle>
                <Pause className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{pausedCount}</div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Contracts table */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileCode className="size-4" />
            Contracts
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : error ? (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" />
              {error}
            </div>
          ) : contracts && contracts.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Name</TableHead>
                  <TableHead>Address</TableHead>
                  <TableHead>Network</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {contracts.map((contract) => {
                  const isExpanded = expandedId === contract.address;
                  return (
                    <>
                      <TableRow
                        key={contract.address}
                        className="cursor-pointer"
                        onClick={() =>
                          setExpandedId(isExpanded ? null : contract.address)
                        }
                      >
                        <TableCell>
                          {isExpanded ? (
                            <ChevronDown className="size-4 text-muted-foreground" />
                          ) : (
                            <ChevronRight className="size-4 text-muted-foreground" />
                          )}
                        </TableCell>
                        <TableCell className="font-medium">
                          {contract.name}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {truncateAddress(contract.address)}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">{contract.network}</Badge>
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary">{contract.type}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm">
                          {(contract.call_count ?? 0).toLocaleString()}
                        </TableCell>
                        <TableCell>
                          {contract.paused ? (
                            <Badge variant="destructive">
                              <Pause className="mr-1 size-3" />
                              Paused
                            </Badge>
                          ) : (
                            <Badge variant="secondary">
                              <CheckCircle className="mr-1 size-3" />
                              Active
                            </Badge>
                          )}
                        </TableCell>
                      </TableRow>
                      {isExpanded && (
                        <TableRow key={`${contract.address}-details`}>
                          <TableCell colSpan={7}>
                            <div className="rounded-lg bg-muted/50 p-4 space-y-2">
                              <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
                                <div>
                                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                    <Hash className="size-3" />
                                    Full Address
                                  </span>
                                  <span className="mt-0.5 block break-all font-mono text-xs">
                                    {contract.address}
                                  </span>
                                </div>
                                <div>
                                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                    <User className="size-3" />
                                    Owner
                                  </span>
                                  <span className="mt-0.5 block font-mono text-xs">
                                    {truncateAddress(contract.owner)}
                                  </span>
                                </div>
                                <div>
                                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                    <Globe className="size-3" />
                                    Network
                                  </span>
                                  <span className="mt-0.5 block text-xs">
                                    {contract.network}
                                  </span>
                                </div>
                                <div>
                                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                    <Calendar className="size-3" />
                                    Deployed
                                  </span>
                                  <span className="mt-0.5 block text-xs">
                                    {fmtDate(contract.deployed_at)}
                                  </span>
                                </div>
                              </div>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </>
                  );
                })}
              </TableBody>
            </Table>
          ) : (
            <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
              <p className="text-sm text-muted-foreground">
                No contracts deployed yet
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
