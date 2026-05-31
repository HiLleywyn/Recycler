"use client";

import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Shield,
  ShieldAlert,
  Users,
  Coins,
  Settings,
  AlertTriangle,
  Activity,
  Search,
  DollarSign,
  Landmark,
  ToggleLeft,
  AlertCircle,
  Plus,
  Trash2,
  RefreshCw,
  RotateCcw,
  Bot,
  Layers,
  Hash,
  Bell,
  Droplets,
  Timer,
  Percent,
  Palette,
  Unlock,
  ShieldOff,
  Image,
  Upload,
} from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { UserLink } from "@/components/ui/user-link";
import { useAuthStore } from "@/stores/auth";

// --- Types ---

interface ServerStats {
  total_users: number;
  total_tokens: number;
  total_trades: number;
  active_loans: number;
  active_stakes: number;
  treasury_balance: number;
  total_volume_usd: number;
  total_market_cap: number;
}

interface TreasuryInfo {
  balance: number;
}

interface ModuleStatus {
  module: string;
  enabled: boolean;
}

interface TokenInfo {
  symbol: string;
  name: string;
  emoji: string;
  consensus: string;
  network?: string;
  start_price: number;
  daily_vol: number;
  max_supply?: number;
  decimals: number;
  tx_fee_rate: number;
  gas_fee: number;
  stablecoin?: boolean;
  created_at?: string;
}

interface UserSearchResult {
  user_id: string;
  net_worth: number;
}

interface GuildSettings {
  guild_id: string;
  server_name?: string;
  currency_name?: string;
  prefix?: string;
  embed_color?: number;
  platform_fee_pct?: number;
  platform_fee_min?: number;
  platform_fee_max?: number;
  treasury_cut_pct?: number;
  drop_interval?: number;
  drop_min?: number;
  drop_max?: number;
  faucet_multiplier?: number;
  faucet_tokens?: string;
  faucet_channel?: string;
  cmd_delete_after?: number;
  reply_delete_after?: number;
  ai_cmd_delete_after?: number;
  ai_reply_delete_after?: number;
  scam_detection?: boolean;
  scam_timeout_minutes?: number;
  whale_alert_threshold?: number;
  whale_alerts_channel?: string;
  reports_feed_channel?: string;
  reports_feed_categories?: string;
  security_log_channel?: string;
  security_audit_roles?: string;
  halted_networks?: string;
  disabled_tokens?: string;
  ai_mm_enabled?: boolean;
  ai_chat_enabled?: boolean;
  ai_commentary_enabled?: boolean;
  ai_flavor_enabled?: boolean;
  ai_events_enabled?: boolean;
  ai_prompt_chat?: string;
  ai_prompt_commentary?: string;
  ai_prompt_events?: string;
  ai_prompt_flavor?: string;
  ai_persona_name?: string;
}

// --- Helpers ---

