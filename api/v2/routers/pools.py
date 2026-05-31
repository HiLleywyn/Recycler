"""Pools router -- liquidity pool endpoints for Discoin v2."""
from __future__ import annotations

import math

from core.framework.scale import to_human, to_raw

import asyncpg
from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from api.v2.schemas.pool import (
    AddLiquidityRequest,
    LPPosition,
    PoolInfo,
    RemoveLiquidityRequest,
)

router = APIRouter(prefix="/pools", tags=["pools"], dependencies=[require_module("pools")], redirect_slashes=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_pool_info(conn: asyncpg.Connection, pool_id: str, guild_id: int) -> dict | None:
    """Fetch a pool row with calculated TVL."""
    row = await conn.fetchrow(
        """SELECT p.pool_id, p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                  COALESCE(pa.price, 0) as price_a,
                  COALESCE(pb.price, 0) as price_b
           FROM pools p
           LEFT JOIN crypto_prices pa ON pa.symbol = p.token_a AND pa.guild_id = p.guild_id
           LEFT JOIN crypto_prices pb ON pb.symbol = p.token_b AND pb.guild_id = p.guild_id
           WHERE p.pool_id = $1 AND p.guild_id = $2""",
        pool_id, guild_id,
    )
    return dict(row) if row else None


def _calc_tvl(reserve_a: float, price_a: float, reserve_b: float, price_b: float) -> float:
    return reserve_a * price_a + reserve_b * price_b


# ---------------------------------------------------------------------------
# 1. GET /pools
# ---------------------------------------------------------------------------

@router.get("", response_model=list[PoolInfo], summary="List liquidity pools")
async def list_pools(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """List all liquidity pools for the authenticated user's guild."""
    gid = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT p.pool_id, p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                  COALESCE(pa.price, 0) as price_a,
                  COALESCE(pb.price, 0) as price_b
           FROM pools p
           LEFT JOIN crypto_prices pa ON pa.symbol = p.token_a AND pa.guild_id = p.guild_id
           LEFT JOIN crypto_prices pb ON pb.symbol = p.token_b AND pb.guild_id = p.guild_id
           WHERE p.guild_id = $1
           ORDER BY p.pool_id""",
        gid,
    )

    results = []
    for r in rows:
        ra = to_human(int(r["reserve_a"] or 0))
        rb = to_human(int(r["reserve_b"] or 0))
        pa = float(r["price_a"])
        pb = float(r["price_b"])
        tvl = _calc_tvl(ra, pa, rb, pb)
        # Calculate 24h volume from transactions
        vol_row = await conn.fetchrow(
            """SELECT COALESCE(SUM(amount_in), 0) as vol
               FROM transactions
               WHERE guild_id = $1
                 AND (symbol_in = $2 OR symbol_out = $2 OR symbol_in = $3 OR symbol_out = $3)
                 AND tx_type = 'SWAP'
                 AND ts > now() - interval '24 hours'""",
            gid, r["token_a"], r["token_b"],
        )
        vol_24h = to_human(int(vol_row["vol"] or 0)) if vol_row else 0.0
        results.append(PoolInfo(
            pool_id=r["pool_id"],
            token_a=r["token_a"],
            token_b=r["token_b"],
            reserve_a=ra,
            reserve_b=rb,
            total_lp=to_human(int(r["total_lp"] or 0)),
            tvl=round(tvl, 2),
            apy=0.0,
            fee_rate=0.003,
            volume_24h=round(vol_24h, 2),
        ))
    return results


# ---------------------------------------------------------------------------
# 2. GET /pools/my-positions  (must be before /{pool_id} to avoid capture)
# ---------------------------------------------------------------------------

@router.get("/my-positions", response_model=list[LPPosition], summary="My LP positions")
async def my_positions(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's liquidity provider positions across all pools."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await conn.fetch(
        """SELECT lp.pool_id, lp.lp_shares, lp.added_at,
                  p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                  COALESCE(pa.price, 0) as price_a,
                  COALESCE(pb.price, 0) as price_b
           FROM lp_positions lp
           JOIN pools p ON p.pool_id = lp.pool_id AND p.guild_id = lp.guild_id
           LEFT JOIN crypto_prices pa ON pa.symbol = p.token_a AND pa.guild_id = p.guild_id
           LEFT JOIN crypto_prices pb ON pb.symbol = p.token_b AND pb.guild_id = p.guild_id
           WHERE lp.user_id = $1 AND lp.guild_id = $2 AND lp.lp_shares > 0
           ORDER BY lp.added_at DESC""",
        user_id, guild_id,
    )

    results = []
    for r in rows:
        total_lp = to_human(int(r["total_lp"] or 0))
        tvl = _calc_tvl(
            to_human(int(r["reserve_a"] or 0)), float(r["price_a"]),
            to_human(int(r["reserve_b"] or 0)), float(r["price_b"]),
        )
        lp = to_human(int(r["lp_shares"] or 0))
        results.append(LPPosition(
            pool_id=r["pool_id"],
            token_a=r["token_a"],
            token_b=r["token_b"],
            lp_shares=lp,
            value_usd=round(lp / total_lp * tvl, 2) if total_lp > 0 else 0.0,
            share_pct=round(lp / total_lp * 100, 4) if total_lp > 0 else 0.0,
            added_at=r["added_at"],
        ))
    return results


# ---------------------------------------------------------------------------
# 3. POST /pools/add-liquidity
# ---------------------------------------------------------------------------

@router.post("/add-liquidity", summary="Add liquidity to a pool")
async def add_liquidity(
    body: AddLiquidityRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Add liquidity to a pool by providing both tokens. Returns LP shares minted."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    pool = await conn.fetchrow(
        "SELECT * FROM pools WHERE pool_id = $1 AND guild_id = $2",
        body.pool_id, guild_id,
    )
    if not pool:
        raise NotFoundError("Pool not found.")

    reserve_a = to_human(int(pool["reserve_a"] or 0))
    reserve_b = to_human(int(pool["reserve_b"] or 0))
    total_lp = to_human(int(pool["total_lp"] or 0))

    # Calculate LP shares to mint (human scale)
    if total_lp == 0:
        # First deposit: LP shares = sqrt(amount_a * amount_b)
        lp_minted = math.sqrt(body.amount_a * body.amount_b)
    else:
        # Proportional deposit
        share_a = body.amount_a / reserve_a if reserve_a > 0 else 0
        share_b = body.amount_b / reserve_b if reserve_b > 0 else 0
        lp_minted = min(share_a, share_b) * total_lp

    if lp_minted <= 0:
        raise ValidationError("Cannot mint zero LP shares.")

    amount_a_raw = to_raw(body.amount_a)
    amount_b_raw = to_raw(body.amount_b)
    lp_minted_raw = to_raw(lp_minted)

    async with conn.transaction():
        # Deduct token A from holdings
        bal_a = await conn.fetchrow(
            """SELECT amount FROM crypto_holdings
               WHERE user_id = $1 AND guild_id = $2 AND symbol = $3""",
            user_id, guild_id, pool["token_a"],
        )
        if not bal_a or to_human(int(bal_a["amount"] or 0)) < body.amount_a:
            raise InsufficientBalanceError(f"Insufficient {pool['token_a']} balance.")
        deducted_a = await conn.fetchrow(
            """UPDATE crypto_holdings SET amount = amount - $1
               WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1
               RETURNING amount""",
            amount_a_raw, user_id, guild_id, pool["token_a"],
        )
        if deducted_a is None:
            raise InsufficientBalanceError(f"Insufficient {pool['token_a']} balance.")

        # Deduct token B from holdings
        deducted_b = await conn.fetchrow(
            """UPDATE crypto_holdings SET amount = amount - $1
               WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1
               RETURNING amount""",
            amount_b_raw, user_id, guild_id, pool["token_b"],
        )
        if deducted_b is None:
            raise InsufficientBalanceError(f"Insufficient {pool['token_b']} balance.")

        # Update pool reserves (raw)
        await conn.execute(
            """UPDATE pools SET reserve_a = reserve_a + $1, reserve_b = reserve_b + $2, total_lp = total_lp + $3
               WHERE pool_id = $4 AND guild_id = $5""",
            amount_a_raw, amount_b_raw, lp_minted_raw, body.pool_id, guild_id,
        )

        # Add LP position (raw)
        await conn.execute(
            """INSERT INTO lp_positions (user_id, guild_id, pool_id, lp_shares)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, pool_id)
               DO UPDATE SET lp_shares = lp_positions.lp_shares + $4""",
            user_id, guild_id, body.pool_id, lp_minted_raw,
        )

        # Record entry snapshot for IL calculation (dimensionless ratios)
        new_total_lp = total_lp + lp_minted
        new_ra = reserve_a + body.amount_a
        new_rb = reserve_b + body.amount_b
        await conn.execute(
            """INSERT INTO lp_snapshots (user_id, guild_id, pool_id, entry_res_a_per_lp, entry_res_b_per_lp)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, pool_id)
               DO UPDATE SET entry_res_a_per_lp = $4, entry_res_b_per_lp = $5""",
            user_id, guild_id, body.pool_id,
            new_ra / new_total_lp if new_total_lp > 0 else 0,
            new_rb / new_total_lp if new_total_lp > 0 else 0,
        )

    return {
        "success": True,
        "message": f"Added liquidity to {body.pool_id}.",
        "lp_shares_minted": round(lp_minted, 8),
        "amount_a": body.amount_a,
        "amount_b": body.amount_b,
    }


# ---------------------------------------------------------------------------
# 4. POST /pools/remove-liquidity
# ---------------------------------------------------------------------------

@router.post("/remove-liquidity", summary="Remove liquidity from a pool")
async def remove_liquidity(
    body: RemoveLiquidityRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Remove liquidity from a pool by redeeming LP shares. Returns both tokens."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    pool = await conn.fetchrow(
        "SELECT * FROM pools WHERE pool_id = $1 AND guild_id = $2",
        body.pool_id, guild_id,
    )
    if not pool:
        raise NotFoundError("Pool not found.")

    total_lp = to_human(int(pool["total_lp"] or 0))
    if total_lp <= 0:
        raise ValidationError("Pool has no liquidity.")

    share = body.lp_shares / total_lp
    reserve_a_h = to_human(int(pool["reserve_a"] or 0))
    reserve_b_h = to_human(int(pool["reserve_b"] or 0))
    amount_a = reserve_a_h * share
    amount_b = reserve_b_h * share

    # Verify proportional withdrawal doesn't violate invariant
    old_k = reserve_a_h * reserve_b_h
    new_k = (reserve_a_h - amount_a) * (reserve_b_h - amount_b)
    if new_k < 0:
        raise ValidationError("Withdrawal would deplete pool")

    lp_shares_raw = to_raw(body.lp_shares)
    amount_a_raw = to_raw(amount_a)
    amount_b_raw = to_raw(amount_b)

    async with conn.transaction():
        # Deduct LP shares from user position
        lp_row = await conn.fetchrow(
            """SELECT lp_shares FROM lp_positions
               WHERE user_id = $1 AND guild_id = $2 AND pool_id = $3""",
            user_id, guild_id, body.pool_id,
        )
        if not lp_row or to_human(int(lp_row["lp_shares"] or 0)) < body.lp_shares:
            raise InsufficientBalanceError("Insufficient LP shares.")
        deducted_lp = await conn.fetchrow(
            """UPDATE lp_positions SET lp_shares = lp_shares - $1
               WHERE user_id = $2 AND guild_id = $3 AND pool_id = $4 AND lp_shares >= $1
               RETURNING lp_shares""",
            lp_shares_raw, user_id, guild_id, body.pool_id,
        )
        if deducted_lp is None:
            raise InsufficientBalanceError("Insufficient LP shares.")

        # Update pool reserves (raw)
        await conn.execute(
            """UPDATE pools SET reserve_a = reserve_a - $1, reserve_b = reserve_b - $2, total_lp = total_lp - $3
               WHERE pool_id = $4 AND guild_id = $5""",
            amount_a_raw, amount_b_raw, lp_shares_raw, body.pool_id, guild_id,
        )

        # Return token A to holdings (raw)
        await conn.execute(
            """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, symbol)
               DO UPDATE SET amount = crypto_holdings.amount + $4""",
            user_id, guild_id, pool["token_a"], amount_a_raw,
        )

        # Return token B to holdings (raw)
        await conn.execute(
            """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, symbol)
               DO UPDATE SET amount = crypto_holdings.amount + $4""",
            user_id, guild_id, pool["token_b"], amount_b_raw,
        )

        # Clean up zero-balance LP positions
        await conn.execute(
            "DELETE FROM lp_positions WHERE user_id = $1 AND guild_id = $2 AND pool_id = $3 AND lp_shares <= 0",
            user_id, guild_id, body.pool_id,
        )

    return {
        "success": True,
        "message": f"Removed liquidity from {body.pool_id}.",
        "lp_shares_redeemed": body.lp_shares,
        "amount_a": round(amount_a, 8),
        "amount_b": round(amount_b, 8),
        "token_a": pool["token_a"],
        "token_b": pool["token_b"],
    }


# ---------------------------------------------------------------------------
# 5. GET /pools/{pool_id}  (parameterized routes after fixed routes)
# ---------------------------------------------------------------------------

@router.get("/{pool_id}", response_model=PoolInfo, summary="Get pool details")
async def get_pool(
    pool_id: str,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get detailed information about a single liquidity pool."""
    gid = int(user["guild_id"])
    r = await _get_pool_info(conn, pool_id, gid)
    if not r:
        raise NotFoundError("Pool not found.")

    ra = to_human(int(r["reserve_a"] or 0))
    rb = to_human(int(r["reserve_b"] or 0))
    pa = float(r["price_a"])
    pb = float(r["price_b"])
    tvl = _calc_tvl(ra, pa, rb, pb)

    vol_row = await conn.fetchrow(
        """SELECT COALESCE(SUM(amount_in), 0) as vol
           FROM transactions
           WHERE guild_id = $1
             AND (symbol_in = $2 OR symbol_out = $2 OR symbol_in = $3 OR symbol_out = $3)
             AND tx_type = 'SWAP'
             AND ts > now() - interval '24 hours'""",
        gid, r["token_a"], r["token_b"],
    )
    vol_24h = to_human(int(vol_row["vol"] or 0)) if vol_row else 0.0

    return PoolInfo(
        pool_id=r["pool_id"],
        token_a=r["token_a"],
        token_b=r["token_b"],
        reserve_a=ra,
        reserve_b=rb,
        total_lp=to_human(int(r["total_lp"] or 0)),
        tvl=round(tvl, 2),
        apy=0.0,
        fee_rate=0.003,
        volume_24h=round(vol_24h, 2),
    )


