"""Mining router -- mining operations endpoints for Discoin v2."""
from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from api.v2.utils import to_iso
from api.v2.schemas.mining import (
    MinerInfo,
    MiningBlockInfo,
    MiningGroupDetail,
    MiningGroupInfo,
    MiningNetworkStats,
    RigInfo,
    UserMiningConfig,
    UserRigInfo,
)
from core.config import Config
from constants.economy import CHAIN_SWITCH_COOLDOWN as _CHAIN_SWITCH_COOLDOWN

router = APIRouter(prefix="/mining", tags=["mining"], dependencies=[require_module("chain")])

# ---------------------------------------------------------------------------
# Rig catalogue  -  derived from the canonical Config.MINING_RIGS used by
# the Discord bot so that API and bot always agree on rig IDs and stats.
# ---------------------------------------------------------------------------
DEFAULT_RIGS: list[dict] = [
    {"rig_id": rig_id, "name": v["name"], "hashrate": v["hashrate"],
     "power": v["power"], "price": v["price"]}
    for rig_id, v in Config.MINING_RIGS.items()
]


# ---------------------------------------------------------------------------
# 1. GET /mining/networks
# ---------------------------------------------------------------------------

@router.get("/networks", response_model=list[MiningNetworkStats], summary="PoW network stats")
async def list_networks(
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get Proof-of-Work network stats for all chains in a guild."""
    guild_id = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT chain_symbol, block_height, difficulty, total_hashrate, current_reward, last_block_ts
           FROM pow_network_state
           WHERE guild_id = $1
           ORDER BY chain_symbol""",
        guild_id,
    )
    return [
        MiningNetworkStats(
            symbol=r["chain_symbol"],
            block_height=r["block_height"],
            difficulty=float(r["difficulty"]),
            total_hashrate=float(r["total_hashrate"]),
            current_reward=to_human(int(r["current_reward"])),
            last_block_ts=to_iso(r["last_block_ts"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 2. GET /mining/networks/{symbol}
# ---------------------------------------------------------------------------

@router.get("/networks/{symbol}", response_model=MiningNetworkStats, summary="Single network stats")
async def get_network(
    symbol: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get stats for a single PoW mining network."""
    guild_id = int(user["guild_id"])
    r = await conn.fetchrow(
        """SELECT chain_symbol, block_height, difficulty, total_hashrate, current_reward, last_block_ts
           FROM pow_network_state
           WHERE guild_id = $1 AND chain_symbol = $2""",
        guild_id, symbol.upper(),
    )
    if not r:
        raise NotFoundError(f"Mining network '{symbol}' not found.")
    return MiningNetworkStats(
        symbol=r["chain_symbol"],
        block_height=r["block_height"],
        difficulty=float(r["difficulty"]),
        total_hashrate=float(r["total_hashrate"]),
        current_reward=float(r["current_reward"]),
        last_block_ts=to_iso(r["last_block_ts"]),
    )


# ---------------------------------------------------------------------------
# 3. GET /mining/miners
# ---------------------------------------------------------------------------

@router.get("/miners", response_model=list[MinerInfo], summary="Active miners leaderboard")
async def list_miners(
    limit: int = Query(20, ge=1, le=100, description="Number of miners."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get a leaderboard of active miners ranked by hashrate."""
    guild_id = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT mr.user_id, COALESCE(u.username, '') as username,
                  SUM(mr.quantity) as rig_count,
                  COALESCE(mb.blocks_mined, 0) as blocks_mined
           FROM mining_rigs mr
           LEFT JOIN users u ON u.user_id = mr.user_id AND u.guild_id = mr.guild_id
           LEFT JOIN (
               SELECT miner_id, COUNT(*) as blocks_mined
               FROM mining_blocks
               WHERE guild_id = $1 AND miner_id IS NOT NULL
               GROUP BY miner_id
           ) mb ON mb.miner_id = mr.user_id
           WHERE mr.guild_id = $1 AND mr.quantity > 0
           GROUP BY mr.user_id, u.username, mb.blocks_mined
           ORDER BY rig_count DESC
           LIMIT $2""",
        guild_id, limit,
    )

    results = []
    for r in rows:
        # Calculate total hashrate from rigs
        rig_rows = await conn.fetch(
            "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
            r["user_id"], guild_id,
        )
        total_hashrate = 0.0
        for rr in rig_rows:
            rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == rr["rig_id"]), None)
            if rig_def:
                total_hashrate += rig_def["hashrate"] * rr["quantity"]

        uname = r["username"] or f"User {str(r['user_id'])[:8]}"
        results.append(MinerInfo(
            user_id=r["user_id"],
            username=uname,
            total_hashrate=total_hashrate,
            rig_count=r["rig_count"],
            blocks_mined=r["blocks_mined"],
        ))

    return results


# ---------------------------------------------------------------------------
# 4. GET /mining/groups
# ---------------------------------------------------------------------------

@router.get("/groups", response_model=list[MiningGroupInfo], summary="Mining groups")
async def list_groups(
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all mining groups for a guild."""
    guild_id = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT mg.group_id, mg.name, mg.founder_id,
                  COUNT(mgm.user_id) as member_count
           FROM mining_groups mg
           LEFT JOIN mining_group_members mgm ON mgm.group_id = mg.group_id AND mgm.guild_id = mg.guild_id
           WHERE mg.guild_id = $1
           GROUP BY mg.group_id, mg.name, mg.founder_id
           ORDER BY member_count DESC""",
        guild_id,
    )

    results = []
    for r in rows:
        # Calculate group hashrate from member rigs
        member_rows = await conn.fetch(
            """SELECT mr.rig_id, SUM(mr.quantity) as total_qty
               FROM mining_rigs mr
               JOIN mining_group_members mgm ON mgm.user_id = mr.user_id AND mgm.guild_id = mr.guild_id
               WHERE mgm.group_id = $1 AND mgm.guild_id = $2
               GROUP BY mr.rig_id""",
            r["group_id"], guild_id,
        )
        total_hashrate = 0.0
        for mr in member_rows:
            rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == mr["rig_id"]), None)
            if rig_def:
                total_hashrate += rig_def["hashrate"] * mr["total_qty"]

        results.append(MiningGroupInfo(
            group_id=r["group_id"],
            name=r["name"],
            founder_id=r["founder_id"],
            member_count=r["member_count"],
            total_hashrate=total_hashrate,
        ))
    return results


# ---------------------------------------------------------------------------
# 5. GET /mining/groups/{group_id}
# ---------------------------------------------------------------------------

@router.get("/groups/{group_id}", response_model=MiningGroupDetail, summary="Mining group details")
async def get_group(
    group_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get details for a mining group including member list."""
    guild_id = int(user["guild_id"])
    group = await conn.fetchrow(
        "SELECT * FROM mining_groups WHERE group_id = $1 AND guild_id = $2",
        group_id, guild_id,
    )
    if not group:
        raise NotFoundError("Mining group not found.")

    members_rows = await conn.fetch(
        """SELECT mgm.user_id, COALESCE(u.username, '') as username
           FROM mining_group_members mgm
           LEFT JOIN users u ON u.user_id = mgm.user_id AND u.guild_id = mgm.guild_id
           WHERE mgm.group_id = $1 AND mgm.guild_id = $2
           ORDER BY mgm.joined_at""",
        group_id, guild_id,
    )

    members = []
    total_hashrate = 0.0
    for mr in members_rows:
        rig_rows = await conn.fetch(
            "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
            mr["user_id"], guild_id,
        )
        user_hashrate = 0.0
        rig_count = 0
        for rr in rig_rows:
            rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == rr["rig_id"]), None)
            if rig_def:
                user_hashrate += rig_def["hashrate"] * rr["quantity"]
            rig_count += rr["quantity"]

        blocks = await conn.fetchval(
            "SELECT COUNT(*) FROM mining_blocks WHERE guild_id = $1 AND miner_id = $2",
            guild_id, mr["user_id"],
        )

        total_hashrate += user_hashrate
        uname = mr["username"] or f"User {str(mr['user_id'])[:8]}"
        members.append(MinerInfo(
            user_id=mr["user_id"],
            username=uname,
            total_hashrate=user_hashrate,
            rig_count=rig_count,
            blocks_mined=blocks or 0,
        ))

    return MiningGroupDetail(
        group_id=group["group_id"],
        name=group["name"],
        founder_id=group["founder_id"],
        member_count=len(members),
        total_hashrate=total_hashrate,
        members=members,
    )


# ---------------------------------------------------------------------------
# 6. GET /mining/rigs
# ---------------------------------------------------------------------------

@router.get("/rigs", response_model=list[RigInfo], summary="Available rig types")
async def list_rigs():
    """List all available mining rig types and their specifications."""
    return [
        RigInfo(
            rig_id=r["rig_id"],
            name=r["name"],
            hashrate=r["hashrate"],
            power=r["power"],
            price=r["price"],
        )
        for r in DEFAULT_RIGS
    ]


# ---------------------------------------------------------------------------
# 7. GET /mining/my-rigs
# ---------------------------------------------------------------------------

@router.get("/my-rigs", response_model=list[UserRigInfo], summary="My rigs")
async def my_rigs(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's owned mining rigs."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await conn.fetch(
        "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
        user_id, guild_id,
    )

    return [
        UserRigInfo(
            rig_id=r["rig_id"],
            quantity=r["quantity"],
            total_hashrate=r["quantity"] * next(
                (d["hashrate"] for d in DEFAULT_RIGS if d["rig_id"] == r["rig_id"]), 0.0
            ),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 8. GET /mining/my-config
# ---------------------------------------------------------------------------

@router.get("/my-config", response_model=UserMiningConfig, summary="My mining configuration")
async def my_config(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's mining configuration (rigs, assignments, group)."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Rigs
    rig_rows = await conn.fetch(
        "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
        user_id, guild_id,
    )
    total_hashrate = 0.0
    total_rigs = 0
    for r in rig_rows:
        rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == r["rig_id"]), None)
        if rig_def:
            total_hashrate += rig_def["hashrate"] * r["quantity"]
        total_rigs += r["quantity"]

    # Chain assignments
    assign_rows = await conn.fetch(
        "SELECT rig_id, chain_symbol, quantity FROM rig_chain_assignments WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
        user_id, guild_id,
    )
    assignments = [
        {"rig_id": a["rig_id"], "chain_symbol": a["chain_symbol"], "quantity": a["quantity"]}
        for a in assign_rows
    ]

    # Group membership
    group_row = await conn.fetchrow(
        "SELECT group_id FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )

    return UserMiningConfig(
        total_hashrate=total_hashrate,
        total_rigs=total_rigs,
        assignments=assignments,
        group_id=group_row["group_id"] if group_row else None,
    )


# ---------------------------------------------------------------------------
# 9. GET /mining/blocks
# ---------------------------------------------------------------------------

@router.get("/blocks", response_model=list[MiningBlockInfo], summary="Recent mining blocks")
async def list_blocks(
    limit: int = Query(20, ge=1, le=100, description="Number of blocks."),
    offset: int = Query(0, ge=0, description="Offset."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get recent mining blocks for a guild."""
    guild_id = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT id, block_height, block_ts, miner_id, reward, total_hashrate
           FROM mining_blocks
           WHERE guild_id = $1
           ORDER BY block_height DESC
           LIMIT $2 OFFSET $3""",
        guild_id, limit, offset,
    )
    return [
        MiningBlockInfo(
            id=r["id"],
            block_height=r["block_height"],
            block_ts=to_iso(r["block_ts"]),
            miner_id=r["miner_id"],
            reward=to_human(int(r["reward"])),
            total_hashrate=float(r["total_hashrate"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 10. GET /mining/rig-types  -  available rig types with prices/stats
# ---------------------------------------------------------------------------

@router.get("/rig-types", response_model=list[RigInfo], summary="Available rig types with prices")
async def rig_types():
    """List all mining rig types with their specifications and prices."""
    return [
        RigInfo(
            rig_id=r["rig_id"],
            name=r["name"],
            hashrate=r["hashrate"],
            power=r["power"],
            price=r["price"],
        )
        for r in DEFAULT_RIGS
    ]


# ---------------------------------------------------------------------------
# 11. POST /mining/buy-rig  -  purchase mining rig
# ---------------------------------------------------------------------------

@router.post("/buy-rig", summary="Purchase a mining rig")
async def buy_rig(
    body: dict,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Purchase mining rigs using USD wallet balance.

    Body: {rig_type: str, quantity: int}
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    rig_type = body.get("rig_type", "").upper()
    quantity = int(body.get("quantity", 1))

    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")

    rig_def = next((r for r in DEFAULT_RIGS if r["rig_id"] == rig_type), None)
    if not rig_def:
        raise NotFoundError(f"Unknown rig type '{rig_type}'. Use GET /mining/rig-types for available options.")

    # Enforce job-based rig slot limit (matches Discord bot cogs/chain_group.py:2547)
    from core.config import Config
    job_row = await conn.fetchrow(
        "SELECT job_id FROM user_jobs WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    job_cfg = Config.JOBS.get(job_row["job_id"], {}) if job_row else {}
    max_slots = job_cfg.get("rig_slots", 2)

    current_rigs_row = await conn.fetchrow(
        "SELECT COALESCE(SUM(quantity), 0) as total FROM mining_rigs WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    current_total = int(current_rigs_row["total"]) if current_rigs_row else 0
    if current_total + quantity > max_slots:
        raise ValidationError(
            f"Rig slot limit reached. Your job allows {max_slots} rigs, "
            f"you have {current_total}. Cannot add {quantity} more."
        )

    total_cost = rig_def["price"] * quantity

    async with conn.transaction():
        # Atomic check-and-deduct  -  RETURNING ensures we know if it matched
        deducted = await conn.fetchrow(
            "UPDATE users SET wallet = wallet - $1 WHERE user_id = $2 AND guild_id = $3 AND wallet >= $1 RETURNING wallet",
            total_cost, user_id, guild_id,
        )
        if deducted is None:
            raise InsufficientBalanceError(f"Insufficient balance. Need ${total_cost:,.2f}.")

        # Add rigs to user
        await conn.execute(
            """INSERT INTO mining_rigs (user_id, guild_id, rig_id, quantity)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, rig_id)
               DO UPDATE SET quantity = mining_rigs.quantity + $4""",
            user_id, guild_id, rig_type, quantity,
        )

        # Auto-assign new rigs to SUN chain by default
        await conn.execute(
            """INSERT INTO rig_chain_assignments (user_id, guild_id, rig_id, chain_symbol, quantity)
               VALUES ($1, $2, $3, 'SUN', $4)
               ON CONFLICT (user_id, guild_id, rig_id, chain_symbol)
               DO UPDATE SET quantity = rig_chain_assignments.quantity + $4""",
            user_id, guild_id, rig_type, quantity,
        )

    # Fetch updated state
    rig_row = await conn.fetchrow(
        "SELECT quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND rig_id = $3",
        user_id, guild_id, rig_type,
    )
    wallet_row = await conn.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )

    return {
        "success": True,
        "rig_type": rig_type,
        "quantity_purchased": quantity,
        "total_cost": total_cost,
        "total_owned": rig_row["quantity"] if rig_row else quantity,
        "new_hashrate": (rig_row["quantity"] if rig_row else quantity) * rig_def["hashrate"],
        "new_balance": to_human(int(wallet_row["wallet"] or 0)) if wallet_row else 0.0,
    }


# ---------------------------------------------------------------------------
# 12. POST /mining/sell-rig  -  sell mining rig
# ---------------------------------------------------------------------------

@router.post("/sell-rig", summary="Sell a mining rig")
async def sell_rig(
    body: dict,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Sell mining rigs for 50% of their purchase price.

    Body: {rig_type: str, quantity: int}
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    rig_type = body.get("rig_type", "").upper()
    quantity = int(body.get("quantity", 1))

    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")

    rig_def = next((r for r in DEFAULT_RIGS if r["rig_id"] == rig_type), None)
    if not rig_def:
        raise NotFoundError(f"Unknown rig type '{rig_type}'.")

    sell_price = rig_def["price"] * 0.5
    total_revenue = sell_price * quantity

    async with conn.transaction():
        # Check and deduct rig quantity
        await conn.execute(
            "UPDATE mining_rigs SET quantity = quantity - $1 WHERE user_id = $2 AND guild_id = $3 AND rig_id = $4 AND quantity >= $1",
            quantity, user_id, guild_id, rig_type,
        )
        verify = await conn.fetchrow(
            "SELECT quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND rig_id = $3",
            user_id, guild_id, rig_type,
        )
        if verify is None or verify["quantity"] < 0:
            raise InsufficientBalanceError(f"Insufficient rigs. Check your inventory with GET /mining/my-rigs.")

        # Also remove from chain assignments (remove from SUN first, then others)
        remaining_to_remove = quantity
        assign_rows = await conn.fetch(
            "SELECT chain_symbol, quantity FROM rig_chain_assignments WHERE user_id = $1 AND guild_id = $2 AND rig_id = $3 AND quantity > 0 ORDER BY chain_symbol",
            user_id, guild_id, rig_type,
        )
        for ar in assign_rows:
            if remaining_to_remove <= 0:
                break
            remove_from_chain = min(remaining_to_remove, ar["quantity"])
            await conn.execute(
                "UPDATE rig_chain_assignments SET quantity = quantity - $1 WHERE user_id = $2 AND guild_id = $3 AND rig_id = $4 AND chain_symbol = $5",
                remove_from_chain, user_id, guild_id, rig_type, ar["chain_symbol"],
            )
            remaining_to_remove -= remove_from_chain

        # Credit wallet
        await conn.execute(
            "UPDATE users SET wallet = wallet + $1 WHERE user_id = $2 AND guild_id = $3",
            total_revenue, user_id, guild_id,
        )

    wallet_row = await conn.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )

    return {
        "success": True,
        "rig_type": rig_type,
        "quantity_sold": quantity,
        "revenue": total_revenue,
        "remaining": verify["quantity"] if verify else 0,
        "new_balance": to_human(int(wallet_row["wallet"] or 0)) if wallet_row else 0.0,
    }


# ---------------------------------------------------------------------------
# 13. POST /mining/set-network  -  switch mining network
# ---------------------------------------------------------------------------

@router.post("/set-network", summary="Reassign rigs to a different network")
async def set_network(
    body: dict,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Reassign rigs from one mining network to another.

    Body: {rig_type: str, from_network: str, to_network: str, quantity: int}
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    rig_type = body.get("rig_type", "").upper()
    from_net = body.get("from_network", "SUN").upper()
    to_net = body.get("to_network", "").upper()
    quantity = int(body.get("quantity", 1))

    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")

    rig_def = next((r for r in DEFAULT_RIGS if r["rig_id"] == rig_type), None)
    if not rig_def:
        raise NotFoundError(f"Unknown rig type '{rig_type}'.")

    # Chain-switch cooldown  -  same rule as Discord bot (prevents chain-hopping exploits)
    last_switch = await conn.fetchval(
        "SELECT last_chain_switch FROM user_mining_config WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    if last_switch:
        import time as _time
        elapsed = _time.time() - last_switch.timestamp()
        if elapsed < _CHAIN_SWITCH_COOLDOWN:
            remaining = int(_CHAIN_SWITCH_COOLDOWN - elapsed)
            mins, secs = divmod(remaining, 60)
            raise ValidationError(
                f"Chain switching is on cooldown. Try again in {mins}m {secs}s. "
                f"This prevents chain-hopping exploitation."
            )

    # Verify source network has enough rigs
    from_row = await conn.fetchrow(
        "SELECT quantity FROM rig_chain_assignments WHERE user_id = $1 AND guild_id = $2 AND rig_id = $3 AND chain_symbol = $4",
        user_id, guild_id, rig_type, from_net,
    )
    if not from_row or from_row["quantity"] < quantity:
        available = from_row["quantity"] if from_row else 0
        raise InsufficientBalanceError(
            f"Not enough {rig_type} rigs on {from_net} (have {available}, need {quantity})."
        )

    async with conn.transaction():
        # Deduct from source
        await conn.execute(
            "UPDATE rig_chain_assignments SET quantity = quantity - $1 WHERE user_id = $2 AND guild_id = $3 AND rig_id = $4 AND chain_symbol = $5",
            quantity, user_id, guild_id, rig_type, from_net,
        )
        # Credit to destination
        await conn.execute(
            """INSERT INTO rig_chain_assignments (user_id, guild_id, rig_id, chain_symbol, quantity)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, rig_id, chain_symbol)
               DO UPDATE SET quantity = rig_chain_assignments.quantity + $5""",
            user_id, guild_id, rig_type, to_net, quantity,
        )
        # Record chain switch for cooldown enforcement
        await conn.execute(
            """INSERT INTO user_mining_config (user_id, guild_id, last_chain_switch)
               VALUES ($1, $2, now())
               ON CONFLICT (user_id, guild_id)
               DO UPDATE SET last_chain_switch = now()""",
            user_id, guild_id,
        )

    return {
        "success": True,
        "rig_type": rig_type,
        "quantity": quantity,
        "from_network": from_net,
        "to_network": to_net,
    }


# ---------------------------------------------------------------------------
# 14. POST /mining/set-mode  -  set solo/pool/group mining mode
# ---------------------------------------------------------------------------

@router.post("/set-mode", summary="Set mining mode (solo/pool/group)")
async def set_mode(
    body: dict,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Set the user's mining mode.

    Body: {mode: "solo" | "pool" | "group"}
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    mode = body.get("mode", "").lower()

    if mode not in ("solo", "pool", "group"):
        raise ValidationError("Mode must be 'solo', 'pool', or 'group'.")

    # If switching to group mode, verify user is in a group
    if mode == "group":
        membership = await conn.fetchrow(
            "SELECT group_id FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        if not membership:
            raise ValidationError("You must join a mining group before switching to group mode.")

    # Update mining config
    await conn.execute(
        """INSERT INTO user_mining_config (user_id, guild_id, mode) VALUES ($1, $2, $3)
           ON CONFLICT (user_id, guild_id) DO UPDATE SET mode = EXCLUDED.mode""",
        user_id, guild_id, mode,
    )

    # Keep legacy mining_pool_members in sync
    if mode == "pool":
        await conn.execute(
            """INSERT INTO mining_pool_members (user_id, guild_id)
               VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            user_id, guild_id,
        )
    else:
        await conn.execute(
            "DELETE FROM mining_pool_members WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

    return {"success": True, "mode": mode}
