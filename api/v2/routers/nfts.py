"""NFT endpoints  -  collections, marketplace, user NFTs, sale history."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

import pathlib

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.framework.scale import to_human, to_raw
from api.v2.dependencies import get_current_user, get_optional_user, get_db, require_module
from api.v2.exceptions import NotFoundError, ForbiddenError, InsufficientBalanceError, ValidationError
from api.v2.utils import to_iso


# ── Request schemas ──────────────────────────────────────────────────────

class DeployCollectionRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10, description="Collection symbol (uppercase)")
    name: str = Field(..., min_length=1, max_length=50, description="Collection name")
    description: str = Field("", max_length=500)
    network: str = Field(..., description="Network: ARC or DSC")
    mint_price: float = Field(..., ge=0, description="Mint price in network coin")
    max_supply: int | None = Field(None, ge=1, description="Max supply, or None for unlimited")


class MintRequest(BaseModel):
    collection_id: int = Field(..., description="Collection to mint from")
    name: str = Field(..., min_length=1, max_length=100, description="NFT name")
    description: str = Field("", max_length=500)
    image_url: str = Field("", max_length=500)
    rarity: str = Field("common", description="Rarity tier")


class ListRequest(BaseModel):
    price: float = Field(..., gt=0, description="Listing price")
    currency: str = Field("COIN", max_length=10, description="Currency for listing")


class TransferRequest(BaseModel):
    to_user_id: int = Field(..., description="Recipient user ID")


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_token_hash(guild_id: int, collection_id: int, token_id: int) -> str:
    nonce = secrets.token_hex(8)
    raw = f"{guild_id}:{collection_id}:{token_id}:{time.time():.3f}:{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _coin_to_usd(db, guild_id: int, amount: float, symbol: str) -> float | None:
    """Convert a coin amount to USD using crypto_prices. Returns None if unknown."""
    if not symbol or symbol.upper() in ("USD", "USDC", "DSD"):
        return round(amount, 2)
    row = await db.fetchrow(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol.upper(),
    )
    if row and row["price"]:
        return round(amount * float(row["price"]), 2)
    return None

router = APIRouter(prefix="/nfts", tags=["nfts"])


@router.get(
    "/summary",
    summary="NFT summary for authenticated user",
    dependencies=[require_module("nft")],
)
async def nft_summary(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    owned_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM nfts WHERE owner_id = $1 AND guild_id = $2",
        user_id,
        guild_id,
    )
    owned_count = int(owned_row["cnt"]) if owned_row else 0

    listed_row = await db.fetchrow(
        """SELECT COUNT(*) AS cnt FROM nft_listings nl
           JOIN nfts n ON n.id = nl.nft_id AND n.guild_id = $2
           WHERE n.owner_id = $1""",
        user_id,
        guild_id,
    )
    listed_count = int(listed_row["cnt"]) if listed_row else 0

    value_rows = await db.fetch(
        """SELECT n.id, n.rarity, n.collection_id,
                  c.mint_price, c.mint_token
           FROM nfts n
           LEFT JOIN nft_collections c ON c.id = n.collection_id
           WHERE n.owner_id = $1 AND n.guild_id = $2""",
        user_id,
        guild_id,
    )
    total_value = 0.0
    for r in value_rows:
        if r["collection_id"]:
            avg_row = await db.fetchrow(
                """SELECT AVG(s.price) AS avg_price FROM nft_sales s
                   JOIN nfts n ON n.id = s.nft_id
                   WHERE s.collection_id = $1 AND n.rarity = $2""",
                r["collection_id"],
                r["rarity"] or "common",
            )
            if avg_row and avg_row["avg_price"]:
                total_value += float(avg_row["avg_price"])
            else:
                price = to_human(int(r["mint_price"])) if r["mint_price"] else 0.0
                mint_token = r["mint_token"] or "USD"
                if mint_token != "USD":
                    cp = await db.fetchrow(
                        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
                        guild_id,
                        mint_token,
                    )
                    if cp:
                        price *= float(cp["price"])
                total_value += price

    return {
        "owned_count": owned_count,
        "total_value": round(total_value, 2),
        "listed_count": listed_count,
    }


@router.get(
    "/collections",
    summary="List all NFT collections for the guild",
)
async def list_collections(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        return []

    rows = await db.fetch(
        """SELECT id, name, symbol, network, description, image_url, mint_price, mint_token,
                  max_supply, minted_count, contract_address, created_at
           FROM nft_collections
           WHERE guild_id = $1
           ORDER BY created_at DESC
           LIMIT $2 OFFSET $3""",
        guild_id,
        limit,
        offset,
    )
    result = []
    for r in rows:
        mint_price = to_human(int(r["mint_price"])) if r["mint_price"] else 0.0
        mint_token = r["mint_token"] or "USD"
        mint_price_usd = await _coin_to_usd(db, guild_id, mint_price, mint_token)
        result.append({
            "id": r["id"],
            "name": r["name"],
            "symbol": r["symbol"],
            "network": r["network"],
            "description": r["description"],
            "image_url": r["image_url"],
            "mint_price": mint_price,
            "mint_token": mint_token,
            "mint_price_usd": mint_price_usd,
            "max_supply": r["max_supply"],
            "minted_count": r["minted_count"],
            "contract_address": r["contract_address"] or "",
            "created_at": to_iso(r["created_at"]),
        })
    return result


@router.get(
    "/collection/{collection_id}",
    summary="Collection details with NFTs",
)
async def collection_detail(
    collection_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        raise NotFoundError("Guild context required.")

    col = await db.fetchrow(
        "SELECT * FROM nft_collections WHERE id = $1 AND guild_id = $2",
        collection_id,
        guild_id,
    )
    if not col:
        raise NotFoundError("Collection not found.")

    nfts = await db.fetch(
        """SELECT id, token_id, name, rarity, image_url, owner_id, token_hash, minted_at
           FROM nfts
           WHERE collection_id = $1 AND guild_id = $2
           ORDER BY token_id ASC
           LIMIT $3 OFFSET $4""",
        collection_id,
        guild_id,
        limit,
        offset,
    )

    total_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM nfts WHERE collection_id = $1 AND guild_id = $2",
        collection_id,
        guild_id,
    )

    floor_row = await db.fetchrow(
        """SELECT MIN(l.price) AS floor_price, l.currency
           FROM nft_listings l JOIN nfts n ON n.id = l.nft_id
           WHERE n.collection_id = $1 AND n.guild_id = $2
           GROUP BY l.currency LIMIT 1""",
        collection_id,
        guild_id,
    )

    sales_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(price), 0) AS volume FROM nft_sales WHERE collection_id = $1",
        collection_id,
    )

    mint_price = to_human(int(col["mint_price"])) if col["mint_price"] else 0.0
    mint_token = col["mint_token"] or "USD"
    mint_price_usd = await _coin_to_usd(db, guild_id, mint_price, mint_token)

    floor_price = float(floor_row["floor_price"]) if floor_row and floor_row["floor_price"] else None
    floor_currency = floor_row["currency"] if floor_row else None
    floor_price_usd = await _coin_to_usd(db, guild_id, floor_price, floor_currency) if floor_price and floor_currency else None

    return {
        "collection": {
            "id": col["id"],
            "name": col["name"],
            "symbol": col["symbol"],
            "network": col["network"],
            "description": col["description"],
            "image_url": col["image_url"],
            "mint_price": mint_price,
            "mint_token": mint_token,
            "mint_price_usd": mint_price_usd,
            "max_supply": col["max_supply"],
            "minted_count": col["minted_count"],
            "contract_address": col["contract_address"] or "",
            "floor_price": floor_price,
            "floor_currency": floor_currency,
            "floor_price_usd": floor_price_usd,
            "total_sales": int(sales_row["cnt"]) if sales_row else 0,
            "total_volume": float(sales_row["volume"]) if sales_row else 0.0,
        },
        "nfts": [
            {
                "id": n["id"],
                "token_id": n["token_id"],
                "name": n["name"],
                "rarity": n["rarity"],
                "image_url": n["image_url"],
                "owner_id": str(n["owner_id"]),
                "token_hash": n["token_hash"] or "",
                "minted_at": to_iso(n["minted_at"]),
            }
            for n in nfts
        ],
        "total": int(total_row["cnt"]) if total_row else 0,
    }


@router.get(
    "/marketplace",
    summary="Current NFT marketplace listings",
)
async def marketplace(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("recent", pattern="^(recent|price_asc|price_desc|rarity)$"),
    collection: str | None = Query(None),
    rarity: str | None = Query(None),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        return {"listings": [], "total": 0}

    order_map = {
        "recent": "nl.listed_at DESC",
        "price_asc": "nl.price ASC",
        "price_desc": "nl.price DESC",
        "rarity": "CASE n.rarity WHEN 'legendary' THEN 1 WHEN 'epic' THEN 2 WHEN 'rare' THEN 3 WHEN 'uncommon' THEN 4 ELSE 5 END",
    }
    order_clause = order_map.get(sort, "nl.listed_at DESC")

    conditions = ["n.guild_id = $1"]
    params: list = [guild_id]
    idx = 2

    if collection:
        conditions.append(f"c.symbol = ${idx}")
        params.append(collection.upper())
        idx += 1

    if rarity:
        conditions.append(f"n.rarity = ${idx}")
        params.append(rarity.lower())
        idx += 1

    where = " AND ".join(conditions)

    params.append(limit)
    params.append(offset)

    rows = await db.fetch(
        f"""SELECT nl.id AS listing_id, nl.price, nl.currency, nl.listed_at,
                  n.id AS nft_id, n.name, n.rarity, n.image_url, n.token_id, n.token_hash,
                  n.owner_id AS seller_id,
                  c.name AS collection_name, c.symbol AS collection_symbol,
                  c.network AS collection_network, c.contract_address
           FROM nft_listings nl
           JOIN nfts n ON n.id = nl.nft_id
           LEFT JOIN nft_collections c ON c.id = n.collection_id
           WHERE {where}
           ORDER BY {order_clause}
           LIMIT ${idx} OFFSET ${idx + 1}""",
        *params,
    )

    total_row = await db.fetchrow(
        f"""SELECT COUNT(*) AS cnt FROM nft_listings nl
           JOIN nfts n ON n.id = nl.nft_id
           LEFT JOIN nft_collections c ON c.id = n.collection_id
           WHERE {where}""",
        *params[:-2],
    )

    listings = []
    for r in rows:
        price = float(r["price"])
        currency = r["currency"] or "USD"
        price_usd = await _coin_to_usd(db, guild_id, price, currency)
        listings.append({
            "listing_id": r["listing_id"],
            "nft_id": r["nft_id"],
            "token_id": r["token_id"],
            "name": r["name"],
            "rarity": r["rarity"],
            "image_url": r["image_url"],
            "price": price,
            "currency": currency,
            "price_usd": price_usd,
            "seller_id": str(r["seller_id"]),
            "collection_name": r["collection_name"],
            "collection_symbol": r["collection_symbol"],
            "network": r["collection_network"],
            "contract_address": r["contract_address"] or "",
            "token_hash": r["token_hash"] or "",
            "listed_at": to_iso(r["listed_at"]),
        })

    return {
        "listings": listings,
        "total": int(total_row["cnt"]) if total_row else 0,
    }


@router.get(
    "/my",
    summary="User's owned NFTs",
    dependencies=[require_module("nft")],
)
async def my_nfts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """SELECT n.id, n.token_id, n.name, n.rarity, n.image_url, n.token_hash, n.minted_at,
                  c.name AS collection_name, c.symbol AS collection_symbol,
                  c.id AS collection_id, c.network, c.contract_address
           FROM nfts n
           LEFT JOIN nft_collections c ON c.id = n.collection_id
           WHERE n.owner_id = $1 AND n.guild_id = $2
           ORDER BY n.minted_at DESC
           LIMIT $3 OFFSET $4""",
        user_id,
        guild_id,
        limit,
        offset,
    )

    total_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM nfts WHERE owner_id = $1 AND guild_id = $2",
        user_id,
        guild_id,
    )

    # Check which NFTs are listed
    nft_ids = [r["id"] for r in rows]
    listed_ids: set[int] = set()
    if nft_ids:
        listed_rows = await db.fetch(
            "SELECT nft_id FROM nft_listings WHERE nft_id = ANY($1::int[])",
            nft_ids,
        )
        listed_ids = {r["nft_id"] for r in listed_rows}

    return {
        "nfts": [
            {
                "id": r["id"],
                "token_id": r["token_id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "image_url": r["image_url"],
                "collection_name": r["collection_name"],
                "collection_symbol": r["collection_symbol"],
                "collection_id": r["collection_id"],
                "network": r["network"],
                "contract_address": r["contract_address"] or "",
                "token_hash": r["token_hash"] or "",
                "is_listed": r["id"] in listed_ids,
                "minted_at": to_iso(r["minted_at"]),
            }
            for r in rows
        ],
        "total": int(total_row["cnt"]) if total_row else 0,
    }


@router.get(
    "/{nft_id}",
    summary="Specific NFT details",
)
async def nft_detail(
    nft_id: int,
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        raise NotFoundError("Guild context required.")

    row = await db.fetchrow(
        """SELECT n.*, c.name AS collection_name, c.symbol AS collection_symbol,
                  c.image_url AS collection_image, c.network AS collection_network,
                  c.contract_address AS collection_contract, c.mint_price, c.mint_token
           FROM nfts n
           LEFT JOIN nft_collections c ON c.id = n.collection_id
           WHERE n.id = $1 AND n.guild_id = $2""",
        nft_id,
        guild_id,
    )
    if not row:
        raise NotFoundError("NFT not found.")

    listing = await db.fetchrow(
        "SELECT id, price, currency, listed_at FROM nft_listings WHERE nft_id = $1",
        nft_id,
    )

    # Get sale history
    sales = await db.fetch(
        """SELECT s.price, s.currency, s.sold_at, s.buyer_id, s.seller_id
           FROM nft_sales s WHERE s.nft_id = $1
           ORDER BY s.sold_at DESC LIMIT 10""",
        nft_id,
    )

    mint_price = to_human(int(row["mint_price"])) if row["mint_price"] else 0.0
    mint_token = row["mint_token"] or "USD"
    mint_price_usd = await _coin_to_usd(db, guild_id, mint_price, mint_token)

    listing_data = None
    if listing:
        l_price = to_human(int(listing["price"]))
        l_currency = listing["currency"] or "USD"
        l_price_usd = await _coin_to_usd(db, guild_id, l_price, l_currency)
        listing_data = {
            "listing_id": listing["id"],
            "price": l_price,
            "currency": l_currency,
            "price_usd": l_price_usd,
            "listed_at": to_iso(listing["listed_at"]),
        }

    sales_list = []
    for s in sales:
        s_price = float(s["price"])
        s_currency = s["currency"] or "USD"
        s_price_usd = await _coin_to_usd(db, guild_id, s_price, s_currency)
        sales_list.append({
            "price": s_price,
            "currency": s_currency,
            "price_usd": s_price_usd,
            "buyer_id": str(s["buyer_id"]),
            "seller_id": str(s["seller_id"]),
            "sold_at": to_iso(s["sold_at"]),
        })

    return {
        "id": row["id"],
        "token_id": row["token_id"],
        "name": row["name"],
        "description": row["description"],
        "rarity": row["rarity"],
        "image_url": row["image_url"],
        "token_hash": row["token_hash"] or "",
        "owner_id": str(row["owner_id"]),
        "minted_by": str(row["minted_by"]),
        "collection_name": row["collection_name"],
        "collection_symbol": row["collection_symbol"],
        "collection_id": row["collection_id"],
        "network": row["collection_network"],
        "contract_address": row["collection_contract"] or "",
        "mint_price": mint_price,
        "mint_token": mint_token,
        "mint_price_usd": mint_price_usd,
        "minted_at": to_iso(row["minted_at"]),
        "listing": listing_data,
        "sales": sales_list,
    }


@router.get(
    "/{nft_id}/sales",
    summary="NFT sale history",
)
async def nft_sales(
    nft_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        raise NotFoundError("Guild context required.")

    nft = await db.fetchrow(
        "SELECT id FROM nfts WHERE id = $1 AND guild_id = $2",
        nft_id,
        guild_id,
    )
    if not nft:
        raise NotFoundError("NFT not found.")

    rows = await db.fetch(
        """SELECT s.id, s.seller_id, s.buyer_id, s.price, s.currency, s.sold_at
           FROM nft_sales s
           WHERE s.nft_id = $1
           ORDER BY s.sold_at DESC
           LIMIT $2 OFFSET $3""",
        nft_id,
        limit,
        offset,
    )

    return [
        {
            "id": r["id"],
            "seller_id": str(r["seller_id"]),
            "buyer_id": str(r["buyer_id"]),
            "price": float(r["price"]),
            "currency": r["currency"] or "USD",
            "sold_at": to_iso(r["sold_at"]),
        }
        for r in rows
    ]


_POS_NETWORKS = {"ARC", "DSC"}
_NETWORK_COIN = {"ARC": "ARC", "DSC": "DSC"}


@router.post(
    "/collections",
    summary="Deploy a new NFT collection",
    dependencies=[require_module("nft")],
)
async def deploy_collection(
    body: DeployCollectionRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Validate network
    network = body.network.upper()
    if network not in _POS_NETWORKS:
        raise ValidationError("Network must be ARC or DSC.")

    # Validate symbol (uppercase)
    symbol = body.symbol.upper()

    # Check job tier
    from core.config import Config
    job_row = await db.fetchrow(
        "SELECT job_id FROM user_jobs WHERE user_id=$1 AND guild_id=$2",
        user_id, guild_id,
    )
    job_id = job_row["job_id"] if job_row else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, {})
    can_deploy = job_cfg.get("perks", {}).get("can_deploy_token", False)
    if not can_deploy:
        raise ForbiddenError("Your job tier does not allow deploying NFT collections.")

    # Check symbol not already taken
    existing = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE guild_id=$1 AND symbol=$2",
        guild_id, symbol,
    )
    if existing:
        raise ValidationError(f"Collection symbol '{symbol}' is already taken.")

    # Compute and charge deploy gas (10x base gas fee)
    net_coin = _NETWORK_COIN[network]
    gas_row = await db.fetchrow(
        "SELECT gas_fee FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
        guild_id, net_coin,
    )
    deploy_gas = float(gas_row["gas_fee"]) * 10 if gas_row else 0.0

    if deploy_gas > 0:
        bal = await db.fetchrow(
            "SELECT amount FROM crypto_holdings WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, net_coin,
        )
        if not bal or float(bal["amount"]) < deploy_gas:
            raise InsufficientBalanceError(f"Need {deploy_gas:.4f} {net_coin} gas to deploy.")
        await db.execute(
            "UPDATE crypto_holdings SET amount = amount - $1"
            " WHERE user_id=$2 AND guild_id=$3 AND symbol=$4",
            deploy_gas, user_id, guild_id, net_coin,
        )

    # Generate contract address
    contract_address = (
        "0x"
        + hashlib.sha256(
            f"{guild_id}:{user_id}:{symbol}:{time.time():.6f}".encode()
        ).hexdigest()[:40]
    )

    # Mint token defaults to network native coin
    mint_token = net_coin

    row = await db.fetchrow(
        "INSERT INTO nft_collections"
        " (guild_id, name, symbol, network, description, image_url,"
        "  max_supply, mint_price, mint_token, creator_id, contract_address)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
        " RETURNING *",
        guild_id, body.name, symbol, network, body.description,
        "", body.max_supply, to_raw(body.mint_price), mint_token, user_id, contract_address,
    )

    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": row["name"],
        "network": row["network"],
        "contract_address": row["contract_address"],
        "mint_price": to_human(int(row["mint_price"])),
        "mint_token": row["mint_token"],
        "deploy_gas_charged": deploy_gas,
        "deploy_gas_coin": net_coin,
    }


@router.post(
    "/mint",
    summary="Mint a new NFT",
    dependencies=[require_module("nft")],
)
async def mint_nft(
    body: MintRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    col = await db.fetchrow(
        "SELECT * FROM nft_collections WHERE id = $1 AND guild_id = $2",
        body.collection_id, guild_id,
    )
    if not col:
        raise NotFoundError("Collection not found.")

    mint_price_raw: int = int(col["mint_price"]) if col["mint_price"] else 0
    mint_price_human: float = to_human(mint_price_raw)
    mint_token = (col["mint_token"] or "USD").upper()

    # Check supply
    if col["max_supply"] is not None and col["minted_count"] >= col["max_supply"]:
        raise ValidationError("Collection is fully minted.")

    # Deduct cost (all wallet/holding columns are NUMERIC(36,0) raw -- compare and operate in raw)
    if mint_price_raw > 0:
        if mint_token == "USD":
            bal = await db.fetchrow(
                "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
                user_id, guild_id,
            )
            if not bal or int(bal["wallet"]) < mint_price_raw:
                raise InsufficientBalanceError(f"Need {mint_price_human:.2f} USD to mint.")
            await db.execute(
                "UPDATE users SET wallet = wallet - $1 WHERE user_id = $2 AND guild_id = $3",
                mint_price_raw, user_id, guild_id,
            )
        else:
            bal = await db.fetchrow(
                "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
                user_id, guild_id, mint_token,
            )
            if not bal or int(bal["amount"]) < mint_price_raw:
                raise InsufficientBalanceError(f"Need {mint_price_human:.4f} {mint_token} to mint.")
            await db.execute(
                "UPDATE crypto_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND symbol = $4",
                mint_price_raw, user_id, guild_id, mint_token,
            )

    _nft_images_root = pathlib.Path(__file__).resolve().parents[3] / "static" / "nft-images"

    async with db.transaction():
        # Increment minted_count
        updated = await db.fetchrow(
            "UPDATE nft_collections SET minted_count = minted_count + 1 WHERE id = $1 RETURNING minted_count",
            body.collection_id,
        )
        token_id = updated["minted_count"]
        token_hash = _make_token_hash(guild_id, body.collection_id, token_id)

        # Use gallery image for this slot if available, renaming to token hash
        image_url = body.image_url
        gallery_row = await db.fetchrow(
            "SELECT id, image_url FROM nft_collection_images"
            " WHERE collection_id = $1 AND slot = $2",
            body.collection_id, token_id,
        )
        if gallery_row:
            old_url: str = gallery_row["image_url"]
            if old_url.startswith("/nft-images/"):
                rel = old_url.removeprefix("/nft-images/")
                old_path = _nft_images_root / rel
                if old_path.exists():
                    new_path = old_path.with_name(f"{token_hash}{old_path.suffix}")
                    old_path.rename(new_path)
                    image_url = f"/nft-images/{rel.rsplit('/', 1)[0]}/{token_hash}{old_path.suffix}"
                else:
                    image_url = old_url
            else:
                image_url = old_url
            await db.execute(
                "UPDATE nft_collection_images SET image_url = $1 WHERE id = $2",
                image_url, gallery_row["id"],
            )

        row = await db.fetchrow(
            """INSERT INTO nfts
               (guild_id, collection_id, token_id, owner_id, name, description,
                image_url, rarity, metadata, token_hash, minted_by)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               RETURNING *""",
            guild_id, body.collection_id, token_id, user_id,
            body.name, body.description, image_url,
            body.rarity, json.dumps({}), token_hash, user_id,
        )

    return {
        "id": row["id"],
        "token_id": row["token_id"],
        "name": row["name"],
        "rarity": row["rarity"],
        "token_hash": row["token_hash"],
        "image_url": row["image_url"],
        "collection_id": body.collection_id,
        "collection_name": col["name"],
        "collection_symbol": col["symbol"],
    }


@router.post(
    "/{nft_id}/list",
    summary="List an NFT for sale",
    dependencies=[require_module("nft")],
)
async def list_nft(
    nft_id: int,
    body: ListRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    nft = await db.fetchrow(
        "SELECT * FROM nfts WHERE id = $1 AND guild_id = $2", nft_id, guild_id,
    )
    if not nft:
        raise NotFoundError("NFT not found.")
    if nft["owner_id"] != user_id:
        raise ForbiddenError("You don't own this NFT.")

    listing = await db.fetchrow(
        """INSERT INTO nft_listings (guild_id, nft_id, seller_id, price, currency)
           VALUES ($1,$2,$3,$4,$5)
           ON CONFLICT (nft_id) DO UPDATE SET price = $4, currency = $5, listed_at = now()
           RETURNING *""",
        guild_id, nft_id, user_id, to_raw(body.price), body.currency.upper(),
    )

    return {
        "listing_id": listing["id"],
        "nft_id": nft_id,
        "price": to_human(int(listing["price"])),
        "currency": listing["currency"],
        "listed_at": to_iso(listing["listed_at"]),
    }


@router.post(
    "/{nft_id}/unlist",
    summary="Remove NFT listing",
    dependencies=[require_module("nft")],
)
async def unlist_nft(
    nft_id: int,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    user_id = int(user["user_id"])
    result = await db.execute(
        "DELETE FROM nft_listings WHERE nft_id = $1 AND seller_id = $2",
        nft_id, user_id,
    )
    if result == "DELETE 0":
        raise NotFoundError("No active listing found for this NFT.")
    return {"success": True, "nft_id": nft_id}


@router.post(
    "/{nft_id}/buy",
    summary="Buy a listed NFT",
    dependencies=[require_module("nft")],
)
async def buy_nft(
    nft_id: int,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    buyer_id = int(user["user_id"])

    listing = await db.fetchrow(
        """SELECT l.*, n.owner_id AS current_owner, n.collection_id,
                  c.mint_token
           FROM nft_listings l
           JOIN nfts n ON n.id = l.nft_id
           JOIN nft_collections c ON c.id = n.collection_id
           WHERE l.nft_id = $1""",
        nft_id,
    )
    if not listing:
        raise NotFoundError("NFT is not listed for sale.")
    if listing["current_owner"] == buyer_id:
        raise ValidationError("You already own this NFT.")

    price_raw: int = int(listing["price"])
    price_human: float = to_human(price_raw)
    currency = (listing["currency"] or "USD").upper()
    seller_id = listing["seller_id"]

    # Check buyer balance (all columns are NUMERIC(36,0) raw -- compare in raw)
    if currency == "USD":
        bal = await db.fetchrow(
            "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
            buyer_id, guild_id,
        )
        if not bal or int(bal["wallet"]) < price_raw:
            raise InsufficientBalanceError(f"Need {price_human:.2f} {currency}.")
    else:
        bal = await db.fetchrow(
            "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
            buyer_id, guild_id, currency,
        )
        if not bal or int(bal["amount"]) < price_raw:
            raise InsufficientBalanceError(f"Need {price_human:.4f} {currency}.")

    async with db.transaction():
        # Remove listing
        deleted = await db.fetchrow(
            "DELETE FROM nft_listings WHERE nft_id = $1 RETURNING *", nft_id,
        )
        if not deleted:
            raise NotFoundError("Listing was removed.")

        # Deduct from buyer, credit seller (raw values for NUMERIC(36,0) columns)
        if currency == "USD":
            await db.execute(
                "UPDATE users SET wallet = wallet - $1 WHERE user_id = $2 AND guild_id = $3",
                price_raw, buyer_id, guild_id,
            )
            await db.execute(
                "UPDATE users SET wallet = wallet + $1 WHERE user_id = $2 AND guild_id = $3",
                price_raw, seller_id, guild_id,
            )
        else:
            await db.execute(
                "UPDATE crypto_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND symbol = $4",
                price_raw, buyer_id, guild_id, currency,
            )
            await db.execute(
                """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (user_id, guild_id, symbol) DO UPDATE SET amount = crypto_holdings.amount + $4""",
                seller_id, guild_id, currency, price_raw,
            )

        # Transfer ownership
        await db.execute(
            "UPDATE nfts SET owner_id = $1 WHERE id = $2", buyer_id, nft_id,
        )

        # Record sale
        await db.execute(
            """INSERT INTO nft_sales (guild_id, nft_id, collection_id, seller_id, buyer_id, price, currency)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            guild_id, nft_id, listing["collection_id"],
            seller_id, buyer_id, price_raw, currency,
        )

    return {
        "success": True,
        "nft_id": nft_id,
        "price": price_human,
        "currency": currency,
    }


