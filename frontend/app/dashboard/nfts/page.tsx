"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Image,
  Search,
  ShoppingCart,
  Tag,
  Package,
  AlertCircle,
  ExternalLink,
  Hash,
  ArrowUpDown,
  Filter,
  Clock,
  TrendingUp,
  Layers,
  Copy,
  Check,
  ChevronDown,
  Upload,
  Rocket,
  X,
  RefreshCw,
} from "lucide-react";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import ModuleGate from "@/components/ModuleGate";

/* ─── Interfaces ───────────────────────────────────────────────────────── */

interface NFTCollection {
  id: number;
  name: string;
  symbol: string;
  network: string;
  description: string;
  image_url: string | null;
  mint_price: number;
  mint_token: string;
  mint_price_usd: number | null;
  max_supply: number | null;
  minted_count: number;
  contract_address: string;
  created_at: string;
}

interface NFTItem {
  id: number;
  token_id: number;
  name: string;
  rarity: string;
  image_url: string | null;
  collection_name: string | null;
  collection_symbol: string | null;
  collection_id: number | null;
  network: string;
  contract_address: string;
  token_hash: string;
  is_listed: boolean;
  minted_at: string;
}

interface NFTListing {
  listing_id: number;
  nft_id: number;
  token_id: number;
  name: string;
  rarity: string;
  image_url: string | null;
  price: number;
  currency: string;
  price_usd: number | null;
  seller_id: string;
  collection_name: string | null;
  collection_symbol: string | null;
  network: string;
  contract_address: string;
  token_hash: string;
  listed_at: string;
}

interface NFTDetail {
  id: number;
  token_id: number;
  name: string;
  description: string;
  rarity: string;
  image_url: string | null;
  token_hash: string;
  owner_id: string;
  minted_by: string;
  collection_name: string;
  collection_symbol: string;
  collection_id: number;
  network: string;
  contract_address: string;
  mint_price: number;
  mint_token: string;
  minted_at: string;
  mint_price_usd: number | null;
  listing: { listing_id: number; price: number; currency: string; price_usd: number | null; listed_at: string } | null;
  sales: { price: number; currency: string; price_usd: number | null; buyer_id: string; seller_id: string; sold_at: string }[];
}

interface CollectionDetail {
  collection: NFTCollection & {
    floor_price: number | null;
    floor_currency: string | null;
    floor_price_usd: number | null;
    total_sales: number;
    total_volume: number;
  };
  nfts: {
    id: number;
    token_id: number;
    name: string;
    rarity: string;
    image_url: string | null;
    owner_id: string;
    token_hash: string;
    minted_at: string;
  }[];
  total: number;
}

interface MyNFTsResponse {
  nfts: NFTItem[];
  total: number;
}

interface MarketplaceResponse {
  listings: NFTListing[];
  total: number;
}

/* ─── Constants ────────────────────────────────────────────────────────── */

const RARITY_COLORS: Record<string, string> = {
  common: "bg-zinc-500/15 text-zinc-400",
  uncommon: "bg-green-500/15 text-green-400",
  rare: "bg-blue-500/15 text-blue-400",
  epic: "bg-purple-500/15 text-purple-400",
  legendary: "bg-amber-500/15 text-amber-400",
};

const RARITY_BORDER: Record<string, string> = {
  common: "border-zinc-500/20",
  uncommon: "border-green-500/30",
  rare: "border-blue-500/30",
  epic: "border-purple-500/30",
  legendary: "border-amber-500/30",
};

const RARITY_GLOW: Record<string, string> = {
  legendary: "shadow-amber-500/20",
  epic: "shadow-purple-500/15",
  rare: "shadow-blue-500/10",
};

const NETWORK_BADGE: Record<string, string> = {
  ARC: "bg-blue-500/15 text-blue-400",
  DSC: "bg-emerald-500/15 text-emerald-400",
};

/* ─── Helpers ──────────────────────────────────────────────────────────── */

function coinPrice(amount: number, token: string, usd: number | null | undefined): string {
  const coinStr = `${amount.toLocaleString(undefined, { maximumFractionDigits: 4 })} ${token}`;
  if (usd != null && token !== "USD") {
    return `${coinStr} (~$${usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })})`;
  }
  return coinStr;
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

/* ─── Components ───────────────────────────────────────────────────────── */

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [text]);
  return (
    <button onClick={handleCopy} className="inline-flex items-center text-muted-foreground hover:text-foreground transition-colors" title="Copy">
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  );
}