# ---------------------------------------------------------------------------
# 6. GET /pools/{pool_id}/positions
# ---------------------------------------------------------------------------

@router.get("/{pool_id}/positions", response_model=list[LPPosition], summary="Pool LP positions")
async def pool_positions(
    pool_id: str,
    user: dict = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: asyncpg.Connection = Depends(get_db),
):
    """List LP positions for a specific pool."""
    gid = int(user["guild_id"])

    pool_row = await _get_pool_info(conn, pool_id, gid)
    if not pool_row:
        raise NotFoundError("Pool not found.")

    total_lp = to_human(int(pool_row["total_lp"] or 0))
    tvl = _calc_tvl(
        to_human(int(pool_row["reserve_a"] or 0)), float(pool_row["price_a"]),
        to_human(int(pool_row["reserve_b"] or 0)), float(pool_row["price_b"]),
    )

    rows = await conn.fetch(
        """SELECT lp.user_id, lp.lp_shares, lp.added_at
           FROM lp_positions lp
           WHERE lp.pool_id = $1 AND lp.guild_id = $2 AND lp.lp_shares > 0
           ORDER BY lp.lp_shares DESC
           LIMIT $3 OFFSET $4""",
        pool_id, gid, limit, offset,
    )

    return [
        LPPosition(
            pool_id=pool_id,
            token_a=pool_row["token_a"],
            token_b=pool_row["token_b"],
            lp_shares=to_human(int(r["lp_shares"] or 0)),
            value_usd=round(to_human(int(r["lp_shares"] or 0)) / total_lp * tvl, 2) if total_lp > 0 else 0.0,
            share_pct=round(to_human(int(r["lp_shares"] or 0)) / total_lp * 100, 4) if total_lp > 0 else 0.0,
            added_at=r["added_at"],
        )
        for r in rows
    ]
