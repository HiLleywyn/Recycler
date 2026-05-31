"""Stake service layer  -  shared by Discord commands and web API.

Encapsulates all stake/unstake logic for PoS validators: validation,
lock-period enforcement, early-unstake penalties, and execution.
No Discord dependencies.

The DB stores stake amounts, holdings, and wallet balances as raw
``NUMERIC(36,0)`` scaled by ``10**18``. The service layer accepts
human-scale ``float`` amounts from callers and converts via ``to_raw``
at the DB boundary, matching the pattern already used by ``cogs/stake.py``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.config import Config
from core.framework.scale import to_human, to_raw

# ── Constants ────────────────────────────────────────────────────────────────
from constants.validators import NET_SHORT
from constants.economy import STAKE_LOCK_PERIOD as LOCK_PERIOD


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class StakeResult:
    success: bool
    tx_hash: str = ""
    amount: float = 0.0
    validator_name: str = ""
    symbol: str = ""
    error: str = ""


@dataclass
class UnstakeResult:
    success: bool
    tx_hash: str = ""
    amount_unstaked: float = 0.0
    amount_received: float = 0.0
    penalty: float = 0.0
    symbol: str = ""
    error: str = ""


# ── Core functions ───────────────────────────────────────────────────────────

async def execute_stake(
    db,
    guild_id: int,
    user_id: int,
    validator_id: str,
    amount: float,
) -> StakeResult:
    """Stake tokens on a validator. ``amount`` is human-scale."""
    try:
        if amount <= 0:
            return StakeResult(success=False, error="Amount must be positive.")

        # 1. Resolve validator
        validator = await db.get_validator(validator_id, guild_id)
        if not validator:
            return StakeResult(success=False, error=f"Unknown validator: {validator_id}")

        network = validator["network"]
        if not network:
            return StakeResult(success=False, error="Validator has no network assigned.")

        symbol = await db.get_network_stake_token(guild_id, network)
        if not symbol:
            return StakeResult(
                success=False,
                error=f"No stake token configured for network {network}.",
            )

        net_short = NET_SHORT.get(network, "")
        if not net_short:
            return StakeResult(success=False, error=f"Unknown network: {network}")

        # 2. Check DeFi wallet exists
        has_wallet = await db.has_defi_wallet(user_id, guild_id, net_short)
        if not has_wallet:
            return StakeResult(
                success=False,
                error=f"You need a DeFi wallet on the {network} to stake.",
            )

        # 3. Get holding (raw int column)
        holding = await db.get_wallet_holding(user_id, guild_id, net_short, symbol)
        balance_raw = int(holding["amount"]) if holding else 0

        # 4. Validate amount in raw int space
        amount_raw = to_raw(amount)
        if amount_raw > balance_raw:
            return StakeResult(
                success=False,
                error=(
                    f"Insufficient {symbol}. "
                    f"Have {to_human(balance_raw):,.6f}, need {amount:,.6f}."
                ),
            )

        # 5. Gas  -  handled by validator blocks, no gas charged for staking
        gas_fee = 0
        gas_coin = ""

        # 6. Execute atomically: debit holding, create/update stake, log tx
        async with db.atomic():
            await db.update_wallet_holding(user_id, guild_id, net_short, symbol, -amount_raw)
            await db.update_stake(user_id, guild_id, validator_id, symbol, amount_raw)
            # Record this deposit as its own lock batch so top-ups don't reset
            # the existing countdown  -  each batch has its own 24h timer.
            await db.insert_stake_batch(user_id, guild_id, validator_id, symbol, amount_raw)

            # 7. Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "STAKE",
                symbol_in=symbol, amount_in=amount_raw,
                symbol_out=validator_id, amount_out=amount_raw,
                network=net_short,
                gas_fee=gas_fee,
                gas_coin=gas_coin,
            )

        return StakeResult(
            success=True,
            tx_hash=tx_hash,
            amount=amount,
            validator_name=validator["name"],
            symbol=symbol,
        )

    except Exception as e:
        return StakeResult(success=False, error=str(e))


async def execute_unstake(
    db,
    guild_id: int,
    user_id: int,
    validator_id: str,
    amount: float,
) -> UnstakeResult:
    """Unstake tokens from a validator. ``amount`` is human-scale."""
    try:
        if amount <= 0:
            return UnstakeResult(success=False, error="Amount must be positive.")

        # 1. Resolve validator and symbol
        validator = await db.get_validator(validator_id, guild_id)
        if not validator:
            return UnstakeResult(success=False, error=f"Unknown validator: {validator_id}")

        network = validator["network"]
        if not network:
            return UnstakeResult(success=False, error="Validator has no network assigned.")

        symbol = await db.get_network_stake_token(guild_id, network)
        if not symbol:
            return UnstakeResult(
                success=False,
                error=f"No stake token configured for network {network}.",
            )

        net_short = NET_SHORT.get(network, "")
        if not net_short:
            return UnstakeResult(success=False, error=f"Unknown network: {network}")

        # 2. Get user's stake for this validator (stakes.amount is raw int)
        stakes = await db.get_user_stakes(user_id, guild_id)
        stake_entry = None
        for s in stakes:
            if s["validator_id"] == validator_id:
                stake_entry = s
                break

        if not stake_entry:
            return UnstakeResult(
                success=False,
                error=f"You have no stake on validator {validator['name']}.",
            )

        staked_amount_raw = int(stake_entry["amount"])

        # 3. Validate amount in raw space.  Clamp a tiny human overshoot (caused
        # by float display rounding when the user types "all") to the exact
        # staked amount so the early-unstake flow still works.
        amount_raw = to_raw(amount)
        overshoot_raw = amount_raw - staked_amount_raw
        if overshoot_raw > 0:
            if to_human(overshoot_raw) > 0.01:
                return UnstakeResult(
                    success=False,
                    error=(
                        f"Insufficient stake. "
                        f"Have {to_human(staked_amount_raw):,.6f}, requested {amount:,.6f}."
                    ),
                )
            amount_raw = staked_amount_raw
            amount = to_human(staked_amount_raw)

        # 4. Check lock period  -  read-only scan of batches, no DB writes yet
        now = time.time()
        batches = await db.get_stake_batches(user_id, guild_id, validator_id)

        _sa_entry = stake_entry.get("staked_at")
        entry_staked_at = _sa_entry.timestamp() if hasattr(_sa_entry, "timestamp") else (_sa_entry or 0.0)

        if batches:
            def _ts(b: dict) -> float:
                s = b.get("staked_at")
                return s.timestamp() if hasattr(s, "timestamp") else float(s or 0)

            unlocked_raw = sum(int(b["amount"]) for b in batches if _ts(b) + LOCK_PERIOD <= now)
            batch_total_raw = sum(int(b["amount"]) for b in batches)
            # Stakes not tracked in batches (auto-compounded rewards) are freely unlocked
            unlocked_raw += max(0, staked_amount_raw - batch_total_raw)

            if amount_raw > unlocked_raw:
                # Allow a tiny (1 cent) human-scale overshoot to match the
                # previous float-epsilon tolerance.
                if to_human(amount_raw - unlocked_raw) > 0.01:
                    still_locked = [b for b in batches if _ts(b) + LOCK_PERIOD > now]
                    if still_locked:
                        soonest = min(_ts(b) for b in still_locked)
                        secs_left = int(soonest + LOCK_PERIOD - now)
                        hours, rem = divmod(max(secs_left, 0), 3600)
                        minutes, secs = divmod(rem, 60)
                        locked_total_raw = sum(int(b["amount"]) for b in still_locked)
                        msg = (
                            f"Only {to_human(unlocked_raw):,.6f} {symbol} is unlocked right now "
                            f"({to_human(locked_total_raw):,.6f} locked, next batch unlocks in "
                            f"{hours}h {minutes}m {secs}s)."
                        )
                    else:
                        msg = "Stake is locked  -  no unlocked batches available."
                    return UnstakeResult(success=False, error=msg)

            # Earliest staked_at for early-penalty window
            earliest_staked_at = min((_ts(b) for b in batches), default=entry_staked_at)
        else:
            # No batches  -  pre-migration stake; no lock enforcement (backward compat)
            earliest_staked_at = entry_staked_at

        # 5. Early unstake penalty (integer math in raw space)
        penalty_raw = 0
        net_received_raw = amount_raw
        if earliest_staked_at > 0 and (now - earliest_staked_at) < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            # Config.STAKING_EARLY_UNSTAKE_PENALTY is a float fraction (e.g. 0.10).
            # Stay in int space by multiplying through a SCALE-based fraction.
            from core.framework.scale import SCALE
            pen_num = int(Config.STAKING_EARLY_UNSTAKE_PENALTY * SCALE)
            penalty_raw = amount_raw * pen_num // SCALE
            net_received_raw = amount_raw - penalty_raw

        # 6. Execute atomically: consume batches + reduce stake + credit holding + log tx
        async with db.atomic():
            # Batch consumption inside the transaction so it rolls back on failure
            await db.consume_stake_batches(user_id, guild_id, validator_id, amount_raw, LOCK_PERIOD)
            await db.update_stake(user_id, guild_id, validator_id, symbol, -amount_raw)
            await db.update_wallet_holding(user_id, guild_id, net_short, symbol, net_received_raw)

            # 7. Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "UNSTAKE",
                symbol_in=validator_id, amount_in=amount_raw,
                symbol_out=symbol, amount_out=net_received_raw,
                network=net_short,
            )

        return UnstakeResult(
            success=True,
            tx_hash=tx_hash,
            amount_unstaked=amount,
            amount_received=to_human(net_received_raw),
            penalty=to_human(penalty_raw),
            symbol=symbol,
        )

    except Exception as e:
        return UnstakeResult(success=False, error=str(e))
