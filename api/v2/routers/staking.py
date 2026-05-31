"""Staking router -- token staking and delegation endpoints for Discoin v2."""
from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from constants.validators import STAKE_LOCK_SECS, DELEGATION_LOCK_SECS
from core.framework.scale import to_human, to_raw
from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from api.v2.schemas.staking import (
    DelegateRequest,
    DelegationInfo,
    PosValidatorInfo,
    StakeInfo,
    StakeRequest,
    UndelegateRequest,
    UnstakeRequest,
    ValidatorInfo,
)

router = APIRouter(prefix="/staking", tags=["staking"], dependencies=[require_module("staking", "validators")])


# ---------------------------------------------------------------------------
# 1. GET /staking/validators
# ---------------------------------------------------------------------------

@router.get("/validators", response_model=list[ValidatorInfo], summary="List validators")
async def list_validators(
    network: str | None = Query(None, description="Filter by network name."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all staking validators for a guild, optionally filtered by network."""
    gid = int(user["guild_id"])
    params: list[Any] = [gid]
    network_filter = ""
    if network:
        network_filter = "AND gn.network_name = $2"
        params.append(network)

    rows = await conn.fetch(
        f"""SELECT v.validator_id, v.name, v.emoji, v.reward_rate, v.uptime_rate, v.slash_rate,
                   COALESCE(gn.network_name, '') as network,
                   COALESCE(st.total_staked, 0) as total_staked,
                   COALESCE(st.staker_count, 0) as staker_count
            FROM validators v
            LEFT JOIN guild_networks gn ON gn.guild_id = v.guild_id
            LEFT JOIN (
                SELECT validator_id, guild_id,
                       SUM(amount) as total_staked,
                       COUNT(DISTINCT user_id) as staker_count
                FROM stakes
                GROUP BY validator_id, guild_id
            ) st ON st.validator_id = v.validator_id AND st.guild_id = v.guild_id
            WHERE v.guild_id = $1 {network_filter}
            ORDER BY v.name""",
        *params,
    )

    return [
        ValidatorInfo(
            validator_id=r["validator_id"],
            name=r["name"],
            network=r["network"],
            emoji=r["emoji"],
            reward_rate=float(r["reward_rate"]),
            uptime=float(r["uptime_rate"]),
            slash_rate=float(r["slash_rate"]),
            total_staked=to_human(int(r["total_staked"])),
            staker_count=r["staker_count"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 2. GET /staking/validators/{id}
# ---------------------------------------------------------------------------

@router.get("/validators/{validator_id}", response_model=ValidatorInfo, summary="Get validator details")
async def get_validator(
    validator_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get details for a single validator, including staker count and total staked."""
    gid = int(user["guild_id"])
    row = await conn.fetchrow(
        """SELECT v.validator_id, v.name, v.emoji, v.reward_rate, v.uptime_rate, v.slash_rate,
                  COALESCE(st.total_staked, 0) as total_staked,
                  COALESCE(st.staker_count, 0) as staker_count
           FROM validators v
           LEFT JOIN (
               SELECT validator_id, guild_id,
                      SUM(amount) as total_staked,
                      COUNT(DISTINCT user_id) as staker_count
               FROM stakes
               GROUP BY validator_id, guild_id
           ) st ON st.validator_id = v.validator_id AND st.guild_id = v.guild_id
           WHERE v.validator_id = $1 AND v.guild_id = $2""",
        validator_id, gid,
    )
    if not row:
        raise NotFoundError("Validator not found.")

    return ValidatorInfo(
        validator_id=row["validator_id"],
        name=row["name"],
        emoji=row["emoji"],
        reward_rate=float(row["reward_rate"]),
        uptime=float(row["uptime_rate"]),
        slash_rate=float(row["slash_rate"]),
        total_staked=to_human(int(row["total_staked"])),
        staker_count=row["staker_count"],
    )


# ---------------------------------------------------------------------------
# 3. GET /staking/pos-validators
# ---------------------------------------------------------------------------

@router.get("/pos-validators", response_model=list[PosValidatorInfo], summary="Player-run PoS validators")
async def list_pos_validators(
    network: str | None = Query(None, description="Filter by network."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all player-run Proof-of-Stake validators."""
    gid = int(user["guild_id"])
    params: list[Any] = [gid]
    net_filter = ""
    if network:
        net_filter = "AND pv.network = $2"
        params.append(network)

    rows = await conn.fetch(
        f"""SELECT pv.*,
                   COALESCE(d.delegation_count, 0) as delegation_count,
                   COALESCE(d.total_delegated, 0) as total_delegated
            FROM pos_validators pv
            LEFT JOIN (
                SELECT validator_user_id, guild_id, network,
                       COUNT(*) as delegation_count,
                       SUM(pd.amount) as total_delegated
                FROM pos_delegations pd
                GROUP BY validator_user_id, guild_id, network
            ) d ON d.validator_user_id = pv.user_id AND d.guild_id = pv.guild_id AND d.network = pv.network
            WHERE pv.guild_id = $1 {net_filter}
            ORDER BY pv.stake_amount DESC""",
        *params,
    )

    return [
        PosValidatorInfo(
            user_id=r["user_id"],
            network=r["network"],
            stake_token=r["stake_token"],
            stake_amount=to_human(int(r["stake_amount"] or 0)),
            is_active=r["is_active"],
            total_blocks_validated=r["total_blocks_validated"],
            total_rewards_earned=to_human(int(r["total_rewards_earned"] or 0)),
            slash_count=r["slash_count"],
            delegation_count=r["delegation_count"],
            total_delegated=to_human(int(r["total_delegated"] or 0)),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 4. POST /staking/stake
# ---------------------------------------------------------------------------

@router.post("/stake", summary="Stake tokens")
async def stake_tokens(
    body: StakeRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Stake tokens with a validator. Deducts from crypto holdings."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Verify validator exists and get its network
    val = await conn.fetchrow(
        "SELECT validator_id, network FROM validators WHERE validator_id = $1 AND guild_id = $2",
        body.validator_id, guild_id,
    )
    if not val:
        raise NotFoundError("Validator not found.")

    # Validate token is stakeable and matches validator's network
    from core.config import Config
    token_cfg = Config.TOKENS.get(body.symbol, {})
    if not token_cfg.get("stakeable"):
        raise ValidationError(f"{body.symbol} is not a stakeable token.")
    required_token = Config.NETWORK_STAKE_TOKEN.get(val["network"])
    if required_token and body.symbol != required_token:
        raise ValidationError(
            f"Cannot stake {body.symbol} on {val['network']} validators. Use {required_token}."
        )

    async with conn.transaction():
        # Verify sufficient balance before deducting
        bal = await conn.fetchrow(
            """SELECT amount FROM crypto_holdings
               WHERE user_id = $1 AND guild_id = $2 AND symbol = $3""",
            user_id, guild_id, body.symbol,
        )
        amount_raw = to_raw(body.amount)
        if not bal or int(bal["amount"]) < amount_raw:
            raise InsufficientBalanceError(f"Insufficient {body.symbol} balance.")

        # Deduct from crypto holdings (NUMERIC(36,0) raw)
        await conn.execute(
            """UPDATE crypto_holdings
               SET amount = amount - $1
               WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1""",
            amount_raw, user_id, guild_id, body.symbol,
        )

        # Add to stakes (NUMERIC(36,0) raw)
        await conn.execute(
            """INSERT INTO stakes (user_id, guild_id, validator_id, symbol, amount)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, validator_id, symbol)
               DO UPDATE SET amount = stakes.amount + $5, staked_at = now()""",
            user_id, guild_id, body.validator_id, body.symbol, amount_raw,
        )

    return {
        "success": True,
        "message": f"Staked {body.amount} {body.symbol} with validator {body.validator_id}.",
        "amount": body.amount,
        "symbol": body.symbol,
        "validator_id": body.validator_id,
    }


# ---------------------------------------------------------------------------
# 5. POST /staking/unstake
# ---------------------------------------------------------------------------

@router.post("/unstake", summary="Unstake tokens")
async def unstake_tokens(
    body: UnstakeRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Unstake tokens from a validator. Returns tokens to crypto holdings.
    Enforces 24h lockup and applies 5% early-unstake penalty within 48h."""
    import time as _time
    from core.config import Config

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    async with conn.transaction():
        # Fetch stake with timestamp
        stake_row = await conn.fetchrow(
            """SELECT amount, staked_at FROM stakes
               WHERE user_id = $1 AND guild_id = $2 AND validator_id = $3 AND symbol = $4""",
            user_id, guild_id, body.validator_id, body.symbol,
        )
        amount_raw = to_raw(body.amount)
        if not stake_row or int(stake_row["amount"]) < amount_raw:
            raise InsufficientBalanceError(f"Insufficient staked {body.symbol} with this validator.")

        # Enforce 24h lockup
        staked_ts = stake_row["staked_at"].timestamp() if stake_row["staked_at"] else 0
        now_ts = _time.time()
        if now_ts - staked_ts < STAKE_LOCK_SECS:
            remaining = int(STAKE_LOCK_SECS - (now_ts - staked_ts))
            hours, mins = remaining // 3600, (remaining % 3600) // 60
            raise ValidationError(
                f"Stake is locked for another {hours}h {mins}m. "
                f"Stakes unlock 24 hours after deposit."
            )

        # Apply early unstake penalty (5% burn within 48h window) -- in raw units
        penalty_raw = 0
        net_amount_raw = amount_raw
        if now_ts - staked_ts < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            penalty_raw = int(round(amount_raw * Config.STAKING_EARLY_UNSTAKE_PENALTY))
            net_amount_raw = amount_raw - penalty_raw

        # Deduct from stake (NUMERIC(36,0) raw)
        deducted = await conn.fetchrow(
            """UPDATE stakes
               SET amount = amount - $1
               WHERE user_id = $2 AND guild_id = $3 AND validator_id = $4 AND symbol = $5 AND amount >= $1
               RETURNING amount""",
            amount_raw, user_id, guild_id, body.validator_id, body.symbol,
        )
        if deducted is None:
            raise InsufficientBalanceError(f"Insufficient staked {body.symbol} with this validator.")

        # Return net amount to crypto holdings (penalty is burned; NUMERIC(36,0) raw)
        if net_amount_raw > 0:
            await conn.execute(
                """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (user_id, guild_id, symbol)
                   DO UPDATE SET amount = crypto_holdings.amount + $4""",
                user_id, guild_id, body.symbol, net_amount_raw,
            )

        # Clean up zero-balance stake rows
        await conn.execute(
            "DELETE FROM stakes WHERE user_id = $1 AND guild_id = $2 AND validator_id = $3 AND symbol = $4 AND amount <= 0",
            user_id, guild_id, body.validator_id, body.symbol,
        )

    penalty_human = to_human(penalty_raw)
    net_human = to_human(net_amount_raw)
    msg = f"Unstaked {body.amount} {body.symbol} from validator {body.validator_id}."
    if penalty_raw > 0:
        msg += f" Early unstake penalty: {penalty_human:.4f} {body.symbol} burned (5%)."

    return {
        "success": True,
        "message": msg,
        "amount": body.amount,
        "net_received": net_human,
        "penalty": penalty_human,
        "symbol": body.symbol,
        "validator_id": body.validator_id,
    }


# ---------------------------------------------------------------------------
# 6. GET /staking/my-stakes
# ---------------------------------------------------------------------------

@router.get("/my-stakes", response_model=list[StakeInfo], summary="My staked positions")
async def my_stakes(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's active staking positions."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await conn.fetch(
        """SELECT s.user_id, s.validator_id, s.symbol, s.amount, s.staked_at,
                  COALESCE(cp.price, 0) as price,
                  COALESCE(v.name, '') as validator_name,
                  COALESCE(v.reward_rate, 0) as reward_rate
           FROM stakes s
           LEFT JOIN crypto_prices cp ON cp.symbol = s.symbol AND cp.guild_id = s.guild_id
           LEFT JOIN validators v ON v.validator_id = s.validator_id AND v.guild_id = s.guild_id
           WHERE s.user_id = $1 AND s.guild_id = $2 AND s.amount > 0
           ORDER BY s.staked_at DESC""",
        user_id, guild_id,
    )

    return [
        StakeInfo(
            user_id=r["user_id"],
            validator_id=r["validator_id"],
            symbol=r["symbol"],
            amount=to_human(int(r["amount"] or 0)),
            value_usd=to_human(int(r["amount"] or 0)) * float(r["price"]),
            staked_at=r["staked_at"],
            validator_name=r["validator_name"],
            reward_rate=float(r["reward_rate"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 7. POST /staking/delegate
# ---------------------------------------------------------------------------

@router.post("/delegate", summary="Delegate to PoS validator")
async def delegate(
    body: DelegateRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Delegate tokens to a player-run PoS validator."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Verify validator exists and get stake token
    val = await conn.fetchrow(
        "SELECT stake_token FROM pos_validators WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND is_active = TRUE",
        body.validator_user_id, guild_id, body.network,
    )
    if not val:
        raise NotFoundError("Active PoS validator not found.")

    token = val["stake_token"]

    async with conn.transaction():
        # Verify sufficient balance before deducting
        bal = await conn.fetchrow(
            """SELECT amount FROM crypto_holdings
               WHERE user_id = $1 AND guild_id = $2 AND symbol = $3""",
            user_id, guild_id, token,
        )
        if not bal or to_human(int(bal["amount"] or 0)) < body.amount:
            raise InsufficientBalanceError(f"Insufficient {token} balance for delegation.")

        amount_raw = to_raw(body.amount)

        # Deduct from crypto holdings
        await conn.execute(
            """UPDATE crypto_holdings
               SET amount = amount - $1
               WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1""",
            amount_raw, user_id, guild_id, token,
        )

        # Create/update delegation
        await conn.execute(
            """INSERT INTO pos_delegations (delegator_id, validator_user_id, guild_id, network, token, amount)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (delegator_id, validator_user_id, guild_id, network)
               DO UPDATE SET amount = pos_delegations.amount + $6""",
            user_id, body.validator_user_id, guild_id, body.network, token, amount_raw,
        )

    return {
        "success": True,
        "message": f"Delegated {body.amount} {token} to validator {body.validator_user_id} on {body.network}.",
        "amount": body.amount,
        "token": token,
        "validator_user_id": body.validator_user_id,
        "network": body.network,
    }


# ---------------------------------------------------------------------------
# 8. POST /staking/undelegate
# ---------------------------------------------------------------------------

@router.post("/undelegate", summary="Undelegate from PoS validator")
async def undelegate(
    body: UndelegateRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Undelegate tokens from a player-run PoS validator. 24h lockup enforced."""
    import time as _time

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    async with conn.transaction():
        # Get delegation with timestamp
        row = await conn.fetchrow(
            """SELECT id, token, amount, delegated_at FROM pos_delegations
               WHERE delegator_id = $1 AND validator_user_id = $2 AND guild_id = $3 AND network = $4""",
            user_id, body.validator_user_id, guild_id, body.network,
        )
        if not row or to_human(int(row["amount"] or 0)) < body.amount:
            raise InsufficientBalanceError("Insufficient delegated amount.")

        # Enforce 24h lockup
        delegated_ts = row["delegated_at"].timestamp() if row.get("delegated_at") else 0
        now_ts = _time.time()
        if now_ts - delegated_ts < DELEGATION_LOCK_SECS:
            remaining = int(DELEGATION_LOCK_SECS - (now_ts - delegated_ts))
            hours, mins = remaining // 3600, (remaining % 3600) // 60
            raise ValidationError(
                f"Delegation is locked for another {hours}h {mins}m. "
                f"Delegations unlock 24 hours after deposit."
            )

        token = row["token"]
        undelegate_raw = to_raw(body.amount)

        # Reduce delegation
        await conn.execute(
            "UPDATE pos_delegations SET amount = amount - $1 WHERE id = $2",
            undelegate_raw, row["id"],
        )

        # Return to crypto holdings
        await conn.execute(
            """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, symbol)
               DO UPDATE SET amount = crypto_holdings.amount + $4""",
            user_id, guild_id, token, undelegate_raw,
        )

        # Clean up zero-balance delegations
        await conn.execute(
            "DELETE FROM pos_delegations WHERE id = $1 AND amount <= 0",
            row["id"],
        )

    return {
        "success": True,
        "message": f"Undelegated {body.amount} {token} from validator {body.validator_user_id} on {body.network}.",
        "amount": body.amount,
        "token": token,
    }


# ---------------------------------------------------------------------------
# 9. GET /staking/my-delegations
# ---------------------------------------------------------------------------

@router.get("/my-delegations", response_model=list[DelegationInfo], summary="My delegations")
async def my_delegations(
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's active delegations."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await conn.fetch(
        """SELECT id, delegator_id, validator_user_id, network, token, amount, total_earned, locked_until, delegated_at
           FROM pos_delegations
           WHERE delegator_id = $1 AND guild_id = $2 AND amount > 0
           ORDER BY delegated_at DESC""",
        user_id, guild_id,
    )

    return [
        DelegationInfo(
            id=r["id"],
            delegator_id=r["delegator_id"],
            validator_user_id=r["validator_user_id"],
            network=r["network"],
            token=r["token"],
            amount=to_human(int(r["amount"] or 0)),
            total_earned=to_human(int(r["total_earned"] or 0)),
            locked_until=r["locked_until"],
            delegated_at=r["delegated_at"],
        )
        for r in rows
    ]