@router.post(
    "/{nft_id}/transfer",
    summary="Transfer an NFT to another user",
    dependencies=[require_module("nft")],
)
async def transfer_nft(
    nft_id: int,
    body: TransferRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    nft = await db.fetchrow(
        "SELECT * FROM nfts WHERE id = $1 AND guild_id = $2", nft_id, guild_id,
    )
    if not nft:
        raise NotFoundError("NFT not found.")
    if nft["owner_id"] != user_id:
        raise ForbiddenError("You don't own this NFT.")
    if body.to_user_id == user_id:
        raise ValidationError("Cannot transfer to yourself.")

    async with db.transaction():
        await db.execute(
            "UPDATE nfts SET owner_id = $1 WHERE id = $2", body.to_user_id, nft_id,
        )
        # Remove any listing
        await db.execute(
            "DELETE FROM nft_listings WHERE nft_id = $1", nft_id,
        )

    return {"success": True, "nft_id": nft_id, "new_owner_id": str(body.to_user_id)}


@router.get(
    "/collection/{collection_id}/sales",
    summary="Collection sale history",
)
async def collection_sales(
    collection_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        raise NotFoundError("Guild context required.")

    col = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE id = $1 AND guild_id = $2",
        collection_id,
        guild_id,
    )
    if not col:
        raise NotFoundError("Collection not found.")

    rows = await db.fetch(
        """SELECT s.id, s.nft_id, s.seller_id, s.buyer_id, s.price, s.currency, s.sold_at,
                  n.name AS nft_name, n.token_id, n.rarity
           FROM nft_sales s
           JOIN nfts n ON n.id = s.nft_id
           WHERE s.collection_id = $1
           ORDER BY s.sold_at DESC
           LIMIT $2 OFFSET $3""",
        collection_id,
        limit,
        offset,
    )

    total_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM nft_sales WHERE collection_id = $1",
        collection_id,
    )

    return {
        "sales": [
            {
                "id": r["id"],
                "nft_id": r["nft_id"],
                "token_id": r["token_id"],
                "nft_name": r["nft_name"],
                "rarity": r["rarity"],
                "seller_id": str(r["seller_id"]),
                "buyer_id": str(r["buyer_id"]),
                "price": float(r["price"]),
                "currency": r["currency"] or "USD",
                "sold_at": to_iso(r["sold_at"]),
            }
            for r in rows
        ],
        "total": int(total_row["cnt"]) if total_row else 0,
    }
