"""Stats & Leaderboard router  -  5 endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_db, get_orm_db
from api.v2.schemas.stats import LeaderboardEntry, ReserveStats, ServerStats

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/stats", response_model=ServerStats, summary="Server stats overview")
async def server_stats(db=Depends(get_db)):
    """Return server-wide aggregated statistics."""
    total_users = await db.fetchval("SELECT COUNT(DISTINCT user_id) FROM users") or 0
    total_tokens = await db.fetchval("SELECT COUNT(*) FROM guild_tokens") or 0
    total_pools = await db.fetchval("SELECT COUNT(*) FROM pools") or 0
    total_trades = await db.fetchval("SELECT COUNT(*) FROM transactions") or 0

    # Volume = sum of USD-side amounts across all trades.
    # For BUY: user spends USD (amount_in when symbol_in='USD')
    # For SELL: user receives USD (amount_out when symbol_out='USD')
    # For SWAP: estimate via price_at * amount_in
    vol_row = await db.fetchrow("""
        SELECT COALESCE(SUM(
            CASE
                WHEN symbol_in = 'USD' THEN COALESCE(amount_in, 0)
                WHEN symbol_out = 'USD' THEN COALESCE(amount_out, 0)
                ELSE COALESCE(amount_in, 0) * COALESCE(price_at, 1)
            END
        ), 0) AS vol FROM transactions
    """)
    total_volume = float(vol_row["vol"]) if vol_row else 0.0

    mcap_row = await db.fetchrow(
        "SELECT COALESCE(SUM(price * circulating_supply), 0) AS mcap FROM crypto_prices"
    )
    total_mcap = float(mcap_row["mcap"]) if mcap_row else 0.0

    treasury_row = await db.fetchrow(
        "SELECT COALESCE(SUM(balance), 0) AS bal FROM guild_treasury"
    )
    treasury = float(treasury_row["bal"]) if treasury_row else 0.0

    active_loans = await db.fetchval(
        "SELECT COUNT(*) FROM loans WHERE outstanding > 0"
    ) or 0
    active_stakes = await db.fetchval(
        "SELECT COUNT(*) FROM stakes WHERE amount > 0"
    ) or 0

    hashrate_row = await db.fetchrow(
        "SELECT COALESCE(SUM(total_hashrate), 0) AS hr FROM pow_network_state"
    )
    hashrate = float(hashrate_row["hr"]) if hashrate_row else 0.0

    return ServerStats(
        total_users=total_users,
        total_tokens=total_tokens,
        total_pools=total_pools,
        total_trades=total_trades,
        total_volume_usd=total_volume,
        total_market_cap=total_mcap,
        treasury_balance=treasury,
        active_loans=active_loans,
        active_stakes=active_stakes,
        mining_hashrate=hashrate,
    )


@router.get("/reserve", response_model=ReserveStats, summary="Reserve and treasury stats")
async def reserve_stats(db=Depends(get_db)):
    """Return public treasury and validator gas fee statistics.

    total_burned is approximate: derived from (initial_circulating_supply -
    current_circulating_supply) across all tokens, where initial = max_supply * 0.5.
    """
    treasury_row = await db.fetchrow(
        "SELECT COALESCE(SUM(balance), 0) AS bal FROM guild_treasury"
    )
    treasury = float(treasury_row["bal"]) if treasury_row else 0.0

    gas_row = await db.fetchrow(
        """SELECT
               COALESCE(SUM(total_gas_collected), 0)    AS gas,
               COALESCE(SUM(validator_reward), 0)        AS rewards
           FROM validator_blocks
           WHERE status = 'confirmed'"""
    )
    total_gas = float(gas_row["gas"]) if gas_row else 0.0
    total_rewards = float(gas_row["rewards"]) if gas_row else 0.0

    # Approximate burn: tokens removed from circulating supply
    # Initial supply seeded at max_supply * 0.5 per markets.py
    burn_row = await db.fetchrow(
        """SELECT COALESCE(SUM(
               GREATEST(0, (max_supply * 0.5) - circulating_supply)
           ), 0) AS burned
           FROM crypto_prices
           WHERE max_supply > 0"""
    )
    burned = float(burn_row["burned"]) if burn_row else 0.0

    return ReserveStats(
        treasury_balance=treasury,
        total_gas_collected=total_gas,
        total_distributed_to_validators=total_rewards,
        total_burned=burned,
    )


@router.get("/leaderboard", response_model=list[LeaderboardEntry], summary="Net worth leaderboard")
async def net_worth_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    orm_db=Depends(get_orm_db),
):
    """Return net worth leaderboard using canonical compute_bulk_net_worth."""
    from services.net_worth import compute_bulk_net_worth

    gid = int(user["guild_id"])
    user_val = await compute_bulk_net_worth(gid, orm_db)
    ranked = sorted(user_val.items(), key=lambda x: x[1], reverse=True)
    page = ranked[offset:offset + limit]
    return [
        LeaderboardEntry(
            rank=offset + i + 1,
            user_id=str(uid),
            value=round(nw, 2),
        )
        for i, (uid, nw) in enumerate(page)
    ]


@router.get("/leaderboard/traders", response_model=list[LeaderboardEntry], summary="Trading profit leaderboard")
async def trading_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """Return leaderboard ranked by realized trading PnL."""
    rows = await db.fetch(
        """
        SELECT user_id, realized_pnl
        FROM user_profiles
        WHERE realized_pnl > 0
        ORDER BY realized_pnl DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [
        LeaderboardEntry(
            rank=offset + i + 1,
            user_id=str(r["user_id"]),
            value=to_human(int(r["realized_pnl"])),
            detail="realized PnL",
        )
        for i, r in enumerate(rows)
    ]


@router.get("/leaderboard/miners", response_model=list[LeaderboardEntry], summary="Mining hashrate leaderboard")
async def mining_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """Return leaderboard ranked by total mining rig count."""
    rows = await db.fetch(
        """
        SELECT user_id, SUM(quantity) AS total_rigs
        FROM mining_rigs
        GROUP BY user_id
        HAVING SUM(quantity) > 0
        ORDER BY total_rigs DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [
        LeaderboardEntry(
            rank=offset + i + 1,
            user_id=str(r["user_id"]),
            value=float(r["total_rigs"]),
            detail="total rigs",
        )
        for i, r in enumerate(rows)
    ]


@router.get("/leaderboard/gamblers", response_model=list[LeaderboardEntry], summary="Gambling profit leaderboard")
async def gambling_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """Return leaderboard ranked by gambling profit."""
    rows = await db.fetch(
        """
        SELECT user_id, total_game_profit
        FROM user_profiles
        WHERE total_game_profit > 0
        ORDER BY total_game_profit DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [
        LeaderboardEntry(
            rank=offset + i + 1,
            user_id=str(r["user_id"]),
            value=to_human(int(r["total_game_profit"])),
            detail="game profit",
        )
        for i, r in enumerate(rows)
    ]
