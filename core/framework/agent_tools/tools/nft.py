"""
core/framework/agent_tools/tools/nft.py -- NFT read tools.

    nft.collections  all NFT collections in this guild (READ).
    nft.inventory    the caller's (or a target's) owned NFTs (READ).
    nft.market       marketplace listings with prices (READ).
"""
from __future__ import annotations

import logging

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.nft")


# -- nft.collections -----------------------------------------------------------

@tool(
    name="nft.collections",
    summary=(
        "List all NFT collections in this guild: symbol, name, network, "
        "mint price, minted/max supply, and creator. Use before recommending "
        "minting or explaining what collections exist."
    ),
    risk=RiskLevel.READ,
    category="nft",
    params=[],
)
async def nft_collections(ctx: ToolContext, args: dict) -> ToolResult:
    gid = int(ctx.guild_id)
    try:
        rows = await ctx.db.get_collections(gid)
    except Exception as exc:
        log.warning("[nft.collections] db error: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    result = []
    for r in rows:
        mint_price = float(r.get("mint_price") or 0)
        result.append({
            "collection_id": r.get("id"),
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "network": r.get("network"),
            "description": r.get("description") or "",
            "mint_price": round(mint_price, 6),
            "mint_token": r.get("mint_token") or "USD",
            "minted_count": r.get("minted_count") or 0,
            "max_supply": r.get("max_supply"),
            "creator_id": r.get("creator_id"),
            "contract_address": r.get("contract_address"),
        })

    return ToolResult.success({"collections": result, "total": len(result)})


# -- nft.inventory -------------------------------------------------------------

@tool(
    name="nft.inventory",
    summary=(
        "Return NFTs owned by the caller (or a target player): name, "
        "collection, rarity, token ID, and whether it is listed for sale."
    ),
    risk=RiskLevel.READ,
    category="nft",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def nft_inventory(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    try:
        rows = await ctx.db.get_user_nfts(uid, gid)
    except Exception as exc:
        log.warning("[nft.inventory] db error: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    nfts = []
    for r in rows:
        nfts.append({
            "nft_id": r.get("id"),
            "token_id": r.get("token_id"),
            "name": r.get("name"),
            "collection_symbol": r.get("collection_symbol"),
            "collection_name": r.get("collection_name"),
            "rarity": r.get("rarity"),
            "network": r.get("network"),
            "listed_for_sale": bool(r.get("listed")),
            "list_price": float(r.get("list_price") or 0) if r.get("listed") else None,
            "acquired_at": r.get("acquired_at"),
        })

    return ToolResult.success({
        "target_id": uid,
        "nfts": nfts,
        "total": len(nfts),
    })


# -- nft.market ----------------------------------------------------------------

@tool(
    name="nft.market",
    summary=(
        "Return the current NFT marketplace listings in this guild: "
        "NFT name, collection, rarity, price, and seller. "
        "Use to help players find NFTs to buy or check floor prices."
    ),
    risk=RiskLevel.READ,
    category="nft",
    params=[
        ParamSpec("limit", "int", required=False, default=25,
                  description="Max listings to return (1-50)."),
    ],
)
async def nft_market(ctx: ToolContext, args: dict) -> ToolResult:
    gid = int(ctx.guild_id)
    limit = min(50, max(1, int(args.get("limit") or 25)))

    try:
        rows = await ctx.db.get_listings(gid, limit=limit)
    except Exception as exc:
        log.warning("[nft.market] db error: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    listings = []
    for r in rows:
        listings.append({
            "nft_id": r.get("nft_id"),
            "token_id": r.get("token_id"),
            "nft_name": r.get("nft_name"),
            "collection_symbol": r.get("collection_symbol"),
            "collection_name": r.get("collection_name"),
            "rarity": r.get("rarity"),
            "network": r.get("collection_network"),
            "price": float(r.get("price") or 0),
            "price_token": r.get("mint_token") or "USD",
            "seller_id": r.get("seller_id"),
            "listed_at": r.get("listed_at"),
        })

    return ToolResult.success({
        "listings": listings,
        "total": len(listings),
    })
