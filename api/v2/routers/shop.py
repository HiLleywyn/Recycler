"""Shop router  -  5 endpoints.

Item prices are denominated in stablecoin (DSD by default, any stablecoin accepted),
matching the Discord bot's ``/shop`` command.  Prices are read from ``Config.SHOP_ITEMS``
(sourced from ``items_config.py``) so they stay in sync with the bot.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.config import Config
from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import InsufficientBalanceError, NotFoundError, ValidationError
from api.v2.utils import to_iso
from api.v2.schemas.shop import (
    BuyRequest,
    BuyResult,
    InventoryItem,
    SellRequest,
    SellResult,
    ShopItem,
    ShopItemDetail,
    ShopItemLeaderEntry,
)

router = APIRouter(prefix="/shop", tags=["shop"], dependencies=[require_module("shop")])

# DB table mapping for each item key
_ITEM_TABLES = {
    "hashstone": "hashstones",
    "lockstone": "lockstones",
    "vaultstone": "vaultstones",
}

# Category mapping for each item
_ITEM_CATEGORIES = {
    "hashstone": "mining",
    "lockstone": "staking",
    "vaultstone": "savings",
}

# Default stablecoin for API operations.
# Network key is "dsc" (Discoin Network) -- migration 0050 renamed every
# legacy "discoin" row to "dsc"; never query for "discoin" here.
_DEFAULT_STABLE = "DSD"
_DEFAULT_NETWORK = "dsc"


def _get_item_meta(key: str) -> dict | None:
    """Build item metadata from the canonical Config.SHOP_ITEMS source."""
    cfg = Config.SHOP_ITEMS.get(key)
    if not cfg:
        return None
    return {
        "name": cfg.get("name", key.title()),
        "description": cfg.get("description", ""),
        "cost_stable": cfg.get("cost_stable", 0.0),
        "category": _ITEM_CATEGORIES.get(key, "other"),
        "table": _ITEM_TABLES.get(key, ""),
        "buy_fee_pct": cfg.get("buy_fee_pct", 0.0),
        "sell_fee_pct": cfg.get("sell_fee_pct", 0.0),
    }


@router.get("/items", response_model=list[ShopItem], summary="List shop items")
async def list_shop_items(db=Depends(get_db)):
    """Return all shop items with leaderboard (top leveled users). Prices in stablecoin."""
    result = []
    for key in _ITEM_TABLES:
        meta = _get_item_meta(key)
        if not meta:
            continue
        rows = await db.fetch(
            f"SELECT user_id, level, xp FROM {meta['table']} ORDER BY level DESC, xp DESC LIMIT 5"
        )
        top_users: list[ShopItemLeaderEntry] = [
            ShopItemLeaderEntry(
                user_id=str(r["user_id"]),
                level=r["level"],
                xp=float(r["xp"]),
            )
            for r in rows
        ]
        result.append(ShopItem(
            key=key,
            name=meta["name"],
            description=meta["description"],
            price=to_human(int(meta["cost_stable"])),
            category=meta["category"],
            currency=_DEFAULT_STABLE,
            top_users=top_users,
        ))
    return result


@router.get("/items/{key}", response_model=ShopItemDetail, summary="Shop item details")
async def get_shop_item(key: str):
    """Return details for a single shop item."""
    meta = _get_item_meta(key)
    if not meta:
        raise NotFoundError("Shop item not found.")
    return ShopItemDetail(
        key=key,
        name=meta["name"],
        description=meta["description"],
        price=to_human(int(meta["cost_stable"])),
        category=meta["category"],
        currency=_DEFAULT_STABLE,
        mechanics={},
    )


@router.get("/my-inventory", response_model=list[InventoryItem], summary="My inventory")
async def my_inventory(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the authenticated user's owned shop items."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    items: list[InventoryItem] = []

    for key in ("hashstone", "lockstone", "vaultstone"):
        meta = _get_item_meta(key)
        if not meta:
            continue
        row = await db.fetchrow(
            f"SELECT level, xp, staked_amount, acquired_at FROM {meta['table']} "
            f"WHERE user_id = $1 AND guild_id = $2",
            uid, gid,
        )
        if row:
            items.append(InventoryItem(
                key=key,
                name=meta["name"],
                level=row["level"],
                xp=float(row["xp"]),
                staked_amount=to_human(int(row["staked_amount"] or 0)),
                acquired_at=to_iso(row["acquired_at"]),
            ))

    return items


@router.post("/buy", response_model=BuyResult, summary="Buy shop item")
async def buy_item(
    body: BuyRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Purchase a shop item. Cost is paid in DSD stablecoin (from Discoin Network wallet)."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    key = body.item_key

    meta = _get_item_meta(key)
    if not meta:
        raise NotFoundError("Shop item not found.")

    # cost_stable from Config is already a raw NUMERIC(36,0) scaled int,
    # so total_cost stays in raw int space for exact balance arithmetic.
    total_cost_raw = int(meta["cost_stable"]) * int(body.quantity)
    total_cost_h = to_human(total_cost_raw)

    async with db.transaction():
        # Deduct DSD atomically  -  fails (returns None) if balance is insufficient
        deducted = await db.fetchrow(
            "UPDATE wallet_holdings SET amount = amount - $3 "
            "WHERE user_id = $1 AND guild_id = $2 AND network = $4 AND symbol = $5 AND amount >= $3 "
            "RETURNING amount",
            uid, gid, total_cost_raw, _DEFAULT_NETWORK, _DEFAULT_STABLE,
        )
        if deducted is None:
            # Fetch actual balance for a helpful error message (outside atomic update)
            stable_row = await db.fetchrow(
                "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
                uid, gid, _DEFAULT_NETWORK, _DEFAULT_STABLE,
            )
            have = to_human(int(stable_row["amount"] or 0)) if stable_row else 0.0
            raise InsufficientBalanceError(
                f"Insufficient DSD balance. Need {total_cost_h:.2f} DSD, have {have:.2f}."
            )

        # Pay buy fee to guild treasury (raw int)
        buy_fee_raw = total_cost_raw * int(meta["buy_fee_pct"] * 10_000) // 10_000
        if buy_fee_raw > 0:
            await db.execute(
                """INSERT INTO guild_treasury (guild_id, symbol, balance)
                   VALUES ($1, 'DSD', $2)
                   ON CONFLICT (guild_id, symbol)
                   DO UPDATE SET balance = guild_treasury.balance + $2""",
                gid, buy_fee_raw,
            )

        # Stones: only one per user  -  INSERT with conflict guard to prevent races
        table = meta["table"]
        inserted = await db.fetchval(
            f"INSERT INTO {table} (user_id, guild_id, staked_amount) VALUES ($1, $2, $3) "
            f"ON CONFLICT (user_id, guild_id) DO NOTHING RETURNING user_id",
            uid, gid, total_cost_raw,
        )
        if inserted is None:
            raise ValidationError(f"You already own a {meta['name']}.")

    new_balance_raw = await db.fetchval(
        "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
        uid, gid, _DEFAULT_NETWORK, _DEFAULT_STABLE,
    )

    return BuyResult(
        success=True,
        message=f"Purchased {body.quantity}x {meta['name']} for {total_cost_h:.2f} DSD.",
        item_key=key,
        cost=total_cost_h,
        currency=_DEFAULT_STABLE,
        new_balance=to_human(int(new_balance_raw or 0)),
    )


@router.post("/sell", response_model=SellResult, summary="Sell item back")
async def sell_item(
    body: SellRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Sell a shop item back. Returns staked stablecoin minus sell fee (credited as DSD)."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    key = body.item_key

    meta = _get_item_meta(key)
    if not meta:
        raise NotFoundError("Shop item not found.")

    async with db.transaction():
        # Stones: return staked amount (stored in staked_amount column) minus sell fee
        if body.quantity != 1:
            raise ValidationError("Stones can only be sold one at a time (quantity must be 1).")

        table = meta["table"]
        deleted = await db.fetchrow(
            f"DELETE FROM {table} WHERE user_id = $1 AND guild_id = $2 RETURNING staked_amount",
            uid, gid,
        )
        if not deleted:
            raise ValidationError(f"You don't own a {meta['name']}.")

        # staked_amount is raw NUMERIC(36,0); keep int math exact.
        staked_raw = int(deleted["staked_amount"]) if deleted["staked_amount"] else int(meta["cost_stable"])
        sell_fee_raw = staked_raw * int(meta["sell_fee_pct"] * 10_000) // 10_000
        total_revenue_raw = staked_raw - sell_fee_raw

        # Pay sell fee to guild treasury (raw int)
        if sell_fee_raw > 0:
            await db.execute(
                """INSERT INTO guild_treasury (guild_id, symbol, balance)
                   VALUES ($1, 'DSD', $2)
                   ON CONFLICT (guild_id, symbol)
                   DO UPDATE SET balance = guild_treasury.balance + $2""",
                gid, sell_fee_raw,
            )

        # Credit DSD back to wallet_holdings
        await db.execute(
            """INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, network, symbol)
               DO UPDATE SET amount = wallet_holdings.amount + $5""",
            uid, gid, _DEFAULT_NETWORK, _DEFAULT_STABLE, total_revenue_raw,
        )

    new_balance_raw = await db.fetchval(
        "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
        uid, gid, _DEFAULT_NETWORK, _DEFAULT_STABLE,
    )

    total_revenue_h = to_human(total_revenue_raw)
    return SellResult(
        success=True,
        message=f"Sold {body.quantity}x {meta['name']} for {total_revenue_h:.2f} DSD.",
        item_key=key,
        revenue=total_revenue_h,
        currency=_DEFAULT_STABLE,
        new_balance=to_human(int(new_balance_raw or 0)),
    )
