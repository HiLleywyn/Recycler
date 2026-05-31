"""
core/framework/agent_tools/tools/shop.py -- shop tools.

    shop.catalog     list every item in Config.SHOP_ITEMS with prices + stats (READ).
    shop.item_info   detailed info for one item (ownership, level, next upgrade) (READ).
    shop.buy         purchase a shop item using DSD or USDC from the DeFi wallet (MUTATE).

shop.buy supports all four leveled stones (hashstone, lockstone, vaultstone,
liqstone) and stackable consumables. It checks balance, deducts from the DeFi
wallet holding, and inserts the stone / increments the consumable count.
Note: vault LP-add side effects are NOT triggered by the tool path (those
require Discord context for the vault progression embeds). The core purchase
always completes correctly.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from core.config import Config
from core.framework.network import STABLE_NETWORK as _STABLE_NETWORK
from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.shop")


# Stone-table key map so the one call handles all four leveled items.
_STONE_GETTERS: dict[str, str] = {
    "hashstone":  "get_hashstone",
    "lockstone":  "get_lockstone",
    "vaultstone": "get_vaultstone",
    "liqstone":   "get_liqstone",
}


def _item_public(key: str, item: dict) -> dict:
    """Build the public-safe shape for one SHOP_ITEMS entry."""
    cost_stable_raw = int(item.get("cost_stable", 0) or 0)
    cost_stable = to_human(cost_stable_raw) if cost_stable_raw >= 10**6 else float(cost_stable_raw)
    return {
        "key":         key,
        "name":        str(item.get("name") or key),
        "emoji":       str(item.get("emoji") or ""),
        "category":    str(item.get("category") or "item"),
        "description": str(item.get("description") or ""),
        "cost_stable": round(cost_stable, 4),
        "buy_fee_pct": float(item.get("buy_fee_pct", 0.0) or 0.0),
        "sell_fee_pct": float(item.get("sell_fee_pct", 0.0) or 0.0),
        "leveled":     bool(item.get("leveled", False)),
        "max_level":   int(item.get("max_level", 0) or 0),
        "stackable":   bool(item.get("stackable", False)),
        "max_stack":   int(item.get("max_stack", 0) or 0),
        "stats":       dict(item.get("stats") or {}),
    }


# -- shop.catalog -------------------------------------------------------------

@tool(
    name="shop.catalog",
    summary=(
        "List every item in the shop with its current price, fees, leveling "
        "state, and stat bonuses. Use this when a player asks 'what can I "
        "buy' or 'what does X do'."
    ),
    risk=RiskLevel.READ,
    category="shop",
    params=[
        ParamSpec("category", "str", required=False, default=None,
                  description="Optional filter: 'item' or 'consumable'."),
    ],
)
async def catalog(ctx: ToolContext, args: dict) -> ToolResult:
    wanted = str(args.get("category") or "").strip().lower() or None

    entries: list[dict] = []
    for key, item in Config.SHOP_ITEMS.items():
        if wanted and str(item.get("category") or "").lower() != wanted:
            continue
        entries.append(_item_public(key, item))

    entries.sort(key=lambda e: (e["category"], e["cost_stable"]))
    return ToolResult.success({
        "count": len(entries),
        "items": entries,
    })


# -- shop.item_info -----------------------------------------------------------

@tool(
    name="shop.item_info",
    summary=(
        "Detailed info for a single shop item including the caller's "
        "ownership state: current level, xp, staked amount (for stones) "
        "or stack count (for consumables)."
    ),
    risk=RiskLevel.READ,
    category="shop",
    params=[
        ParamSpec("item_key", "str",
                  description="Shop item key (e.g. 'hashstone', 'validator_guard')."),
    ],
)
async def item_info(ctx: ToolContext, args: dict) -> ToolResult:
    key = str(args.get("item_key") or "").strip().lower()
    if not key:
        return ToolResult.fail("item_key required")

    item = Config.SHOP_ITEMS.get(key)
    if item is None:
        return ToolResult.fail(f"unknown_item: {key}")

    payload: dict[str, Any] = _item_public(key, item)

    # Ownership snapshot from the canonical stone / consumable tables.
    owned: dict[str, Any] = {"owns": False}
    try:
        getter_name = _STONE_GETTERS.get(key)
        if getter_name:
            getter = getattr(ctx.db, getter_name, None)
            if getter is not None:
                row = await getter(int(ctx.user_id), int(ctx.guild_id))
                if row:
                    owned = {
                        "owns":          True,
                        "level":         int(row.get("level") or 0),
                        "xp":            float(row.get("xp") or 0.0),
                        "staked_amount": row.h("staked_amount"),
                        "acquired_at":   row.get("acquired_at"),
                    }
        elif key == "validator_guard":
            count = await ctx.db.get_validator_guard_count(
                int(ctx.user_id), int(ctx.guild_id),
            )
            owned = {"owns": count > 0, "count": int(count)}
        elif key == "yield_guard":
            get_fn = getattr(ctx.db, "get_yield_guard_count", None)
            if get_fn is not None:
                count = await get_fn(int(ctx.user_id), int(ctx.guild_id))
                owned = {"owns": count > 0, "count": int(count)}
    except Exception as exc:
        log.warning("[shop.item_info] ownership read failed for %s: %s", key, exc)

    payload["ownership"] = owned
    return ToolResult.success(payload)


# -- shop.buy ------------------------------------------------------------------

# Stone creator method map (matches _STONE_GETTERS read map above).
_STONE_CREATORS: dict[str, str] = {
    "hashstone":  "create_hashstone",
    "lockstone":  "create_lockstone",
    "vaultstone": "create_vaultstone",
    "liqstone":   "create_liqstone",
}

# Consumable add-method map.
_CONSUMABLE_ADDERS: dict[str, str] = {
    "validator_guard": "add_validator_guard",
    "yield_guard":     "add_yield_guard",
}


@tool(
    name="shop.buy",
    summary=(
        "Purchase a shop item (hashstone, lockstone, vaultstone, liqstone, "
        "validator_guard, yield_guard) using DSD or USDC from the caller's "
        "DeFi wallet. Checks balance, deducts the cost, and creates the stone "
        "or increments the consumable stack. "
        "Note: vault treasury side effects (LP-add, vault progression) are not "
        "triggered here -- they require Discord context."
    ),
    risk=RiskLevel.MUTATE,
    category="shop",
    params=[
        ParamSpec("item_key", "str",
                  description=(
                      "Item to buy: hashstone, lockstone, vaultstone, liqstone, "
                      "validator_guard, yield_guard."
                  )),
        ParamSpec("currency", "str", required=False, default="DSD",
                  description="Stablecoin to pay with: DSD (default) or USDC."),
    ],
)
async def buy(ctx: ToolContext, args: dict) -> ToolResult:
    uid      = int(ctx.user_id)
    gid      = int(ctx.guild_id)
    key      = str(args.get("item_key") or "").strip().lower()
    currency = str(args.get("currency") or "DSD").upper()

    if not key:
        return ToolResult.fail("item_key required")

    item = Config.SHOP_ITEMS.get(key)
    if item is None:
        available = ", ".join(Config.SHOP_ITEMS.keys())
        return ToolResult.fail(f"unknown_item: {key}. Available: {available}")

    if currency not in _STABLE_NETWORK:
        accepted = ", ".join(_STABLE_NETWORK.keys())
        return ToolResult.fail(
            f"unsupported_currency: {currency}. Accepted stablecoins: {accepted}"
        )

    network = _STABLE_NETWORK[currency]
    cost_raw = int(item.get("cost_stable") or 0)
    if cost_raw <= 0:
        return ToolResult.fail(f"item_not_purchasable: {key} has no cost configured")

    fee_raw    = int(Decimal(str(cost_raw)) * Decimal(str(item.get("buy_fee_pct") or 0.0)))
    staked_raw = cost_raw - fee_raw

    # Balance check.
    wh = await ctx.db.get_wallet_holding(uid, gid, network, currency)
    balance_raw = int((wh or {}).get("amount") or 0)
    if balance_raw < cost_raw:
        return ToolResult.fail(
            f"insufficient_balance: need {to_human(cost_raw):.6f} {currency} "
            f"but wallet holds {to_human(balance_raw):.6f} {currency}"
        )

    is_stone      = key in _STONE_CREATORS
    is_consumable = key in _CONSUMABLE_ADDERS

    if not is_stone and not is_consumable:
        return ToolResult.fail(f"item_not_buyable_via_tool: {key}")

    # Leveled stones are one-per-user.
    if is_stone:
        getter_name = _STONE_GETTERS.get(key)
        if getter_name:
            getter = getattr(ctx.db, getter_name, None)
            if getter is not None:
                existing = await getter(uid, gid)
                if existing:
                    return ToolResult.fail(
                        f"already_owned: you already own a {key}. "
                        "Sell or transfer it before buying another."
                    )

    # Verify DB methods exist before touching balances.
    if is_stone:
        creator_name = _STONE_CREATORS[key]
        creator = getattr(ctx.db, creator_name, None)
        if creator is None:
            return ToolResult.fail(f"db_method_missing: {creator_name}")
    else:
        adder_name = _CONSUMABLE_ADDERS[key]
        adder = getattr(ctx.db, adder_name, None)
        if adder is None:
            return ToolResult.fail(f"db_method_missing: {adder_name}")

    result_data: dict[str, Any] = {
        "item_key":  key,
        "currency":  currency,
        "cost":      round(to_human(cost_raw), 6),
        "fee":       round(to_human(fee_raw), 6),
    }

    # Deduct cost and create item in a single transaction so neither
    # side can succeed without the other.
    try:
        async with ctx.db.atomic():
            await ctx.db.update_wallet_holding(uid, gid, network, currency, -cost_raw)

            if is_stone:
                await creator(uid, gid, staked_raw, lp_currency=currency)
                result_data["staked"] = round(to_human(staked_raw), 6)
                result_data["message"] = (
                    f"You now own a Level 1 {item.get('name', key)}. "
                    "Mine blocks to gain XP and level it up."
                )
            else:
                new_count = await adder(uid, gid)
                result_data["new_count"] = int(new_count)
                result_data["message"] = (
                    f"Purchase successful. You now have {new_count} "
                    f"{item.get('name', key)}(s) in inventory."
                )
    except ValueError as exc:
        return ToolResult.fail(f"deduct_failed: {exc}")
    except Exception as exc:
        log.error("[shop.buy] atomic buy failed for %s: %s", key, exc)
        return ToolResult.fail(f"purchase_failed: {exc}")

    try:
        tx_hash = await ctx.db.log_tx(
            gid, uid, "SHOP_BUY",
            symbol_in=currency, amount_in=cost_raw, network=network,
        )
    except Exception:
        tx_hash = ""
    result_data["tx_hash"] = tx_hash

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "shop_buy",
                guild_id=gid, user_id=uid,
                item_key=key, currency=currency,
                cost=to_human(cost_raw), tx_hash=tx_hash,
            )
        except Exception:
            pass

    return ToolResult.success(result_data)
