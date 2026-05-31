"use client";

import { useState } from "react";
import RuleBanner from "@/components/RuleBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ShoppingBag,
  Search,
  Tag,
  Package,
  AlertCircle,
  ShoppingCart,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";

// --- API response types ---

interface LeaderboardEntry {
  user_id: string;
  level: number;
  xp: number;
}

interface ShopItem {
  key: string;
  name: string;
  description: string;
  price: number;
  currency?: string;
  category: string;
  top_users: LeaderboardEntry[];
}

interface InventoryItem {
  item_key: string;
  level: number;
  xp: number;
  staked_sun: number;
  count: number;
}

// --- Component ---

export default function ShopPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const [search, setSearch] = useState("");

  const {
    data: items,
    loading: itemsLoading,
    error: itemsError,
  } = useApi<ShopItem[]>("/shop/items");

  const {
    data: inventory,
    loading: inventoryLoading,
    error: inventoryError,
    refetch: refetchInventory,
  } = useApi<InventoryItem[]>(isAuthenticated ? "/shop/my-inventory" : null);

  const { mutate: buyItem, loading: buying } = useApiMutation<{
    success: boolean;
    message?: string;
    item_key?: string;
    cost?: number;
    currency?: string;
    new_balance?: number;
  }>("/shop/buy");

  const filteredItems = items?.filter(
    (item) =>
      item.name.toLowerCase().includes(search.toLowerCase()) ||
      item.category.toLowerCase().includes(search.toLowerCase())
  );

  async function handleBuy(itemKey: string) {
    const result = await buyItem({ item_key: itemKey });
    if (result) {
      toast.success(
        result.message ||
          `Purchased ${itemKey}. New balance: ${(result.new_balance ?? 0).toLocaleString()} SUN`
      );
      refetchInventory();
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Shop</h1>
          <p className="text-sm text-muted-foreground">
            Buy items, roles, and collectibles with your tokens
          </p>
        </div>
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search items..."
            className="pl-9"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      <RuleBanner title="Shop Rules" rules={[
        "All prices in SUN — buy SUN via mining or swap first",
        "5% buy fee on gems (Hashstone, Lockstone, Vaultstone)",
        "Gems level up with use — higher level = stronger bonuses",
        "Consumables (Validator Guard, Yield Guard) stack up to 50",
      ]} />

      {/* Summary stats */}
      <div className="grid gap-4 sm:grid-cols-2">
        {itemsLoading ? (
          <>
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
          </>
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Items Available
                </CardTitle>
                <Tag className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {items?.length ?? 0}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Items Owned
                </CardTitle>
                <Package className="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {inventory?.reduce((sum, i) => sum + i.count, 0) ?? 0}
                </div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Shop catalog */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShoppingBag className="size-4" />
            Shop Items
          </CardTitle>
        </CardHeader>
        <CardContent>
          {itemsLoading ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-40" />
              ))}
            </div>
          ) : itemsError ? (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" />
              {itemsError}
            </div>
          ) : filteredItems && filteredItems.length > 0 ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {filteredItems.map((item) => (
                <div
                  key={item.key}
                  className="flex flex-col justify-between rounded-lg border border-border p-4 space-y-3"
                >
                  <div>
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">{item.name}</span>
                      <Badge variant="outline">{item.category}</Badge>
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {item.description}
                    </p>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">
                      {(item.price ?? 0).toLocaleString()} {item.currency || "SUN"}
                    </span>
                    {isAuthenticated && (
                      <Button
                        size="sm"
                        disabled={buying}
                        onClick={() => handleBuy(item.key)}
                      >
                        <ShoppingCart className="mr-1 size-3" />
                        Buy
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              {search ? "No items match your search" : "No items available"}
            </p>
          )}
        </CardContent>
      </Card>

      {/* User inventory */}
      {isAuthenticated && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Package className="size-4" />
              Your Inventory
            </CardTitle>
          </CardHeader>
          <CardContent>
            {inventoryLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 2 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full" />
                ))}
              </div>
            ) : inventoryError ? (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />
                {inventoryError}
              </div>
            ) : inventory && inventory.length > 0 ? (
              <div className="space-y-2">
                {inventory.map((inv) => {
                  const itemInfo = items?.find((i) => i.key === inv.item_key);
                  return (
                    <div
                      key={inv.item_key}
                      className="flex items-center justify-between rounded-lg bg-muted/50 px-4 py-3"
                    >
                      <div>
                        <span className="font-medium">
                          {itemInfo?.name ?? inv.item_key}
                        </span>
                        <p className="text-xs text-muted-foreground">
                          Level {inv.level} &middot; {(inv.xp ?? 0).toLocaleString()}{" "}
                          XP
                          {(inv.staked_sun ?? 0) > 0 && (
                            <> &middot; {(inv.staked_sun ?? 0).toLocaleString()} SUN staked</>
                          )}
                        </p>
                      </div>
                      <Badge variant="secondary">x{inv.count}</Badge>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Your inventory is empty
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
