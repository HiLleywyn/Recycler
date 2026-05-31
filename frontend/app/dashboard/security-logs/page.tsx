"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  RefreshCw,
  Plus,
  Trash2,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { useAuthStore } from "@/stores/auth";

// ── Types ──────────────────────────────────────────────────────────────────

interface AuditEntry {
  id: number;
  guild_id: string;
  admin_id: string;
  action: string;
  target_user: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

interface Enforcement {
  id: number;
  user_id: string;
  action_type: string;
  scope: string;
  reason: string;
  enacted_by: string;
  expires_at: string | null;
  created_at: string;
}

interface Exemption {
  id: number;
  target_type: "user" | "role";
  target_id: string;
  granted_by: string;
  notes: string | null;
  created_at: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function actionBadgeColor(action: string): string {
  if (action.includes("freeze") || action.includes("ban") || action.includes("lockdown")) return "destructive";
  if (action.includes("lift") || action.includes("unfreeze") || action.includes("clear")) return "secondary";
  if (action.includes("exempt")) return "outline";
  return "secondary";
}

// ── Audit Log Tab ──────────────────────────────────────────────────────────

function AuditLogTab({ token }: { token: string }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const limit = 25;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/v2/security/audit?limit=${limit}&offset=${page * limit}`,
        { headers: { Authorization: `Bearer ${token}` } }
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load audit log");
      setEntries(data.entries || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [token, page]);

  useEffect(() => { void load(); }, [load]);

  if (loading) return <Skeleton className="h-64 w-full" />;
  if (error) return <p className="text-sm text-destructive">{error}</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Showing {entries.length} entries (page {page + 1})
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0}>
            <ChevronLeft className="size-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={() => setPage((p) => p + 1)} disabled={entries.length < limit}>
            <ChevronRight className="size-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw className="size-4" />
          </Button>
        </div>
      </div>

      {entries.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No audit entries found.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Action</TableHead>
              <TableHead>By (admin)</TableHead>
              <TableHead>Target</TableHead>
              <TableHead>Details</TableHead>
              <TableHead>Time</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((e) => (
              <TableRow key={e.id}>
                <TableCell>
                  <Badge variant={actionBadgeColor(e.action) as "destructive" | "secondary" | "outline"}>
                    {e.action}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-xs">{e.admin_id}</TableCell>
                <TableCell className="font-mono text-xs">{e.target_user ?? "—"}</TableCell>
                <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">
                  {e.details ? JSON.stringify(e.details) : "—"}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">{fmtDate(e.created_at)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

// ── Active Enforcements Tab ────────────────────────────────────────────────

function EnforcementsTab({ token }: { token: string }) {
  const [enforcements, setEnforcements] = useState<Enforcement[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/v2/security/enforcements?limit=50", {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load enforcements");
      setEnforcements(data.enforcements || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  if (loading) return <Skeleton className="h-64 w-full" />;
  if (error) return <p className="text-sm text-destructive">{error}</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{enforcements.length} active enforcement(s)</p>
        <Button variant="outline" size="sm" onClick={load}>
          <RefreshCw className="size-4" />
        </Button>
      </div>

      {enforcements.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No active enforcements.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>User</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Scope</TableHead>
              <TableHead>Reason</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead>Enacted By</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {enforcements.map((enf) => (
              <TableRow key={enf.id}>
                <TableCell className="font-mono text-xs">{enf.user_id}</TableCell>
                <TableCell>
                  <Badge variant={enf.action_type === "ban" || enf.action_type === "freeze" ? "destructive" : "secondary"}>
                    {enf.action_type}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs">{enf.scope}</TableCell>
                <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">{enf.reason}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{fmtDate(enf.expires_at) || "Never"}</TableCell>
                <TableCell className="font-mono text-xs">{enf.enacted_by}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

// ── Exemptions Tab ─────────────────────────────────────────────────────────

function ExemptionsTab({ token, isOwner }: { token: string; isOwner: boolean }) {
  const [exemptions, setExemptions] = useState<Exemption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newTargetType, setNewTargetType] = useState<"user" | "role">("user");
  const [newTargetId, setNewTargetId] = useState("");
  const [newNotes, setNewNotes] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/v2/security/exempt", {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load exemptions");
      setExemptions(data.exemptions || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  const addExemption = async () => {
    if (!newTargetId.trim()) return;
    setAdding(true);
    setAddError(null);
    try {
      const res = await fetch("/api/v2/security/exempt", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          target_type: newTargetType,
          target_id: parseInt(newTargetId),
          notes: newNotes || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to add exemption");
      setNewTargetId("");
      setNewNotes("");
      await load();
    } catch (e: unknown) {
      setAddError(e instanceof Error ? e.message : "Failed to add");
    } finally {
      setAdding(false);
    }
  };

  const removeExemption = async (targetType: string, targetId: string) => {
    try {
      const res = await fetch(`/api/v2/security/exempt/${targetType}/${targetId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || "Failed to remove");
      }
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    }
  };

  if (loading) return <Skeleton className="h-64 w-full" />;
  if (error) return <p className="text-sm text-destructive">{error}</p>;

  return (
    <div className="space-y-6">
      {isOwner && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Plus className="size-4" />
              Add Exemption
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex gap-2">
              <select
                value={newTargetType}
                onChange={(e) => setNewTargetType(e.target.value as "user" | "role")}
                className="rounded-md border border-input bg-background px-3 py-2 text-sm"
              >
                <option value="user">User</option>
                <option value="role">Role</option>
              </select>
              <Input
                placeholder="Discord ID"
                value={newTargetId}
                onChange={(e) => setNewTargetId(e.target.value)}
                className="w-48"
              />
              <Input
                placeholder="Notes (optional)"
                value={newNotes}
                onChange={(e) => setNewNotes(e.target.value)}
                className="flex-1"
              />
              <Button onClick={addExemption} disabled={adding || !newTargetId.trim()}>
                {adding ? "Adding…" : "Add"}
              </Button>
            </div>
            {addError && <p className="text-sm text-destructive">{addError}</p>}
          </CardContent>
        </Card>
      )}

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            {exemptions.length} exemption(s) — server owner is always exempt
          </p>
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw className="size-4" />
          </Button>
        </div>

        {exemptions.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No exemptions configured.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Type</TableHead>
                <TableHead>Target ID</TableHead>
                <TableHead>Granted By</TableHead>
                <TableHead>Notes</TableHead>
                <TableHead>Added</TableHead>
                {isOwner && <TableHead />}
              </TableRow>
            </TableHeader>
            <TableBody>
              {exemptions.map((ex) => (
                <TableRow key={ex.id}>
                  <TableCell>
                    <Badge variant="outline">{ex.target_type}</Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{ex.target_id}</TableCell>
                  <TableCell className="font-mono text-xs">{ex.granted_by}</TableCell>
                  <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">
                    {ex.notes ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{fmtDate(ex.created_at)}</TableCell>
                  {isOwner && (
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 text-destructive hover:text-destructive"
                        onClick={() => removeExemption(ex.target_type, ex.target_id)}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────

export default function SecurityLogsPage() {
  const user = useAuthStore((s) => s.user);
  const token = useAuthStore((s) => s.token);
  const router = useRouter();

  const hasAccess = user?.isOwner || user?.isAdmin;

  useEffect(() => {
    if (user !== null && !hasAccess) {
      // Redirect users without access after auth is resolved
      router.replace("/dashboard");
    }
  }, [user, hasAccess, router]);

  if (!user || !token) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Skeleton className="h-8 w-48" />
      </div>
    );
  }

  if (!hasAccess) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <ShieldOff className="size-10 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">
          This page requires server owner or administrator access.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center gap-3">
        <ShieldAlert className="size-6 text-primary" />
        <div>
          <h1 className="text-xl font-bold">Security Logs</h1>
          <p className="text-sm text-muted-foreground">
            Audit log, active enforcements, and exemption management
          </p>
        </div>
        {user.isOwner && (
          <Badge variant="outline" className="ml-auto flex items-center gap-1">
            <ShieldCheck className="size-3" />
            Server Owner
          </Badge>
        )}
      </div>

      <Tabs defaultValue="audit">
        <TabsList>
          <TabsTrigger value="audit">Audit Log</TabsTrigger>
          <TabsTrigger value="enforcements">Active Enforcements</TabsTrigger>
          <TabsTrigger value="exemptions">Exemptions</TabsTrigger>
        </TabsList>

        <TabsContent value="audit" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Security Audit Log</CardTitle>
            </CardHeader>
            <CardContent>
              <AuditLogTab token={token} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="enforcements" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Active Enforcements</CardTitle>
            </CardHeader>
            <CardContent>
              <EnforcementsTab token={token} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="exemptions" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Security Exemptions</CardTitle>
            </CardHeader>
            <CardContent>
              <ExemptionsTab token={token} isOwner={user.isOwner ?? false} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