function fmt(n: number | null | undefined, decimals = 2): string {
  const v = n ?? 0;
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtNum(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

// --- Sub-components ---

function UsersTab() {
  const token = useAuthStore((s) => s.token);
  const [searchQ, setSearchQ] = useState("");
  const [results, setResults] = useState<UserSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState<string | null>(null);

  const [targetId, setTargetId] = useState("");
  const [amount, setAmount] = useState("");
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  const [secMsg, setSecMsg] = useState<string | null>(null);
  const [secErr, setSecErr] = useState<string | null>(null);
  const [secActing, setSecActing] = useState(false);

  const doSearch = async () => {
    if (!searchQ.trim() || !token) return;
    setSearching(true);
    setSearchErr(null);
    try {
      const res = await fetch(`/api/v2/users/search?q=${encodeURIComponent(searchQ)}&limit=20`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Search failed");
      setResults(data);
    } catch (e: unknown) {
      setSearchErr(e instanceof Error ? e.message : "Search failed");
    } finally {
      setSearching(false);
    }
  };

  const doSecurityAction = async (action: "unfreeze" | "clear_score") => {
    if (!targetId.trim() || !token) return;
    setSecActing(true);
    setSecMsg(null);
    setSecErr(null);
    try {
      const res = await fetch(`/api/v2/security/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ action, user_id: parseInt(targetId), reason: "Admin dashboard action" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Action failed");
      setSecMsg(data.message || "Done.");
    } catch (e: unknown) {
      setSecErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setSecActing(false);
    }
  };

  const doAction = async (action: "give" | "take" | "reset" | "set-balance") => {
    if (!targetId.trim() || !token) return;
    setActing(true);
    setActionMsg(null);
    setActionErr(null);
    try {
      let body: Record<string, unknown> = {};
      let path = `/api/v2/admin/users/${targetId}/${action}`;
      if (action === "give") body = { amount: parseFloat(amount) };
      else if (action === "take") body = { amount: parseFloat(amount) };
      else if (action === "set-balance") body = { wallet: parseFloat(amount) };
      else if (action === "reset") path = `/api/v2/admin/users/${targetId}/reset`;

      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Action failed");
      setActionMsg(data.message || "Done.");
    } catch (e: unknown) {
      setActionErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setActing(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* User search */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Search className="size-4" />
            Search Users
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Input
              placeholder="User ID prefix..."
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
              className="font-mono"
            />
            <Button onClick={doSearch} disabled={searching || !searchQ.trim()}>
              {searching ? <RefreshCw className="size-4 animate-spin" /> : <Search className="size-4" />}
            </Button>
          </div>
          {searchErr && (
            <p className="text-xs text-destructive">{searchErr}</p>
          )}
          {results && (
            results.length === 0 ? (
              <p className="text-sm text-muted-foreground">No users found.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>User ID</TableHead>
                    <TableHead className="text-right">Net Worth</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {results.map((u) => (
                    <TableRow key={u.user_id}>
                      <TableCell><UserLink userId={u.user_id} /></TableCell>
                      <TableCell className="text-right">{fmt(u.net_worth)}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setTargetId(u.user_id)}
                        >
                          Select
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )
          )}
        </CardContent>
      </Card>

      {/* User actions */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="size-4" />
            User Actions
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Target User ID</Label>
              <Input
                placeholder="User ID..."
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                className="mt-1 font-mono"
              />
            </div>
            <div>
              <Label>Amount (USD)</Label>
              <Input
                type="number"
                placeholder="0.00"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="mt-1"
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={acting || !targetId || !amount}
              onClick={() => doAction("give")}
            >
              <Plus className="mr-1 size-3" /> Give
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={acting || !targetId || !amount}
              onClick={() => doAction("take")}
            >
              <Trash2 className="mr-1 size-3" /> Take
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={acting || !targetId || !amount}
              onClick={() => doAction("set-balance")}
            >
              <DollarSign className="mr-1 size-3" /> Set Balance
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={acting || !targetId}
              onClick={() => doAction("reset")}
            >
              <AlertTriangle className="mr-1 size-3" /> Reset User
            </Button>
          </div>
          {actionMsg && <p className="text-xs text-muted-foreground">{actionMsg}</p>}
          {actionErr && <p className="text-xs text-destructive">{actionErr}</p>}
        </CardContent>
      </Card>

      {/* Security locks */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Shield className="size-4" />
            Security Locks
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Use the Target User ID above. These actions call the security engine directly.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={secActing || !targetId}
              onClick={() => doSecurityAction("unfreeze")}
            >
              <Unlock className="mr-1 size-3" /> Unfreeze User
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={secActing || !targetId}
              onClick={() => doSecurityAction("clear_score")}
            >
              <ShieldOff className="mr-1 size-3" /> Clear Threat Score
            </Button>
          </div>
          {secMsg && <p className="text-xs text-muted-foreground">{secMsg}</p>}
          {secErr && <p className="text-xs text-destructive">{secErr}</p>}
        </CardContent>
      </Card>
    </div>
  );
}

function EconomyTab() {
  const token = useAuthStore((s) => s.token);
  const { data: treasury, loading: treasuryLoading, error: treasuryError, refetch: refetchTreasury } =
    useApi<TreasuryInfo>("/admin/treasury");
  const { data: modules, loading: modulesLoading, error: modulesError, refetch: refetchModules } =
    useApi<ModuleStatus[]>("/admin/modules");

  const [treasuryAction, setTreasuryAction] = useState<"give" | "drain">("give");
  const [treasuryTarget, setTreasuryTarget] = useState("");
  const [treasuryAmount, setTreasuryAmount] = useState("");
  const [treasuryMsg, setTreasuryMsg] = useState<string | null>(null);
  const [treasuryErr, setTreasuryErr] = useState<string | null>(null);
  const [treasuryActing, setTreasuryActing] = useState(false);

  const [togglingModule, setTogglingModule] = useState<string | null>(null);

  const doTreasuryAction = async () => {
    if (!token || !treasuryAmount) return;
    setTreasuryActing(true);
    setTreasuryMsg(null);
    setTreasuryErr(null);
    try {
      const body: Record<string, unknown> = { action: treasuryAction, amount: parseFloat(treasuryAmount) };
      if (treasuryAction === "give" && treasuryTarget) body.target_user_id = treasuryTarget;
      const res = await fetch("/api/v2/admin/treasury", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Action failed");
      setTreasuryMsg(data.message || "Done.");
      refetchTreasury();
    } catch (e: unknown) {
      setTreasuryErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setTreasuryActing(false);
    }
  };

  const toggleModule = async (moduleName: string, currentEnabled: boolean) => {
    if (!token) return;
    setTogglingModule(moduleName);
    try {
      const res = await fetch(`/api/v2/admin/modules/${moduleName}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ enabled: !currentEnabled }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || "Toggle failed");
      }
      refetchModules();
    } catch {
      // silently ignore — refetch will correct the state
    } finally {
      setTogglingModule(null);
    }
  };

  const moduleLabel = (key: string) =>
    key.replace("module_", "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="space-y-6">
      {/* Treasury */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Landmark className="size-4" />
            Treasury
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {treasuryLoading ? (
            <Skeleton className="h-8 w-40" />
          ) : treasuryError ? (
            <p className="text-sm text-destructive">{treasuryError}</p>
          ) : (
            <div className="text-2xl font-bold">{fmt(treasury?.balance)}</div>
          )}

          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Action</Label>
              <div className="mt-1 flex gap-2">
                <Button
                  variant={treasuryAction === "give" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTreasuryAction("give")}
                >
                  Give to user
                </Button>
                <Button
                  variant={treasuryAction === "drain" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTreasuryAction("drain")}
                >
                  Drain
                </Button>
              </div>
            </div>
            {treasuryAction === "give" && (
              <div>
                <Label>Target User ID</Label>
                <Input
                  placeholder="User ID..."
                  value={treasuryTarget}
                  onChange={(e) => setTreasuryTarget(e.target.value)}
                  className="mt-1 font-mono"
                />
              </div>
            )}
            <div>
              <Label>Amount</Label>
              <Input
                type="number"
                placeholder="0.00"
                value={treasuryAmount}
                onChange={(e) => setTreasuryAmount(e.target.value)}
                className="mt-1"
              />
            </div>
          </div>
          <Button
            size="sm"
            disabled={treasuryActing || !treasuryAmount}
            onClick={doTreasuryAction}
          >
            {treasuryActing ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <DollarSign className="mr-1 size-3" />}
            Execute
          </Button>
          {treasuryMsg && <p className="text-xs text-muted-foreground">{treasuryMsg}</p>}
          {treasuryErr && <p className="text-xs text-destructive">{treasuryErr}</p>}
        </CardContent>
      </Card>

      {/* Modules */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ToggleLeft className="size-4" />
            Feature Modules
          </CardTitle>
        </CardHeader>
        <CardContent>
          {modulesLoading ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {Array.from({ length: 8 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : modulesError ? (
            <p className="text-sm text-destructive">{modulesError}</p>
          ) : modules && modules.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {modules.map((m) => (
                <div key={m.module} className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
                  <span className="text-sm font-medium">{moduleLabel(m.module)}</span>
                  <Switch
                    checked={m.enabled}
                    disabled={togglingModule === m.module}
                    onCheckedChange={() => toggleModule(m.module, m.enabled)}
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No modules found.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function TokensTab() {
  const token = useAuthStore((s) => s.token);
  const { data: tokens, loading: tokensLoading, error: tokensError, refetch: refetchTokens } =
    useApi<TokenInfo[]>("/admin/tokens");

  // Create token form state
  const [sym, setSym]               = useState("");
  const [name, setName]             = useState("");
  const [emoji, setEmoji]           = useState("●");
  const [consensus, setConsensus]   = useState("PoS");
  const [network, setNetwork]       = useState("Arcadia Network");
  const [startPrice, setStartPrice] = useState("1.00");
  const [dailyVol, setDailyVol]     = useState("0.05");
  const [maxSupply, setMaxSupply]   = useState("");
  const [decimals, setDecimals]     = useState("18");
  const [txFeeRate, setTxFeeRate]   = useState("0.001");
  const [gasFee, setGasFee]         = useState("0.05");
  const [isStable, setIsStable]     = useState(false);
  const [creating, setCreating]     = useState(false);
  const [createMsg, setCreateMsg]   = useState<string | null>(null);
  const [createErr, setCreateErr]   = useState<string | null>(null);

  // Set-price state
  const [priceSymbol, setPriceSymbol] = useState("");
  const [priceVal, setPriceVal]       = useState("");
  const [settingPrice, setSettingPrice] = useState(false);
  const [priceMsg, setPriceMsg]       = useState<string | null>(null);
  const [priceErr, setPriceErr]       = useState<string | null>(null);

  // Auto-zero volatility for stablecoins
  const effectiveDailyVol = isStable ? "0.00" : dailyVol;

  const NETWORKS = [
    "Sun Network",
    "Moneta Chain",
    "Arcadia Network",
    "Discoin Network",
  ];

  const createToken = async () => {
    if (!token || !sym || !name) return;
    setCreating(true);
    setCreateMsg(null);
    setCreateErr(null);
    try {
      const body: Record<string, unknown> = {
        symbol:       sym.toUpperCase(),
        name,
        emoji,
        consensus,
        network:      network || null,
        start_price:  parseFloat(startPrice),
        daily_vol:    parseFloat(effectiveDailyVol),
        decimals:     parseInt(decimals, 10),
        tx_fee_rate:  parseFloat(txFeeRate),
        gas_fee:      parseFloat(gasFee),
        stablecoin:   isStable,
      };
      if (maxSupply) body.max_supply = parseInt(maxSupply, 10);
      const res = await fetch("/api/v2/admin/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Create failed");
      setCreateMsg(data.message || "Token created.");
      setSym(""); setName(""); setEmoji("●"); setMaxSupply("");
      refetchTokens();
    } catch (e: unknown) {
      setCreateErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setCreating(false);
    }
  };

  const deleteToken = async (symbol: string) => {
    if (!token) return;
    try {
      const res = await fetch(`/api/v2/admin/tokens?symbol=${encodeURIComponent(symbol)}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || "Delete failed");
      }
      refetchTokens();
    } catch {
      // ignore
    }
  };

  const setPrice = async () => {
    if (!token || !priceSymbol || !priceVal) return;
    setSettingPrice(true);
    setPriceMsg(null);
    setPriceErr(null);
    try {
      const res = await fetch(`/api/v2/admin/tokens/${encodeURIComponent(priceSymbol.toUpperCase())}/set-price`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ price: parseFloat(priceVal) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setPriceMsg(data.message || "Price updated.");
      refetchTokens();
    } catch (e: unknown) {
      setPriceErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setSettingPrice(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Token list */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins className="size-4" />
            Tokens ({tokens?.length ?? 0})
          </CardTitle>
        </CardHeader>
        <CardContent>
          {tokensLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : tokensError ? (
            <p className="text-sm text-destructive">{tokensError}</p>
          ) : tokens && tokens.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Network</TableHead>
                  <TableHead>Consensus</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead className="text-right">Vol</TableHead>
                  <TableHead className="text-right">Dec</TableHead>
                  <TableHead className="text-right">Tx Fee</TableHead>
                  <TableHead className="text-right">Gas</TableHead>
                  <TableHead className="text-right">Max Supply</TableHead>
                  <TableHead className="text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {tokens.map((t) => (
                  <TableRow key={t.symbol}>
                    <TableCell className="font-mono font-bold">{t.emoji} {t.symbol}</TableCell>
                    <TableCell>{t.name}{t.stablecoin && <Badge className="ml-1" variant="outline">stable</Badge>}</TableCell>
                    <TableCell>
                      {t.network ? <Badge variant="outline">{t.network}</Badge> : <span className="text-muted-foreground">—</span>}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{t.consensus}</Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">${t.start_price.toFixed(4)}</TableCell>
                    <TableCell className="text-right font-mono text-sm">{(t.daily_vol * 100).toFixed(1)}%</TableCell>
                    <TableCell className="text-right font-mono text-sm">{t.decimals}</TableCell>
                    <TableCell className="text-right font-mono text-sm">{(t.tx_fee_rate * 100).toFixed(2)}%</TableCell>
                    <TableCell className="text-right font-mono text-sm">${t.gas_fee.toFixed(2)}</TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {t.max_supply != null ? t.max_supply.toLocaleString() : <span className="text-muted-foreground">∞</span>}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 text-destructive hover:text-destructive"
                        onClick={() => deleteToken(t.symbol)}
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">No tokens configured.</p>
          )}
        </CardContent>
      </Card>

      {/* Create token */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="size-4" />
            Create Token
          </CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Configure all on-chain parameters — equivalent to deploying a real EVM token.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Row 1: identity */}
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Symbol <span className="text-destructive">*</span></Label>
              <Input placeholder="e.g. DSC" value={sym} onChange={(e) => setSym(e.target.value)} className="mt-1 font-mono uppercase" />
            </div>
            <div>
              <Label>Name <span className="text-destructive">*</span></Label>
              <Input placeholder="e.g. Discoin" value={name} onChange={(e) => setName(e.target.value)} className="mt-1" />
            </div>
            <div>
              <Label>Emoji</Label>
              <Input placeholder="●" value={emoji} onChange={(e) => setEmoji(e.target.value)} className="mt-1 w-20" maxLength={2} />
            </div>
          </div>

          {/* Row 2: classification */}
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Network</Label>
              <select
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background"
                value={network}
                onChange={(e) => setNetwork(e.target.value)}
              >
                <option value="Sun Network">☀ Sun Network (PoW)</option>
                <option value="Moneta Chain">🟡 Moneta Chain (PoW)</option>
                <option value="Arcadia Network">🔵 Arcadia Network (PoS)</option>
                <option value="Discoin Network">🪙 Discoin Network (PoS)</option>
              </select>
            </div>
            <div>
              <Label>Consensus</Label>
              <select
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background"
                value={consensus}
                onChange={(e) => setConsensus(e.target.value)}
              >
                <option value="PoS">PoS — Proof of Stake</option>
                <option value="PoW">PoW — Proof of Work</option>
                <option value="Fiat">Fiat — Stablecoin</option>
              </select>
            </div>
          </div>

          {/* Row 3: price & supply */}
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Start Price (USD)</Label>
              <Input type="number" min="0" step="0.01" value={startPrice} onChange={(e) => setStartPrice(e.target.value)} className="mt-1" />
            </div>
            <div>
              <Label>Daily Volatility {isStable && <span className="text-muted-foreground">(forced 0 for stable)</span>}</Label>
              <Input
                type="number" min="0" max="0.50" step="0.01"
                value={effectiveDailyVol}
                disabled={isStable}
                onChange={(e) => setDailyVol(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label>Max Supply <span className="text-muted-foreground">(leave blank = unlimited)</span></Label>
              <Input type="number" min="1" placeholder="e.g. 21000000" value={maxSupply} onChange={(e) => setMaxSupply(e.target.value)} className="mt-1 font-mono" />
            </div>
          </div>

          {/* Row 4: on-chain params */}
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Decimals</Label>
              <select
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background"
                value={decimals}
                onChange={(e) => setDecimals(e.target.value)}
              >
                <option value="18">18 — EVM standard (ARC, DSC, VTR)</option>
                <option value="8">8 — Moneta-style (MTA, SUN)</option>
                <option value="6">6 — USDC-style</option>
              </select>
            </div>
            <div>
              <Label>Transfer Fee Rate <span className="text-muted-foreground">(e.g. 0.001 = 0.1%)</span></Label>
              <Input type="number" min="0" max="0.10" step="0.0001" value={txFeeRate} onChange={(e) => setTxFeeRate(e.target.value)} className="mt-1 font-mono" />
            </div>
            <div>
              <Label>Base Gas Fee (USD)</Label>
              <Input type="number" min="0" step="0.01" value={gasFee} onChange={(e) => setGasFee(e.target.value)} className="mt-1 font-mono" />
            </div>
          </div>

          {/* Stablecoin flag */}
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="stablecoin-flag"
              checked={isStable}
              onChange={(e) => setIsStable(e.target.checked)}
              className="size-4 rounded border-input"
            />
            <label htmlFor="stablecoin-flag" className="text-sm">
              Stablecoin — price pegged to $1.00 (volatility forced to 0)
            </label>
          </div>

          <Button size="sm" disabled={creating || !sym || !name} onClick={createToken}>
            {creating ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Plus className="mr-1 size-3" />}
            Create Token
          </Button>
          {createMsg && <p className="text-xs text-muted-foreground">{createMsg}</p>}
          {createErr && <p className="text-xs text-destructive">{createErr}</p>}
        </CardContent>
      </Card>

      {/* Set token price */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <DollarSign className="size-4" />
            Override Token Price
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Symbol</Label>
              <Input placeholder="e.g. ARC" value={priceSymbol} onChange={(e) => setPriceSymbol(e.target.value)} className="mt-1 font-mono uppercase" />
            </div>
            <div>
              <Label>New Price ($)</Label>
              <Input type="number" placeholder="0.00" value={priceVal} onChange={(e) => setPriceVal(e.target.value)} className="mt-1" />
            </div>
          </div>
          <Button size="sm" disabled={settingPrice || !priceSymbol || !priceVal} onClick={setPrice}>
            {settingPrice ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <DollarSign className="mr-1 size-3" />}
            Set Price
          </Button>
          {priceMsg && <p className="text-xs text-muted-foreground">{priceMsg}</p>}
          {priceErr && <p className="text-xs text-destructive">{priceErr}</p>}
        </CardContent>
      </Card>
    </div>
  );
}


function ConfigTab() {
  const token = useAuthStore((s) => s.token);
  const { data: settings, loading, error, refetch } =
    useApi<GuildSettings>("/admin/settings");

  // Basic
  const [serverName, setServerName] = useState("");
  const [currencyName, setCurrencyName] = useState("");
  const [prefix, setPrefix] = useState("");
  const [embedColor, setEmbedColor] = useState("");
  // Fees
  const [feePct, setFeePct] = useState("");
  const [feeMin, setFeeMin] = useState("");
  const [feeMax, setFeeMax] = useState("");
  const [treasuryCut, setTreasuryCut] = useState("");
  // Drops & Faucet
  const [dropInterval, setDropInterval] = useState("");
  const [dropMin, setDropMin] = useState("");
  const [dropMax, setDropMax] = useState("");
  const [faucetMultiplier, setFaucetMultiplier] = useState("");
  const [faucetTokens, setFaucetTokens] = useState("");
  // Auto-delete
  const [cmdDelete, setCmdDelete] = useState("0");
  const [replyDelete, setReplyDelete] = useState("0");
  const [aiCmdDelete, setAiCmdDelete] = useState("0");
  const [aiReplyDelete, setAiReplyDelete] = useState("0");
  // Notifications
  const [whaleThreshold, setWhaleThreshold] = useState("");
  const [reportCategories, setReportCategories] = useState("");
  const [securityAuditRoles, setSecurityAuditRoles] = useState("");

  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [prefilled, setPrefilled] = useState(false);

  useEffect(() => {
    if (settings && !prefilled) {
      setServerName(settings.server_name ?? "");
      setCurrencyName(settings.currency_name ?? "");
      setPrefix(settings.prefix ?? "");
      setEmbedColor(settings.embed_color ? "#" + settings.embed_color.toString(16).padStart(6, "0") : "");
      setFeePct(settings.platform_fee_pct?.toString() ?? "");
      setFeeMin(settings.platform_fee_min?.toString() ?? "");
      setFeeMax(settings.platform_fee_max?.toString() ?? "");
      setTreasuryCut(settings.treasury_cut_pct?.toString() ?? "");
      setDropInterval(settings.drop_interval?.toString() ?? "");
      setDropMin(settings.drop_min?.toString() ?? "");
      setDropMax(settings.drop_max?.toString() ?? "");
      setFaucetMultiplier(settings.faucet_multiplier?.toString() ?? "1");
      setFaucetTokens(settings.faucet_tokens ?? "");
      setCmdDelete(settings.cmd_delete_after?.toString() ?? "0");
      setReplyDelete(settings.reply_delete_after?.toString() ?? "0");
      setAiCmdDelete(settings.ai_cmd_delete_after?.toString() ?? "0");
      setAiReplyDelete(settings.ai_reply_delete_after?.toString() ?? "0");
      setWhaleThreshold(settings.whale_alert_threshold?.toString() ?? "");
      setReportCategories(settings.reports_feed_categories ?? "");
      setSecurityAuditRoles(settings.security_audit_roles ?? "");
      setPrefilled(true);
    }
  }, [settings, prefilled]);

  const save = async () => {
    if (!token) return;
    setSaving(true);
    setSaveMsg(null);
    setSaveErr(null);
    try {
      const body: Record<string, unknown> = {};
      if (serverName) body.server_name = serverName;
      if (currencyName) body.currency_name = currencyName;
      if (prefix) body.prefix = prefix;
      const hex = embedColor.replace("#", "");
      if (/^[0-9a-fA-F]{6}$/.test(hex)) body.embed_color = parseInt(hex, 16);
      if (feePct) body.platform_fee_pct = parseFloat(feePct);
      if (feeMin) body.platform_fee_min = parseFloat(feeMin);
      if (feeMax) body.platform_fee_max = parseFloat(feeMax);
      if (treasuryCut) body.treasury_cut_pct = parseFloat(treasuryCut);
      if (dropInterval) body.drop_interval = parseInt(dropInterval);
      if (dropMin) body.drop_min = parseFloat(dropMin);
      if (dropMax) body.drop_max = parseFloat(dropMax);
      if (faucetMultiplier) body.faucet_multiplier = parseFloat(faucetMultiplier);
      body.faucet_tokens = faucetTokens.trim();
      body.cmd_delete_after = parseInt(cmdDelete) || 0;
      body.reply_delete_after = parseInt(replyDelete) || 0;
      body.ai_cmd_delete_after = parseInt(aiCmdDelete) || 0;
      body.ai_reply_delete_after = parseInt(aiReplyDelete) || 0;
      if (whaleThreshold) body.whale_alert_threshold = parseFloat(whaleThreshold);
      body.reports_feed_categories = reportCategories.trim();
      body.security_audit_roles = securityAuditRoles.trim();
      const res = await fetch("/api/v2/admin/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      setSaveMsg(data.message || "Settings saved.");
      refetch();
    } catch (e: unknown) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="space-y-3">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-24 w-full" />)}</div>;
  if (error) return <div className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="size-4" />{error}</div>;

  return (
    <div className="space-y-4">
      {/* Basic */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Settings className="size-4" />Basic</CardTitle></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Server Name</Label>
              <Input value={serverName} onChange={(e) => setServerName(e.target.value)} className="mt-1" placeholder="Server name…" />
            </div>
            <div>
              <Label>Currency Name</Label>
              <Input value={currencyName} onChange={(e) => setCurrencyName(e.target.value)} className="mt-1" placeholder="e.g. Discoin" />
            </div>
            <div>
              <Label>Command Prefix</Label>
              <Input value={prefix} onChange={(e) => setPrefix(e.target.value)} className="mt-1 font-mono" placeholder="$" />
            </div>
            <div>
              <Label className="flex items-center gap-1"><Palette className="size-3" />Embed Color</Label>
              <div className="mt-1 flex gap-2">
                <Input value={embedColor} onChange={(e) => setEmbedColor(e.target.value)} className="font-mono" placeholder="#5865F2" />
                {embedColor && /^#[0-9a-fA-F]{6}$/.test(embedColor) && (
                  <div className="size-9 rounded-md border flex-shrink-0" style={{ background: embedColor }} />
                )}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Fees */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Percent className="size-4" />Platform Fees</CardTitle></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Fee % <span className="text-muted-foreground text-xs">(e.g. 0.002 = 0.2%)</span></Label>
              <Input type="number" step="0.001" min="0" max="1" value={feePct} onChange={(e) => setFeePct(e.target.value)} className="mt-1 font-mono" placeholder="0.002" />
            </div>
            <div>
              <Label>Treasury Cut % <span className="text-muted-foreground text-xs">(of fee)</span></Label>
              <Input type="number" step="0.01" min="0" max="1" value={treasuryCut} onChange={(e) => setTreasuryCut(e.target.value)} className="mt-1 font-mono" placeholder="0.5" />
            </div>
            <div>
              <Label>Min Fee ($)</Label>
              <Input type="number" step="0.01" min="0" value={feeMin} onChange={(e) => setFeeMin(e.target.value)} className="mt-1 font-mono" placeholder="0.10" />
            </div>
            <div>
              <Label>Max Fee ($)</Label>
              <Input type="number" step="0.01" min="0" value={feeMax} onChange={(e) => setFeeMax(e.target.value)} className="mt-1 font-mono" placeholder="20.00" />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Drops & Faucet */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Droplets className="size-4" />Drops &amp; Faucet</CardTitle></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Drop Interval (s)</Label>
              <Input type="number" min="60" step="60" value={dropInterval} onChange={(e) => setDropInterval(e.target.value)} className="mt-1 font-mono" placeholder="1800" />
            </div>
            <div>
              <Label>Drop Min ($)</Label>
              <Input type="number" min="0" step="1" value={dropMin} onChange={(e) => setDropMin(e.target.value)} className="mt-1 font-mono" placeholder="100" />
            </div>
            <div>
              <Label>Drop Max ($)</Label>
              <Input type="number" min="0" step="1" value={dropMax} onChange={(e) => setDropMax(e.target.value)} className="mt-1 font-mono" placeholder="2000" />
            </div>
            <div>
              <Label>Faucet Multiplier</Label>
              <Input type="number" step="0.1" min="0.1" max="100" value={faucetMultiplier} onChange={(e) => setFaucetMultiplier(e.target.value)} className="mt-1 font-mono" placeholder="1.0" />
              <p className="text-xs text-muted-foreground mt-1">Scale all auto-faucet payouts</p>
            </div>
            <div className="sm:col-span-2">
              <Label>Faucet Token Whitelist</Label>
              <Input value={faucetTokens} onChange={(e) => setFaucetTokens(e.target.value)} className="mt-1 font-mono" placeholder="MTA,DSC,ARC,SUN  (blank = all eligible)" />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Auto-delete */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Timer className="size-4" />Auto-Delete <span className="text-muted-foreground font-normal text-xs">(0 = off, max 3600s)</span></CardTitle></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Delete Commands After (s)</Label>
              <Input type="number" min="0" max="3600" step="1" value={cmdDelete} onChange={(e) => setCmdDelete(e.target.value)} className="mt-1 font-mono" />
            </div>
            <div>
              <Label>Delete Replies After (s)</Label>
              <Input type="number" min="0" max="3600" step="1" value={replyDelete} onChange={(e) => setReplyDelete(e.target.value)} className="mt-1 font-mono" />
            </div>
            <div>
              <Label>Delete AI Commands After (s)</Label>
              <Input type="number" min="0" max="3600" step="1" value={aiCmdDelete} onChange={(e) => setAiCmdDelete(e.target.value)} className="mt-1 font-mono" />
            </div>
            <div>
              <Label>Delete AI Replies After (s)</Label>
              <Input type="number" min="0" max="3600" step="1" value={aiReplyDelete} onChange={(e) => setAiReplyDelete(e.target.value)} className="mt-1 font-mono" />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Notifications */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Bell className="size-4" />Notifications &amp; Audit</CardTitle></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Whale Alert Threshold ($)</Label>
              <Input type="number" min="0" step="1000" value={whaleThreshold} onChange={(e) => setWhaleThreshold(e.target.value)} className="mt-1 font-mono" placeholder="50000" />
              <p className="text-xs text-muted-foreground mt-1">Alert on transactions above this USD value</p>
            </div>
            <div>
              <Label>Reports Feed Categories</Label>
              <Input value={reportCategories} onChange={(e) => setReportCategories(e.target.value)} className="mt-1 font-mono" placeholder="bugs,suggestions,users,other" />
              <p className="text-xs text-muted-foreground mt-1">Comma-separated. Empty = all categories</p>
            </div>
            <div className="sm:col-span-2">
              <Label>Security Audit Role IDs</Label>
              <Input value={securityAuditRoles} onChange={(e) => setSecurityAuditRoles(e.target.value)} className="mt-1 font-mono" placeholder="123456789,987654321" />
              <p className="text-xs text-muted-foreground mt-1">Comma-separated Discord role IDs that can view the security audit log</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Halts (read-only) */}
      {(settings?.halted_networks || settings?.disabled_tokens) && (
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><AlertTriangle className="size-4 text-yellow-500" />Active Halts <span className="text-muted-foreground font-normal text-xs">(manage via bot: .admin halt/unhalt)</span></CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {settings.halted_networks && (
              <div>
                <span className="text-muted-foreground">Halted Networks: </span>
                <span className="font-mono">{settings.halted_networks}</span>
              </div>
            )}
            {settings.disabled_tokens && (
              <div>
                <span className="text-muted-foreground">Disabled Tokens: </span>
                <span className="font-mono">{settings.disabled_tokens}</span>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="flex items-center gap-3">
        <Button disabled={saving} onClick={save}>
          {saving ? <RefreshCw className="mr-1 size-4 animate-spin" /> : <Settings className="mr-1 size-4" />}
          Save Configuration
        </Button>
        {saveMsg && <p className="text-xs text-muted-foreground">{saveMsg}</p>}
        {saveErr && <p className="text-xs text-destructive">{saveErr}</p>}
      </div>
    </div>
  );
}

// --- Permissions Tab ---

interface PermOverview {
  total_admins: number;
  total_exemptions: number;
  total_permission_overrides: number;
  bot_manager_id: string | null;
  bot_manager_exempt: boolean;
}

interface AdminUser {
  id: number;
  user_id: string;
  granted_by: string | null;
  notes: string | null;
  created_at: string | null;
}

interface PermOverride {
  id: number;
  target_type: string;
  target_id: string;
  permission: string;
  granted_by: string | null;
  created_at: string | null;
}

interface SecurityExemption {
  id: number;
  target_type: string;
  target_id: string;
  granted_by: string | null;
  notes: string | null;
  created_at: string | null;
}

interface BotManagerCfg {
  bot_manager_id: number | null;
  auto_exempt: boolean;
  all_permissions: boolean;
}

function PermissionsTab() {
  const token = useAuthStore((s) => s.token);
  const { data: overview, loading: overviewLoading, refetch: refetchOverview } =
    useApi<PermOverview>("/admin/permissions/overview");
  const { data: adminsData, loading: adminsLoading, refetch: refetchAdmins } =
    useApi<{ admins: AdminUser[] }>("/admin/permissions/admins");
  const { data: overridesData, loading: overridesLoading, refetch: refetchOverrides } =
    useApi<{ overrides: PermOverride[] }>("/admin/permissions/overrides");
  const { data: exemptionsData, loading: exemptionsLoading, refetch: refetchExemptions } =
    useApi<{ exemptions: SecurityExemption[] }>("/admin/permissions/exemptions");
  const { data: reportCfg, loading: reportLoading, refetch: refetchReport } =
    useApi<BotManagerCfg>("/admin/permissions/bot-manager");

  // Admin user form
  const [newAdminId, setNewAdminId] = useState("");
  const [adminNotes, setAdminNotes] = useState("");
  const [adminMsg, setAdminMsg] = useState<string | null>(null);
  const [adminErr, setAdminErr] = useState<string | null>(null);
  const [adminActing, setAdminActing] = useState(false);

  // Permission override form
  const [overrideTargetType, setOverrideTargetType] = useState("user");
  const [overrideTargetId, setOverrideTargetId] = useState("");
  const [overridePermission, setOverridePermission] = useState("");
  const [overrideMsg, setOverrideMsg] = useState<string | null>(null);
  const [overrideErr, setOverrideErr] = useState<string | null>(null);
  const [overrideActing, setOverrideActing] = useState(false);

  // Exemption form
  const [exemptTargetType, setExemptTargetType] = useState("user");
  const [exemptTargetId, setExemptTargetId] = useState("");
  const [exemptNotes, setExemptNotes] = useState("");
  const [exemptMsg, setExemptMsg] = useState<string | null>(null);
  const [exemptErr, setExemptErr] = useState<string | null>(null);
  const [exemptActing, setExemptActing] = useState(false);

  // Report user form
  const [reportUserId, setReportUserId] = useState("");
  const [reportAutoExempt, setReportAutoExempt] = useState(true);
  const [reportAllPerms, setReportAllPerms] = useState(true);
  const [reportMsg, setReportMsg] = useState<string | null>(null);
  const [reportErr, setReportErr] = useState<string | null>(null);
  const [reportActing, setReportActing] = useState(false);

  useEffect(() => {
    if (reportCfg) {
      setReportUserId(reportCfg.bot_manager_id ? String(reportCfg.bot_manager_id) : "");
      setReportAutoExempt(reportCfg.auto_exempt);
      setReportAllPerms(reportCfg.all_permissions);
    }
  }, [reportCfg]);

  const addAdmin = async () => {
    if (!newAdminId.trim() || !token) return;
    setAdminActing(true);
    setAdminMsg(null);
    setAdminErr(null);
    try {
      const res = await fetch("/api/v2/admin/permissions/admins", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ user_id: newAdminId, is_admin: true, notes: adminNotes || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setAdminMsg(data.message || "Admin granted.");
      setNewAdminId("");
      setAdminNotes("");
      refetchAdmins();
      refetchOverview();
    } catch (e: unknown) {
      setAdminErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setAdminActing(false);
    }
  };

  const removeAdmin = async (userId: string) => {
    if (!token) return;
    setAdminActing(true);
    setAdminMsg(null);
    setAdminErr(null);
    try {
      const res = await fetch(`/api/v2/admin/permissions/admins/${userId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setAdminMsg(data.message || "Admin removed.");
      refetchAdmins();
      refetchOverview();
    } catch (e: unknown) {
      setAdminErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setAdminActing(false);
    }
  };

  const addOverride = async () => {
    if (!overrideTargetId.trim() || !overridePermission.trim() || !token) return;
    setOverrideActing(true);
    setOverrideMsg(null);
    setOverrideErr(null);
    try {
      const res = await fetch("/api/v2/admin/permissions/overrides", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          target_type: overrideTargetType,
          target_id: overrideTargetId,
          permission: overridePermission,
          granted: true,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setOverrideMsg(data.message || "Permission granted.");
      setOverrideTargetId("");
      setOverridePermission("");
      refetchOverrides();
      refetchOverview();
    } catch (e: unknown) {
      setOverrideErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setOverrideActing(false);
    }
  };

  const deleteOverride = async (id: number) => {
    if (!token) return;
    setOverrideActing(true);
    setOverrideMsg(null);
    setOverrideErr(null);
    try {
      const res = await fetch(`/api/v2/admin/permissions/overrides/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setOverrideMsg(data.message || "Override removed.");
      refetchOverrides();
      refetchOverview();
    } catch (e: unknown) {
      setOverrideErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setOverrideActing(false);
    }
  };

  const addExemption = async () => {
    if (!exemptTargetId.trim() || !token) return;
    setExemptActing(true);
    setExemptMsg(null);
    setExemptErr(null);
    try {
      const res = await fetch("/api/v2/admin/permissions/exemptions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          target_type: exemptTargetType,
          target_id: exemptTargetId,
          notes: exemptNotes || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setExemptMsg(data.message || "Exemption added.");
      setExemptTargetId("");
      setExemptNotes("");
      refetchExemptions();
      refetchOverview();
    } catch (e: unknown) {
      setExemptErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setExemptActing(false);
    }
  };

  const removeExemption = async (targetType: string, targetId: string) => {
    if (!token) return;
    setExemptActing(true);
    setExemptMsg(null);
    setExemptErr(null);
    try {
      const res = await fetch(`/api/v2/admin/permissions/exemptions/${targetType}/${targetId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setExemptMsg(data.message || "Exemption removed.");
      refetchExemptions();
      refetchOverview();
    } catch (e: unknown) {
      setExemptErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setExemptActing(false);
    }
  };

  const saveReportUser = async () => {
    if (!token) return;
    setReportActing(true);
    setReportMsg(null);
    setReportErr(null);
    try {
      const res = await fetch("/api/v2/admin/permissions/bot-manager", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          bot_manager_id: reportUserId ? parseInt(reportUserId, 10) : null,
          auto_exempt: reportAutoExempt,
          all_permissions: reportAllPerms,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      setReportMsg(data.message || "Saved.");
      refetchReport();
      refetchOverview();
    } catch (e: unknown) {
      setReportErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setReportActing(false);
    }
  };

  const PERMISSION_OPTIONS = [
    "admin", "trade", "transfer", "gamble", "earn",
    "pool", "loan", "mine", "stake", "shop",
    "security_audit", "manage_tokens", "manage_users",
    "manage_settings", "manage_treasury", "all",
  ];

  return (
    <div className="space-y-6">
      {/* Overview stats */}
      <div className="grid gap-4 sm:grid-cols-4">
        {overviewLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <Skeleton className="h-4 w-24" />
              </CardHeader>
              <CardContent><Skeleton className="h-7 w-16" /></CardContent>
            </Card>
          ))
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Admin Users</CardTitle>
                <Shield className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{overview?.total_admins ?? 0}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Exemptions</CardTitle>
                <Shield className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{overview?.total_exemptions ?? 0}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Overrides</CardTitle>
                <Settings className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent><div className="text-2xl font-bold">{overview?.total_permission_overrides ?? 0}</div></CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Bot Manager</CardTitle>
                <Users className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-sm font-mono">
                  {overview?.bot_manager_id ? overview.bot_manager_id : "Not set"}
                </div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Bot Manager Config */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Shield className="size-4" />
            Bot Manager (Auto-Exempt Superuser)
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Configure a per-guild bot manager who is automatically exempt from all security enforcement and has all permissions. Only the server owner can change this setting.
          </p>
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Bot Manager User ID</Label>
              <Input
                placeholder="Discord User ID..."
                value={reportUserId}
                onChange={(e) => setReportUserId(e.target.value)}
                className="mt-1 font-mono"
              />
            </div>
            <div className="flex items-center gap-2 mt-6">
              <Switch checked={reportAutoExempt} onCheckedChange={setReportAutoExempt} />
              <Label>Auto-Exempt from Security</Label>
            </div>
            <div className="flex items-center gap-2 mt-6">
              <Switch checked={reportAllPerms} onCheckedChange={setReportAllPerms} />
              <Label>All Permissions</Label>
            </div>
          </div>
          <Button onClick={saveReportUser} disabled={reportActing} size="sm">
            {reportActing ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Shield className="mr-1 size-3" />}
            Save Bot Manager
          </Button>
          {reportMsg && <p className="text-xs text-muted-foreground">{reportMsg}</p>}
          {reportErr && <p className="text-xs text-destructive">{reportErr}</p>}
        </CardContent>
      </Card>

      {/* Admin Users */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="size-4" />
            Admin Users
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>User ID</Label>
              <Input
                placeholder="Discord User ID..."
                value={newAdminId}
                onChange={(e) => setNewAdminId(e.target.value)}
                className="mt-1 font-mono"
              />
            </div>
            <div>
              <Label>Notes (optional)</Label>
              <Input
                placeholder="Reason..."
                value={adminNotes}
                onChange={(e) => setAdminNotes(e.target.value)}
                className="mt-1"
              />
            </div>
            <div className="flex items-end">
              <Button onClick={addAdmin} disabled={adminActing || !newAdminId.trim()} size="sm">
                <Plus className="mr-1 size-3" /> Grant Admin
              </Button>
            </div>
          </div>
          {adminMsg && <p className="text-xs text-muted-foreground">{adminMsg}</p>}
          {adminErr && <p className="text-xs text-destructive">{adminErr}</p>}
          {adminsLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : adminsData?.admins && adminsData.admins.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User ID</TableHead>
                  <TableHead>Granted By</TableHead>
                  <TableHead>Notes</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {adminsData.admins.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell className="font-mono text-xs">{a.user_id}</TableCell>
                    <TableCell className="font-mono text-xs">{a.granted_by || "-"}</TableCell>
                    <TableCell className="text-xs">{a.notes || "-"}</TableCell>
                    <TableCell className="text-right">
                      <Button variant="destructive" size="sm" onClick={() => removeAdmin(a.user_id)} disabled={adminActing}>
                        <Trash2 className="size-3" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">No admin users configured.</p>
          )}
        </CardContent>
      </Card>

      {/* Security Exemptions */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Shield className="size-4" />
            Security Exemptions
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Users and roles exempt from security enforcement (threat detection, freezing, etc.).
          </p>
          <div className="grid gap-3 sm:grid-cols-4">
            <div>
              <Label>Type</Label>
              <select
                value={exemptTargetType}
                onChange={(e) => setExemptTargetType(e.target.value)}
                className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="user">User</option>
                <option value="role">Role</option>
              </select>
            </div>
            <div>
              <Label>ID</Label>
              <Input
                placeholder="Discord ID..."
                value={exemptTargetId}
                onChange={(e) => setExemptTargetId(e.target.value)}
                className="mt-1 font-mono"
              />
            </div>
            <div>
              <Label>Notes</Label>
              <Input
                placeholder="Optional..."
                value={exemptNotes}
                onChange={(e) => setExemptNotes(e.target.value)}
                className="mt-1"
              />
            </div>
            <div className="flex items-end">
              <Button onClick={addExemption} disabled={exemptActing || !exemptTargetId.trim()} size="sm">
                <Plus className="mr-1 size-3" /> Add Exemption
              </Button>
            </div>
          </div>
          {exemptMsg && <p className="text-xs text-muted-foreground">{exemptMsg}</p>}
          {exemptErr && <p className="text-xs text-destructive">{exemptErr}</p>}
          {exemptionsLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : exemptionsData?.exemptions && exemptionsData.exemptions.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Type</TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead>Granted By</TableHead>
                  <TableHead>Notes</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {exemptionsData.exemptions.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell>
                      <Badge variant={e.target_type === "user" ? "default" : "secondary"}>
                        {e.target_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{e.target_id}</TableCell>
                    <TableCell className="font-mono text-xs">{e.granted_by || "-"}</TableCell>
                    <TableCell className="text-xs">{e.notes || "-"}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => removeExemption(e.target_type, e.target_id)}
                        disabled={exemptActing}
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">No security exemptions configured.</p>
          )}
        </CardContent>
      </Card>

      {/* Permission Overrides */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings className="size-4" />
            Permission Overrides
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Grant specific permissions to individual users or roles beyond their default access level.
          </p>
          <div className="grid gap-3 sm:grid-cols-4">
            <div>
              <Label>Type</Label>
              <select
                value={overrideTargetType}
                onChange={(e) => setOverrideTargetType(e.target.value)}
                className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="user">User</option>
                <option value="role">Role</option>
              </select>
            </div>
            <div>
              <Label>ID</Label>
              <Input
                placeholder="Discord ID..."
                value={overrideTargetId}
                onChange={(e) => setOverrideTargetId(e.target.value)}
                className="mt-1 font-mono"
              />
            </div>
            <div>
              <Label>Permission</Label>
              <select
                value={overridePermission}
                onChange={(e) => setOverridePermission(e.target.value)}
                className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="">Select...</option>
                {PERMISSION_OPTIONS.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <Button
                onClick={addOverride}
                disabled={overrideActing || !overrideTargetId.trim() || !overridePermission}
                size="sm"
              >
                <Plus className="mr-1 size-3" /> Grant
              </Button>
            </div>
          </div>
          {overrideMsg && <p className="text-xs text-muted-foreground">{overrideMsg}</p>}
          {overrideErr && <p className="text-xs text-destructive">{overrideErr}</p>}
          {overridesLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : overridesData?.overrides && overridesData.overrides.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Type</TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead>Permission</TableHead>
                  <TableHead>Granted By</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {overridesData.overrides.map((o) => (
                  <TableRow key={o.id}>
                    <TableCell>
                      <Badge variant={o.target_type === "user" ? "default" : "secondary"}>
                        {o.target_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{o.target_id}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{o.permission}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{o.granted_by || "-"}</TableCell>
                    <TableCell className="text-right">
                      <Button variant="destructive" size="sm" onClick={() => deleteOverride(o.id)} disabled={overrideActing}>
                        <Trash2 className="size-3" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">No permission overrides configured.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// --- Security Settings Tab ---

interface ScamSettings {
  scam_detection: boolean;
  scam_timeout_minutes: number;
}

interface SecurityConfig {
  // Detection windows
  scan_interval_seconds: number;
  lookback_seconds: number;
  // Economy
  income_velocity_limit: number;
  gambling_velocity_limit: number;
  wash_trade_min_cycles: number;
  transfer_ring_min: number;
  lp_churn_min: number;
  tx_flood_limit: number;
  // API/Session
  auth_failure_limit: number;
  auth_failure_window: number;
  session_ip_change_window: number;
  api_request_flood_limit: number;
  api_request_flood_window: number;
  // Command flood
  command_flood_limit: number;
  command_flood_window: number;
  identical_command_limit: number;
  // Correlation
  correlation_window: number;
  correlation_event_min: number;
  // DeFi
  flash_loan_window: number;
  oracle_manipulation_trades: number;
  oracle_manipulation_window: number;
  // Scoring
  score_decay_half_life: number;
  score_weights: Record<string, number>;
  // Response levels
  level_1_threshold: number;
  level_2_threshold: number;
  level_3_threshold: number;
  level_4_threshold: number;
  level_5_threshold: number;
  // Enforcement durations
  throttle_duration: number;
  freeze_duration: number;
  flag_duration: number;
  lockdown_duration: number;
  throttled_rate_limit: number;
  // Alerts
  alert_cooldown_seconds: number;
  // Behavior profiling
  anomaly_stddev_threshold: number;
  baseline_min_samples: number;
  // Whale / repeat
  whale_concentration_limit: number;
  repeat_offender_limit: number;
  _overrides: string[];
}

// Small helper: number field row with a "reset to default" button
function CfgField({
  label,
  description,
  fieldKey,
  value,
  overrides,
  onChange,
  onReset,
  step = 1,
  min,
}: {
  label: string;
  description?: string;
  fieldKey: string;
  value: number;
  overrides: string[];
  onChange: (key: string, v: number) => void;
  onReset: (key: string) => void;
  step?: number;
  min?: number;
}) {
  const isOverridden = overrides.includes(fieldKey);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <Label className="flex items-center gap-1.5">
          {label}
          {isOverridden && (
            <Badge variant="secondary" className="text-[10px] px-1 py-0">custom</Badge>
          )}
        </Label>
        {isOverridden && (
          <button
            type="button"
            onClick={() => onReset(fieldKey)}
            className="flex items-center gap-0.5 text-[10px] text-muted-foreground hover:text-foreground"
            title="Reset to global default"
          >
            <RotateCcw className="size-2.5" /> reset
          </button>
        )}
      </div>
      <Input
        type="number"
        step={step}
        min={min}
        value={value}
        onChange={(e) => onChange(fieldKey, parseFloat(e.target.value) || 0)}
        className="font-mono"
      />
      {description && (
        <p className="text-[11px] text-muted-foreground">{description}</p>
      )}
    </div>
  );
}

function SecurityTab() {
  const token = useAuthStore((s) => s.token);

  // Scam detection
  const [scamEnabled, setScamEnabled] = useState(false);
  const [scamTimeout, setScamTimeout] = useState(10);
  const [scamLoading, setScamLoading] = useState(true);
  const [scamMsg, setScamMsg] = useState<string | null>(null);
  const [scamErr, setScamErr] = useState<string | null>(null);
  const [scamSaving, setScamSaving] = useState(false);

  // Security thresholds
  const [cfg, setCfg] = useState<SecurityConfig | null>(null);
  const [cfgLoading, setCfgLoading] = useState(true);
  const [cfgErr, setCfgErr] = useState<string | null>(null);
  const [cfgMsg, setCfgMsg] = useState<string | null>(null);
  const [cfgSaving, setCfgSaving] = useState(false);
  // Track local edits as partial overrides
  const [localEdits, setLocalEdits] = useState<Record<string, number | Record<string, number>>>({});
  // Track fields to reset (send null to clear override)
  const [pendingResets, setPendingResets] = useState<Set<string>>(new Set());
  const hasPendingChanges = Object.keys(localEdits).length > 0 || pendingResets.size > 0;

  // --- Load scam detection ---
  const loadScam = useCallback(async () => {
    if (!token) return;
    setScamLoading(true);
    try {
      const res = await fetch("/api/v2/admin/scam-detection", {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data: ScamSettings = await res.json();
      if (!res.ok) throw new Error((data as unknown as { detail: string }).detail || "Failed");
      setScamEnabled(data.scam_detection);
      setScamTimeout(data.scam_timeout_minutes);
    } catch {
      // ignore — non-critical
    } finally {
      setScamLoading(false);
    }
  }, [token]);

  const saveScam = async () => {
    if (!token) return;
    setScamSaving(true);
    setScamMsg(null);
    setScamErr(null);
    try {
      const res = await fetch("/api/v2/admin/scam-detection", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ scam_detection: scamEnabled, scam_timeout_minutes: scamTimeout }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      setScamMsg(data.message || "Saved.");
    } catch (e: unknown) {
      setScamErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setScamSaving(false);
    }
  };

  // --- Load security config ---
  const loadCfg = useCallback(async () => {
    if (!token) return;
    setCfgLoading(true);
    setCfgErr(null);
    try {
      const res = await fetch("/api/v2/security/config", {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load config");
      setCfg(data as SecurityConfig);
      setLocalEdits({});
      setPendingResets(new Set());
    } catch (e: unknown) {
      setCfgErr(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setCfgLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadScam();
    void loadCfg();
  }, [loadScam, loadCfg]);

  // Get effective numeric value for a scalar config field (local edit wins over loaded config)
  const val = (key: string): number => {
    const edit = localEdits[key];
    if (edit !== undefined && typeof edit === "number") return edit;
    const loaded = cfg ? (cfg as unknown as Record<string, unknown>)[key] : undefined;
    return typeof loaded === "number" ? loaded : 0;
  };

  const overrides = [...(cfg?._overrides ?? []), ...Object.keys(localEdits)].filter(
    (k) => !pendingResets.has(k)
  );

  const handleChange = (key: string, v: number) => {
    setLocalEdits((prev) => ({ ...prev, [key]: v }));
    setPendingResets((prev) => {
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  };

  const handleReset = (key: string) => {
    setLocalEdits((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setPendingResets((prev) => new Set(prev).add(key));
  };

  const saveCfg = async () => {
    if (!token) return;
    setCfgSaving(true);
    setCfgMsg(null);
    setCfgErr(null);
    try {
      // Build the PATCH body: local edits as values, pending resets as explicit nulls
      const body: Record<string, number | null> = {};
      for (const [k, v] of Object.entries(localEdits)) {
        body[k] = v as number;
      }
      for (const k of pendingResets) {
        body[k] = null;
      }
      if (Object.keys(body).length === 0) {
        setCfgMsg("No changes to save.");
        return;
      }
      const res = await fetch("/api/v2/security/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      setCfgMsg(data.message || "Saved.");
      await loadCfg();
    } catch (e: unknown) {
      setCfgErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setCfgSaving(false);
    }
  };

  // Get the effective score_weights (local edits merged over loaded values)
  const effectiveWeights = (): Record<string, number> => {
    const base = cfg?.score_weights ?? {};
    const localW = localEdits["score_weights"];
    if (localW !== undefined && typeof localW === "object" && !Array.isArray(localW)) {
      return { ...base, ...(localW as Record<string, number>) };
    }
    return base;
  };

  // Update a single weight key in the localEdits score_weights map
  const handleWeightChange = (type: string, newVal: number) => {
    setLocalEdits((prev) => {
      const prevWeights =
        typeof prev["score_weights"] === "object" && prev["score_weights"] !== null
          ? (prev["score_weights"] as Record<string, number>)
          : {};
      return {
        ...prev,
        score_weights: { ...cfg?.score_weights, ...prevWeights, [type]: newVal },
      };
    });
    setPendingResets((prev) => {
      const next = new Set(prev);
      next.delete("score_weights");
      return next;
    });
  };

  if (cfgLoading && scamLoading) return <Skeleton className="h-64 w-full" />;

  return (
    <div className="space-y-6">
      {/* ── Scam Detection ────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <AlertTriangle className="size-4" />
            Scam Detection
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {scamLoading ? (
            <Skeleton className="h-8 w-full" />
          ) : (
            <>
              <div className="flex items-center gap-3">
                <Switch checked={scamEnabled} onCheckedChange={setScamEnabled} />
                <span className="text-sm font-medium">
                  {scamEnabled ? "Enabled" : "Disabled"}
                </span>
              </div>
              <div className="max-w-xs">
                <Label>Timeout (minutes)</Label>
                <Input
                  type="number"
                  min={1}
                  value={scamTimeout}
                  onChange={(e) => setScamTimeout(parseInt(e.target.value, 10) || 1)}
                  className="mt-1"
                />
                <p className="text-[11px] text-muted-foreground mt-1">
                  Duration to timeout users flagged as scammers.
                </p>
              </div>
              <Button size="sm" disabled={scamSaving} onClick={saveScam}>
                {scamSaving ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Settings className="mr-1 size-3" />}
                Save Scam Settings
              </Button>
              {scamMsg && <p className="text-xs text-muted-foreground">{scamMsg}</p>}
              {scamErr && <p className="text-xs text-destructive">{scamErr}</p>}
            </>
          )}
        </CardContent>
      </Card>

      {/* ── Security Thresholds ────────────────────────────────────────── */}
      {cfgErr ? (
        <p className="text-sm text-destructive">{cfgErr}</p>
      ) : cfg ? (
        <>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Threat Detection Thresholds</p>
              <p className="text-xs text-muted-foreground">
                Fields marked <Badge variant="secondary" className="text-[10px] px-1">custom</Badge>{" "}
                override the global default. Click <em>reset</em> to revert to the global default.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => void loadCfg()}>
                <RefreshCw className="size-4" />
              </Button>
              <Button
                size="sm"
                disabled={cfgSaving || !hasPendingChanges}
                onClick={() => void saveCfg()}
              >
                {cfgSaving ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Shield className="mr-1 size-3" />}
                Save Thresholds
              </Button>
            </div>
          </div>
          {cfgMsg && <p className="text-xs text-muted-foreground">{cfgMsg}</p>}
          {cfgErr && <p className="text-xs text-destructive">{cfgErr}</p>}

          {/* Detection Windows */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Detection Windows</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <CfgField label="Scan Interval (s)" fieldKey="scan_interval_seconds"
                description="How often the background scan loop runs."
                value={val("scan_interval_seconds")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
              <CfgField label="Lookback Window (s)" fieldKey="lookback_seconds"
                description="How far back detectors query for events."
                value={val("lookback_seconds")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
            </CardContent>
          </Card>

          {/* Economy Detectors */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Economy Detectors</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-3">
              <CfgField label="Income Velocity Limit" fieldKey="income_velocity_limit"
                description="Max income events within the lookback window."
                value={val("income_velocity_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Gambling Velocity Limit" fieldKey="gambling_velocity_limit"
                description="Max gambling transactions within the lookback window."
                value={val("gambling_velocity_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Wash Trade Min Cycles" fieldKey="wash_trade_min_cycles"
                description="Min cyclic trade count to flag wash trading."
                value={val("wash_trade_min_cycles")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={2} />
              <CfgField label="Transfer Ring Min Nodes" fieldKey="transfer_ring_min"
                description="Min nodes in a circular transfer ring to flag."
                value={val("transfer_ring_min")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={2} />
              <CfgField label="LP Churn Min Cycles" fieldKey="lp_churn_min"
                description="Min pool add/remove cycles to flag manipulation."
                value={val("lp_churn_min")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={2} />
              <CfgField label="TX Flood Limit" fieldKey="tx_flood_limit"
                description="Max transactions in the lookback window before flagging."
                value={val("tx_flood_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
            </CardContent>
          </Card>

          {/* API / Session Detectors */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">API / Session Detectors</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-3">
              <CfgField label="Auth Failure Limit" fieldKey="auth_failure_limit"
                description="Max failed auth attempts per window."
                value={val("auth_failure_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Auth Failure Window (s)" fieldKey="auth_failure_window"
                description="Counting window for auth failures."
                value={val("auth_failure_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={60} />
              <CfgField label="Session IP Change Window (s)" fieldKey="session_ip_change_window"
                description="Flag if a session's IP changes within this many seconds."
                value={val("session_ip_change_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
              <CfgField label="API Flood Limit (req/window)" fieldKey="api_request_flood_limit"
                description="Max API requests per flood window per user."
                value={val("api_request_flood_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
              <CfgField label="API Flood Window (s)" fieldKey="api_request_flood_window"
                description="Counting window for API flood detection."
                value={val("api_request_flood_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
            </CardContent>
          </Card>

          {/* Command Flood */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Bot Command Flood</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-3">
              <CfgField label="Command Flood Limit" fieldKey="command_flood_limit"
                description="Max distinct commands per window."
                value={val("command_flood_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Command Flood Window (s)" fieldKey="command_flood_window"
                description="Counting window for command flood."
                value={val("command_flood_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
              <CfgField label="Identical Command Limit" fieldKey="identical_command_limit"
                description="Max identical command invocations per window."
                value={val("identical_command_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
            </CardContent>
          </Card>

          {/* Cross-Platform Correlation */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Cross-Platform Correlation</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <CfgField label="Correlation Window (s)" fieldKey="correlation_window"
                description="Window for linking bot + API events."
                value={val("correlation_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
              <CfgField label="Correlation Event Min" fieldKey="correlation_event_min"
                description="Min events from both platforms to trigger flag."
                value={val("correlation_event_min")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={2} />
            </CardContent>
          </Card>

          {/* DeFi Exploits */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">DeFi Exploit Patterns</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-3">
              <CfgField label="Flash Loan Window (s)" fieldKey="flash_loan_window"
                description="Borrow→trade→repay within N seconds triggers flag."
                value={val("flash_loan_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={5} />
              <CfgField label="Oracle Manipulation Trades" fieldKey="oracle_manipulation_trades"
                description="Rapid same-token trades to flag oracle manipulation."
                value={val("oracle_manipulation_trades")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={2} />
              <CfgField label="Oracle Manipulation Window (s)" fieldKey="oracle_manipulation_window"
                description="Counting window for oracle manipulation."
                value={val("oracle_manipulation_window")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={10} />
            </CardContent>
          </Card>

          {/* Threat Scoring */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Threat Scoring</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <CfgField label="Score Decay Half-Life (s)" fieldKey="score_decay_half_life"
                  description="Time (seconds) for a threat score to halve via exponential decay."
                  value={val("score_decay_half_life")} overrides={overrides}
                  onChange={handleChange} onReset={handleReset} step={60} min={60} />
              </div>
              <div>
                <p className="text-sm font-medium mb-2">Score Weights (points per detection type)</p>
                <div className="grid gap-3 sm:grid-cols-3">
                  {Object.entries(effectiveWeights()).map(([type, pts]) => (
                    <div key={type} className="space-y-1">
                      <Label className="text-xs capitalize">{type.replace(/_/g, " ")}</Label>
                      <Input
                        type="number"
                        step={0.5}
                        min={0}
                        value={pts}
                        onChange={(e) => handleWeightChange(type, parseFloat(e.target.value) || 0)}
                        className="font-mono"
                      />
                    </div>
                  ))}
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Response Level Thresholds */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Response Level Thresholds</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-xs text-muted-foreground mb-3">
                Threat score required to trigger each automatic response level.
              </p>
              <div className="grid gap-4 sm:grid-cols-5">
                {(
                  [
                    ["level_1_threshold", "L1 — Monitor", "Log + monitor"],
                    ["level_2_threshold", "L2 — Throttle", "Rate-limit user"],
                    ["level_3_threshold", "L3 — Freeze", "Freeze all activity"],
                    ["level_4_threshold", "L4 — Flag", "Flag + admin alert"],
                    ["level_5_threshold", "L5 — Lockdown", "Emergency lockdown"],
                  ] as [string, string, string][]
                ).map(([key, label, desc]) => (
                  <CfgField key={key} label={label} fieldKey={key}
                    description={desc}
                    value={val(key)} overrides={overrides}
                    onChange={handleChange} onReset={handleReset} step={0.5} min={1} />
                ))}
              </div>
            </CardContent>
          </Card>

          {/* Enforcement Durations */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Enforcement Durations</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-3">
              <CfgField label="Throttle Duration (s)" fieldKey="throttle_duration"
                description="How long a level-2 throttle lasts."
                value={val("throttle_duration")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
              <CfgField label="Freeze Duration (s)" fieldKey="freeze_duration"
                description="How long a level-3 freeze lasts."
                value={val("freeze_duration")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
              <CfgField label="Flag Duration (s)" fieldKey="flag_duration"
                description="How long a level-4 flag lasts."
                value={val("flag_duration")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
              <CfgField label="Lockdown Duration (s)" fieldKey="lockdown_duration"
                description="How long a level-5 lockdown lasts."
                value={val("lockdown_duration")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
              <CfgField label="Throttled Rate Limit" fieldKey="throttled_rate_limit"
                description="Requests per 10-second window for throttled users."
                value={val("throttled_rate_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Alert Cooldown (s)" fieldKey="alert_cooldown_seconds"
                description="Min seconds between admin alerts for the same user."
                value={val("alert_cooldown_seconds")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={30} />
            </CardContent>
          </Card>

          {/* Behavior Profiling */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Behavior Profiling</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <CfgField label="Anomaly Std-Dev Threshold" fieldKey="anomaly_stddev_threshold"
                description="Std-deviation multiplier to flag statistical anomalies."
                value={val("anomaly_stddev_threshold")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} step={0.1} min={0.5} />
              <CfgField label="Baseline Min Samples" fieldKey="baseline_min_samples"
                description="Data-points needed before anomaly detection activates."
                value={val("baseline_min_samples")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={5} />
              <CfgField label="Whale Concentration Limit" fieldKey="whale_concentration_limit"
                description="Token concentration multiplier to flag a whale."
                value={val("whale_concentration_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
              <CfgField label="Repeat Offender Limit" fieldKey="repeat_offender_limit"
                description="Enforcements before a user is treated as repeat offender."
                value={val("repeat_offender_limit")} overrides={overrides}
                onChange={handleChange} onReset={handleReset} min={1} />
            </CardContent>
          </Card>

          {/* Bottom save bar */}
          <div className="flex items-center gap-3">
            <Button
              disabled={cfgSaving || !hasPendingChanges}
              onClick={() => void saveCfg()}
            >
              {cfgSaving ? <RefreshCw className="mr-1 size-4 animate-spin" /> : <Shield className="mr-1 size-4" />}
              Save All Threshold Changes
            </Button>
            {hasPendingChanges && (
              <p className="text-xs text-muted-foreground">
                {Object.keys(localEdits).length} edit(s), {pendingResets.size} reset(s) pending
              </p>
            )}
            {cfgMsg && <p className="text-xs text-muted-foreground">{cfgMsg}</p>}
            {cfgErr && <p className="text-xs text-destructive">{cfgErr}</p>}
          </div>
        </>
      ) : null}
    </div>
  );
}

// --- Modules Tab ---

const MODULE_GROUPS: { label: string; modules: string[] }[] = [
  { label: "Economy", modules: ["module_economy", "module_daily", "module_work", "module_shop"] },
  { label: "Gambling", modules: ["module_gambling", "module_gambling_coinflip", "module_gambling_dice", "module_gambling_roulette", "module_gambling_blackjack", "module_gambling_slots", "module_games"] },
  { label: "Finance", modules: ["module_lending", "module_savings", "module_staking", "module_pools"] },
  { label: "Crypto / Chain", modules: ["module_crypto", "module_mining", "module_chain", "module_validators", "module_contracts"] },
  { label: "Social / Other", modules: ["module_groups", "module_drops", "module_faucet", "module_chart"] },
];

function fmtModule(key: string): string {
  return key.replace(/^module_/, "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function ModulesTab() {
  const token = useAuthStore((s) => s.token);
  const { data: modules, loading, error, refetch } = useApi<ModuleStatus[]>("/admin/modules");
  const [toggling, setToggling] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const toggle = async (mod: string, enabled: boolean) => {
    if (!token) return;
    setToggling(mod);
    setMsg(null);
    try {
      const res = await fetch(`/api/v2/admin/modules/${mod}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        const d = await res.json();
        throw new Error((d as { detail?: string }).detail || "Toggle failed");
      }
      refetch();
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setToggling(null);
    }
  };

  const moduleMap = new Map(modules?.map((m) => [m.module, m.enabled]) ?? []);

  if (loading) return <div className="space-y-3">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-32 w-full" />)}</div>;
  if (error) return <div className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="size-4" />{error}</div>;

  return (
    <div className="space-y-4">
      {msg && <p className="text-xs text-destructive">{msg}</p>}
      {MODULE_GROUPS.map((group) => (
        <Card key={group.label}>
          <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Layers className="size-4" />{group.label}</CardTitle></CardHeader>
          <CardContent>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {group.modules.map((mod) => {
                const enabled = moduleMap.get(mod) ?? true;
                const isToggling = toggling === mod;
                return (
                  <div key={mod} className="flex items-center justify-between rounded-lg border px-3 py-2">
                    <Label className="cursor-pointer text-sm" htmlFor={`mod-${mod}`}>{fmtModule(mod)}</Label>
                    <Switch
                      id={`mod-${mod}`}
                      checked={enabled}
                      disabled={isToggling}
                      onCheckedChange={(v) => void toggle(mod, v)}
                    />
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// --- Channels Tab ---

interface ChannelAssignment {
  channel_key: string;
  channel_id: string | null;
}

const CHANNEL_GROUPS: { label: string; channels: { key: string; label: string }[] }[] = [
  {
    label: "Trading & Markets",
    channels: [
      { key: "trade_channel", label: "Trade Feed" },
      { key: "crypto_channel", label: "Crypto Feed" },
      { key: "pools_channel", label: "Pools Feed" },
      { key: "gambling_channel", label: "Gambling Feed" },
      { key: "staking_channel", label: "Staking Feed" },
    ],
  },
  {
    label: "Mining & Chain",
    channels: [
      { key: "mine_channel", label: "Mining Feed" },
      { key: "validators_channel", label: "Validators Feed" },
      { key: "contracts_channel", label: "Contracts Feed" },
    ],
  },
  {
    label: "Drops & Faucet",
    channels: [
      { key: "drops_spawn_channel", label: "Drops Spawn Channel" },
      { key: "drops_channel", label: "Drops Log Feed" },
      { key: "faucet_channel", label: "Faucet Channel" },
      { key: "job_channel", label: "Job / Work Feed" },
      { key: "wallet_channel", label: "DeFi Wallet Feed" },
    ],
  },
  {
    label: "Admin & Alerts",
    channels: [
      { key: "error_channel", label: "Error Log" },
      { key: "scam_channel", label: "Scam Alerts" },
      { key: "whale_alerts_channel", label: "Whale Alerts" },
      { key: "reports_feed_channel", label: "Reports Feed" },
      { key: "security_log_channel", label: "Security Audit Log" },
    ],
  },
];

function ChannelsTab() {
  const token = useAuthStore((s) => s.token);
  const { data: channelsData, loading, error, refetch } = useApi<ChannelAssignment[]>("/admin/channels");
  const [assignments, setAssignments] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [prefilled, setPrefilled] = useState(false);

  useEffect(() => {
    if (channelsData && !prefilled) {
      const map: Record<string, string> = {};
      for (const a of channelsData) map[a.channel_key] = a.channel_id ?? "";
      setAssignments(map);
      setPrefilled(true);
    }
  }, [channelsData, prefilled]);

  const save = async () => {
    if (!token) return;
    setSaving(true);
    setSaveMsg(null);
    setSaveErr(null);
    try {
      const body: Record<string, string | null> = {};
      for (const [k, v] of Object.entries(assignments)) {
        body[k] = v.trim() || null;
      }
      const res = await fetch("/api/v2/admin/channels", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ assignments: body }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data as { detail?: string }).detail || "Save failed");
      setSaveMsg((data as { message?: string }).message || "Channels saved.");
      refetch();
    } catch (e: unknown) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="space-y-3">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-40 w-full" />)}</div>;
  if (error) return <div className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="size-4" />{error}</div>;

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">Enter the Discord channel ID (right-click a channel → Copy ID). Leave blank to unset.</p>
      {CHANNEL_GROUPS.map((group) => (
        <Card key={group.label}>
          <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Hash className="size-4" />{group.label}</CardTitle></CardHeader>
          <CardContent>
            <div className="grid gap-3 sm:grid-cols-2">
              {group.channels.map(({ key, label }) => (
                <div key={key}>
                  <Label>{label}</Label>
                  <Input
                    value={assignments[key] ?? ""}
                    onChange={(e) => setAssignments((prev) => ({ ...prev, [key]: e.target.value }))}
                    className="mt-1 font-mono"
                    placeholder="Channel ID…"
                  />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ))}
      <div className="flex items-center gap-3">
        <Button disabled={saving} onClick={save}>
          {saving ? <RefreshCw className="mr-1 size-4 animate-spin" /> : <Hash className="mr-1 size-4" />}
          Save Channels
        </Button>
        {saveMsg && <p className="text-xs text-muted-foreground">{saveMsg}</p>}
        {saveErr && <p className="text-xs text-destructive">{saveErr}</p>}
      </div>
    </div>
  );
}

// --- AI Tab ---

const AI_FEATURES: { key: keyof GuildSettings; label: string; description: string }[] = [
  { key: "ai_mm_enabled", label: "Market Maker AI", description: "AI decides buy/sell timing and size for the market maker bot" },
  { key: "ai_chat_enabled", label: "Chat AI (.ask)", description: "Lets users chat with the AI directly in Discord" },
  { key: "ai_commentary_enabled", label: "Market Commentary", description: "AI posts commentary to the crypto channel after large price moves" },
  { key: "ai_flavor_enabled", label: "Flavor Text (.work)", description: "AI generates flavor text for .work command responses" },
  { key: "ai_events_enabled", label: "Event Narration", description: "AI narrates notable trade events (large buys, liquidations, etc.)" },
];

const AI_PROMPTS: { key: keyof GuildSettings; label: string }[] = [
  { key: "ai_prompt_chat", label: "Chat System Prompt" },
  { key: "ai_prompt_commentary", label: "Commentary System Prompt" },
  { key: "ai_prompt_events", label: "Events System Prompt" },
  { key: "ai_prompt_flavor", label: "Flavor Text System Prompt" },
];

function AITab() {
  const token = useAuthStore((s) => s.token);
  const { data: settings, loading, error, refetch } = useApi<GuildSettings>("/admin/settings");
  const [toggles, setToggles] = useState<Record<string, boolean>>({});
  const [prompts, setPrompts] = useState<Record<string, string>>({});
  const [personaName, setPersonaName] = useState("");
  const [toggling, setToggling] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [prefilled, setPrefilled] = useState(false);

  useEffect(() => {
    if (settings && !prefilled) {
      setToggles({
        ai_mm_enabled: settings.ai_mm_enabled ?? true,
        ai_chat_enabled: settings.ai_chat_enabled ?? true,
        ai_commentary_enabled: settings.ai_commentary_enabled ?? true,
        ai_flavor_enabled: settings.ai_flavor_enabled ?? false,
        ai_events_enabled: settings.ai_events_enabled ?? true,
      });
      setPrompts({
        ai_prompt_chat: settings.ai_prompt_chat ?? "",
        ai_prompt_commentary: settings.ai_prompt_commentary ?? "",
        ai_prompt_events: settings.ai_prompt_events ?? "",
        ai_prompt_flavor: settings.ai_prompt_flavor ?? "",
      });
      setPersonaName(settings.ai_persona_name ?? "");
      setPrefilled(true);
    }
  }, [settings, prefilled]);

  const handleToggle = async (key: string, enabled: boolean) => {
    if (!token) return;
    setToggling(key);
    setToggles((prev) => ({ ...prev, [key]: enabled }));
    try {
      const res = await fetch("/api/v2/admin/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ [key]: enabled }),
      });
      if (!res.ok) {
        const d = await res.json();
        throw new Error((d as { detail?: string }).detail || "Toggle failed");
      }
    } catch (e: unknown) {
      // Revert on failure
      setToggles((prev) => ({ ...prev, [key]: !enabled }));
      setSaveErr(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setToggling(null);
    }
  };

  const savePrompts = async () => {
    if (!token) return;
    setSaving(true);
    setSaveMsg(null);
    setSaveErr(null);
    try {
      const body: Record<string, unknown> = { ...prompts };
      if (personaName.trim()) body.ai_persona_name = personaName.trim();
      const res = await fetch("/api/v2/admin/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data as { detail?: string }).detail || "Save failed");
      setSaveMsg((data as { message?: string }).message || "Prompts saved.");
      refetch();
    } catch (e: unknown) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-32 w-full" />)}</div>;
  if (error) return <div className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="size-4" />{error}</div>;

  return (
    <div className="space-y-4">
      {/* Feature toggles */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Bot className="size-4" />AI Features</CardTitle></CardHeader>
        <CardContent>
          <div className="space-y-3">
            {AI_FEATURES.map(({ key, label, description }) => (
              <div key={key} className="flex items-start justify-between gap-4 rounded-lg border px-3 py-3">
                <div>
                  <Label className="text-sm">{label}</Label>
                  <p className="text-xs text-muted-foreground">{description}</p>
                </div>
                <Switch
                  checked={toggles[key as string] ?? false}
                  disabled={toggling === key}
                  onCheckedChange={(v) => void handleToggle(key as string, v)}
                />
              </div>
            ))}
          </div>
          {saveErr && <p className="text-xs text-destructive mt-2">{saveErr}</p>}
        </CardContent>
      </Card>

      {/* Persona */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Bot className="size-4" />Persona</CardTitle></CardHeader>
        <CardContent>
          <Label>Active Persona Name</Label>
          <Input value={personaName} onChange={(e) => setPersonaName(e.target.value)} className="mt-1" placeholder="Default persona…" />
          <p className="text-xs text-muted-foreground mt-1">Name of the MM persona to activate (must exist in the Personas list)</p>
        </CardContent>
      </Card>

      {/* Prompts */}
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Bot className="size-4" />System Prompts <span className="text-muted-foreground font-normal text-xs">(leave blank to use defaults)</span></CardTitle></CardHeader>
        <CardContent className="space-y-4">
          {AI_PROMPTS.map(({ key, label }) => (
            <div key={key}>
              <Label>{label}</Label>
              <textarea
                value={prompts[key as string] ?? ""}
                onChange={(e) => setPrompts((prev) => ({ ...prev, [key as string]: e.target.value }))}
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono resize-y min-h-[80px] ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="Leave blank to use the default system prompt…"
              />
            </div>
          ))}
          <div className="flex items-center gap-3">
            <Button size="sm" disabled={saving} onClick={savePrompts}>
              {saving ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Bot className="mr-1 size-3" />}
              Save Prompts &amp; Persona
            </Button>
            {saveMsg && <p className="text-xs text-muted-foreground">{saveMsg}</p>}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// --- NFTs Tab ---

interface AdminNFTCollection {
  id: number;
  symbol: string;
  name: string;
  network: string;
  minted_count: number;
  max_supply: number | null;
  mint_price: number;
  mint_token: string;
  image_url: string | null;
  contract_address: string;
  created_at: string | null;
}

function NFTsTab() {
  const token = useAuthStore((s) => s.token);
  const { data: collections, loading, error, refetch } = useApi<AdminNFTCollection[]>("/admin/nfts");

  // Create form state
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [network, setNetwork] = useState("ARC");
  const [mintPrice, setMintPrice] = useState("");
  const [mintToken, setMintToken] = useState("ARC");
  const [maxSupply, setMaxSupply] = useState("");
  const [createGalleryFiles, setCreateGalleryFiles] = useState<FileList | null>(null);
  const [creating, setCreating] = useState(false);
  const [createMsg, setCreateMsg] = useState("");

  // Per-row gallery upload state: symbol → FileList
  const [galleryFiles, setGalleryFiles] = useState<Record<string, FileList | null>>({});
  const [galleryUploading, setGalleryUploading] = useState<Record<string, boolean>>({});
  const [galleryMsg, setGalleryMsg] = useState<Record<string, string>>({});
  const [deleting, setDeleting] = useState<string | null>(null);

  async function handleCreate() {
    if (!symbol || !name || !mintPrice) return;
    setCreating(true);
    setCreateMsg("");
    try {
      const res = await fetch("/api/v2/admin/nfts", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          symbol: symbol.toUpperCase(),
          name,
          description,
          network,
          mint_price: parseFloat(mintPrice),
          mint_token: mintToken || network,
          max_supply: maxSupply ? parseInt(maxSupply) : null,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setCreateMsg(data.detail || "Failed to create collection");
        return;
      }
      // Upload gallery images if any were selected
      let finalMsg = `Collection ${data.symbol} created!`;
      if (createGalleryFiles && createGalleryFiles.length > 0) {
        const form = new FormData();
        for (let i = 0; i < createGalleryFiles.length; i++) form.append("files", createGalleryFiles[i]);
        const gRes = await fetch(`/api/v2/admin/nfts/${data.symbol}/gallery`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        });
        const gData = await gRes.json().catch(() => ({}));
        if (gRes.ok || gRes.status === 207) {
          finalMsg += ` Uploaded ${gData.uploaded} image(s).`;
        } else {
          finalMsg += ` (Image upload failed: ${gData.detail || "unknown error"})`;
        }
      }
      setCreateMsg(finalMsg);
      setSymbol(""); setName(""); setDescription(""); setMintPrice(""); setMintToken("ARC"); setMaxSupply(""); setCreateGalleryFiles(null);
      refetch();
    } catch {
      setCreateMsg("An error occurred");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(sym: string) {
    if (!confirm(`Delete collection ${sym}? This cannot be undone.`)) return;
    setDeleting(sym);
    try {
      const res = await fetch(`/api/v2/admin/nfts/${sym}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        refetch();
      }
    } finally {
      setDeleting(null);
    }
  }

  async function handleGalleryUpload(sym: string) {
    const files = galleryFiles[sym];
    if (!files || files.length === 0) return;
    setGalleryUploading((prev) => ({ ...prev, [sym]: true }));
    setGalleryMsg((prev) => ({ ...prev, [sym]: "" }));
    try {
      const form = new FormData();
      for (let i = 0; i < files.length; i++) form.append("files", files[i]);
      const res = await fetch(`/api/v2/admin/nfts/${sym}/gallery`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: form,
      });
      const data = await res.json();
      if (res.ok || res.status === 207) {
        setGalleryMsg((prev) => ({ ...prev, [sym]: `Uploaded ${data.uploaded} image(s)` }));
        setGalleryFiles((prev) => ({ ...prev, [sym]: null }));
      } else {
        setGalleryMsg((prev) => ({ ...prev, [sym]: data.detail || "Upload failed" }));
      }
    } catch {
      setGalleryMsg((prev) => ({ ...prev, [sym]: "Upload error" }));
    } finally {
      setGalleryUploading((prev) => ({ ...prev, [sym]: false }));
    }
  }

  return (
    <div className="space-y-6">
      {/* Collection List */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Image className="size-4" />
            NFT Collections
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          ) : error ? (
            <div className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="size-4" />{error}</div>
          ) : !collections || collections.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">No collections yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Network</TableHead>
                    <TableHead>Minted / Supply</TableHead>
                    <TableHead>Mint Price</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {collections.map((col) => (
                    <TableRow key={col.id}>
                      <TableCell className="font-mono font-semibold">{col.symbol}</TableCell>
                      <TableCell>{col.name}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">{col.network}</Badge>
                      </TableCell>
                      <TableCell className="text-sm">
                        {col.minted_count}{col.max_supply ? `/${col.max_supply}` : ""}
                      </TableCell>
                      <TableCell className="text-sm">
                        {col.mint_price} {col.mint_token}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2 flex-wrap">
                          {/* Gallery upload inline */}
                          <label className="cursor-pointer">
                            <input
                              type="file"
                              multiple
                              accept="image/jpeg,image/png,image/gif,image/webp"
                              className="sr-only"
                              onChange={(e) => setGalleryFiles((prev) => ({ ...prev, [col.symbol]: e.target.files }))}
                            />
                            <span className="inline-flex items-center gap-1 rounded-md border border-input bg-background px-2 py-1 text-xs hover:bg-accent hover:text-accent-foreground transition-colors">
                              <Upload className="size-3" />
                              {galleryFiles[col.symbol] ? `${galleryFiles[col.symbol]!.length} file(s)` : "Browse"}
                            </span>
                          </label>
                          {galleryFiles[col.symbol] && (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={galleryUploading[col.symbol]}
                              onClick={() => handleGalleryUpload(col.symbol)}
                            >
                              {galleryUploading[col.symbol] ? "Uploading..." : "Upload"}
                            </Button>
                          )}
                          {galleryMsg[col.symbol] && (
                            <span className="text-xs text-muted-foreground">{galleryMsg[col.symbol]}</span>
                          )}
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={deleting === col.symbol}
                            onClick={() => handleDelete(col.symbol)}
                          >
                            <Trash2 className="size-3" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Create Collection */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Plus className="size-4" />
            Create Collection
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <Label className="text-xs">Symbol *</Label>
              <Input
                placeholder="e.g. PUNKS"
                value={symbol}
                maxLength={10}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="mt-1 font-mono uppercase"
              />
            </div>
            <div>
              <Label className="text-xs">Name *</Label>
              <Input
                placeholder="Collection name"
                value={name}
                maxLength={50}
                onChange={(e) => setName(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label className="text-xs">Network *</Label>
              <select
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                value={network}
                onChange={(e) => setNetwork(e.target.value)}
              >
                <option value="ARC">ARC — Arcadia</option>
                <option value="DSC">DSC — Discoin</option>
              </select>
            </div>
            <div>
              <Label className="text-xs">Mint Price *</Label>
              <Input
                type="number"
                min="0"
                step="0.0001"
                placeholder="0.05"
                value={mintPrice}
                onChange={(e) => setMintPrice(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label className="text-xs">Mint Token</Label>
              <Input
                placeholder="ARC"
                value={mintToken}
                maxLength={10}
                onChange={(e) => setMintToken(e.target.value.toUpperCase())}
                className="mt-1 font-mono uppercase"
              />
            </div>
            <div>
              <Label className="text-xs">Max Supply</Label>
              <Input
                type="number"
                min="1"
                placeholder="Unlimited"
                value={maxSupply}
                onChange={(e) => setMaxSupply(e.target.value)}
                className="mt-1"
              />
            </div>
          </div>
          <div>
            <Label className="text-xs">Description</Label>
            <textarea
              placeholder="Optional description..."
              value={description}
              maxLength={500}
              onChange={(e) => setDescription(e.target.value)}
              className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm resize-none min-h-[60px] ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <div>
            <Label className="text-xs flex items-center gap-1">
              <Upload className="size-3" />
              Gallery Images (optional - upload after creation)
            </Label>
            <input
              type="file"
              multiple
              accept="image/jpeg,image/png,image/gif,image/webp"
              onChange={(e) => setCreateGalleryFiles(e.target.files)}
              className="mt-1 w-full cursor-pointer rounded-md border border-input bg-background px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-primary/10 file:px-2 file:py-1 file:text-xs file:text-primary"
            />
            {createGalleryFiles && createGalleryFiles.length > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">{createGalleryFiles.length} image{createGalleryFiles.length !== 1 ? "s" : ""} selected</p>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" disabled={creating || !symbol || !name || !mintPrice} onClick={handleCreate}>
              {creating ? <RefreshCw className="mr-1 size-3 animate-spin" /> : <Plus className="mr-1 size-3" />}
              Create Collection
            </Button>
            {createMsg && <p className="text-xs text-muted-foreground">{createMsg}</p>}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// --- Main Component ---

export default function AdminPage() {
  const { data: stats, loading: statsLoading, error: statsError } = useApi<ServerStats>("/stats/stats");

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2">
          <Shield className="size-5 text-primary" />
          <h1 className="text-2xl font-bold tracking-tight">Admin Panel</h1>
        </div>
        <p className="text-sm text-muted-foreground">
          Server administration and economy management
        </p>
      </div>

      {/* Quick stats */}
      <div className="grid gap-4 sm:grid-cols-4">
        {statsLoading ? (
          <>
            {Array.from({ length: 4 }).map((_, i) => (
              <Card key={i}>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="size-4" />
                </CardHeader>
                <CardContent>
                  <Skeleton className="h-7 w-20" />
                </CardContent>
              </Card>
            ))}
          </>
        ) : statsError ? (
          <Card className="sm:col-span-4">
            <CardContent className="flex items-center gap-2 py-6 text-sm text-destructive">
              <AlertCircle className="size-4" />
              {statsError}
            </CardContent>
          </Card>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Total Users
                </CardTitle>
                <Users className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmtNum(stats?.total_users)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Total Tokens
                </CardTitle>
                <Coins className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmtNum(stats?.total_tokens)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  24h Transactions
                </CardTitle>
                <Activity className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmtNum(stats?.total_trades)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Treasury
                </CardTitle>
                <AlertTriangle className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{fmt(stats?.treasury_balance)}</div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      <Tabs defaultValue="users">
        <TabsList className="flex-wrap h-auto">
          <TabsTrigger value="users">Users</TabsTrigger>
          <TabsTrigger value="economy">Economy</TabsTrigger>
          <TabsTrigger value="tokens">Tokens</TabsTrigger>
          <TabsTrigger value="modules">
            <Layers className="mr-1 size-3.5" />
            Modules
          </TabsTrigger>
          <TabsTrigger value="channels">
            <Hash className="mr-1 size-3.5" />
            Channels
          </TabsTrigger>
          <TabsTrigger value="ai">
            <Bot className="mr-1 size-3.5" />
            AI
          </TabsTrigger>
          <TabsTrigger value="config">Config</TabsTrigger>
          <TabsTrigger value="permissions">Permissions</TabsTrigger>
          <TabsTrigger value="security">
            <ShieldAlert className="mr-1 size-3.5" />
            Security
          </TabsTrigger>
          <TabsTrigger value="nfts">
            <Image className="mr-1 size-3.5" />
            NFTs
          </TabsTrigger>
        </TabsList>
        <TabsContent value="users" className="mt-4">
          <UsersTab />
        </TabsContent>
        <TabsContent value="economy" className="mt-4">
          <EconomyTab />
        </TabsContent>
        <TabsContent value="tokens" className="mt-4">
          <TokensTab />
        </TabsContent>
        <TabsContent value="modules" className="mt-4">
          <ModulesTab />
        </TabsContent>
        <TabsContent value="channels" className="mt-4">
          <ChannelsTab />
        </TabsContent>
        <TabsContent value="ai" className="mt-4">
          <AITab />
        </TabsContent>
        <TabsContent value="config" className="mt-4">
          <ConfigTab />
        </TabsContent>
        <TabsContent value="permissions" className="mt-4">
          <PermissionsTab />
        </TabsContent>
        <TabsContent value="security" className="mt-4">
          <SecurityTab />
        </TabsContent>
        <TabsContent value="nfts" className="mt-4">
          <NFTsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