function NFTCard({
  name,
  rarity,
  imageUrl,
  collectionName,
  tokenId,
  network,
  tokenHash,
  children,
  onClick,
}: {
  name: string;
  rarity: string;
  imageUrl: string | null;
  collectionName: string | null;
  tokenId?: number;
  network?: string;
  tokenHash?: string;
  children?: React.ReactNode;
  onClick?: () => void;
}) {
  const rarityClass = RARITY_COLORS[rarity] || RARITY_COLORS.common;
  const borderClass = RARITY_BORDER[rarity] || "border-border";
  const glowClass = RARITY_GLOW[rarity] || "";
  const netBadge = NETWORK_BADGE[network || ""] || "bg-zinc-500/15 text-zinc-400";

  return (
    <div
      className={`group flex flex-col overflow-hidden rounded-xl border ${borderClass} bg-card transition-all hover:border-primary/30 hover:shadow-lg ${glowClass} ${onClick ? "cursor-pointer" : ""}`}
      onClick={onClick}
    >
      <div className="relative aspect-square bg-muted/50">
        {imageUrl ? (
          <img
            src={imageUrl}
            alt={name}
            className="size-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <div className="flex size-full items-center justify-center bg-gradient-to-br from-muted/30 to-muted/60">
            <Image className="size-12 text-muted-foreground/30" />
          </div>
        )}
        <div className="absolute right-2 top-2 flex gap-1">
          <Badge className={`${rarityClass} border-none text-xs capitalize`}>
            {rarity}
          </Badge>
        </div>
        {network && (
          <Badge className={`absolute left-2 top-2 ${netBadge} border-none text-xs`}>
            {network}
          </Badge>
        )}
        {tokenId !== undefined && (
          <div className="absolute bottom-2 left-2 rounded-md bg-black/60 px-2 py-0.5 text-xs font-mono text-white/80">
            #{tokenId}
          </div>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div>
          <p className="truncate font-semibold">{name}</p>
          <div className="flex items-center gap-1">
            {collectionName && (
              <p className="truncate text-xs text-muted-foreground">{collectionName}</p>
            )}
          </div>
        </div>
        {tokenHash && (
          <div className="flex items-center gap-1 text-[10px] font-mono text-muted-foreground/70">
            <Hash className="size-2.5 shrink-0" />
            <span className="truncate select-all" title={tokenHash}>{tokenHash}</span>
            <CopyButton text={tokenHash} />
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

function CollectionCard({
  collection,
  onClick,
}: {
  collection: NFTCollection;
  onClick?: () => void;
}) {
  const netBadge = NETWORK_BADGE[collection.network || ""] || "bg-zinc-500/15 text-zinc-400";
  const progress = collection.max_supply
    ? Math.min((collection.minted_count / collection.max_supply) * 100, 100)
    : null;

  return (
    <div
      className={`flex flex-col overflow-hidden rounded-xl border border-border bg-card transition-all hover:border-primary/30 hover:shadow-lg ${onClick ? "cursor-pointer" : ""}`}
      onClick={onClick}
    >
      <div className="relative aspect-video bg-muted/50">
        {collection.image_url ? (
          <img
            src={collection.image_url}
            alt={collection.name}
            className="size-full object-cover"
          />
        ) : (
          <div className="flex size-full items-center justify-center bg-gradient-to-br from-muted/20 to-muted/50">
            <Package className="size-12 text-muted-foreground/30" />
          </div>
        )}
        <Badge className={`absolute right-2 top-2 ${netBadge} border-none text-xs`}>
          {collection.network}
        </Badge>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">{collection.name}</h3>
          <Badge variant="outline">{collection.symbol}</Badge>
        </div>
        {collection.description && (
          <p className="line-clamp-2 text-sm text-muted-foreground">
            {collection.description}
          </p>
        )}
        {collection.contract_address && (
          <div className="flex items-center gap-1 text-[10px] font-mono text-muted-foreground/70">
            <Hash className="size-2.5 shrink-0" />
            <span className="truncate select-all" title={collection.contract_address}>{collection.contract_address}</span>
            <CopyButton text={collection.contract_address} />
          </div>
        )}
        <div className="mt-auto space-y-2 pt-2">
          {progress !== null && (
            <div className="space-y-1">
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>
          )}
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">
              Mint: {coinPrice(collection.mint_price, collection.mint_token, collection.mint_price_usd)}
            </span>
            <span className="text-muted-foreground">
              {collection.minted_count}
              {collection.max_supply ? `/${collection.max_supply}` : ""} minted
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function NFTDetailModal({
  nftId,
  onClose,
  onAction,
}: {
  nftId: number;
  onClose: () => void;
  onAction?: () => void;
}) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const userId = useAuthStore((s) => s.user?.id);
  const { data: nft, loading, error } = useApi<NFTDetail>(`/nfts/${nftId}`);
  const { mutate: buyNft, loading: buying } = useApiMutation<{ success: boolean }>(`/nfts/${nftId}/buy`);
  const { mutate: unlistNft, loading: unlisting } = useApiMutation<{ success: boolean }>(`/nfts/${nftId}/unlist`);

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
        <div className="w-full max-w-2xl rounded-2xl bg-card p-6" onClick={(e) => e.stopPropagation()}>
          <Skeleton className="mb-4 h-8 w-48" />
          <Skeleton className="aspect-square w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (error || !nft) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
        <div className="w-full max-w-md rounded-2xl bg-card p-6 text-center" onClick={(e) => e.stopPropagation()}>
          <AlertCircle className="mx-auto mb-2 size-8 text-destructive" />
          <p className="text-sm text-muted-foreground">{error || "NFT not found"}</p>
          <Button variant="outline" className="mt-4" onClick={onClose}>Close</Button>
        </div>
      </div>
    );
  }

  const rarityClass = RARITY_COLORS[nft.rarity] || RARITY_COLORS.common;
  const netBadge = NETWORK_BADGE[nft.network || ""] || "bg-zinc-500/15 text-zinc-400";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="w-full max-w-3xl max-h-[90vh] overflow-y-auto rounded-2xl bg-card border border-border shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="grid gap-0 md:grid-cols-2">
          {/* Image */}
          <div className="relative aspect-square bg-muted/50">
            {nft.image_url ? (
              <img src={nft.image_url} alt={nft.name} className="size-full object-cover" />
            ) : (
              <div className="flex size-full items-center justify-center bg-gradient-to-br from-muted/30 to-muted/60">
                <Image className="size-16 text-muted-foreground/30" />
              </div>
            )}
            <Badge className={`absolute right-3 top-3 ${rarityClass} border-none capitalize`}>
              {nft.rarity}
            </Badge>
            <Badge className={`absolute left-3 top-3 ${netBadge} border-none`}>
              {nft.network}
            </Badge>
          </div>

          {/* Details */}
          <div className="flex flex-col gap-4 p-6">
            <div>
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span>{nft.collection_symbol}</span>
                <span>-</span>
                <span>{nft.collection_name}</span>
              </div>
              <h2 className="mt-1 text-2xl font-bold">{nft.name}</h2>
              {nft.description && (
                <p className="mt-2 text-sm text-muted-foreground">{nft.description}</p>
              )}
            </div>

            {/* Listing + Actions */}
            {nft.listing && (
              <div className="rounded-lg border border-primary/20 bg-primary/5 p-3 space-y-2">
                <div>
                  <p className="text-xs text-muted-foreground">Current Price</p>
                  <p className="text-xl font-bold">
                    {coinPrice(nft.listing.price, nft.listing.currency, nft.listing.price_usd)}
                  </p>
                  <p className="text-xs text-muted-foreground">Listed {timeAgo(nft.listing.listed_at)}</p>
                </div>
                {isAuthenticated && nft.owner_id !== String(userId) && (
                  <Button
                    className="w-full"
                    disabled={buying}
                    onClick={async () => {
                      const result = await buyNft({});
                      if (result) {
                        toast.success("NFT purchased!");
                        onAction?.();
                        onClose();
                      }
                    }}
                  >
                    <ShoppingCart className="mr-2 size-4" />
                    {buying ? "Buying..." : `Buy for ${coinPrice(nft.listing.price, nft.listing.currency, nft.listing.price_usd)}`}
                  </Button>
                )}
                {isAuthenticated && nft.owner_id === String(userId) && (
                  <Button
                    variant="outline"
                    className="w-full"
                    disabled={unlisting}
                    onClick={async () => {
                      const result = await unlistNft({});
                      if (result) {
                        toast.success("Listing removed.");
                        onAction?.();
                        onClose();
                      }
                    }}
                  >
                    {unlisting ? "Removing..." : "Remove Listing"}
                  </Button>
                )}
              </div>
            )}

            {/* Properties */}
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg bg-muted/30 p-2.5">
                <p className="text-[10px] uppercase text-muted-foreground">Token ID</p>
                <p className="font-mono text-sm font-semibold">#{nft.token_id}</p>
              </div>
              <div className="rounded-lg bg-muted/30 p-2.5">
                <p className="text-[10px] uppercase text-muted-foreground">Mint Price</p>
                <p className="text-sm font-semibold">{coinPrice(nft.mint_price, nft.mint_token, nft.mint_price_usd)}</p>
              </div>
            </div>

            {/* Blockchain */}
            <div className="space-y-1.5">
              <p className="text-xs font-semibold uppercase text-muted-foreground">Blockchain</p>
              {nft.token_hash && (
                <div className="rounded-md bg-muted/20 px-2.5 py-1.5 text-xs space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Token Hash</span>
                    <CopyButton text={nft.token_hash} />
                  </div>
                  <p className="font-mono text-[10px] break-all select-all leading-relaxed">{nft.token_hash}</p>
                </div>
              )}
              {nft.contract_address && (
                <div className="rounded-md bg-muted/20 px-2.5 py-1.5 text-xs space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Contract</span>
                    <CopyButton text={nft.contract_address} />
                  </div>
                  <p className="font-mono text-[10px] break-all select-all leading-relaxed">{nft.contract_address}</p>
                </div>
              )}
              <div className="flex items-center justify-between rounded-md bg-muted/20 px-2.5 py-1.5 text-xs">
                <span className="text-muted-foreground">Standard</span>
                <span className="font-semibold">ERC-721</span>
              </div>
            </div>

            {/* Sale History */}
            {nft.sales.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-xs font-semibold uppercase text-muted-foreground">Sale History</p>
                <div className="max-h-32 space-y-1 overflow-y-auto">
                  {nft.sales.map((s, i) => (
                    <div key={i} className="flex items-center justify-between rounded-md bg-muted/20 px-2.5 py-1.5 text-xs">
                      <span className="font-semibold">{coinPrice(s.price, s.currency, s.price_usd)}</span>
                      <span className="text-muted-foreground">{timeAgo(s.sold_at)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="mt-auto pt-2">
              <Button variant="outline" className="w-full" onClick={onClose}>Close</Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function CollectionDetailModal({
  collectionId,
  onClose,
  onSelectNft,
  onAction,
}: {
  collectionId: number;
  onClose: () => void;
  onSelectNft: (id: number) => void;
  onAction?: () => void;
}) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { data, loading, error } = useApi<CollectionDetail>(`/nfts/collection/${collectionId}`);
  const { mutate: mintNft, loading: minting } = useApiMutation<{ id: number; token_id: number }>("/nfts/mint");

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
        <div className="w-full max-w-4xl rounded-2xl bg-card p-6" onClick={(e) => e.stopPropagation()}>
          <Skeleton className="mb-4 h-8 w-48" />
          <GridSkeleton count={4} />
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
        <div className="w-full max-w-md rounded-2xl bg-card p-6 text-center" onClick={(e) => e.stopPropagation()}>
          <AlertCircle className="mx-auto mb-2 size-8 text-destructive" />
          <p className="text-sm text-muted-foreground">{error || "Collection not found"}</p>
          <Button variant="outline" className="mt-4" onClick={onClose}>Close</Button>
        </div>
      </div>
    );
  }

  const col = data.collection;
  const netBadge = NETWORK_BADGE[col.network || ""] || "bg-zinc-500/15 text-zinc-400";
  const progress = col.max_supply ? Math.min((col.minted_count / col.max_supply) * 100, 100) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="w-full max-w-5xl max-h-[90vh] overflow-y-auto rounded-2xl bg-card border border-border shadow-2xl p-6"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:gap-6 mb-6">
          <div className="size-24 shrink-0 overflow-hidden rounded-xl bg-muted/50">
            {col.image_url ? (
              <img src={col.image_url} alt={col.name} className="size-full object-cover" />
            ) : (
              <div className="flex size-full items-center justify-center">
                <Package className="size-8 text-muted-foreground/30" />
              </div>
            )}
          </div>
          <div className="flex-1 space-y-2">
            <div className="flex items-center gap-2">
              <h2 className="text-2xl font-bold">{col.name}</h2>
              <Badge variant="outline">{col.symbol}</Badge>
              <Badge className={`${netBadge} border-none text-xs`}>{col.network}</Badge>
            </div>
            {col.description && (
              <p className="text-sm text-muted-foreground">{col.description}</p>
            )}
            {col.contract_address && (
              <div className="space-y-0.5">
                <div className="flex items-center gap-1 text-xs text-muted-foreground">
                  <Hash className="size-3 shrink-0" />
                  <span>ERC-721 Contract</span>
                  <CopyButton text={col.contract_address} />
                </div>
                <p className="font-mono text-[10px] text-muted-foreground break-all select-all leading-relaxed pl-4">{col.contract_address}</p>
              </div>
            )}
          </div>
          <div className="flex gap-2 shrink-0">
            {isAuthenticated && (col.max_supply === null || col.minted_count < col.max_supply) && (
              <Button
                disabled={minting}
                onClick={async () => {
                  const name = `${col.name} #${col.minted_count + 1}`;
                  const result = await mintNft({
                    collection_id: collectionId,
                    name,
                    description: "",
                    image_url: col.image_url || "",
                    rarity: "common",
                  });
                  if (result) {
                    toast.success(`Minted ${name}!`);
                    onAction?.();
                  }
                }}
              >
                {minting ? "Minting..." : `Mint (${coinPrice(col.mint_price, col.mint_token, col.mint_price_usd)})`}
              </Button>
            )}
            <Button variant="outline" onClick={onClose}>Close</Button>
          </div>
        </div>

        {/* Stats */}
        <div className="mb-6 grid gap-3 grid-cols-2 sm:grid-cols-4">
          <div className="rounded-lg border bg-muted/20 p-3 text-center">
            <p className="text-[10px] uppercase text-muted-foreground">Minted</p>
            <p className="text-lg font-bold">{col.minted_count}{col.max_supply ? `/${col.max_supply}` : ""}</p>
          </div>
          <div className="rounded-lg border bg-muted/20 p-3 text-center">
            <p className="text-[10px] uppercase text-muted-foreground">Mint Price</p>
            <p className="text-lg font-bold">{coinPrice(col.mint_price, col.mint_token, col.mint_price_usd)}</p>
          </div>
          <div className="rounded-lg border bg-muted/20 p-3 text-center">
            <p className="text-[10px] uppercase text-muted-foreground">Floor Price</p>
            <p className="text-lg font-bold">
              {col.floor_price && col.floor_currency ? coinPrice(col.floor_price, col.floor_currency, col.floor_price_usd) : "—"}
            </p>
          </div>
          <div className="rounded-lg border bg-muted/20 p-3 text-center">
            <p className="text-[10px] uppercase text-muted-foreground">Total Volume</p>
            <p className="text-lg font-bold">
              {col.total_volume > 0 ? col.total_volume.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}
            </p>
          </div>
        </div>

        {progress !== null && (
          <div className="mb-6 space-y-1">
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{Math.round(progress)}% minted</span>
              <span>{col.max_supply! - col.minted_count} remaining</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {/* NFTs Grid */}
        <h3 className="mb-3 font-semibold">Items ({data.total})</h3>
        {data.nfts.length > 0 ? (
          <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {data.nfts.map((n) => (
              <NFTCard
                key={n.id}
                name={n.name}
                rarity={n.rarity}
                imageUrl={n.image_url}
                collectionName={null}
                tokenId={n.token_id}
                tokenHash={n.token_hash}
                onClick={() => onSelectNft(n.id)}
              />
            ))}
          </div>
        ) : (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No NFTs minted yet in this collection.
          </p>
        )}
      </div>
    </div>
  );
}

function ListNFTModal({
  nftId,
  onClose,
  onListed,
}: {
  nftId: number;
  onClose: () => void;
  onListed: () => void;
}) {
  const [price, setPrice] = useState("");
  const [currency, setCurrency] = useState("COIN");
  const { mutate: listNft, loading } = useApiMutation<{ listing_id: number }>(`/nfts/${nftId}/list`);

  async function handleList() {
    if (!price || parseFloat(price) <= 0) return;
    const result = await listNft({ price: parseFloat(price), currency });
    if (result) {
      toast.success("NFT listed for sale!");
      onListed();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="w-full max-w-sm rounded-2xl bg-card border border-border shadow-2xl p-6 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-bold">List NFT for Sale</h3>
        <div className="space-y-2">
          <label className="text-sm text-muted-foreground">Price</label>
          <Input
            type="number"
            placeholder="0.00"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            min="0.01"
            step="0.01"
          />
        </div>
        <div className="space-y-2">
          <label className="text-sm text-muted-foreground">Currency</label>
          <select
            className="w-full rounded-md border bg-card px-3 py-2 text-sm"
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
          >
            <option value="COIN">COIN</option>
            <option value="USD">USD</option>
            <option value="ARC">ARC</option>
            <option value="MTA">MTA</option>
          </select>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" className="flex-1" onClick={onClose}>Cancel</Button>
          <Button className="flex-1" disabled={loading || !price} onClick={handleList}>
            {loading ? "Listing..." : "List for Sale"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function GridSkeleton({ count = 6 }: { count?: number }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: count }).map((_, i) => (
        <Skeleton key={i} className="aspect-square rounded-xl" />
      ))}
    </div>
  );
}

/* ─── Deploy Collection Modal ──────────────────────────────────────────── */

function DeployCollectionModal({ onClose, onDeployed, isAdmin }: {
  onClose: () => void;
  onDeployed: () => void;
  isAdmin: boolean;
}) {
  const token = useAuthStore((s) => s.token);
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [network, setNetwork] = useState("ARC");
  const [mintPrice, setMintPrice] = useState("");
  const [mintToken, setMintToken] = useState("ARC");
  const [maxSupply, setMaxSupply] = useState("");
  const [galleryFiles, setGalleryFiles] = useState<FileList | null>(null);
  const [loading, setLoading] = useState(false);

  const handleNetworkChange = (val: string) => {
    setNetwork(val);
    setMintToken(isAdmin ? mintToken : val); // player: mint token = network coin
  };

  const handleSubmit = async () => {
    if (!token || !symbol || !name || !mintPrice) return;
    setLoading(true);
    try {
      const endpoint = isAdmin ? "/api/v2/admin/nfts" : "/api/v2/nfts/collections";
      const body: Record<string, unknown> = {
        symbol: symbol.toUpperCase(),
        name,
        description,
        network,
        mint_price: parseFloat(mintPrice),
        max_supply: maxSupply ? parseInt(maxSupply) : null,
      };
      if (isAdmin) body.mint_token = mintToken;

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Deploy failed");

      // Upload gallery if admin and files selected - collection is already created at this point,
      // so warn on failure rather than throwing (which would make it look like creation failed)
      if (isAdmin && galleryFiles && galleryFiles.length > 0) {
        const form = new FormData();
        for (let i = 0; i < galleryFiles.length; i++) form.append("files", galleryFiles[i]);
        const galleryRes = await fetch(`/api/v2/admin/nfts/${data.symbol}/gallery`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        });
        const galleryData = await galleryRes.json().catch(() => ({}));
        if (galleryRes.ok || galleryRes.status === 207) {
          toast.success(`Collection ${data.symbol} deployed! Uploaded ${galleryData.uploaded} image(s).`);
        } else {
          toast.success(`Collection ${data.symbol} deployed!`);
          toast.warning(`Image upload failed: ${galleryData.detail || "unknown error"}. Use the admin panel to upload images.`);
        }
      } else {
        toast.success(`Collection ${data.symbol} deployed!`);
      }
      onDeployed();
      onClose();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Deploy failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl bg-card border border-border shadow-2xl" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div className="flex items-center gap-2">
            <Rocket className="size-5 text-primary" />
            <h2 className="text-lg font-semibold">Deploy Collection</h2>
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground transition-colors">
            <X className="size-5" />
          </button>
        </div>

        <div className="space-y-4 p-6">
          {!isAdmin && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-400">
              Requires <strong>Protocol Dev</strong> or <strong>Exploiter</strong> tier. Deploy gas will be deducted from your DeFi wallet.
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Symbol <span className="text-destructive">*</span></Label>
              <Input
                placeholder="e.g. PUNKS"
                value={symbol}
                maxLength={10}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="mt-1 font-mono uppercase"
              />
            </div>
            <div>
              <Label>Network <span className="text-destructive">*</span></Label>
              <select
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                value={network}
                onChange={(e) => handleNetworkChange(e.target.value)}
              >
                <option value="ARC">ARC — Arcadia Network</option>
                <option value="DSC">DSC — Discoin Network</option>
              </select>
            </div>
          </div>

          <div>
            <Label>Name <span className="text-destructive">*</span></Label>
            <Input
              placeholder="e.g. Cool Punks"
              value={name}
              maxLength={50}
              onChange={(e) => setName(e.target.value)}
              className="mt-1"
            />
          </div>

          <div>
            <Label>Description</Label>
            <Textarea
              placeholder="Optional description..."
              value={description}
              maxLength={500}
              onChange={(e) => setDescription(e.target.value)}
              className="mt-1 resize-none"
              rows={2}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>Mint Price <span className="text-destructive">*</span></Label>
              <div className="mt-1 flex gap-2">
                <Input
                  type="number"
                  min="0"
                  step="0.0001"
                  placeholder="0.05"
                  value={mintPrice}
                  onChange={(e) => setMintPrice(e.target.value)}
                />
                {isAdmin ? (
                  <Input
                    className="w-24 font-mono uppercase"
                    placeholder="ARC"
                    value={mintToken}
                    maxLength={10}
                    onChange={(e) => setMintToken(e.target.value.toUpperCase())}
                  />
                ) : (
                  <span className="flex items-center rounded-md border border-input bg-muted px-3 text-sm font-mono text-muted-foreground">
                    {network}
                  </span>
                )}
              </div>
            </div>
            <div>
              <Label>Max Supply</Label>
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

          {isAdmin && (
            <div>
              <Label className="flex items-center gap-1.5">
                <Upload className="size-3.5" />
                Gallery Images
              </Label>
              <input
                type="file"
                multiple
                accept="image/jpeg,image/png,image/gif,image/webp"
                onChange={(e) => setGalleryFiles(e.target.files)}
                className="mt-1 w-full cursor-pointer rounded-md border border-input bg-background px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-primary/10 file:px-2 file:py-1 file:text-xs file:text-primary"
              />
              {galleryFiles && galleryFiles.length > 0 && (
                <p className="mt-1 text-xs text-muted-foreground">{galleryFiles.length} image{galleryFiles.length !== 1 ? "s" : ""} selected</p>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={onClose} disabled={loading}>Cancel</Button>
            <Button
              disabled={loading || !symbol || !name || !mintPrice}
              onClick={handleSubmit}
            >
              {loading ? <RefreshCw className="mr-1.5 size-4 animate-spin" /> : <Rocket className="mr-1.5 size-4" />}
              Deploy
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── Main Page ────────────────────────────────────────────────────────── */

export default function NFTsPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const isAdmin = useAuthStore((s) => s.user?.isAdmin ?? false);
  const [search, setSearch] = useState("");
  const [selectedNftId, setSelectedNftId] = useState<number | null>(null);
  const [selectedCollectionId, setSelectedCollectionId] = useState<number | null>(null);
  const [sortMarket, setSortMarket] = useState("recent");
  const [filterRarity, setFilterRarity] = useState("");
  const [listingNftId, setListingNftId] = useState<number | null>(null);
  const [showDeploy, setShowDeploy] = useState(false);

  const {
    data: collections,
    loading: collectionsLoading,
    error: collectionsError,
    refetch: refetchCollections,
  } = useApi<NFTCollection[]>("/nfts/collections");

  const marketUrl = `/nfts/marketplace?sort=${sortMarket}${filterRarity ? `&rarity=${filterRarity}` : ""}`;
  const {
    data: marketplaceData,
    loading: marketLoading,
    error: marketError,
    refetch: refetchMarket,
  } = useApi<MarketplaceResponse>(marketUrl);

  const {
    data: myNftsData,
    loading: myLoading,
    error: myError,
    refetch: refetchMyNfts,
  } = useApi<MyNFTsResponse>(isAuthenticated ? "/nfts/my" : null);

  const refetchAll = useCallback(() => {
    refetchCollections();
    refetchMarket();
    refetchMyNfts();
  }, [refetchCollections, refetchMarket, refetchMyNfts]);

  const filteredCollections = collections?.filter(
    (c) =>
      c.name.toLowerCase().includes(search.toLowerCase()) ||
      c.symbol.toLowerCase().includes(search.toLowerCase())
  );

  const filteredListings = marketplaceData?.listings.filter(
    (l) =>
      l.name.toLowerCase().includes(search.toLowerCase()) ||
      (l.collection_name ?? "").toLowerCase().includes(search.toLowerCase())
  );

  const filteredMyNfts = myNftsData?.nfts.filter(
    (n) =>
      n.name.toLowerCase().includes(search.toLowerCase()) ||
      (n.collection_name ?? "").toLowerCase().includes(search.toLowerCase())
  );

  return (
    <ModuleGate modules={["nft"]}>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">NFT Marketplace</h1>
            <p className="text-sm text-muted-foreground">
              Mint, collect, and trade NFTs on the DSC and ARC networks
            </p>
          </div>
          <div className="flex w-full gap-2 sm:w-auto">
            <div className="relative flex-1 sm:w-72">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Search collections and NFTs..."
                className="pl-9"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            {isAuthenticated && (
              <Button size="sm" onClick={() => setShowDeploy(true)}>
                <Rocket className="mr-1.5 size-4" />
                Deploy
              </Button>
            )}
          </div>
        </div>

        {/* Stats */}
        <div className="grid gap-4 grid-cols-2 sm:grid-cols-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Collections</CardTitle>
              <Layers className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{collections?.length ?? 0}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Listed</CardTitle>
              <Tag className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{marketplaceData?.total ?? 0}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">My NFTs</CardTitle>
              <Image className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{myNftsData?.total ?? 0}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Networks</CardTitle>
              <TrendingUp className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="flex gap-2">
                <Badge className="bg-blue-500/15 text-blue-400 border-none text-xs">ARC</Badge>
                <Badge className="bg-emerald-500/15 text-emerald-400 border-none text-xs">DSC</Badge>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Tabs */}
        <Tabs defaultValue="marketplace">
          <TabsList>
            <TabsTrigger value="marketplace">Marketplace</TabsTrigger>
            <TabsTrigger value="collections">Collections</TabsTrigger>
            {isAuthenticated && (
              <TabsTrigger value="my-nfts">My NFTs</TabsTrigger>
            )}
          </TabsList>

          {/* Marketplace Tab */}
          <TabsContent value="marketplace">
            <div className="mt-4 space-y-4">
              {/* Filters */}
              <div className="flex flex-wrap items-center gap-2">
                <div className="flex items-center gap-1">
                  <ArrowUpDown className="size-3.5 text-muted-foreground" />
                  <select
                    className="rounded-md border bg-card px-2 py-1 text-xs"
                    value={sortMarket}
                    onChange={(e) => setSortMarket(e.target.value)}
                  >
                    <option value="recent">Recently Listed</option>
                    <option value="price_asc">Price: Low to High</option>
                    <option value="price_desc">Price: High to Low</option>
                    <option value="rarity">Rarity</option>
                  </select>
                </div>
                <div className="flex items-center gap-1">
                  <Filter className="size-3.5 text-muted-foreground" />
                  <select
                    className="rounded-md border bg-card px-2 py-1 text-xs"
                    value={filterRarity}
                    onChange={(e) => setFilterRarity(e.target.value)}
                  >
                    <option value="">All Rarities</option>
                    <option value="common">Common</option>
                    <option value="uncommon">Uncommon</option>
                    <option value="rare">Rare</option>
                    <option value="epic">Epic</option>
                    <option value="legendary">Legendary</option>
                  </select>
                </div>
              </div>

              {marketLoading ? (
                <GridSkeleton />
              ) : marketError ? (
                <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {marketError}
                </div>
              ) : filteredListings && filteredListings.length > 0 ? (
                <div className="grid gap-4 grid-cols-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                  {filteredListings.map((listing) => (
                    <NFTCard
                      key={listing.listing_id}
                      name={listing.name}
                      rarity={listing.rarity}
                      imageUrl={listing.image_url}
                      collectionName={listing.collection_name}
                      tokenId={listing.token_id}
                      network={listing.network}
                      tokenHash={listing.token_hash}
                      onClick={() => setSelectedNftId(listing.nft_id)}
                    >
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-bold">
                            {coinPrice(listing.price, listing.currency, listing.price_usd)}
                          </p>
                          <p className="text-[10px] text-muted-foreground">{timeAgo(listing.listed_at)}</p>
                        </div>
                        {isAuthenticated && (
                          <Button
                            size="sm"
                            variant="default"
                            className="h-7 text-xs"
                            onClick={(e) => {
                              e.stopPropagation();
                              setSelectedNftId(listing.nft_id);
                            }}
                          >
                            <ShoppingCart className="mr-1 size-3" />
                            Buy
                          </Button>
                        )}
                      </div>
                    </NFTCard>
                  ))}
                </div>
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  {search ? "No listings match your search" : "No listings available"}
                </p>
              )}
            </div>
          </TabsContent>

          {/* Collections Tab */}
          <TabsContent value="collections">
            <div className="mt-4">
              {collectionsLoading ? (
                <GridSkeleton count={4} />
              ) : collectionsError ? (
                <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                  <AlertCircle className="size-4" />
                  {collectionsError}
                </div>
              ) : filteredCollections && filteredCollections.length > 0 ? (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {filteredCollections.map((c) => (
                    <CollectionCard
                      key={c.id}
                      collection={c}
                      onClick={() => setSelectedCollectionId(c.id)}
                    />
                  ))}
                </div>
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  {search ? "No collections match your search" : "No collections available"}
                </p>
              )}
            </div>
          </TabsContent>

          {/* My NFTs Tab */}
          {isAuthenticated && (
            <TabsContent value="my-nfts">
              <div className="mt-4">
                {myLoading ? (
                  <GridSkeleton />
                ) : myError ? (
                  <div className="flex items-center gap-2 py-8 text-sm text-destructive">
                    <AlertCircle className="size-4" />
                    {myError}
                  </div>
                ) : filteredMyNfts && filteredMyNfts.length > 0 ? (
                  <div className="grid gap-4 grid-cols-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                    {filteredMyNfts.map((nft) => (
                      <NFTCard
                        key={nft.id}
                        name={nft.name}
                        rarity={nft.rarity}
                        imageUrl={nft.image_url}
                        collectionName={nft.collection_name}
                        tokenId={nft.token_id}
                        network={nft.network}
                        tokenHash={nft.token_hash}
                        onClick={() => setSelectedNftId(nft.id)}
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-muted-foreground">
                            {nft.collection_symbol}
                          </span>
                          {nft.is_listed ? (
                            <Badge variant="outline" className="text-[10px] text-amber-400 border-amber-400/30">
                              Listed
                            </Badge>
                          ) : (
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 text-xs"
                              onClick={(e) => {
                                e.stopPropagation();
                                setListingNftId(nft.id);
                              }}
                            >
                              <Tag className="mr-1 size-3" />
                              List
                            </Button>
                          )}
                        </div>
                      </NFTCard>
                    ))}
                  </div>
                ) : (
                  <p className="py-8 text-center text-sm text-muted-foreground">
                    {search ? "No NFTs match your search" : "You don't own any NFTs yet"}
                  </p>
                )}
              </div>
            </TabsContent>
          )}
        </Tabs>
      </div>

      {/* Modals */}
      {selectedNftId !== null && (
        <NFTDetailModal
          nftId={selectedNftId}
          onClose={() => setSelectedNftId(null)}
          onAction={refetchAll}
        />
      )}
      {selectedCollectionId !== null && (
        <CollectionDetailModal
          collectionId={selectedCollectionId}
          onClose={() => setSelectedCollectionId(null)}
          onSelectNft={(id) => {
            setSelectedCollectionId(null);
            setSelectedNftId(id);
          }}
          onAction={refetchAll}
        />
      )}
      {listingNftId !== null && (
        <ListNFTModal
          nftId={listingNftId}
          onClose={() => setListingNftId(null)}
          onListed={() => {
            setListingNftId(null);
            refetchAll();
          }}
        />
      )}
      {showDeploy && (
        <DeployCollectionModal
          isAdmin={isAdmin}
          onClose={() => setShowDeploy(false)}
          onDeployed={refetchAll}
        />
      )}
    </ModuleGate>
  );
}
