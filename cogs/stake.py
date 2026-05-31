"""
cogs/stake.py  -  Unified Staking & Validator System

Merges the former staking.py (yield-farm staking) and validators.py
(player-driven PoS validator system) under a single `/stake` command group.

Subcommand layout:
  /stake               -  show help / overview
  /stake farm          -  deposit into yield farm
  /stake unstake       -  withdraw from yield farm
  /stake list          -  list yield farms
  /stake mine          -  show your active deposits
  /stake validator …   -  all validator subcommands

Architecture (validator subsystem):
  - Players register as validators by staking the network's stake token
  - Every VALIDATOR_TICK seconds, one validator per network is selected (weighted by stake)
  - Selected validator processes all pending mempool actions into a validator_block
  - Gas fees are split: 10% to validator, 90% to LP/treasury (guild wallet)
  - Invalid actions are rejected; bad-faith validators can be slashed

Arcadia analogy:
  - VALIDATOR_TICK = slot time (~120s)
  - Mempool = pending tx pool
  - validator_block = execution block
  - Gas fee = priority fee paid to validator
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
log = logging.getLogger(__name__)

import discord
from discord.ext import commands, tasks

from core.config import Config
from cogs.shop import _item_stat, _lockstone_stat, notify_item_levelup_ready, cap_xp
from core.framework.ui import send_paginated
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from core.framework.cooldowns import user_cooldown
from core.framework.utils import parse_sym_amt, parse_amount
from core.framework.tx import set_tx
from core.framework import whale as _whale
from core.framework.heartbeat import pulse, register_interval
from core.framework.ui import (
    C_ERROR, C_INFO, C_NEUTRAL, C_PURPLE, C_AMBER, C_SUCCESS, C_TEAL, C_WARNING,
    ConfirmView, CategoryPaginator, fmt_bonus, fmt_gas, fmt_rel, fmt_token, fmt_usd, fmt_pct,
    FormatKit, mention,
)
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.scale import SCALE, to_raw, to_human
from services.safety_module import (
    apply_sm_yield,
    begin_unstake as sm_begin_unstake,
    cooldown_remaining as sm_cooldown_remaining,
    sm_current_apy_pct,
    sm_current_daily_rate,
    stake_sm,
    withdraw_sm,
)

# ── Constants ────────────────────────────────────────────────────────────────
from constants.validators import (
    VALIDATOR_TICK, VALIDATOR_REWARD, TREASURY_CUT, MIN_STAKE, MIN_VALIDATORS,
    STAKE_LOCK_SECS, MAX_SLASH_COUNT, MAX_MEMPOOL, DELEGATION_VALIDATOR_KEEP, DELEGATION_LOCK_SECS,
    MIN_DELEGATION, REJECTION_SLASH_RATE, GAS_TIERS,
    GAS_MIN_MULT as _GAS_MIN_MULT, GAS_MAX_MULT as _GAS_MAX_MULT, NET_SHORT,
)

# Local alias kept for backward compatibility (cog-internal usage)
_NET_SHORT = NET_SHORT

# Canonical alias normalization lives in :mod:`core.framework.network`.
from core.framework.network import normalize_full as _fw_normalize_full


def _normalize_network(name: str) -> str:
    """Map any user-supplied network alias to its canonical long-form name.

    Returns the input unchanged if it is not a recognized network alias, so
    callers can still fall through to their own error paths.
    """
    return _fw_normalize_full(name) or name


def _slash_loss_rate_per_tick(validator: dict) -> float:
    """Effective stake-loss fraction for one hourly slash tick."""
    divisor = max(float(Config.STAKING_SLASH_TICK_DIVISOR), 1e-9)
    return float(validator.get("slash_rate", 0.0)) / divisor


def _stake_reward_per_day(amount: float, reward_rate: float) -> float:
    """Configured daily staking reward before warmup, slashing, and bonus items/jobs."""
    divisor = max(float(Config.STAKING_REWARD_DIVISOR), 1e-9)
    return float(amount) * float(reward_rate) / divisor


# Validator-wide tick events. Rolled once per validator per hour so every
# staker on the same validator shares the same roll -- mirrors how real
# MEV / slot-lottery payouts correlate across delegators. HOT doubles the
# tick reward; COLD cuts it to 40%. Total variance is small (90% of ticks
# pay the normal rate) but visible enough that stakers have something to
# chase / cope with, analogous to the existing per-tick slash risk.
# Safety Module config (VTR/DSY single-token yield staking).
_SM = Config.SAFETY_MODULE


_STAKE_HOT_CHANCE:  float = 0.05
_STAKE_COLD_CHANCE: float = 0.05
_STAKE_HOT_MULT:    float = 2.00
_STAKE_COLD_MULT:   float = 0.40

# Persistent validator heat. Stored on validators.heat (NUMERIC(6,4), range
# -1..1). Each tick we first decay toward 0 by (1 - _HEAT_DECAY), then add
# a delta for HOT/COLD events. The final reward is multiplied by
# (1 + heat * _HEAT_REWARD_TILT), so a validator on a hot streak tilts
# rewards up by up to +15% per tick and a cold streak tilts them down by
# the same -- giving stakers a reason to watch and chase (or bail).
_HEAT_DECAY:        float = 0.92   # heat *= this every tick; ~10h half-life
_HEAT_DELTA_HOT:    float = 0.20
_HEAT_DELTA_COLD:   float = -0.20
_HEAT_REWARD_TILT:  float = 0.15   # max +/-15% APR swing from heat extremes


def _roll_validator_event() -> tuple[float, str | None]:
    """Roll one HOT/COLD/normal event for a validator's next reward tick.

    Returns ``(multiplier, tag)`` where ``tag`` is ``"HOT"`` / ``"COLD"``
    for events worth surfacing to the user, or ``None`` for a normal tick.
    """
    r = random.random()
    if r < _STAKE_HOT_CHANCE:
        return _STAKE_HOT_MULT, "HOT"
    if r < _STAKE_HOT_CHANCE + _STAKE_COLD_CHANCE:
        return _STAKE_COLD_MULT, "COLD"
    return 1.0, None


def _advance_heat(current: float, event_tag: str | None) -> float:
    """Compute next heat value: decay then apply event delta, clamped to [-1, 1]."""
    h = float(current or 0.0) * _HEAT_DECAY
    if event_tag == "HOT":
        h += _HEAT_DELTA_HOT
    elif event_tag == "COLD":
        h += _HEAT_DELTA_COLD
    return max(-1.0, min(1.0, h))


def _format_heat(heat: float) -> str:
    """Compact heat display: emoji + bar + signed numeric value."""
    h = max(-1.0, min(1.0, float(heat or 0.0)))
    # Map heat in [-1, 1] onto a 5-cell bar centered on 0.
    cells = 5
    idx = int(round((h + 1.0) * (cells - 1) / 2.0))
    bar_cells = ["□"] * cells
    bar_cells[idx] = "■"
    bar = "".join(bar_cells)
    if   h >=  0.55: emoji = "🔥"
    elif h >=  0.20: emoji = "♨️"
    elif h <= -0.55: emoji = "🧊"
    elif h <= -0.20: emoji = "❄️"
    else:            emoji = "➖"
    return f"{emoji} `{bar}`  **{h:+.2f}**"


async def _hashrate_imbalance_bonus(db, guild_id: int, validator_network: str) -> float:
    """Return a bonus multiplier [0, IMBALANCE_BONUS_MAX] when the PoW network
    associated with this validator is underrepresented vs its peer.

    Sun Network validators → bonus when SUN mining hashrate < MTA × threshold
    ARC / DSC validators   → bonus when MTA hashrate < SUN × threshold

    Bonus scales linearly: full bonus when peer has 0 miners, 0 bonus at threshold.
    """
    try:
        sun_net = await db.get_pow_network(guild_id, "SUN")
        btc_net = await db.get_pow_network(guild_id, "MTA")
        sun_hr = float((sun_net or {}).get("total_hashrate", 0))
        btc_hr = float((btc_net or {}).get("total_hashrate", 0))
    except Exception:
        return 0.0

    if sun_hr <= 0 and btc_hr <= 0:
        return 0.0

    max_bonus = float(Config.STAKING_IMBALANCE_BONUS_MAX)
    threshold = float(Config.STAKING_IMBALANCE_THRESHOLD)

    if validator_network == "Sun Network":
        # SUN validators: bonus when SUN mining is thin vs MTA
        if btc_hr <= 0:
            return 0.0
        ratio = sun_hr / btc_hr
        if ratio >= threshold:
            return 0.0
        return max_bonus * (1.0 - ratio / threshold)
    else:
        # ARC / DSC validators: bonus when MTA mining is thin vs SUN
        if sun_hr <= 0:
            return 0.0
        ratio = btc_hr / sun_hr
        if ratio >= threshold:
            return 0.0
        return max_bonus * (1.0 - ratio / threshold)

# Gas units consumed per action type (dimensionless, like Arcadia gas units)
GAS_UNITS = {
    "send":             21_000,
    "swap":            100_000,
    "buy":              21_000,
    "sell":             21_000,
    "stake":            50_000,
    "unstake":          50_000,
    "addlp":           150_000,
    "removelp":        150_000,
    "contract_deploy": 500_000,
    "contract_call":   200_000,
    "default":          30_000,
}

# Initial base gas price per gas unit for each network (in network's native coin)
# Scales like gwei for ARC, uDSC for Discoin, etc.
# These adjust dynamically after each block (EIP-1559).
INITIAL_BASE_GAS: dict[str, float] = {
    "Sun Network":       1e-7,    # SUN-denominated, moderate
    "Moneta Chain":   1e-8,    # satoshi-scale, PoW
    "Arcadia Network":  1e-9,    # ~1 gwei per gas unit, realistic ARC scale
    "Discoin Network":   1e-6,    # DSC gas, PoS
}

# ── Module-level helpers (importable by other cogs) ──────────────────────────

def _gas_priority(tier: str) -> float:
    """Return priority tip multiplier for sorting (higher = more priority)."""
    return GAS_TIERS.get(tier, GAS_TIERS["medium"])


def gas_coin_for_network(network: str) -> str:
    """Return the native gas coin symbol for a network."""
    return Config.NETWORK_COINS.get(network, "SUN")


async def gas_fee_for_network(
    db,
    guild_id: int,
    action_type: str,
    gas_price: str,
    network: str,
) -> tuple[str, float]:
    """
    Calculate gas fee in the network's native coin.

    Returns (gas_coin_symbol, fee_amount).

    Formula (EIP-1559-style):
        priority_fee = base_fee_per_unit * tip_multiplier
        total_fee    = (base_fee_per_unit + priority_fee) * gas_units
                     = base_fee_per_unit * (1 + tip_multiplier) * gas_units
    """
    units     = GAS_UNITS.get(action_type, GAS_UNITS["default"])
    tip_mult  = GAS_TIERS.get(gas_price, GAS_TIERS["medium"])
    base_fee  = await db.get_base_fee(guild_id, network)
    fee = base_fee * (1.0 + tip_mult) * units
    coin = gas_coin_for_network(network)
    return coin, round(fee, 10)


def adjust_base_fee(current: float, confirmed: int, capacity: int, network: str) -> float:
    """
    EIP-1559 base fee adjustment.
    If block > 50% full → increase 12.5%; if < 50% → decrease 12.5%.
    Clamped to [initial * 0.1, initial * 100].
    """
    target = capacity / 2
    if confirmed > target:
        new = current * 1.125
    else:
        new = current * 0.875
    initial = INITIAL_BASE_GAS.get(network, INITIAL_BASE_GAS["Sun Network"])
    return max(initial * _GAS_MIN_MULT, min(initial * _GAS_MAX_MULT, new))


def _fmt_fee(v: float) -> str:
    """Format a small float without exponential notation."""
    return f"{v:.10f}".rstrip("0").rstrip(".")


def _select_validator(
    validators: list[dict],
    last_validator_id: str | None,
    delegated_totals: dict[int, float] | None = None,
) -> dict | None:
    """
    Weighted random selection by effective stake (own stake + delegated stake).
    Penalises the last-selected validator to avoid back-to-back selection.
    Mirrors Arcadia's proposer selection via VRF weighting.
    """
    if not validators:
        return None

    delegated_totals = delegated_totals or {}
    weights = []
    for v in validators:
        effective = float(v["stake_amount"]) + delegated_totals.get(v["user_id"], 0.0)
        w = effective
        if v["user_id"] == last_validator_id:
            w *= 0.1   # heavy back-to-back penalty
        weights.append(max(w, 0.001))

    total = sum(weights)
    probs = [w / total for w in weights]
    return random.choices(validators, weights=probs, k=1)[0]


# ── Cog ──────────────────────────────────────────────────────────────────────

class Stake(commands.Cog):
    """Unified staking & validator system."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # Validator subsystem state
        self._last_validator: dict[tuple, str] = {}
        # Start background tasks
        self.staking_tick.start()
        self.validator_tick.start()
        register_interval("staking_tick", 3600)
        register_interval("validator_tick", VALIDATOR_TICK)

    def cog_unload(self) -> None:
        self.staking_tick.cancel()
        self.validator_tick.cancel()

    async def cog_check(self, ctx) -> bool:
        if ctx.guild:
            staking_ok = await module_allowed(ctx, "staking")
            validators_ok = await module_allowed(ctx, "validators")
            if not staking_ok and not validators_ok:
                raise commands.CheckFailure("The **staking** module is disabled on this server.")
        return True

    # ══════════════════════════════════════════════════════════════════════════
    # BACKGROUND TASK: Staking tick (hourly yield farm rewards)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(hours=1)
    async def staking_tick(self) -> None:
        """Every hour: distribute rewards or slash stakers per validator uptime."""
        for guild in self.bot.guilds:
            if not (await self.bot.db.module_enabled(guild.id, "staking") or
                    await self.bot.db.module_enabled(guild.id, "validators")):
                continue
            try:
                await self._staking_tick_guild(guild)
            except Exception as e:
                log.error("[staking_tick] Error processing guild %s: %s", guild.id, e, exc_info=True)
        pulse("staking_tick")

    async def _staking_tick_guild(self, guild: discord.Guild) -> None:
        """Process hourly staking rewards/slashing for a single guild."""
        _gs = await self.bot.db.get_guild_settings(guild.id)
        _staking_mult = float(_gs.get("staking_multiplier") or 1.0)

        validators = await self.bot.db.get_validators(guild.id)
        if not validators:
            await self.bot.db.seed_validators(guild.id)
            validators = await self.bot.db.get_validators(guild.id)

        # Accumulate auto-compound events per user so we send one batched DM per tick
        _pending_ac_dms: dict[int, list[dict]] = {}

        for v in validators:
            vid = v["validator_id"]
            stakes = await self.bot.db.get_stakes_for_validator(vid, guild.id)
            if not stakes:
                continue

            # Determine the accepted stake token for this validator's network
            required_token = await self.bot.db.get_network_stake_token(guild.id, v.get("network", ""))
            if not required_token:
                # No network configured  -  skip; rewarding arbitrary tokens would be incorrect
                continue
            uptime_ok = random.random() < v["uptime_rate"]
            # Roll a validator-wide tick event (HOT/COLD/normal). Shared by
            # every staker on this validator this hour -- see the module-level
            # _STAKE_HOT_CHANCE / _STAKE_COLD_CHANCE constants for tuning.
            event_mult, event_tag = _roll_validator_event()
            # Advance persistent heat from the pre-tick value, then fold it
            # into rewards for every staker this tick. Persisted at the end
            # of the validator loop so concurrent readers see consistent state.
            heat_before = float(v.get("heat") or 0.0)
            heat_after  = _advance_heat(heat_before, event_tag)
            heat_tilt   = 1.0 + heat_after * _HEAT_REWARD_TILT
            if event_tag:
                log.info(
                    "stake.tick: validator=%s network=%s event=%s mult=%.2f heat=%.3f->%.3f",
                    vid, v.get("network", ""), event_tag, event_mult, heat_before, heat_after,
                )
            slashed_victims = []
            total_rewarded = 0
            rewarded_count = 0

            for stake in stakes:
                if stake["amount"] <= 0:
                    continue
                # Only process stakes in the correct token
                if stake["symbol"] != required_token:
                    continue
                if uptime_ok:
                    # Hourly reward = daily_rate / 24, with stake_bonus perk + item bonus.
                    # Compute the full multiplier as a Decimal so the raw stake
                    # amount stays in int space across the chain of small-float
                    # bonuses (fractional rates like 0.001 don't round-trip
                    # cleanly through IEEE-754 multiplications).
                    job = await self.bot.db.get_user_job(stake["user_id"], guild.id)
                    job_cfg = Config.JOBS.get(job["job_id"], Config.JOBS["HOMELESS"])
                    stake_bonus = job_cfg.get("perks", {}).get("stake_bonus", 0.0)
                    hashstone = await self.bot.db.get_hashstone(stake["user_id"], guild.id)
                    stake_bonus += _item_stat(hashstone, "stake_bonus")
                    lockstone = await self.bot.db.get_lockstone(stake["user_id"], guild.id)
                    stake_bonus += _lockstone_stat(lockstone, "stake_bonus")
                    imbalance_bonus = await _hashrate_imbalance_bonus(self.bot.db, guild.id, v.get("network", ""))

                    from decimal import Decimal as _D
                    stake_raw = int(stake["amount"])
                    reward_factor = (
                        _D(str(v["reward_rate"]))
                        / _D(str(Config.STAKING_REWARD_DIVISOR))
                        / _D(24)
                        * (_D(1) + _D(str(stake_bonus)))
                        * (_D(1) + _D(str(imbalance_bonus)))
                        * _D(str(_staking_mult))
                        * _D(str(event_mult))
                        * _D(str(heat_tilt))
                    )
                    reward_raw = int(_D(stake_raw) * reward_factor)

                    # Warmup: rewards ramp linearly over STAKING_WARMUP_SECONDS
                    _sa = stake.get("staked_at")
                    staked_at = _sa.timestamp() if hasattr(_sa, 'timestamp') else (_sa or 0.0)
                    if staked_at > 0 and Config.STAKING_WARMUP_SECONDS > 0:
                        time_staked = time.time() - staked_at
                        warmup_factor = min(1.0, time_staked / Config.STAKING_WARMUP_SECONDS)
                        reward_raw = int(_D(reward_raw) * _D(str(warmup_factor)))

                    reward_h = to_human(reward_raw)

                    # Cap reward to remaining supply headroom so staking
                    # cannot inflate a token beyond its hard max_supply.
                    token_cfg = Config.TOKENS.get(stake["symbol"], {})
                    max_sup = token_cfg.get("max_supply")
                    if max_sup is None:
                        # Custom token  -  query guild_tokens for its cap
                        _gt_row = await self.bot.db.fetch_one(
                            "SELECT max_supply, circulating_supply FROM guild_tokens "
                            "WHERE guild_id=$1 AND symbol=$2",
                            guild.id, stake["symbol"],
                        )
                        if _gt_row:
                            max_sup_h = _gt_row.h("max_supply") if _gt_row["max_supply"] else None
                            _circ_h = _gt_row.h("circulating_supply")
                        else:
                            max_sup_h = None
                            _circ_h = 0.0
                    else:
                        _cp_row = await self.bot.db.get_price(stake["symbol"], guild.id)
                        _circ_h = _cp_row.h("circulating_supply") if _cp_row else 0.0
                        max_sup_h = float(max_sup)  # Config.TOKENS max_supply is already human-scale

                    if max_sup_h is not None:
                        headroom_raw = max(0, to_raw(max_sup_h) - to_raw(_circ_h))
                        if reward_raw > headroom_raw:
                            reward_raw = headroom_raw
                        if reward_raw <= 0:
                            continue  # supply cap reached  -  skip this tick

                    reward_h = to_human(reward_raw)
                    net_prefix = _NET_SHORT.get(v.get("network", ""), "")
                    # Credit the reward and bump session/total_earned atomically so
                    # a DB failure between the two writes cannot leave the ledger
                    # inconsistent (tokens credited without tracking, or vice versa).
                    async with self.bot.db.atomic():
                        if net_prefix:
                            await self.bot.db.update_wallet_holding(
                                stake["user_id"], guild.id, net_prefix, stake["symbol"], reward_raw
                            )
                        else:
                            await self.bot.db.update_holding(
                                stake["user_id"], guild.id, stake["symbol"], reward_raw
                            )
                        # Track earned on this position (session resets on full exit, total is lifetime)
                        # session_earned / total_earned are NUMERIC(28,8) - human scale, not raw
                        await self.bot.db.execute(
                            "UPDATE stakes SET session_earned = session_earned + $1, total_earned = total_earned + $1 "
                            "WHERE user_id=$2 AND guild_id=$3 AND validator_id=$4 AND symbol=$5",
                            reward_h, stake["user_id"], guild.id, vid, stake["symbol"],
                        )

                    total_rewarded += reward_raw
                    rewarded_count += 1

                    # Auto-compound: restake reward if user has it enabled (beta feature)
                    try:
                        ac_row = await self.bot.db.fetch_one(
                            "SELECT enabled FROM auto_compound_settings "
                            "WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND symbol=$4 AND enabled=TRUE",
                            stake["user_id"], guild.id, vid, stake["symbol"],
                        )
                        if ac_row:
                            # Atomically move reward from wallet back into stake
                            # Use direct SQL to avoid update_stake resetting staked_at
                            async with self.bot.db.atomic():
                                if net_prefix:
                                    await self.bot.db.update_wallet_holding(
                                        stake["user_id"], guild.id, net_prefix, stake["symbol"], -reward_raw
                                    )
                                else:
                                    await self.bot.db.update_holding(
                                        stake["user_id"], guild.id, stake["symbol"], -reward_raw
                                    )
                                # Increment stake amount WITHOUT resetting staked_at
                                # Also update circulating_supply (same as update_stake does for +delta)
                                await self.bot.db.execute(
                                    "UPDATE stakes SET amount = amount + $1 "
                                    "WHERE user_id=$2 AND guild_id=$3 AND validator_id=$4 AND symbol=$5",
                                    reward_raw, stake["user_id"], guild.id, vid, stake["symbol"],
                                )
                                await self.bot.db.execute(
                                    "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                                    "WHERE guild_id = $2 AND symbol = $3",
                                    reward_raw, guild.id, stake["symbol"],
                                )
                                await self.bot.db.execute(
                                    "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                                    "WHERE guild_id = $2 AND symbol = $3",
                                    reward_raw, guild.id, stake["symbol"],
                                )
                                # Track compound amount for stats
                                await self.bot.db.execute(
                                    "UPDATE auto_compound_settings SET total_compounded = COALESCE(total_compounded, 0) + $1, "
                                    "last_compound_at = now(), compound_count = COALESCE(compound_count, 0) + 1 "
                                    "WHERE user_id=$2 AND guild_id=$3 AND validator_id=$4 AND symbol=$5",
                                    reward_raw, stake["user_id"], guild.id, vid, stake["symbol"],
                                )
                            # Queue DM notification (batched per user, sent after validators loop)
                            _pending_ac_dms.setdefault(stake["user_id"], []).append({
                                "vid": vid,
                                "sym": stake["symbol"],
                                "amount_h": reward_h,
                            })
                    except Exception:
                        pass  # non-critical  -  don't break reward flow
                    # Lockstone XP: grant per staking reward tick
                    from core.config import Config as _Cfg
                    _LS_CFG = _Cfg.SHOP_ITEMS.get("lockstone", {})
                    if lockstone and lockstone["level"] < _LS_CFG.get("max_level", 50) and stake["amount"] > 0:
                        base_xp = _LS_CFG.get("xp_per_stake_reward", 10.0)
                        # Proportional XP scaling based on stake USD value  -  no minimum floor
                        _tp_row = await self.bot.db.get_price(stake["symbol"], guild.id)
                        _tp = float(_tp_row["price"]) if _tp_row else 0.0
                        _stake_usd = to_human(stake["amount"]) * _tp
                        if _stake_usd > 0:
                            xp_scale = min(_Cfg.XP_SCALE_MAX, _stake_usd / _Cfg.XP_STAKE_REFERENCE_USD)
                            xp_gain = base_xp * xp_scale
                            xp_result = await self.bot.db.add_lockstone_xp(stake["user_id"], guild.id, xp_gain)
                            if xp_result:
                                live_xp, live_level = xp_result
                                capped_xp = cap_xp(live_xp, live_level, _LS_CFG)
                                if capped_xp < live_xp:
                                    await self.bot.db.update_lockstone_xp(stake["user_id"], guild.id, capped_xp, live_level)
                                await notify_item_levelup_ready(self.bot, stake["user_id"], guild, "lockstone", live_xp - xp_gain, live_xp, live_level, lockstone["staked_amount"])
                else:
                    # Slash  -  clamp to available amount to avoid exceptions.
                    # Compute the loss in raw int space by routing the float
                    # rate through a SCALE-based fraction, so an existing
                    # precision-sensitive stake doesn't round-trip through a
                    # float representation that loses the bottom raw digits.
                    slash_loss_rate = _slash_loss_rate_per_tick(v)
                    stake_amt = int(stake["amount"])
                    _rate_num = int(slash_loss_rate * SCALE)
                    loss = stake_amt * _rate_num // SCALE
                    actual_loss = min(loss, stake_amt)
                    if actual_loss <= 0:
                        log.warning(
                            "Slash skipped for validator %s (guild %s): "
                            "slash_rate=%.4f stake=%.4f  -  check Config.VALIDATORS",
                            vid, guild.id, v.get("slash_rate", 0), stake["amount"],
                        )
                        continue
                    # Check for Yield Guard  -  absorbs one slash event
                    _yg_used = await self.bot.db.use_yield_guard(stake["user_id"], guild.id)
                    if _yg_used:
                        await self.bot.bus.publish(
                            "yield_guard_used",
                            guild=guild,
                            user_id=stake["user_id"],
                            ltv=0.0,
                        )
                        continue
                    await self.bot.db.update_stake(
                        stake["user_id"], guild.id, vid, stake["symbol"], -actual_loss
                    )
                    slashed_victims.append({
                        "user_id": stake["user_id"],
                        "loss": actual_loss,
                        "amount": stake["amount"],
                    })

            if slashed_victims:
                await self.bot.bus.publish(
                    "validator_slashed",
                    guild=guild,
                    validator_id=vid,
                    victims=slashed_victims,
                    avg_loss_pct=_slash_loss_rate_per_tick(v),
                )

            if uptime_ok and rewarded_count > 0:
                net_prefix = _NET_SHORT.get(v.get("network", ""), "")
                # Tag HOT/COLD events on the tx_type so they're visible in
                # .history and leaderboard tx queries, while normal ticks
                # keep the long-standing VALIDATOR_REWARD label.
                tx_type = f"VALIDATOR_REWARD_{event_tag}" if event_tag else "VALIDATOR_REWARD"
                reward_tx = await self.bot.db.log_tx(
                    guild.id, None, tx_type,
                    symbol_out=required_token, amount_out=total_rewarded,
                    network=net_prefix,
                )
                await self.bot.bus.publish(
                    "validator_reward",
                    guild=guild,
                    validator_id=vid,
                    symbol=required_token,
                    staker_count=rewarded_count,
                    total_rewarded=total_rewarded,
                    tx_hash=reward_tx,
                    event_tag=event_tag,
                    heat=heat_after,
                )

            # Persist heat even when no stakers got paid -- decay still runs
            # so a validator doesn't freeze at an old heat value just because
            # nobody was staking during an event tick. Best-effort: a failed
            # write just means we catch up the decay on the next tick, so it
            # must never block subsequent validators in this guild from
            # getting paid.
            if abs(heat_after - heat_before) > 1e-4:
                try:
                    await self.bot.db.update_validator_heat(vid, guild.id, heat_after)
                except Exception:
                    log.exception(
                        "stake.tick: update_validator_heat failed for %s/%s; "
                        "decay continues next tick",
                        guild.id, vid,
                    )

        # ── Auto-compound DMs: one batched message per user per tick ─────────
        for _ac_uid, _compounds in _pending_ac_dms.items():
            try:
                _ac_user = self.bot.get_user(_ac_uid) or await self.bot.fetch_user(_ac_uid)
                if not _ac_user or _ac_user.bot:
                    continue
                _lines = [
                    f"**{c['vid']}** -- {fmt_token(c['amount_h'], c['sym'])} restaked"
                    for c in _compounds
                ]
                _dm_embed = (
                    card(
                        "🔄 Auto-Compound",
                        description="\n".join(_lines),
                        color=C_TEAL,
                    )
                    .footer(f"Server: {guild.name}  |  .autocompound status to view totals")
                    .build()
                )
                await _ac_user.send(embed=_dm_embed)
            except Exception:
                pass  # user has DMs disabled or left  -  skip silently

        # ── Liqstone XP: grant based on LP value * hold time ──────────────
        try:
            _LQ_CFG = Config.SHOP_ITEMS.get("liqstone", {})
            if _LQ_CFG and not _LQ_CFG.get("disabled"):
                # Join pool tokens so we can detect group-token LP without
                # a per-position lookup -- the Liqstone tick runs guild-wide
                # every hour so keeping it a single query matters.
                lp_positions = await self.bot.db.fetch_all(
                    "SELECT lp.user_id, lp.pool_id, lp.lp_shares, lp.added_at, "
                    "       lp.lock_tier, lp.locked_until, "
                    "       p.token_a, p.token_b "
                    "FROM lp_positions lp "
                    "JOIN pools p ON lp.pool_id = p.pool_id AND lp.guild_id = p.guild_id "
                    "WHERE lp.guild_id = $1 AND lp.lp_shares > 0",
                    guild.id,
                )
                _lq_base_xp = float(_LQ_CFG.get("xp_per_lp_tick", 25.0))
                _lq_max_tick = float(_LQ_CFG.get("xp_max_per_tick", 200.0))
                _lq_min_hold = int(_LQ_CFG.get("min_hold_secs", 3600))
                now_ts = time.time()

                # Pre-fetch the guild's user-created token symbols once so
                # the per-position membership check is O(1).
                from services.liquidity import user_created_token_symbols
                _user_syms = await user_created_token_symbols(self.bot.db, guild.id)
                _user_mult = float(Config.USER_LP_LIQSTONE_MULT)

                # Group LP positions by user, weighting each position by its
                # opt-in time-lock multiplier (Config.LP_LOCK_TIERS) AND the
                # user-created-token multiplier. Expired locks don't need a
                # DB write to lapse -- they revert to 1.0x weight here.
                user_lp_usd: dict[int, float] = {}
                user_min_added: dict[int, float] = {}
                for lp in lp_positions:
                    uid = lp["user_id"]
                    _added = lp.get("added_at")
                    added_ts = _added.timestamp() if hasattr(_added, "timestamp") else 0
                    # Skip if held less than minimum
                    if now_ts - added_ts < _lq_min_hold:
                        continue
                    cur_tier = int(lp.get("lock_tier") or 0)
                    _lu = lp.get("locked_until")
                    _lu_ts = _lu.timestamp() if hasattr(_lu, "timestamp") else 0
                    if cur_tier > 0 and _lu_ts and now_ts < _lu_ts:
                        lock_mult = float(
                            Config.LP_LOCK_TIERS.get(cur_tier, {}).get("xp_mult", 1.0)
                        )
                    else:
                        lock_mult = 1.0
                    umult = _user_mult if (
                        _user_syms
                        and (lp.get("token_a") in _user_syms or lp.get("token_b") in _user_syms)
                    ) else 1.0
                    shares_val = to_human(lp["lp_shares"]) * lock_mult * umult
                    user_lp_usd[uid] = user_lp_usd.get(uid, 0.0) + shares_val
                    user_min_added.setdefault(uid, added_ts)

                for uid, lp_val in user_lp_usd.items():
                    if lp_val <= 0:
                        continue
                    liqstone = await self.bot.db.get_liqstone(uid, guild.id)
                    if not liqstone or liqstone["level"] >= _LQ_CFG.get("max_level", 100):
                        continue
                    xp_ref = float(getattr(Config, "XP_STAKE_REFERENCE_USD", 1000.0))
                    xp_scale = min(float(getattr(Config, "XP_SCALE_MAX", 5.0)), lp_val / xp_ref)
                    xp_gain = min(_lq_base_xp * xp_scale, _lq_max_tick)
                    old_xp = liqstone["xp"]
                    new_xp = cap_xp(old_xp + xp_gain, liqstone["level"], _LQ_CFG)
                    if new_xp > old_xp:
                        await self.bot.db.update_liqstone_xp(uid, guild.id, new_xp, liqstone["level"])
                        await notify_item_levelup_ready(self.bot, uid, guild, "liqstone", old_xp, new_xp, liqstone["level"], liqstone["staked_amount"])
        except Exception as exc:
            log.warning("Liqstone XP tick failed for guild %s: %s", guild.id, exc)

        # Safety Module: auto-compound positions re-stake their accrued yield
        # into the staked balance every hour without any user action.
        for sm_sym in Config.SAFETY_MODULE:
            try:
                await apply_sm_yield(
                    self.bot.db, guild.id, sm_sym,
                    auto_compound_only=True,
                )
            except Exception as exc:
                log.warning(
                    "SM auto-compound tick failed gid=%s sym=%s: %s",
                    guild.id, sm_sym, exc,
                )

    @staking_tick.before_loop
    async def before_staking_tick(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.bot.db.seed_validators(guild.id)
            settings = await self.bot.db.get_guild_settings(guild.id)
            if not settings.get("staking_channel") and not settings.get("validators_channel"):
                log.warning(
                    "[staking] Guild %d (%s): no staking_channel configured  -  "
                    "reward/slash events will be silent. Set one with `.channel staking #channel`.",
                    guild.id, guild.name,
                )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.bot.db.seed_validators(guild.id)

    # ══════════════════════════════════════════════════════════════════════════
    # BACKGROUND TASK: Validator tick (block production loop)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=VALIDATOR_TICK)
    async def validator_tick(self) -> None:
        """Main slot loop  -  runs every VALIDATOR_TICK seconds across all guilds."""
        for guild in self.bot.guilds:
            if not (await self.bot.db.module_enabled(guild.id, "staking") or
                    await self.bot.db.module_enabled(guild.id, "validators")):
                continue
            try:
                await self._process_guild(guild)
            except Exception as e:
                # Never crash the loop
                log.exception("[validators] Error processing guild %s: %s", guild.id, e)
        pulse("validator_tick")

    @validator_tick.before_loop
    async def before_validator_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_guild(self, guild: discord.Guild) -> None:
        """Process one validator block cycle for every active network in this guild."""
        all_validators = await self.bot.db.get_pos_validators(guild.id)
        if not all_validators:
            return

        by_network: dict[str, list[dict]] = {}
        for v in all_validators:
            if v["is_active"]:
                by_network.setdefault(v["network"], []).append(v)

        for network, validators in by_network.items():
            if len(validators) < MIN_VALIDATORS:
                continue
            await self._process_network_block(guild, network, validators)

    async def _process_network_block(
        self,
        guild: discord.Guild,
        network: str,
        validators: list[dict],
    ) -> None:
        """Process one validator block for a single network."""
        _gs_val = await self.bot.db.get_guild_settings(guild.id)
        _validator_mult = float(_gs_val.get("validator_multiplier") or 1.0)

        pending = await self.bot.db.get_pending_mempool(guild.id, network, limit=MAX_MEMPOOL)
        if not pending:
            return

        pending.sort(key=lambda x: _gas_priority(x["gas_price"]), reverse=True)

        # ── MEV Protection: fair ordering within gas tiers ──────────────────
        if Config.MEV_SHUFFLE_WITHIN_TIER:
            buckets: dict[str, list] = {"high": [], "medium": [], "low": []}
            for action in pending:
                tier = action.get("gas_price", "medium")
                if tier not in buckets:
                    tier = "medium"
                buckets[tier].append(action)
            for bucket in buckets.values():
                random.shuffle(bucket)
            pending = buckets["high"] + buckets["medium"] + buckets["low"]

        delegated_totals: dict[int, float] = {}
        for v in validators:
            delegated_totals[v["user_id"]] = await self.bot.db.get_total_delegated_stake(
                v["user_id"], guild.id, network
            )

        last_vid = self._last_validator.get((guild.id, network))
        validator = _select_validator(validators, last_vid, delegated_totals)
        if not validator:
            return
        self._last_validator[(guild.id, network)] = validator["user_id"]

        # ── MEV Protection: move validator's own transactions to end ────────
        if Config.MEV_VALIDATOR_LAST:
            validator_user_id = validator.get("user_id") or validator.get("owner_id")
            if validator_user_id:
                validator_txs = [a for a in pending if a.get("user_id") == validator_user_id]
                other_txs = [a for a in pending if a.get("user_id") != validator_user_id]
                pending = other_txs + validator_txs

        # ── MEV Protection: per-user swap limit per block ──────────────────
        if Config.MEV_MAX_SWAPS_PER_USER_PER_BLOCK > 0:
            user_swap_counts: dict[int, int] = {}
            filtered: list[dict] = []
            deferred: list[dict] = []
            for action in pending:
                if action.get("action_type") == "swap":
                    uid = action.get("user_id", 0)
                    count = user_swap_counts.get(uid, 0)
                    if count >= Config.MEV_MAX_SWAPS_PER_USER_PER_BLOCK:
                        deferred.append(action)
                        continue
                    user_swap_counts[uid] = count + 1
                filtered.append(action)
            pending = filtered
            # deferred actions stay in mempool for next block (don't remove them)

        block_id = await self.bot.db.create_validator_block(guild.id, network, validator["user_id"])

        _active_validator_ids: set[int] = {v["user_id"] for v in validators}

        results: list[dict] = []
        total_gas = 0

        for action in pending:
            success, reason = await self._execute_action(guild, action)
            status = "confirmed" if success else "rejected"
            gas = int(action["gas_fee"]) if success else 0
            total_gas += gas

            if not success:
                await self._refund_action(guild, action)
                _net_prefix = _NET_SHORT.get(network, "")
                await self.bot.db.log_tx(
                    guild.id, action["user_id"],
                    "REJECTED_" + action["action_type"].upper(),
                    network=_net_prefix,
                )
                if action["user_id"] in _active_validator_ids:
                    slash_result = await self.bot.db.slash_pos_validator(
                        action["user_id"], guild.id, network, REJECTION_SLASH_RATE
                    )
                    await self.bot.db.slash_pos_delegations(
                        action["user_id"], guild.id, network, REJECTION_SLASH_RATE
                    )
                    if slash_result:
                        await self.bot.bus.publish(
                            "pos_validator_slashed",
                            guild=guild,
                            validator_user_id=action["user_id"],
                            network=network,
                            slash_result=slash_result,
                            reason=reason,
                            action_type=action["action_type"],
                        )
                        if slash_result.get("deactivated"):
                            _active_validator_ids.discard(action["user_id"])
                            delegation_rows = await self.bot.db.wipe_delegations_for_validator(
                                action["user_id"], guild.id, network
                            )
                            _dn = _NET_SHORT.get(network, "")
                            for d in delegation_rows:
                                if _dn:
                                    await self.bot.db.update_wallet_holding(
                                        d["delegator_id"], guild.id, _dn, d["token"], d["amount"]
                                    )
                                else:
                                    await self.bot.db.update_holding(
                                        d["delegator_id"], guild.id, d["token"], d["amount"]
                                    )

            was_resolved = await self.bot.db.resolve_mempool_action(action["id"], status, block_id)
            if not was_resolved:
                log.warning("[validators] Action %d already resolved (possible race)  -  skipping.", action["id"])
                continue
            results.append({
                "action": action,
                "success": success,
                "reason": reason,
                "gas": gas,
            })

        # Distribute rewards in network's native coin
        gas_coin = gas_coin_for_network(network)
        _fee_cfg = await self.bot.db.guilds.get_fee_config(guild.id)
        _t_cut = _fee_cfg["treasury_cut_pct"]
        # total_gas is raw int; compute derived values as raw ints
        validator_reward = int(total_gas * (1.0 - _t_cut) * _validator_mult)
        treasury_cut     = total_gas - validator_reward

        # Split validator reward using validator's chosen commission rate
        commission = validator.get("commission_rate", DELEGATION_VALIDATOR_KEEP)
        commission = max(0.30, min(0.90, commission))  # lowered min from 0.60 for delegator-friendly validators
        delegation_pool           = int(validator_reward * (1.0 - commission))
        adjusted_validator_reward = validator_reward - delegation_pool

        # Wealth Bottleneck on the validator's own slice. Drag (rich
        # validators) flows back to treasury so the network keeps the
        # inflation headroom; boost (poor validators) is paid as USD
        # to the wallet from the per-guild pool.
        from services.bottleneck import apply_bottleneck, CreditKind
        net_short = _NET_SHORT.get(network, "")
        if adjusted_validator_reward > 0:
            _v_bn = await apply_bottleneck(
                self.bot.db,
                uid=int(validator["user_id"]), gid=guild.id,
                gross_raw=int(adjusted_validator_reward),
                kind=CreditKind.POS_REWARD,
                symbol=gas_coin,
            )
            treasury_cut += (adjusted_validator_reward - _v_bn.net_credit_raw)
            adjusted_validator_reward = _v_bn.net_credit_raw
            if adjusted_validator_reward > 0:
                if net_short:
                    await self.bot.db.update_wallet_holding(
                        validator["user_id"], guild.id, net_short, gas_coin,
                        adjusted_validator_reward,
                    )
                else:
                    await self.bot.db.update_holding(
                        validator["user_id"], guild.id, gas_coin,
                        adjusted_validator_reward,
                    )
            if _v_bn.boost_wallet_raw > 0:
                await self.bot.db.update_wallet(
                    validator["user_id"], guild.id, int(_v_bn.boost_wallet_raw),
                )

        # Distribute delegation pool proportionally among delegators
        if delegation_pool > 0:
            delegations = await self.bot.db.get_delegations_for_validator(
                validator["user_id"], guild.id, network
            )
            total_delegated = sum(int(d["amount"]) for d in delegations)
            if total_delegated > 0:
                for d in delegations:
                    share = int(d["amount"]) / total_delegated
                    payout = int(delegation_pool * share)
                    # Per-delegator wealth bottleneck. Drag goes to
                    # treasury (network captures the value); boost is
                    # paid in USD to the delegator's wallet from the pool.
                    _d_bn = None
                    if payout > 0:
                        _d_bn = await apply_bottleneck(
                            self.bot.db,
                            uid=int(d["delegator_id"]), gid=guild.id,
                            gross_raw=int(payout),
                            kind=CreditKind.DELEGATION_REWARD,
                            symbol=gas_coin,
                        )
                        treasury_cut += (payout - _d_bn.net_credit_raw)
                        payout = _d_bn.net_credit_raw
                    if payout > 0:
                        # Credit the delegator and bump increment_delegation_earned
                        # atomically - otherwise a failure between the two writes
                        # would leave the delegator paid without tracking, or the
                        # tracker bumped with no corresponding credit.
                        async with self.bot.db.atomic():
                            if net_short:
                                await self.bot.db.update_wallet_holding(
                                    d["delegator_id"], guild.id, net_short, gas_coin, payout
                                )
                            else:
                                await self.bot.db.update_holding(
                                    d["delegator_id"], guild.id, gas_coin, payout
                                )
                            await self.bot.db.increment_delegation_earned(
                                d["delegator_id"], validator["user_id"], guild.id, network, payout
                            )
                    if _d_bn is not None and _d_bn.boost_wallet_raw > 0:
                        await self.bot.db.update_wallet(
                            d["delegator_id"], guild.id,
                            int(_d_bn.boost_wallet_raw),
                        )
            else:
                # No delegators  -  give unclaimed pool back to validator
                if net_short:
                    await self.bot.db.update_wallet_holding(validator["user_id"], guild.id, net_short, gas_coin, delegation_pool)
                else:
                    await self.bot.db.update_holding(validator["user_id"], guild.id, gas_coin, delegation_pool)
                adjusted_validator_reward += delegation_pool

        if treasury_cut > 0:
            await self.bot.db.add_to_treasury(guild.id, treasury_cut)
            from services.vault import deposit_to_vault
            _vault_net = _NET_SHORT.get(network, "")
            if _vault_net:
                await deposit_to_vault(self.bot.db, guild.id, _vault_net, treasury_cut, bot=self.bot)

        # Confirm the block
        await self.bot.db.confirm_validator_block(
            block_id, total_gas, adjusted_validator_reward, treasury_cut
        )

        # Update validator stats (track adjusted reward actually received)
        await self.bot.db.increment_validator_blocks(validator["user_id"], guild.id, adjusted_validator_reward)

        # Lockstone XP: grant per validator block confirmed
        _LS_CFG = Config.SHOP_ITEMS.get("lockstone", {})
        if _LS_CFG:
            lockstone = await self.bot.db.get_lockstone(validator["user_id"], guild.id)
            if lockstone and lockstone["level"] < _LS_CFG.get("max_level", 50):
                base_xp = _LS_CFG.get("xp_per_block", 10.0)
                # Proportional XP scaling based on validator's total stake USD value
                _val_total_stake = await self.bot.db.get_total_delegated_stake(
                    validator["user_id"], guild.id, network
                )
                _stake_token = Config.NETWORK_STAKE_TOKEN.get(network, "SUN")
                _val_tp_row = await self.bot.db.get_price(_stake_token, guild.id)
                _val_tp = float(_val_tp_row["price"]) if _val_tp_row else 0.0
                _val_stake_usd = _val_total_stake * _val_tp
                if _val_stake_usd > 0:
                    xp_scale = min(Config.XP_SCALE_MAX, _val_stake_usd / Config.XP_STAKE_REFERENCE_USD)
                    xp_gain = base_xp * xp_scale
                    xp_result = await self.bot.db.add_lockstone_xp(validator["user_id"], guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _LS_CFG)
                        if capped_xp < live_xp:
                            await self.bot.db.update_lockstone_xp(validator["user_id"], guild.id, capped_xp, live_level)

        # EIP-1559: adjust base fee for next block based on how full this block was
        confirmed_count = sum(1 for r in results if r["success"])
        is_valid = True
        current_base = await self.bot.db.get_base_fee(guild.id, network)
        new_base = adjust_base_fee(current_base, confirmed_count, MAX_MEMPOOL, network)
        await self.bot.db.set_base_fee(guild.id, network, new_base)

        # Log to session file
        from core.framework import session_log as _sl
        sl = _sl.get()
        if sl is not None:
            confirmed_count_log = sum(1 for r in results if r["success"])
            rejected_count_log  = len(results) - confirmed_count_log
            sl.validator_block(
                guild_name=guild.name,
                network=network,
                validator_id=validator["user_id"],
                total_actions=len(results),
                confirmed=confirmed_count_log,
                rejected=rejected_count_log,
                total_gas=total_gas,
                gas_coin=gas_coin,
                results=results,
            )

        # Publish event so trades.py can post the feed embed
        await self.bot.bus.publish(
            "validator_block",
            guild=guild,
            network=network,
            validator=validator,
            block_id=block_id,
            results=results,
            total_gas=total_gas,
            gas_coin=gas_coin,
            validator_reward=adjusted_validator_reward,
            delegation_pool=delegation_pool,
            treasury_cut=treasury_cut,
            is_valid=is_valid,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Validator action execution helpers
    # ══════════════════════════════════════════════════════════════════════════

    async def _refund_action(self, guild: discord.Guild, action: dict) -> None:
        """Refund locked tokens to sender when a mempool action is rejected."""
        import json as _json
        payload = _json.loads(action["payload"])
        action_type = action["action_type"]
        user_id = action["user_id"]

        _action_net = _NET_SHORT.get(action.get("network", ""), "")
        try:
            if action_type == "send":
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    if _action_net:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _action_net, symbol, to_raw(amount))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
            elif action_type == "swap":
                token_in = payload.get("token_in", "").upper()
                amount_in = float(payload.get("amount_in", 0))
                if token_in and amount_in > 0:
                    # Refund to token_in's OWN network. See the matching
                    # comment in cogs/validators.py _refund_action.
                    _payload_net_in = payload.get("net_in") or ""
                    _in_net_short = _NET_SHORT.get(_payload_net_in, "") or _action_net
                    if _in_net_short:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _in_net_short, token_in, to_raw(amount_in))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, token_in, to_raw(amount_in))
            elif action_type == "buy":
                amount_usd = float(payload.get("amount_usd", 0))
                if amount_usd > 0:
                    await self.bot.db.update_wallet(user_id, guild.id, to_raw(amount_usd))
            elif action_type == "sell":
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
            elif action_type == "stake":
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    if _action_net:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _action_net, symbol, to_raw(amount))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
            elif action_type == "addlp":
                token_a  = payload.get("token_a", "").upper()
                token_b  = payload.get("token_b", "").upper()
                amount_a = float(payload.get("amount_a", 0))
                amount_b = float(payload.get("amount_b", 0))
                if token_a and amount_a > 0:
                    if _action_net:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _action_net, token_a, to_raw(amount_a))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, token_a, to_raw(amount_a))
                if token_b and amount_b > 0:
                    if _action_net:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _action_net, token_b, to_raw(amount_b))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, token_b, to_raw(amount_b))
            elif action_type == "removelp":
                pool_id   = payload.get("pool_id", "")
                lp_shares = float(payload.get("lp_shares", 0))
                if pool_id and lp_shares > 0:
                    await self.bot.db.update_lp_position(user_id, guild.id, pool_id, to_raw(lp_shares))
            elif action_type == "contract_call":
                pass
            # unstake, contract_deploy: nothing locked at submission
            # Note: gas_fee is NOT refunded on rejection (like real Arcadia)
        except Exception as e:
            log.exception("[validators] Refund failed for action %s: %s", action['id'], e)

    async def _execute_action(
        self, guild: discord.Guild, action: dict
    ) -> tuple[bool, str]:
        """
        Execute a mempool action against the live game state.
        Returns (success, reason_string).

        Supports: send, swap, buy, sell, stake, unstake, addlp, removelp,
                  contract_deploy, contract_call.
        """
        action_type = action["action_type"]
        payload = json.loads(action["payload"])
        user_id = action["user_id"]
        network = action.get("network", "")

        # ── Network halt check ────────────────────────────────────────────────
        if network and await self.bot.db.is_network_halted(guild.id, network):
            return (False, f"{network.upper()} network is currently halted by an admin.")

        try:
            if action_type == "send":
                return await self._exec_send(guild, user_id, payload, action.get("network", ""))
            elif action_type == "swap":
                return await self._exec_swap(guild, user_id, payload, action.get("network", ""))
            elif action_type == "buy":
                return await self._exec_buy(guild, user_id, payload)
            elif action_type == "sell":
                return await self._exec_sell(guild, user_id, payload)
            elif action_type == "stake":
                return await self._exec_stake(guild, user_id, payload, action.get("network", ""))
            elif action_type == "unstake":
                return await self._exec_unstake(guild, user_id, payload, action.get("network", ""))
            elif action_type == "addlp":
                return await self._exec_addlp(guild, user_id, payload, action.get("network", ""))
            elif action_type == "removelp":
                return await self._exec_removelp(guild, user_id, payload, action.get("network", ""))
            elif action_type == "contract_deploy":
                return await self._exec_contract_deploy(guild, user_id, payload)
            elif action_type == "contract_call":
                return await self._exec_contract_call(guild, user_id, payload)
            else:
                return False, f"Unknown action type: {action_type}"
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Internal error: {e}"

    async def _exec_send(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a wallet-to-wallet token send from mempool."""
        to_id  = payload.get("to_user_id")
        symbol = payload.get("symbol", "").upper()
        amount = float(payload.get("amount", 0))

        if not to_id or not symbol or amount <= 0:
            return False, "Invalid send payload"

        await self.bot.db.ensure_user(to_id, guild.id)
        if symbol == "USD":
            await self.bot.db.update_wallet(to_id, guild.id, to_raw(amount))
        else:
            net_short = _NET_SHORT.get(network, "")
            if net_short:
                await self.bot.db.update_wallet_holding(to_id, guild.id, net_short, symbol, to_raw(amount))
            else:
                await self.bot.db.update_holding(to_id, guild.id, symbol, to_raw(amount))

        await self.bot.db.log_tx(
            guild.id, user_id, "token_send",
            symbol_in=symbol, amount_in=to_raw(amount),
            symbol_out=symbol, amount_out=to_raw(amount),
            network=_NET_SHORT.get(network, ""),
        )
        return True, f"Sent {amount} {symbol}"

    async def _exec_swap(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued AMM swap from the mempool."""
        token_in  = payload.get("token_in", "").upper()
        token_out = payload.get("token_out", "").upper()
        amount_in = float(payload.get("amount_in", 0))
        pool_id   = payload.get("pool_id", "")

        if not token_in or not token_out or amount_in <= 0 or not pool_id:
            return False, "Invalid swap payload"

        pool = await self.bot.db.get_pool(pool_id, guild.id)
        if not pool:
            return False, f"Pool {pool_id} not found"

        ca = pool["token_a"]
        # Read reserves as raw ints; do math via human floats but apply the
        # debit/credit to the raw column directly so reserves stay exact.
        if token_in == ca:
            reserve_in_raw = int(pool["reserve_a"])
            reserve_out_raw = int(pool["reserve_b"])
        else:
            reserve_in_raw = int(pool["reserve_b"])
            reserve_out_raw = int(pool["reserve_a"])
        reserve_in = to_human(reserve_in_raw)
        reserve_out = to_human(reserve_out_raw)

        if reserve_in <= 0 or reserve_out <= 0:
            return False, "Pool has no liquidity"

        FEE = 0.003
        amount_in_with_fee = amount_in * (1 - FEE)
        amount_out = reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee)

        if amount_out <= 0:
            return False, "Swap output too small"

        # ── MEV Protection: slippage check ─────────────────────────────────
        min_out = float(payload.get("min_amount_out", 0))
        if min_out > 0 and amount_out < min_out:
            # Slippage exceeded  -  reject the swap, tokens are refunded by caller
            return False, f"Slippage exceeded: expected >= {min_out:.6f}, got {amount_out:.6f}"

        net_short = _NET_SHORT.get(network, "")
        amount_in_raw = to_raw(amount_in)
        amount_out_raw = to_raw(amount_out)
        # Credit token_out to its OWN network. See the matching comment in
        # cogs/validators.py _exec_swap -- vault-pair pools cross networks
        # (e.g. Moon Network group token + Moneta Chain coin) and using
        # the mempool's network for both sides creates the duplicate-wallet
        # display bug.
        _payload_net_out = payload.get("net_out") or ""
        _out_net_short = _NET_SHORT.get(_payload_net_out, "")
        if not _out_net_short:
            try:
                _all_tokens = await self.bot.db.get_all_tokens_for_guild(guild.id)
                _out_net_short = _NET_SHORT.get(
                    _all_tokens.get(token_out, {}).get("network", ""), "",
                )
            except Exception:
                _out_net_short = ""
        _out_net_short = _out_net_short or net_short
        if _out_net_short:
            await self.bot.db.update_wallet_holding(user_id, guild.id, _out_net_short, token_out, amount_out_raw)
        else:
            await self.bot.db.update_holding(user_id, guild.id, token_out, amount_out_raw)

        # Update reserves in raw int space so the pool row never drifts.
        new_reserve_in_raw = reserve_in_raw + amount_in_raw
        new_reserve_out_raw = reserve_out_raw - amount_out_raw
        if token_in == ca:
            await self.bot.db.update_pool_reserves(
                pool_id, guild.id,
                new_reserve_in_raw, new_reserve_out_raw, int(pool["total_lp"]),
            )
        else:
            await self.bot.db.update_pool_reserves(
                pool_id, guild.id,
                new_reserve_out_raw, new_reserve_in_raw, int(pool["total_lp"]),
            )

        await self.bot.db.log_tx(
            guild.id, user_id, "SWAP",
            symbol_in=token_in, amount_in=amount_in_raw,
            symbol_out=token_out, amount_out=amount_out_raw,
            network=_NET_SHORT.get(network, ""),
        )
        return True, f"Swapped {amount_in} {token_in} → {amount_out:.6f} {token_out}"

    async def _exec_buy(
        self, guild: discord.Guild, user_id: int, payload: dict
    ) -> tuple[bool, str]:
        """Execute a queued buy at live oracle price."""
        symbol     = payload.get("symbol", "").upper()
        amount_usd = float(payload.get("amount_usd", 0))

        if not symbol or amount_usd <= 0:
            return False, "Invalid buy payload"

        price_row = await self.bot.db.get_price(symbol, guild.id)
        if not price_row or price_row["price"] <= 0:
            return False, f"No price data for {symbol}"

        # Apply impact as per-trade slippage; oracle is not updated by individual trades
        from core.config import Config as _Cfg
        impact = amount_usd / _Cfg.PRICE_IMPACT_DIVISOR
        _eff_price_buy = max(1e-15, float(price_row["price"]) * (1 + impact))
        qty = amount_usd / _eff_price_buy
        await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(qty))

        await self.bot.db.log_tx(
            guild.id, user_id, "BUY",
            symbol_in="USD", amount_in=to_raw(amount_usd),
            symbol_out=symbol, amount_out=to_raw(qty),
            price_at=float(price_row["price"]),
        )
        return True, f"Bought {qty:.6f} {symbol} at ${_eff_price_buy:.4f}"

    async def _exec_sell(
        self, guild: discord.Guild, user_id: int, payload: dict
    ) -> tuple[bool, str]:
        """Execute a queued sell at live oracle price."""
        symbol = payload.get("symbol", "").upper()
        amount = float(payload.get("amount", 0))

        if not symbol or amount <= 0:
            return False, "Invalid sell payload"

        price_row = await self.bot.db.get_price(symbol, guild.id)
        if not price_row or price_row["price"] <= 0:
            return False, f"No price data for {symbol}"

        # Apply impact as per-trade slippage; oracle is not updated by individual trades
        from core.config import Config as _Cfg
        spot_revenue = float(price_row["price"]) * amount
        impact = spot_revenue / _Cfg.PRICE_IMPACT_DIVISOR
        _eff_price_sell = max(1e-9, float(price_row["price"]) * (1 - impact))
        revenue = amount * _eff_price_sell
        await self.bot.db.update_wallet(user_id, guild.id, to_raw(revenue))

        await self.bot.db.log_tx(
            guild.id, user_id, "SELL",
            symbol_in=symbol, amount_in=to_raw(amount),
            symbol_out="USD", amount_out=to_raw(revenue),
            price_at=float(price_row["price"]),
        )
        return True, f"Sold {amount:.6f} {symbol} for ${revenue:.4f}"

    async def _exec_stake(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued stake."""
        validator_id = payload.get("validator_id", "").upper()
        symbol       = payload.get("symbol", "").upper()
        amount       = float(payload.get("amount", 0))

        if not validator_id or not symbol or amount <= 0:
            return False, "Invalid stake payload"

        v = await self.bot.db.get_validator(validator_id, guild.id)
        if not v:
            return False, f"Validator {validator_id} not found"

        await self.bot.db.update_stake(user_id, guild.id, validator_id, symbol, to_raw(amount))
        await self.bot.db.insert_stake_batch(user_id, guild.id, validator_id, symbol, to_raw(amount))
        await self.bot.db.log_tx(
            guild.id, user_id, "STAKE",
            symbol_in=symbol, amount_in=to_raw(amount),
            symbol_out=validator_id, amount_out=to_raw(amount),
            network=_NET_SHORT.get(network, ""),
        )
        # Lockstone XP: award on stake action for immediate feedback
        _LS_CFG = Config.SHOP_ITEMS.get("lockstone", {})
        if _LS_CFG:
            lockstone = await self.bot.db.get_lockstone(user_id, guild.id)
            if lockstone and lockstone["level"] < _LS_CFG.get("max_level", 50) and amount > 0:
                base_xp = _LS_CFG.get("xp_per_stake_reward", 10.0) * 0.25
                # Proportional XP scaling based on stake USD value  -  no minimum floor
                _tp_row = await self.bot.db.get_price(symbol, guild.id)
                _tp = float(_tp_row["price"]) if _tp_row else 0.0
                _stake_usd = amount * _tp
                if _stake_usd > 0:
                    xp_scale = min(Config.XP_SCALE_MAX, _stake_usd / Config.XP_STAKE_REFERENCE_USD)
                    xp_gain = base_xp * xp_scale
                    xp_result = await self.bot.db.add_lockstone_xp(user_id, guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _LS_CFG)
                        if capped_xp < live_xp:
                            await self.bot.db.update_lockstone_xp(user_id, guild.id, capped_xp, live_level)
                        await notify_item_levelup_ready(self.bot, user_id, guild, "lockstone", live_xp - xp_gain, live_xp, live_level, lockstone["staked_amount"])
        return True, f"Staked {amount:.6f} {symbol} with {validator_id}"

    async def _exec_unstake(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued unstake."""
        validator_id = payload.get("validator_id", "").upper()
        symbol       = payload.get("symbol", "").upper()
        original_amount = float(payload.get("amount", 0))

        if not validator_id or not symbol or original_amount <= 0:
            return False, "Invalid unstake payload"

        # Consume unlocked batches; the validator unstake command already checked
        # lock status before queuing  -  this just keeps batches in sync.
        await self.bot.db.consume_stake_batches(
            user_id, guild.id, validator_id, original_amount, STAKE_LOCK_SECS
        )

        # Early unstake penalty (burn portion if within penalty window)
        _sa = payload.get("staked_at", 0)
        staked_at = _sa.timestamp() if hasattr(_sa, 'timestamp') else float(_sa)
        penalty = 0.0
        if staked_at > 0 and time.time() - staked_at < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            penalty = original_amount * Config.STAKING_EARLY_UNSTAKE_PENALTY
        net_received = original_amount - penalty

        # Debit full original amount from stake record
        await self.bot.db.update_stake(user_id, guild.id, validator_id, symbol, to_raw(-original_amount))

        # Credit net received back to the correct wallet.
        # Use the canonical short code; if the network is given as a full name,
        # look it up in _NET_SHORT (full→short). If a short code was passed, use directly.
        net_short = _NET_SHORT.get(network, "") or _NET_SHORT.get(
            _normalize_network(network), ""
        )
        if net_short:
            await self.bot.db.update_wallet_holding(user_id, guild.id, net_short, symbol, to_raw(net_received))
        else:
            # Validator network not recognized as a DeFi network  -  credit CeFi holdings
            await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(net_received))

        await self.bot.db.log_tx(
            guild.id, user_id, "UNSTAKE",
            symbol_in=validator_id, amount_in=to_raw(original_amount),
            symbol_out=symbol, amount_out=to_raw(net_received),
            network=net_short,
        )
        penalty_msg = ""
        if penalty > 0:
            penalty_msg = f" (-{Config.STAKING_EARLY_UNSTAKE_PENALTY*100:.0f}% early penalty: {penalty:.6f} {symbol} burned)"
        return True, f"Unstaked {net_received:.6f} {symbol} from {validator_id}{penalty_msg}"

    async def _exec_addlp(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued add-liquidity."""
        import math as _math
        pool_id  = payload.get("pool_id", "")
        token_a  = payload.get("token_a", "").upper()
        token_b  = payload.get("token_b", "").upper()
        amount_a = float(payload.get("amount_a", 0))
        amount_b = float(payload.get("amount_b", 0))

        if not pool_id or not token_a or not token_b or amount_a <= 0 or amount_b <= 0:
            return False, "Invalid addlp payload"

        pool = await self.bot.db.get_pool(pool_id, guild.id)
        if not pool:
            return False, f"Pool {pool_id} not found"

        # LP mint math in raw int space so pool reserves stay exact.
        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        total_lp_raw = int(pool["total_lp"])
        amount_a_raw = to_raw(amount_a)
        amount_b_raw = to_raw(amount_b)

        if total_lp_raw == 0:
            # Geometric mean stays in raw scale: sqrt(SCALE**2) == SCALE.
            lp_mint_raw = _math.isqrt(amount_a_raw * amount_b_raw)
        else:
            if reserve_a_raw <= 0 or reserve_b_raw <= 0:
                return False, "Pool has no reserves"
            mint_from_a = total_lp_raw * amount_a_raw // reserve_a_raw
            mint_from_b = total_lp_raw * amount_b_raw // reserve_b_raw
            lp_mint_raw = min(mint_from_a, mint_from_b)

        if lp_mint_raw <= 0:
            return False, "LP shares too small"

        new_res_a_raw = reserve_a_raw + amount_a_raw
        new_res_b_raw = reserve_b_raw + amount_b_raw
        new_total_raw = total_lp_raw + lp_mint_raw

        await self.bot.db.update_pool_reserves(
            pool_id, guild.id, new_res_a_raw, new_res_b_raw, new_total_raw,
        )
        await self.bot.db.update_lp_position(user_id, guild.id, pool_id, lp_mint_raw)
        res_a_per_lp = to_human(new_res_a_raw) / to_human(new_total_raw) if new_total_raw > 0 else 0
        res_b_per_lp = to_human(new_res_b_raw) / to_human(new_total_raw) if new_total_raw > 0 else 0
        await self.bot.db.upsert_lp_snapshot(user_id, guild.id, pool_id, to_raw(res_a_per_lp), to_raw(res_b_per_lp))

        await self.bot.db.log_tx(
            guild.id, user_id, "ADDLP",
            symbol_in=f"{token_a}/{token_b}", amount_in=lp_mint_raw,
            network=_NET_SHORT.get(network, ""),
        )
        lp_mint = to_human(lp_mint_raw)
        return True, f"Added LP: {amount_a:.4f} {token_a} + {amount_b:.4f} {token_b} → {lp_mint:.6f} LP"

    async def _exec_removelp(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued remove-liquidity."""
        pool_id   = payload.get("pool_id", "")
        token_a   = payload.get("token_a", "").upper()
        token_b   = payload.get("token_b", "").upper()
        lp_shares = float(payload.get("lp_shares", 0))

        if not pool_id or lp_shares <= 0:
            return False, "Invalid removelp payload"

        pool = await self.bot.db.get_pool(pool_id, guild.id)
        if not pool or pool["total_lp"] <= 0:
            return False, "Pool not found or empty"

        # Block removal of LP shares that are locked by item stakes
        lp_pos = await self.bot.db.get_user_lp(user_id, guild.id, pool_id)
        if lp_pos:
            locked_raw = float(lp_pos.get("locked_lp_shares", 0) or 0)
            if locked_raw > 0:
                free_raw = float(lp_pos["lp_shares"]) - locked_raw
                if to_raw(lp_shares) > max(0.0, free_raw):
                    free_h = to_human(int(max(0.0, free_raw)))
                    return False, (
                        f"Only {free_h:.6f} LP is withdrawable. "
                        f"The rest is locked by your item stakes. "
                        f"Sell the item (hashstone/lockstone/vaultstone/liqstone) to unlock."
                    )

        # LP exit math in raw int space so reserves stay exact across removals.
        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        total_lp_raw = int(pool["total_lp"])
        # Cap the deduction at the actual raw balance: to_raw(to_human(raw))
        # can overshoot and trigger "Insufficient LP shares" on full exits.
        _user_lp_raw = int(lp_pos["lp_shares"]) if lp_pos else 0
        shares_raw = min(to_raw(lp_shares), _user_lp_raw)
        if shares_raw <= 0:
            return False, "No LP shares to remove."
        out_a_raw = reserve_a_raw * shares_raw // total_lp_raw if total_lp_raw > 0 else 0
        out_b_raw = reserve_b_raw * shares_raw // total_lp_raw if total_lp_raw > 0 else 0
        out_a = to_human(out_a_raw)
        out_b = to_human(out_b_raw)

        await self.bot.db.update_lp_position(user_id, guild.id, pool_id, -shares_raw)
        await self.bot.db.update_pool_reserves(
            pool_id, guild.id,
            reserve_a_raw - out_a_raw,
            reserve_b_raw - out_b_raw,
            total_lp_raw - shares_raw,
        )

        ca_sym = token_a or pool.get("token_a", "")
        cb_sym = token_b or pool.get("token_b", "")
        net_short = _NET_SHORT.get(network, "")
        if net_short:
            await self.bot.db.update_wallet_holding(user_id, guild.id, net_short, ca_sym, out_a_raw)
            await self.bot.db.update_wallet_holding(user_id, guild.id, net_short, cb_sym, out_b_raw)
        else:
            await self.bot.db.update_holding(user_id, guild.id, ca_sym, out_a_raw)
            await self.bot.db.update_holding(user_id, guild.id, cb_sym, out_b_raw)

        await self.bot.db.log_tx(
            guild.id, user_id, "REMOVELP",
            symbol_in=f"{ca_sym}/{cb_sym}", amount_in=shares_raw,
            network=_NET_SHORT.get(network, ""),
        )
        return True, f"Removed {lp_shares:.6f} LP → {out_a:.4f} {ca_sym} + {out_b:.4f} {cb_sym}"

    async def _exec_contract_deploy(
        self, guild: discord.Guild, user_id: int, payload: dict
    ) -> tuple[bool, str]:
        """Execute a queued contract deployment."""
        from cogs.contracts import ContractEngine
        name        = payload.get("name", "")
        network     = payload.get("network", "")
        ctype       = payload.get("type", "custom")
        definition  = payload.get("definition", {})
        description = payload.get("description", "")

        if not name or not network or not definition:
            return False, "Invalid contract_deploy payload"

        try:
            ContractEngine.validate_definition(definition)
        except ValueError as e:
            return False, f"Invalid contract definition: {e}"

        address = await self.bot.db.deploy_contract(
            guild.id, user_id, network, name, ctype, definition, description
        )
        _net_prefix = _NET_SHORT.get(network, "")
        await self.bot.db.log_tx(
            guild.id, user_id, "CONTRACT_DEPLOY",
            symbol_in="GAS", amount_in=0,
            symbol_out=address, amount_out=0,
            network=_net_prefix,
        )
        contract = await self.bot.db.get_contract(guild.id, address)
        if contract:
            await self.bot.bus.publish(
                "contract_event",
                guild=guild,
                contract=contract,
                action="deploy",
                caller_id=user_id,
                block_id=None,
                events=[],
                extra={"network": network, "type": ctype},
            )
        return True, f"Deployed contract {name} at {address}"

    async def _exec_contract_call(
        self, guild: discord.Guild, user_id: int, payload: dict
    ) -> tuple[bool, str]:
        """Execute a queued contract function call."""
        from cogs.contracts import ContractEngine
        address       = payload.get("address", "")
        function_name = payload.get("function", "")
        args          = payload.get("args", {})
        block_id      = payload.get("block_id")

        if not address or not function_name:
            return False, "Invalid contract_call payload"

        contract = await self.bot.db.get_contract(guild.id, address)
        if not contract:
            return False, f"Contract {address} not found"
        if contract["is_paused"]:
            return False, f"Contract {address} is paused"

        success, reason = await ContractEngine.execute(
            self.bot.db, guild, contract, function_name, args, user_id, block_id
        )
        if success:
            events = await self.bot.db.get_contract_events(guild.id, address, limit=5)
            if block_id is not None:
                events = [e for e in events if e.get("block_id") == block_id]
            await self.bot.bus.publish(
                "contract_event",
                guild=guild,
                contract=contract,
                action="call",
                function=function_name,
                caller_id=user_id,
                block_id=block_id,
                events=events,
                extra={},
            )
        return success, reason

    # ══════════════════════════════════════════════════════════════════════════
    # DIRECT SUBCOMMANDS OF /stake (yield-farm staking from staking.py)
    # ══════════════════════════════════════════════════════════════════════════

    async def _unstake_everything(self, ctx: DiscoContext) -> None:
        """Withdraw all staked positions that are past the lock period."""
        uid, gid = ctx.author.id, ctx.guild_id
        stakes = await ctx.db.get_user_stakes(uid, gid)
        if not stakes:
            await ctx.reply_error("You have no staked positions.")
            return

        now_ts = time.time()
        eligible: list[dict] = []
        locked: list[str] = []
        for s in stakes:
            total_amt = to_human(s["amount"])
            if total_amt <= 0:
                continue
            vid = s["validator_id"]
            batches = await ctx.db.get_stake_batches(uid, gid, vid)
            unlocked = sum(
                to_human(b["amount"]) for b in batches
                if (b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0))
                   + STAKE_LOCK_SECS <= now_ts
            )
            # Auto-compounded rewards beyond tracked batches are freely unlocked
            batch_total = sum(to_human(b["amount"]) for b in batches)
            unlocked += max(0.0, total_amt - batch_total)

            # Report locked batches per validator
            for b in batches:
                _sa = b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0)
                if _sa + STAKE_LOCK_SECS > now_ts:
                    secs_left = int(_sa + STAKE_LOCK_SECS - now_ts)
                    h, r = divmod(max(secs_left, 0), 3600)
                    m = r // 60
                    locked.append(f"**{vid}**: {fmt_token(to_human(b['amount']), s['symbol'])} (unlocks in {h}h {m}m)")

            if unlocked > 0.000001:
                eligible.append({**s, "amount": unlocked})

        if not eligible:
            lock_str = "\n".join(locked) if locked else "All positions are locked."
            await ctx.reply_error(f"No eligible positions to unstake.\n{lock_str}")
            return

        # Pre-calculate gas for each eligible position
        gas_info: list[dict] = []  # parallel to eligible
        total_gas_by_coin: dict[str, float] = {}
        for s in eligible:
            vid = s["validator_id"]
            v = await ctx.db.get_validator(vid, gid)
            network = v.get("network", "Unknown") if v else "Unknown"
            net_short = _NET_SHORT.get(network, "")
            g_fee = 0.0
            g_coin = ""
            g_emoji = ""
            active_v2 = [v2 for v2 in await ctx.db.get_pos_validators_for_network(gid, network) if v2["is_active"]]
            if active_v2:
                g_coin, g_fee = await gas_fee_for_network(ctx.db, gid, "unstake", "medium", network)
                g_cfg = Config.TOKENS.get(g_coin, {})
                g_emoji = g_cfg.get("emoji", "●")
            gas_info.append({"fee": g_fee, "coin": g_coin, "emoji": g_emoji, "network": network, "net_short": net_short})
            if g_fee > 0:
                total_gas_by_coin[g_coin] = total_gas_by_coin.get(g_coin, 0.0) + g_fee

        # Build preview with gas
        lines = []
        for s, gi in zip(eligible, gas_info):
            line = f"**{s['validator_id']}**: {fmt_token(s['amount'], s['symbol'])}"
            if gi["fee"] > 0:
                line += f"  ⛽ {fmt_gas(gi['fee'], gi['coin'], gi['emoji'])}"
            lines.append(line)

        desc = "Unstaking **all** eligible positions:\n\n" + "\n".join(lines)
        if total_gas_by_coin:
            gas_summary = " + ".join(f"**{fmt_gas(v, k, Config.TOKENS.get(k, {}).get('emoji', ''))}**" for k, v in total_gas_by_coin.items())
            desc += f"\n\n⛽ **Total gas:** {gas_summary}"
        if locked:
            desc += "\n\n**Still locked:**\n" + "\n".join(locked)

        confirm_embed = card("🔓 Unstake Everything", description=desc, color=C_AMBER).footer("Confirm within 30 seconds").build()
        view = ConfirmView(ctx.author.id, timeout=30)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        view.message = msg
        confirmed = await view.wait_result()
        if confirmed is not True:
            try:
                await msg.edit(embed=card("🔓 Unstake Cancelled", color=C_NEUTRAL).build(), view=None)
            except Exception:
                pass
            return

        # Execute unstakes with gas deductions
        result_lines = []
        total_gas_paid: dict[str, float] = {}
        for s, gi in zip(eligible, gas_info):
            vid = s["validator_id"]
            sym = s["symbol"]
            amt_human = s["amount"]  # human-scale (set during preview phase)
            net_short = gi["net_short"]
            g_fee = gi["fee"]
            g_coin = gi["coin"]
            g_emoji = gi["emoji"]
            try:
                # Check gas balance if gas required
                if g_fee > 0:
                    gas_h = (
                        await ctx.db.get_wallet_holding(uid, gid, net_short, g_coin) if net_short
                        else await ctx.db.get_holding(uid, gid, g_coin)
                    )
                    gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
                    # If gas coin == stake coin, the returned tokens can cover it
                    if g_coin == sym:
                        if gas_bal < g_fee and amt_human < g_fee:
                            result_lines.append(f"**{vid}**: skipped (insufficient gas)")
                            continue
                    elif gas_bal < g_fee:
                        result_lines.append(f"**{vid}**: skipped (need {fmt_gas(g_fee, g_coin, g_emoji)} gas)")
                        continue

                async with ctx.db.atomic():
                    # Lock row and read current amount, then zero it
                    cur = await ctx.db.fetch_one(
                        "SELECT amount FROM stakes "
                        "WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND symbol=$4 "
                        "FOR UPDATE",
                        uid, gid, vid, sym,
                    )
                    if not cur or cur["amount"] <= 0:
                        result_lines.append(f"**{vid}**: skipped (already empty)")
                        continue
                    actual = int(cur["amount"])  # raw int for DB operations
                    amt_human = to_human(actual)  # human scale for display/gas
                    await ctx.db.execute(
                        "UPDATE stakes SET amount = 0 "
                        "WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND symbol=$4",
                        uid, gid, vid, sym,
                    )
                    # Update circulating supply (same as update_stake does)
                    for _tbl in ("crypto_prices", "guild_tokens"):
                        await ctx.db.execute(
                            f"UPDATE {_tbl} SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                            "WHERE guild_id = $2 AND symbol = $3",
                            actual, gid, sym,
                        )
                    if net_short:
                        await ctx.db.update_wallet_holding(uid, gid, net_short, sym, actual)
                    else:
                        await ctx.db.update_holding(uid, gid, sym, actual)

                    if g_fee > 0:
                        g_fee_raw = to_raw(g_fee)
                        if net_short:
                            await ctx.db.update_wallet_holding(uid, gid, net_short, g_coin, -g_fee_raw)
                        else:
                            await ctx.db.update_holding(uid, gid, g_coin, -g_fee_raw)
                        total_gas_paid[g_coin] = total_gas_paid.get(g_coin, 0.0) + g_fee

                # Log transaction (mirrors single unstake)
                net_prefix = _NET_SHORT.get(gi["network"], "")
                tx_hash = await ctx.db.log_tx(
                    gid, uid, "UNSTAKE",
                    symbol_in=vid, amount_in=actual,
                    symbol_out=sym, amount_out=actual,
                    network=net_prefix,
                    gas_fee=to_raw(g_fee) if g_fee > 0 else 0, gas_coin=g_coin,
                )

                # Contribute gas to network vault for server level
                if g_fee > 0:
                    from services.vault import deposit_to_vault
                    _vault_net = net_prefix or _NET_SHORT.get(gi["network"], "")
                    if _vault_net:
                        await deposit_to_vault(ctx.db, gid, _vault_net, g_fee, bot=ctx.bot)

                net_label = f" [{gi['network']}]" if gi["network"] != "Unknown" else ""
                line = f"**{vid}**{net_label}: +{fmt_token(amt_human, sym)}"
                if g_fee > 0:
                    line += f"  ⛽ -{fmt_gas(g_fee, g_coin, g_emoji)}"
                result_lines.append(line)
            except Exception as exc:
                result_lines.append(f"**{vid}**: failed ({str(exc)[:50]})")

        result_desc = "\n".join(result_lines) or "Nothing unstaked."
        if total_gas_paid:
            gas_total_str = " + ".join(f"**{fmt_gas(v, k, Config.TOKENS.get(k, {}).get('emoji', ''))}**" for k, v in total_gas_paid.items())
            result_desc += f"\n\n⛽ **Total gas paid:** {gas_total_str}"

        result_embed = (
            card("🔓 Unstake Everything - Complete", color=C_SUCCESS)
            .description(result_desc)
            .build()
        )
        try:
            await msg.edit(embed=result_embed, view=None)
        except Exception:
            await ctx.reply(embed=result_embed, mention_author=False)

    @commands.hybrid_group(name="stake", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def stake_group(self, ctx: DiscoContext) -> None:
        """Unified staking: yield farms, validators, and Safety Module (VTR/DSY)."""
        if await suggest_subcommand(ctx, self.stake_group):
            return
        p = ctx.prefix or Config.PREFIX
        await ctx.reply(
            f"**Yield farming:** `{p}stake list` · `{p}stake farm` · `{p}stake unstake` · `{p}stake mine`\n"
            f"**Validators:** `{p}stake validator` (register, delegate, list, ...)\n"
            f"**Safety Module:** `{p}stake vtr deposit` · `{p}stake dsy deposit` "
            f"(unstake / withdraw / claim / status)\n"
            f"**Moon Network yield:** `{p}moon stake` (Lunar Mint) · `{p}moon pool stake` (Tier 2 basket)",
            mention_author=False,
        )

    @stake_group.command(name="list", aliases=["farmlist", "nodelist", "validators", "valis"])
    @guild_only
    async def node_list(self, ctx: DiscoContext) -> None:
        """List all yield farms grouped by network."""
        vs = await ctx.db.get_validators(ctx.guild_id)
        if not vs:
            await ctx.db.seed_validators(ctx.guild_id)
            vs = await ctx.db.get_validators(ctx.guild_id)
        # Filter to PoS networks only (exclude PoW networks like Sun Network)
        pos_networks = set(Config.NETWORK_STAKE_TOKEN.keys())
        vs = [v for v in vs if v.get("network") in pos_networks]
        if not vs:
            await ctx.reply_error("No validators found.")
            return

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)

        by_network: dict[str, list] = {}
        for v in vs:
            net = v.get("network") or "Unknown"
            by_network.setdefault(net, []).append(v)

        networks_in_order = sorted(by_network)
        page_by_network: dict[str, discord.Embed] = {}
        for network in networks_in_order:
            stake_token = await ctx.db.get_network_stake_token(ctx.guild_id, network) or "?"
            token_meta = all_tokens.get(stake_token, {})
            token_emoji = token_meta.get("emoji", "")
            consensus = token_meta.get("consensus", "PoS")

            _b = card(
                f"💎 Yield Farms  -  {network}",
                description=(
                    f"🌐 **Network:** {network}\n"
                    f"🪙 **Stake Token:** {token_emoji} `{stake_token}` ({consensus})\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=C_PURPLE,
            )
            # One field per validator, everything stacked in the body. The
            # old layout was 5 fields per validator (15 fields per page)
            # which Discord rendered as an illegible grid; this mirrors the
            # moon-stake panel pattern where each position gets a single
            # multiline field and relevant numbers line up vertically.
            for v in by_network[network]:
                uptime_bar   = FormatKit.bar(v['uptime_rate'], 1.0, width=8)
                heat_val     = float(v.get("heat") or 0.0)
                heat_label   = _format_heat(heat_val)
                heat_tilt    = heat_val * _HEAT_REWARD_TILT * 100
                slash_pct    = _slash_loss_rate_per_tick(v) * 100
                tilt_str     = f" (heat tilt **{heat_tilt:+.1f}%**)" if abs(heat_tilt) >= 0.1 else ""
                body = (
                    f"**ID:** `{v['validator_id']}`  ·  "
                    f"**APY:** `{v['reward_rate']*100:.1f}%/day`{tilt_str}\n"
                    f"🌡 Heat: {heat_label}\n"
                    f"⏱ Uptime: `{uptime_bar}`  ·  "
                    f"⚠️ Slash: `{slash_pct:.2f}%`/tick"
                )
                _b.field(f"{v['emoji']} {v['name']}", body, False)
            _b.footer(
                f"💡 {ctx.prefix}stake farm <FARM_ID> <amount> to start earning yield\n"
                f"🌕 Also earn on Moon Network: {ctx.prefix}moon stake <GROUP_TOKEN> "
                f"(Lunar Mint) · {ctx.prefix}moon pool stake (MTA/ARC/DSC/SUN basket yield)"
            )
            page_by_network[network] = _b.build()

        # Network dropdown -- same pattern as .crypto and .pool list. Single
        # network guilds fall back to a plain send since a one-option Select
        # is noise.
        if len(networks_in_order) <= 1:
            await send_paginated(ctx, list(page_by_network.values()))
            return

        _NET_EMOJIS = {
            "Sun Network":      "☀",
            "Moneta Chain":  "🔸",
            "Arcadia Network": "🔷",
            "Discoin Network":  "🪙",
            "Moon Network":     "\U0001F315",
        }
        first_net = networks_in_order[0]

        class FarmNetworkSelect(discord.ui.Select):
            def __init__(self_inner) -> None:
                options = [
                    discord.SelectOption(
                        label=net,
                        value=net,
                        emoji=_NET_EMOJIS.get(net, "🌐"),
                        description=f"{len(by_network[net])} farm(s)",
                        default=(net == first_net),
                    )
                    for net in networks_in_order
                ]
                super().__init__(
                    placeholder="Select a network…",
                    min_values=1, max_values=1,
                    options=options,
                )

            async def callback(self_inner, interaction: discord.Interaction) -> None:
                selected = self_inner.values[0]
                for opt in self_inner.options:
                    opt.default = (opt.value == selected)
                await interaction.response.edit_message(
                    embed=page_by_network[selected], view=view,
                )

        class FarmListView(discord.ui.View):
            def __init__(self_inner) -> None:
                super().__init__(timeout=120)
                self_inner.add_item(FarmNetworkSelect())

            async def on_timeout(self_inner) -> None:
                try:
                    for item in self_inner.children:
                        item.disabled = True
                    await msg.edit(view=self_inner)
                except Exception:
                    pass

        view = FarmListView()
        msg = await ctx.reply(embed=page_by_network[first_net], view=view, mention_author=False)

    @stake_group.command(name="farm", aliases=["stake", "node"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def node_stake(self, ctx: DiscoContext, arg1: str, arg2: str = "") -> None:
        """Deposit tokens into a yield farm. Accepts FARM_ID amount or amount FARM_ID."""
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}stake farm <FARM_ID> <amount>` or `{ctx.prefix}stake farm <amount> <FARM_ID>`")
            return
        validator_id, amount = parse_sym_amt(arg1, arg2)
        validator_id = validator_id.upper()

        v = await ctx.db.get_validator(validator_id, ctx.guild_id)
        if v is None:
            await ctx.reply_error(f"Unknown farm `{validator_id}`. Use `{ctx.prefix}stake list` to see options.")
            return

        network = v.get("network") or "Unknown"
        symbol  = await ctx.db.get_network_stake_token(ctx.guild_id, network)
        if not symbol:
            await ctx.reply_error(f"Validator `{validator_id}` has no configured network.")
            return

        net_short = _NET_SHORT.get(network, "")
        if not net_short or not await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, net_short):
            await ctx.reply_error_action(
                f"You need a DeFi wallet on **{network}** to stake.",
                f"Create {network} Wallet",
                f"wallet create {net_short or network.lower()}",
                rerun_original=True,
            )
            return

        holding    = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, symbol)
        avail_raw  = int(holding["amount"]) if holding else 0
        available  = to_human(avail_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            qty_raw = avail_raw
            qty = available
            if qty_raw == 0:
                await ctx.reply_error(f"You have no **{symbol}** available to stake.")
                return
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                _price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                qty = _parsed / float(_price_row["price"]) if _price_row and _price_row.get("price", 0) > 0 else 0.0
            else:
                qty = _parsed
            if not math.isfinite(qty) or qty <= 0:
                await ctx.reply_error("Amount must be a positive finite number.")
                return
            qty_raw = to_raw(qty)

        if qty_raw > avail_raw:
            await ctx.reply_error(
                f"You only have **{fmt_token(available, symbol)}** in your DeFi wallet.\n"
                f"**{v['name']}** is on **{network}**  -  only **{symbol}** is accepted."
            )
            return

        # Charge gas in network coin if validators are active on this network
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        active_v = [v2 for v2 in await ctx.db.get_pos_validators_for_network(ctx.guild_id, network) if v2["is_active"]]
        if active_v:
            gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, "stake", "medium", network)
            gas_cfg = Config.TOKENS.get(gas_coin, {})
            gas_em = gas_cfg.get("emoji", "●")
            gas_h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, gas_coin)
            gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
            if gas_bal < gas_fee:
                await ctx.reply_error(
                    f"Need **{fmt_gas(gas_fee, gas_coin, gas_em)}** for gas. You have **{fmt_gas(gas_bal, gas_coin, gas_em)}**."
                )
                return

        # When staking "all" and gas is the same token, reserve gas from the stake amount
        if _is_all and gas_fee > 0 and gas_coin == symbol:
            gas_raw = to_raw(gas_fee)
            qty_raw = max(0, avail_raw - gas_raw)
            qty = to_human(qty_raw)
            if qty_raw <= 0:
                await ctx.reply_error(
                    f"Insufficient balance  -  need at least **{fmt_gas(gas_fee, gas_coin, gas_em)}** for gas."
                )
                return

        # ── Confirmation ──────────────────────────────────────────────────────
        # Compute lockstone/job bonus for the yield preview
        _job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        _job_cfg = Config.JOBS.get(_job_row["job_id"] if _job_row else "HOMELESS", Config.JOBS["HOMELESS"])
        _pre_bonus = _job_cfg.get("perks", {}).get("stake_bonus", 0.0)
        _pre_hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        _pre_bonus += _item_stat(_pre_hashstone, "stake_bonus")
        _pre_lockstone = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
        _pre_bonus += _lockstone_stat(_pre_lockstone, "stake_bonus")

        hourly_yield = _stake_reward_per_day(qty, v["reward_rate"]) / 24
        _yield_hr_str = fmt_bonus(f"**{fmt_token(hourly_yield, symbol)}**", _pre_bonus)
        _yield_day_str = fmt_bonus(f"**{fmt_token(hourly_yield * 24, symbol)}**", _pre_bonus)
        _cb = (
            card(
                f"⚠️ Confirm Farm Deposit",
                description=(
                    f"{v['emoji']} **{v['name']}** on **{network}**\n"
                    f"Review before committing  -  deposit is **locked 24 hours**."
                ),
                color=C_AMBER,
            )
            .field("🌐 Network",      network,                                         True)
            .field("🪙 Token",         Config.currency_label(symbol, detail=True),      True)
            .field("💎 Staking",       f"**{fmt_token(qty, symbol)}**",                True)
            .field("⚡ Yield / hr",    _yield_hr_str,                                   True)
            .field("📊 Yield / day",   _yield_day_str,                                  True)
            .field("🔒 Lock Period",   "**24 hours**",                                  True)
        )
        if gas_fee > 0:
            _cb.field("⛽ Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        _cb.footer("Locked 24 h. Yield accrues each tick.")
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=_cb.build(), view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card(description="Deposit cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        if gas_fee > 0:
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, gas_coin, to_raw(-gas_fee))

        await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, symbol, -qty_raw)
        await ctx.db.update_stake(ctx.author.id, ctx.guild_id, validator_id, symbol, qty_raw)
        await ctx.db.insert_stake_batch(ctx.author.id, ctx.guild_id, validator_id, symbol, qty_raw)

        net_prefix = _NET_SHORT.get(network, "")
        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "STAKE",
            symbol_in=symbol, amount_in=qty_raw,
            symbol_out=validator_id, amount_out=qty_raw,
            network=net_prefix,
            gas_fee=to_raw(gas_fee) if gas_fee > 0 else 0, gas_coin=gas_coin,
        )
        await ctx.bot.bus.publish(
            "staked",
            guild=ctx.guild, user=ctx.author,
            validator_id=validator_id, symbol=symbol, amount=qty, tx_hash=tx_hash,
            gas_fee=gas_fee, gas_coin=gas_coin,
        )
        _usd = await _whale.usd_value_of(ctx.bot, symbol, qty, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "stake", _usd, symbol=symbol, amount=qty)

        _job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        _job_cfg = Config.JOBS.get(_job_row["job_id"] if _job_row else "HOMELESS", Config.JOBS["HOMELESS"])
        _stake_bonus = _job_cfg.get("perks", {}).get("stake_bonus", 0.0)
        hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        _stake_bonus += _item_stat(hashstone, "stake_bonus")
        lockstone = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
        _stake_bonus += _lockstone_stat(lockstone, "stake_bonus")
        _yield_hr = fmt_bonus(f"**{fmt_token(_stake_reward_per_day(qty, v['reward_rate']) / 24, symbol)}**/hr", _stake_bonus)
        _yield_day = fmt_bonus(f"**{fmt_token(_stake_reward_per_day(qty, v['reward_rate']), symbol)}**/day", _stake_bonus)
        _b = (
            card(f"💎 Staked Successfully", color=C_PURPLE)
            .field("💰 Staked",        f"**{fmt_token(qty, symbol)}**\n🪙 {Config.currency_label(symbol, detail=True)}", True)
            .field("⚡ Yield",         f"{_yield_hr}\n{_yield_day}", True)
            .field("🏦 Farm",          f"{v['emoji']} {v['name']}\n🌐 {network}",                      True)
        )
        if gas_fee > 0:
            _b.field("⛽ Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        embed = _b.build()
        set_tx(embed, ctx.guild_id, tx_hash, footer_extra=f"Network: {network} · Slash risk: {_slash_loss_rate_per_tick(v)*100:.2f}% per tick")
        await ctx.reply(embed=embed, mention_author=False)

    @stake_group.command(name="unstake", aliases=["unnode", "unfarm"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def node_unstake(self, ctx: DiscoContext, arg1: str, arg2: str = "") -> None:
        """Withdraw tokens from a yield farm. Accepts FARM_ID amount or amount FARM_ID.
        Use 'unstake everything' to withdraw all staked positions."""
        if arg1.lower() == "everything" and not arg2:
            await self._unstake_everything(ctx)
            return
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}stake unstake <FARM_ID> <amount>` or `{ctx.prefix}stake unstake <amount> <FARM_ID>`\nOr `{ctx.prefix}stake unstake everything` to withdraw all.")
            return
        validator_id, amount = parse_sym_amt(arg1, arg2)
        validator_id = validator_id.upper()

        v = await ctx.db.get_validator(validator_id, ctx.guild_id)
        if v is None:
            await ctx.reply_error(f"Unknown farm `{validator_id}`.")
            return

        network = v.get("network") or "Unknown"
        symbol = await ctx.db.get_network_stake_token(ctx.guild_id, network)
        if not symbol:
            await ctx.reply_error(f"Validator `{validator_id}` has no configured network.")
            return

        net_short = _NET_SHORT.get(network, "")

        # Get current stake
        stakes = await ctx.db.get_user_stakes(ctx.author.id, ctx.guild_id)
        current_raw = 0
        for s in stakes:
            if s["validator_id"] == validator_id and s["symbol"] == symbol:
                current_raw = int(s["amount"])
                break
        current = to_human(current_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            amt_raw = current_raw
            amt = current
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                _price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                amt = _parsed / float(_price_row["price"]) if _price_row and _price_row.get("price", 0) > 0 else 0.0
            else:
                amt = _parsed
            if not math.isfinite(amt):
                await ctx.reply_error("Amount must be a finite number.")
                return
            amt_raw = to_raw(amt)

        if amt_raw <= 0 or current_raw == 0:
            await ctx.reply_error(f"No `{symbol}` deposited with `{validator_id}`.")
            return

        if amt_raw > current_raw:
            await ctx.reply_error(f"You only have **{fmt_token(current, symbol)}** deposited here.")
            return

        # Enforce 24h lock  -  check per-batch countdowns
        now_ts = time.time()
        batches = await ctx.db.get_stake_batches(ctx.author.id, ctx.guild_id, validator_id)
        unlocked_total = sum(
            to_human(b["amount"]) for b in batches
            if (b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0))
               + STAKE_LOCK_SECS <= now_ts
        )
        # Any amount beyond what batches track (e.g. auto-compounded rewards) is free
        batch_total = sum(to_human(b["amount"]) for b in batches)
        unlocked_total += max(0.0, current - batch_total)

        if amt > unlocked_total + 0.01:
            locked_batches = [
                b for b in batches
                if (b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0))
                   + STAKE_LOCK_SECS > now_ts
            ]
            lines = []
            for b in sorted(locked_batches, key=lambda x: x["staked_at"]):
                _sa = b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0)
                secs_left = int(_sa + STAKE_LOCK_SECS - now_ts)
                h, r = divmod(max(secs_left, 0), 3600)
                m = r // 60
                lines.append(f"• {fmt_token(to_human(b['amount']), symbol)}  -  unlocks in **{h}h {m}m**")
            lock_str = "\n".join(lines) if lines else "All batches locked."
            avail_str = f"\n\n**Available to withdraw:** {fmt_token(unlocked_total, symbol)}" if unlocked_total > 0 else ""
            await ctx.reply_error(
                f"Not enough unlocked {symbol} to withdraw **{fmt_token(amt, symbol)}**.\n\n"
                f"**Locked batches:**\n{lock_str}{avail_str}"
            )
            return

        # Use oldest batch staked_at for early-unstake penalty window check
        staked_at = 0.0
        if batches:
            _sa = batches[0]["staked_at"]
            staked_at = _sa.timestamp() if hasattr(_sa, "timestamp") else float(_sa or 0.0)

        # Charge gas in network coin if validators are active on this network
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        active_v2 = [v2 for v2 in await ctx.db.get_pos_validators_for_network(ctx.guild_id, network) if v2["is_active"]]
        if active_v2:
            gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, "unstake", "medium", network)
            gas_cfg = Config.TOKENS.get(gas_coin, {})
            gas_em = gas_cfg.get("emoji", "●")
            gas_h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, gas_coin) if net_short else await ctx.db.get_holding(ctx.author.id, ctx.guild_id, gas_coin)
            gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
            # When gas coin == stake coin, allow gas to be covered by the returned tokens
            if gas_coin == symbol:
                if gas_bal < gas_fee:
                    # User has insufficient wallet balance  -  can gas come from the unstaked amount?
                    if amt < gas_fee:
                        await ctx.reply_error(
                            f"Gas fee is **{fmt_gas(gas_fee, gas_coin, gas_em)}** but you're only unstaking **{fmt_token(amt, symbol)}**  -  insufficient to cover gas."
                        )
                        return
                    # Gas will be deducted from the returned tokens after unstaking
            elif gas_bal < gas_fee:
                await ctx.reply_error(
                    f"Need **{fmt_gas(gas_fee, gas_coin, gas_em)}** for gas. You have **{fmt_gas(gas_bal, gas_coin, gas_em)}**."
                )
                return

        # Net amount user actually receives after gas (when gas paid from same token)
        gas_from_returned = gas_fee > 0 and gas_coin == symbol and (to_human(gas_h["amount"]) if gas_h else 0.0) < gas_fee
        net_received = amt - gas_fee if gas_from_returned else amt

        # Early unstake penalty
        early_penalty = 0.0
        penalty_msg = ""
        if staked_at > 0 and time.time() - staked_at < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            early_penalty = amt * Config.STAKING_EARLY_UNSTAKE_PENALTY
            net_received -= early_penalty
            penalty_msg = f"\n-{Config.STAKING_EARLY_UNSTAKE_PENALTY*100:.0f}% early unstake penalty: `{fmt_token(early_penalty, symbol)}` burned"

        # ── Confirmation ──────────────────────────────────────────────────────
        _cb = (
            card(
                f"⚠️ Confirm Farm Withdrawal",
                description=(
                    f"{v['emoji']} **{v['name']}** on **{network}**\n"
                    f"Yield stops immediately on the withdrawn amount."
                    + (penalty_msg if penalty_msg else "")
                ),
                color=C_AMBER,
            )
            .field("🌐 Network",        network,                                         True)
            .field("🪙 Token",           Config.currency_label(symbol, detail=True),      True)
            .field("💸 Withdrawing",     f"**{fmt_token(amt, symbol)}**",                True)
            .field("📥 You Receive",     f"**{fmt_token(net_received, symbol)}**",       True)
            .field("📉 Remaining Stake", f"**{fmt_token(current - amt, symbol)}**",      True)
        )
        if early_penalty > 0:
            _cb.field("🔥 Early Penalty", f"**{fmt_token(early_penalty, symbol)}** burned", True)
        if gas_fee > 0:
            _cb.field("⛽ Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        _cb.footer("Yield stops on the withdrawn amount.")
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=_cb.build(), view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card(description="Withdrawal cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        # Execution: atomic unstake + credit + gas deduction
        async with ctx.db.atomic():
            # Use the raw-scaled amount directly so "all" deducts the exact
            # deposit without float round-trip leaving dust behind.
            await ctx.db.update_stake(ctx.author.id, ctx.guild_id, validator_id, symbol, -amt_raw)
            credited = amt - early_penalty  # penalty is burned (not returned)
            if net_short:
                await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, symbol, to_raw(credited))
            else:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(credited))

            if gas_fee > 0:
                if net_short:
                    await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, gas_coin, to_raw(-gas_fee))
                else:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-gas_fee))

        net_prefix = _NET_SHORT.get(network, "")
        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "UNSTAKE",
            symbol_in=validator_id, amount_in=to_raw(amt),
            symbol_out=symbol, amount_out=to_raw(net_received),
            network=net_prefix,
            gas_fee=to_raw(gas_fee) if gas_fee > 0 else 0, gas_coin=gas_coin,
        )
        await ctx.bot.bus.publish(
            "unstaked",
            guild=ctx.guild, user=ctx.author,
            validator_id=validator_id, symbol=symbol, amount=amt, tx_hash=tx_hash,
            gas_fee=gas_fee, gas_coin=gas_coin,
        )
        _usd = await _whale.usd_value_of(ctx.bot, symbol, amt, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "unstake", _usd, symbol=symbol, amount=amt)

        _b = (
            card(f"📤 Unstaked Successfully", color=C_PURPLE)
            .field("🏦 Farm",     f"{v['emoji']} {v['name']}\n🌐 {network}",             True)
            .field("📥 Returned", f"**{fmt_token(net_received, symbol)}**\n🪙 {Config.currency_label(symbol, detail=True)}", True)
        )
        if early_penalty > 0:
            _b.field("🔥 Early Penalty", f"**{fmt_token(early_penalty, symbol)}** burned", True)
        if gas_fee > 0:
            _b.field("⛽ Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        embed = _b.build()
        set_tx(embed, ctx.guild_id, tx_hash, footer_extra=f"Network: {network}")
        await ctx.reply(embed=embed, mention_author=False)

    @stake_group.command(name="mine", aliases=["mynodes", "myfarms", "mystakes", "staked", "staking"])
    @guild_only
    @no_bots
    @ensure_registered
    async def node_mystakes(self, ctx: DiscoContext) -> None:
        """Show your active farm deposits and estimated daily yield."""
        uid = ctx.author.id
        gid = ctx.guild_id
        stakes = await ctx.db.get_user_stakes(uid, gid)
        lunar_rows = await ctx.db.get_lunar_stakes_for_user(uid, gid)
        moon_pool_row = await ctx.db.get_moon_stake(uid, gid)
        moon_pool_raw = int(moon_pool_row["amount"]) if moon_pool_row else 0

        # Safety Module positions (VTR/DSY single-token yield staking)
        sm_rows: list[tuple[str, dict]] = []
        for _sm_sym in ("VTR", "DSY"):
            _sm_row = await ctx.db.get_sm_stake(uid, gid, _sm_sym)
            if _sm_row and int(_sm_row.get("amount", 0)) > 0:
                sm_rows.append((_sm_sym, _sm_row))

        # Disc.Fun stakes (graduated proto tokens earning DFUN yield)
        try:
            from services import discfun as _df_for_mystakes
            df_rows = await _df_for_mystakes.list_user_stakes(
                ctx.db, gid, uid, accrue=False,
            )
        except Exception:
            df_rows = []

        if (not stakes and not lunar_rows and moon_pool_raw <= 0
                and not sm_rows and not df_rows):
            await ctx.reply_error_action(
                f"You have no active staking positions. Use `{ctx.prefix}stake farm <FARM_ID> <amount>`, "
                f"`{ctx.prefix}stake vtr deposit <amount>`, or `{ctx.prefix}stake dsy deposit <amount>` to start earning.",
                "View Farms",
                "stake list",
            )
            return

        by_network: dict[str, list] = {}
        total_daily: dict[str, float] = {}
        for s in stakes:
            network = s.get("network") or Config.VALIDATORS.get(s["validator_id"], {}).get("network", "") or "Other"
            by_network.setdefault(network, []).append(s)
            daily = s.h("amount") * s["reward_rate"] / max(Config.STAKING_REWARD_DIVISOR, 1e-9)
            total_daily[s["symbol"]] = total_daily.get(s["symbol"], 0.0) + daily

        summary = "  ".join(f"+{fmt_token(v, k)}" for k, v in total_daily.items()) if total_daily else ""

        _job_row2 = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        _job_cfg2 = Config.JOBS.get(_job_row2["job_id"] if _job_row2 else "HOMELESS", Config.JOBS["HOMELESS"])
        ls_bonus = _job_cfg2.get("perks", {}).get("stake_bonus", 0.0)
        hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        ls_bonus += _item_stat(hashstone, "stake_bonus")
        lockstone = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
        ls_bonus += _lockstone_stat(lockstone, "stake_bonus")

        # Pre-fetch batches for all stakes so we can show per-batch lock countdowns
        _now_ts = time.time()
        _batch_map: dict[str, list[dict]] = {}
        for s in stakes:
            vid = s["validator_id"]
            if vid not in _batch_map:
                _batch_map[vid] = await ctx.db.get_stake_batches(ctx.author.id, ctx.guild_id, vid)

        def _lock_line(vid: str, total_amt: float, symbol: str) -> str:
            batches = _batch_map.get(vid, [])
            locked = [
                b for b in batches
                if (b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0))
                   + STAKE_LOCK_SECS > _now_ts
            ]
            if not locked:
                return "🔓 All unlocked"
            parts = []
            for b in sorted(locked, key=lambda x: x["staked_at"]):
                _sa = b["staked_at"].timestamp() if hasattr(b["staked_at"], "timestamp") else float(b["staked_at"] or 0)
                secs_left = int(_sa + STAKE_LOCK_SECS - _now_ts)
                h, r = divmod(max(secs_left, 0), 3600)
                m = r // 60
                parts.append(f"{fmt_token(b.h('amount'), symbol)} in {h}h {m}m")
            return "🔒 " + " · ".join(parts)

        # Pre-fetch oracle prices for every staked symbol so each row can
        # render the staked + earned + daily-estimate balance in USD
        # alongside the token amount. Players asked to see the dollar
        # value of their positions inline on ``,stake mine``.
        _stake_symbols: set[str] = {s["symbol"] for s in stakes}
        _stake_prices: dict[str, float] = {}
        for _sym in _stake_symbols:
            try:
                _row = await ctx.db.get_price(_sym, gid)
                _stake_prices[_sym] = float(_row["price"]) if _row else 0.0
            except Exception:
                _stake_prices[_sym] = 0.0

        def _usd_str(amount_h: float, sym: str) -> str:
            px = _stake_prices.get(sym, 0.0)
            return f" (≈ {fmt_usd(amount_h * px)})" if px > 0 and amount_h > 0 else ""

        def _build_embed(net: str, net_stakes: list) -> discord.Embed:
            _b = (
                card(f"💼 My Staking Portfolio  -  {net}", color=C_PURPLE)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            )
            for s in net_stakes:
                daily = s.h("amount") * s["reward_rate"] / max(Config.STAKING_REWARD_DIVISOR, 1e-9)
                uptime_bar = FormatKit.bar(s["uptime_rate"], 1.0, width=8)
                _b.field(
                    f"{s['emoji']} {s['name']}",
                    f"🪙 {Config.currency_label(s['symbol'], detail=True)}",
                    False,
                )
                earned = float(s.get("session_earned") or 0)
                earned_str = (
                    f"\n💰 Earned: **{fmt_token(earned, s['symbol'])}**{_usd_str(earned, s['symbol'])}"
                    if earned > 0 else ""
                )
                lock_str = _lock_line(s["validator_id"], s.h("amount"), s["symbol"])
                staked_h = s.h("amount")
                _b.field(
                    "💎 Staked",
                    f"**{fmt_token(staked_h, s['symbol'])}**{_usd_str(staked_h, s['symbol'])}"
                    f"{earned_str}\n{lock_str}",
                    True,
                )
                daily_str = fmt_bonus(f"+{fmt_token(daily, s['symbol'])}", ls_bonus)
                _b.field(
                    "📊 Daily Est",
                    f"**{daily_str}**{_usd_str(daily, s['symbol'])}",
                    True,
                )
                _b.field("⏱ Uptime",    f"`{uptime_bar}`",                                True)
            _b.footer(f"📈 Est. total daily yield: {summary}")
            return _b.build()

        has_moons = bool(lunar_rows) or moon_pool_raw > 0

        # Lunar Mint embed (staked group tokens earning MOON hourly)
        lunar_embed: discord.Embed | None = None
        if lunar_rows:
            _all_tokens = await ctx.db.get_all_tokens_for_guild(gid)
            _lb = (
                card("🌕 Lunar Mint Positions", color=C_PURPLE)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description("Stake group tokens on Moon Network, earn MOON hourly.")
            )
            # Lunar staked = group token (any sym); session/lifetime = MOON.
            # Pre-fetch oracle prices for both legs so the panel shows
            # USD value alongside each token amount.
            _lunar_syms: set[str] = {row["symbol"] for row in lunar_rows} | {"MOON"}
            _lunar_prices: dict[str, float] = {}
            for _ls_sym in _lunar_syms:
                try:
                    _ls_row = await ctx.db.get_price(_ls_sym, gid)
                    _lunar_prices[_ls_sym] = float(_ls_row["price"]) if _ls_row else 0.0
                except Exception:
                    _lunar_prices[_ls_sym] = 0.0

            def _ls_usd(amt: float, sym: str) -> str:
                px = _lunar_prices.get(sym, 0.0)
                return f" (≈ {fmt_usd(amt * px)})" if px > 0 and amt > 0 else ""

            for row in lunar_rows:
                sym = row["symbol"]
                token_emoji = _all_tokens.get(sym, {}).get("emoji", "")
                _session = float(row.get("session_earned") or 0)
                _lifetime = float(row.get("total_earned") or 0)
                _staked_h = row.h("amount")
                _lb.field(
                    f"{token_emoji} {sym}",
                    (
                        f"💎 Staked: **{fmt_token(_staked_h, sym)}**{_ls_usd(_staked_h, sym)}\n"
                        f"💰 Session: `{fmt_token(_session, 'MOON')}`{_ls_usd(_session, 'MOON')}\n"
                        f"🏆 Lifetime: `{fmt_token(_lifetime, 'MOON')}`{_ls_usd(_lifetime, 'MOON')}"
                    ),
                    True,
                )
            _lb.footer(
                f"Open/top up: {ctx.prefix}moon stake <SYM> <amount>  ·  "
                f"Close: {ctx.prefix}moon unstake <SYM>"
            )
            lunar_embed = _lb.build()

        # Moon Pool embed (staked MOON earning MTA/ARC/DSC/SUN basket from Moon Network revenue)
        moon_pool_embed: discord.Embed | None = None
        if moon_pool_raw > 0:
            pool_total_raw = await ctx.db.get_moon_pool_total_raw(gid)
            share = (moon_pool_raw / pool_total_raw) if pool_total_raw > 0 else 0.0
            distributable = await ctx.db.get_moon_vault_distributable(gid)
            _session_h = float(moon_pool_row.get("session_earned") or 0) if moon_pool_row else 0.0
            _lifetime_h = float(moon_pool_row.get("total_earned") or 0) if moon_pool_row else 0.0
            moon_pool_embed = (
                card("🌕 Moon Pool Position", color=C_PURPLE)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description("Stake MOON, earn a basket of MTA / ARC / DSC / SUN from Moon Network trade revenue.")
                .field("💎 Staked", f"**{fmt_token(to_human(moon_pool_raw), 'MOON')}**", True)
                .field("🏖 Pool Share", f"**{fmt_pct(share * 100)}**", True)
                .field("🏛 Vault USD", f"**{fmt_usd(distributable)}**", True)
                .field("💰 Session", f"`{fmt_usd(_session_h)}`", True)
                .field("🏆 Lifetime", f"`{fmt_usd(_lifetime_h)}`", True)
                .footer(
                    f"Open/top up: {ctx.prefix}moon pool stake <amount>  ·  "
                    f"Close: {ctx.prefix}moon pool unstake"
                )
                .build()
            )

        # Safety Module embed (VTR/DSY single-token yield staking)
        sm_embed: discord.Embed | None = None
        if sm_rows:
            _smb = (
                card("🛡 Safety Module Positions", color=C_TEAL)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description("Stake VTR or DSY for protocol-fee yield. 24h unstake cooldown.")
            )
            for _sym, _row in sm_rows:
                _cfg = _SM[_sym]
                _emoji = Config.TOKENS[_sym]["emoji"]
                _yt = _cfg["yield_token"]
                _yt_emoji = Config.TOKENS.get(_yt, {}).get("emoji", "")
                _staked_h = to_human(int(_row["amount"]))
                _pending_h, _daily_h, _is_auto = await self._sm_pending_yield(ctx, _sym)
                _cd = sm_cooldown_remaining(_row)
                if _row.get("cooldown_at"):
                    _status = (
                        f"🔒 Cooldown: {_cd/3600:.1f}h left"
                        if _cd > 0
                        else f"✅ Ready: `{ctx.prefix}stake {_sym.lower()} withdraw`"
                    )
                else:
                    _status = "🔁 Auto-compounding" if _is_auto else "✅ Earning"
                _smb.field(
                    f"{_emoji} {_sym}",
                    (
                        f"💎 Staked: **{fmt_token(_staked_h, _sym, _emoji)}**\n"
                        f"💰 Pending: **{fmt_token(_pending_h, _yt, _yt_emoji)}**\n"
                        f"📊 Daily: `{fmt_token(_daily_h, _yt, _yt_emoji)}`\n"
                        f"{_status}"
                    ),
                    True,
                )
            _smb.footer(
                f"Manage: {ctx.prefix}stake vtr deposit/unstake/withdraw/claim/status  ·  "
                f"{ctx.prefix}stake dsy ..."
            )
            sm_embed = _smb.build()

        # Single-network NPC farm path with no Moons / Safety Module positions:
        # preserve legacy single-embed reply for the simple case.
        if len(by_network) == 1 and not has_moons and sm_embed is None:
            net, net_stakes = next(iter(by_network.items()))
            await ctx.reply(embed=_build_embed(net, net_stakes), mention_author=False)
            return

        categories: dict[str, list[discord.Embed]] = {
            f"\U0001f310 {net}": [_build_embed(net, net_stakes)]
            for net, net_stakes in sorted(by_network.items())
        }
        if lunar_embed is not None:
            categories["🌕 Lunar Mint"] = [lunar_embed]
        if moon_pool_embed is not None:
            categories["🌕 Moon Pool"] = [moon_pool_embed]
        if sm_embed is not None:
            categories["🛡 Safety Module"] = [sm_embed]

        # Disc.Fun stakes embed (graduated proto tokens -> DFUN yield)
        df_embed: discord.Embed | None = None
        if df_rows:
            try:
                from services import discfun as _df_mystakes
                df_apy = await _df_mystakes.current_staking_apy_pct(ctx.db, gid)
                df_dfun_usd_row = await ctx.db.get_price("DFUN", gid)
                df_dfun_usd = (
                    float(df_dfun_usd_row["price"]) if df_dfun_usd_row else 0.0
                )
                _dfb = (
                    card(
                        f"🎢 Disc.Fun Stakes  ·  live APY {df_apy:,.1f}% (variable)",
                        color=C_PURPLE,
                    )
                    .author(
                        ctx.author.display_name,
                        icon_url=ctx.author.display_avatar.url,
                    )
                    .description(
                        f"Yield in DFUN, emission-based & variable. "
                        f"Manage with `{ctx.prefix}fun stakes` / "
                        f"`{ctx.prefix}fun claim` / "
                        f"`{ctx.prefix}fun unstake SYMBOL <amt|all>`."
                    )
                )
                total_value_dfun = 0.0
                total_pending_dfun = 0.0
                for r in df_rows:
                    sym = str(r.get("symbol") or "")
                    amt_raw = int(r.get("amount") or 0)
                    pending_raw = int(r.get("pending_dfun") or 0)
                    is_ac = bool(r.get("auto_compound"))
                    spot = await _df_mystakes._amm_spot_dfun(ctx.db, gid, sym)
                    staked_h = amt_raw / SCALE if amt_raw else 0.0
                    pending_h = pending_raw / SCALE if pending_raw else 0.0
                    value_dfun = staked_h * spot
                    total_value_dfun += value_dfun
                    total_pending_dfun += pending_h
                    ac = "🔁 AUTO" if is_ac else "⚙️ manual"
                    usd_value = (
                        f" ({fmt_usd(value_dfun * df_dfun_usd)})"
                        if df_dfun_usd > 0 and value_dfun > 0 else ""
                    )
                    pending_usd = (
                        f" ({fmt_usd(pending_h * df_dfun_usd)})"
                        if df_dfun_usd > 0 and pending_h > 0 else ""
                    )
                    _dfb.field(
                        f"{sym}  ·  {ac}",
                        (
                            f"💎 Staked: **{staked_h:,.4f} {sym}**\n"
                            f"≈ `{value_dfun:,.4f} DFUN`{usd_value}\n"
                            f"💰 Pending: `{pending_h:,.4f} DFUN`{pending_usd}"
                        ),
                        True,
                    )
                total_usd = (
                    f"  ≈ {fmt_usd(total_value_dfun * df_dfun_usd)}"
                    if df_dfun_usd > 0 else ""
                )
                pending_usd = (
                    f"  ≈ {fmt_usd(total_pending_dfun * df_dfun_usd)}"
                    if df_dfun_usd > 0 else ""
                )
                _dfb.footer(
                    f"Total staked: {total_value_dfun:,.4f} DFUN{total_usd}  ·  "
                    f"Pending: {total_pending_dfun:,.4f} DFUN{pending_usd}"
                )
                df_embed = _dfb.build()
            except Exception:
                log.exception("mystakes: Disc.Fun section failed gid=%s uid=%s", gid, uid)
                df_embed = None

        if df_embed is not None:
            categories["🎢 Disc.Fun"] = [df_embed]

        await CategoryPaginator.send(ctx, categories)

    # ══════════════════════════════════════════════════════════════════════════
    # VALIDATOR SUBGROUP  -  /stake validator ...
    # ══════════════════════════════════════════════════════════════════════════

    @stake_group.group(name="validator", aliases=["val", "v"], invoke_without_command=True)
    async def validator_group(self, ctx: DiscoContext) -> None:
        """Validator commands. Use subcommands like register, unregister, delegate, etc."""
        if await suggest_subcommand(ctx, self.validator_group):
            return
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @validator_group.command(name="register", aliases=["vreg", "vregister"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def vregister(self, ctx: DiscoContext, network: str, amount: str) -> None:
        """Register as a validator by staking tokens on a network.

        Usage: .stake validator register <network> <amount|all>
        Example: .stake validator register Arcadia 500
        """
        network = _normalize_network(network)

        # Block PoW networks  -  SUN is mined, not staked
        net_tokens = Config.TOKENS
        for _sym, _cfg in net_tokens.items():
            if _cfg.get("network") == network and _cfg.get("consensus") == "PoW":
                await ctx.reply_error(f"**{network}** uses Proof of Work. Validators cannot be registered on PoW networks  -  use mining instead.")
                return

        stake_token = await self.bot.db.get_network_stake_token(ctx.guild.id, network)
        if not stake_token:
            await ctx.reply_error(f"Unknown network `{network}`. Check `{ctx.prefix}stake validator networks` for options.")
            return

        net_short = _NET_SHORT.get(network, "")
        if not net_short or not await self.bot.db.has_defi_wallet(ctx.author.id, ctx.guild.id, net_short):
            await ctx.reply_error_action(
                f"You need a DeFi wallet on **{network}** to register as a validator.",
                f"Create {network} Wallet",
                f"wallet create {net_short or network.lower()}",
                rerun_original=True,
            )
            return

        holding = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token)
        have_raw = int(holding["amount"]) if holding else 0
        have = to_human(have_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            amount_raw = have_raw
            amount_val = have
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                _price_row = await self.bot.db.get_price(stake_token, ctx.guild.id)
                amount_val = _parsed / float(_price_row["price"]) if _price_row and _price_row.get("price", 0) > 0 else 0.0
            else:
                amount_val = _parsed
            amount_raw = to_raw(amount_val)

        if amount_val < MIN_STAKE:
            await ctx.reply_error(f"Minimum stake is **{MIN_STAKE:,.0f} {stake_token}**.")
            return

        if amount_raw > have_raw:
            await ctx.reply_error(
                f"You need **{amount_val:,.4f} {stake_token}** in your DeFi wallet but only have **{have:,.4f}**."
            )
            return

        all_my_validators = await self.bot.db.get_pos_validators_for_user(ctx.author.id, ctx.guild.id)
        registered_on_other = [v for v in all_my_validators if v["network"] != network and v["is_active"]]
        if registered_on_other:
            nets = ", ".join(v["network"] for v in registered_on_other)
            await ctx.reply_error(
                f"You are already registered as a validator on **{nets}**.\n"
                f"Each player may only run one validator. Unregister that one first with `{ctx.prefix}stake validator unregister`."
            )
            return

        existing = await self.bot.db.get_pos_validator(ctx.author.id, ctx.guild.id, network)
        if existing:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -amount_raw)
            await self.bot.db.update_pos_validator_stake(ctx.author.id, ctx.guild.id, network, amount_raw)
            # Reactivate if inactive (e.g. previously unregistered or slashed)
            if not existing["is_active"]:
                await self.bot.db.reactivate_pos_validator(ctx.author.id, ctx.guild.id, network)
            new_total = existing.h("stake_amount") + amount_val
            embed = (
                card(
                    "🔒 Validator Stake Increased" if existing["is_active"] else "✅ Validator Re-activated",
                    description=f"🌐 **{network}** · {'Top-up accepted and locked.' if existing['is_active'] else 'Validator re-registered and locked.'}",
                    color=C_SUCCESS,
                )
                .field("➕ Added",     f"**{amount_val:,.4f} {stake_token}**",  True)
                .field("💎 New Total", f"**{new_total:,.4f} {stake_token}**",   True)
                .field("🔒 Status",    "Locked 24 h",                           True)
                .build()
            )
        else:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -amount_raw)
            lock_until = time.time() + STAKE_LOCK_SECS
            await self.bot.db.create_pos_validator(
                ctx.author.id, ctx.guild.id, network, stake_token, amount_raw, lock_until
            )
            await self.bot.bus.publish(
                "validator_registered",
                guild=ctx.guild, user=ctx.author,
                network=network, stake_token=stake_token, amount=amount_val,
            )
            embed = (
                card(
                    "✅ Validator Registered",
                    description=(
                        f"You are now an active validator on **{network}**!\n"
                        f"You'll be selected to process blocks proportionally to your stake."
                    ),
                    color=C_SUCCESS,
                )
                .field("🌐 Network",     network,                                True)
                .field("💎 Staked",      f"**{amount_val:,.4f} {stake_token}**", True)
                .field("🔒 Locked For",  "**24 hours**",                          True)
                .field("🏆 Gas Reward",  f"**{VALIDATOR_REWARD*100:.0f}%** of block gas fees",  True)
                .field("🏛 Treasury Cut", f"**{TREASURY_CUT*100:.0f}%** to treasury",            True)
                .build()
            )
        await ctx.reply(embed=embed, mention_author=False)

    @validator_group.command(name="unregister", aliases=["vunreg", "vunregister", "deactivate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vunregister(self, ctx: DiscoContext, network: str) -> None:
        """Withdraw your validator stake from a network.

        Usage: .stake validator unregister <network>
        """
        network = _normalize_network(network)
        v = await self.bot.db.get_pos_validator(ctx.author.id, ctx.guild.id, network)
        if not v:
            await ctx.reply_error(f"You are not a validator on **{network}**.")
            return

        _slu = v["stake_locked_until"]
        _slu_ts = _slu.timestamp() if hasattr(_slu, "timestamp") else _slu
        if _slu_ts and time.time() < _slu_ts:
            remaining = int(_slu_ts - time.time())
            hours = remaining // 3600
            mins  = (remaining % 3600) // 60
            await ctx.reply_error(
                f"Your stake is locked for another **{hours}h {mins}m**."
            )
            return

        delegation_rows = await self.bot.db.wipe_delegations_for_validator(
            ctx.author.id, ctx.guild.id, network
        )
        net_short = _NET_SHORT.get(network, "")
        for d in delegation_rows:
            if net_short:
                await self.bot.db.update_wallet_holding(d["delegator_id"], ctx.guild.id, net_short, d["token"], d["amount"])
            else:
                await self.bot.db.update_holding(d["delegator_id"], ctx.guild.id, d["token"], d["amount"])

        amount = v["stake_amount"]
        token  = v["stake_token"]
        if net_short:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, token, amount)
        else:
            await self.bot.db.update_holding(ctx.author.id, ctx.guild.id, token, amount)
        await self.bot.db.deactivate_pos_validator(ctx.author.id, ctx.guild.id, network)

        _uw = (
            card(
                "🔓 Validator Stake Withdrawn",
                description=f"You are no longer a validator on **{network}**.",
                color=C_WARNING,
            )
            .field("🌐 Network",   network,                               True)
            .field("📤 Returned",  f"**{amount:,.4f} {token}**",         True)
            .field("📊 Status",    "Deactivated",                         True)
        )
        if delegation_rows:
            _uw.field("↩️ Delegators Refunded", f"**{len(delegation_rows)}** delegator(s) returned their stake", False)
        embed = _uw.build()
        await ctx.reply(embed=embed, mention_author=False)

    @validator_group.command(name="commission", aliases=["vcommission", "vcomm", "vsetcommission"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vsetcommission(self, ctx: DiscoContext, network: str, rate: float) -> None:
        """Set your validator's commission rate  -  the % of gas you keep (rest goes to delegators).

        Usage: .stake validator commission <network> <rate>
        Rate is a percentage: 60-90 (e.g. 80 = you keep 80%, delegators share 20%)
        """
        network = _normalize_network(network)
        v = await self.bot.db.get_pos_validator(ctx.author.id, ctx.guild.id, network)
        if not v or not v["is_active"]:
            await ctx.reply_error(f"You are not an active validator on **{network}**.")
            return

        MIN_COMMISSION, MAX_COMMISSION = 0.30, 0.90
        rate_frac = rate / 100.0
        if not (MIN_COMMISSION <= rate_frac <= MAX_COMMISSION):
            await ctx.reply_error(
                f"Commission must be between **60%** and **90%**.\n"
                f"(You keep that %, delegators share the rest.)"
            )
            return

        await self.bot.db.set_commission_rate(ctx.author.id, ctx.guild.id, network, rate_frac)
        delegator_share = 100.0 - rate
        embed = (
            card(
                "⚙️ Commission Rate Updated",
                description=f"🌐 **{network}** validator commission adjusted.",
                color=C_SUCCESS,
            )
            .field("🏆 You Keep",        f"**{rate:.0f}%** of gas earnings",          True)
            .field("👥 Delegators Share", f"**{delegator_share:.0f}%** proportionally", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Delegation Commands ────────────────────────────────────────────────────

    @validator_group.command(name="delegate", aliases=["vdel", "vdelegate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vdelegate(self, ctx: DiscoContext, validator_member: discord.Member, network: str, amount: str) -> None:
        """Delegate stake to a PoS validator and earn a share of their gas rewards.

        Usage: .stake validator delegate <@validator> <network> <amount|all>
        """
        network = _normalize_network(network)

        if validator_member.id == ctx.author.id:
            await ctx.reply_error("You cannot delegate to yourself.")
            return

        v = await self.bot.db.get_pos_validator(validator_member.id, ctx.guild.id, network)
        if not v or not v["is_active"]:
            await ctx.reply_error(f"{validator_member.mention} is not an active validator on **{network}**.")
            return

        stake_token = v["stake_token"]
        net_short = _NET_SHORT.get(network, "")
        if not net_short or not await self.bot.db.has_defi_wallet(ctx.author.id, ctx.guild.id, net_short):
            await ctx.reply_error_action(
                f"You need a DeFi wallet on **{network}** to delegate.",
                f"Create {network} Wallet",
                f"wallet create {net_short or network.lower()}",
                rerun_original=True,
            )
            return

        holding = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token)
        have_raw = int(holding["amount"]) if holding else 0
        have = to_human(have_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            amount_raw = have_raw
            amount_val = have
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                _price_row = await self.bot.db.get_price(stake_token, ctx.guild.id)
                amount_val = _parsed / float(_price_row["price"]) if _price_row and _price_row.get("price", 0) > 0 else 0.0
            else:
                amount_val = _parsed
            amount_raw = to_raw(amount_val)

        MAX_DELEGATIONS = 3
        existing_dels = await self.bot.db.get_user_delegations(ctx.author.id, ctx.guild.id)
        active_dels = [d for d in existing_dels if d["amount"] > 0 and d["validator_user_id"] != validator_member.id]
        if len(active_dels) >= MAX_DELEGATIONS:
            await ctx.reply_error(
                f"You already have **{len(active_dels)} active delegations** (max {MAX_DELEGATIONS}).\n"
                "Undelegate from one before delegating to another."
            )
            return

        gas_coin, gas_fee = await gas_fee_for_network(self.bot.db, ctx.guild.id, "delegate", "medium", network)
        gas_cfg = Config.TOKENS.get(gas_coin, {})
        gas_em = gas_cfg.get("emoji", "●")
        gas_h = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, gas_coin)
        gas_bal = gas_h.h("amount") if gas_h else 0.0

        gas_raw = to_raw(gas_fee) if (gas_fee > 0 and gas_coin == stake_token) else 0
        # When delegating "all" with same-coin gas, reserve gas from the delegation amount
        if _is_all and gas_raw > 0:
            amount_raw = max(0, amount_raw - gas_raw)
            amount_val = to_human(amount_raw)

        if amount_val < MIN_DELEGATION:
            await ctx.reply_error(f"Minimum delegation is **{MIN_DELEGATION:,.0f} {stake_token}**.")
            return

        need_raw = amount_raw + gas_raw
        if need_raw > have_raw:
            await ctx.reply_error(
                f"You need **{amount_val:,.4f} {stake_token}** + gas in your DeFi wallet but only have **{have:,.4f}**."
            )
            return
        if gas_coin != stake_token and gas_bal < gas_fee:
            await ctx.reply_error(
                f"Need **{fmt_gas(gas_fee, gas_coin, gas_em)}** for gas. You have **{fmt_gas(gas_bal, gas_coin, gas_em)}**."
            )
            return

        _b = (
            card(
                "🔒 Confirm Delegation",
                description=(
                    f"Delegate to {validator_member.mention} on **{network}**?\n"
                    f"⚠️ Slashing risk  -  if the validator is slashed, your delegation is too."
                ),
                color=C_WARNING,
            )
            .field("🌐 Network",    network,                               True)
            .field("💎 Delegating", f"**{amount_val:,.4f} {stake_token}**", True)
            .field("🔒 Lock",        "**24 hours**",                        True)
        )
        if gas_fee > 0:
            _b.field("⛽ Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=_b.build(), view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Delegation cancelled.", color=C_ERROR).build())
            return

        if gas_fee > 0:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, gas_coin, -to_raw(gas_fee))

        lock_until = time.time() + DELEGATION_LOCK_SECS
        await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -amount_raw)
        new_total_raw = await self.bot.db.create_or_add_delegation(
            ctx.author.id, validator_member.id, ctx.guild.id, network, stake_token, amount_raw, lock_until
        )
        new_total = to_human(int(new_total_raw))
        embed = (
            card("✅ Delegation Confirmed", color=C_SUCCESS)
            .field("🌐 Network",      network,                                True)
            .field("👤 Validator",    validator_member.mention,               True)
            .field("💎 Delegated",    f"**{amount_val:,.4f} {stake_token}**", True)
            .field("📊 Your Total",   f"**{new_total:,.4f} {stake_token}**",  True)
            .field("🔒 Lock",          "24 hours",                             True)
            .build()
        )
        await msg.edit(embed=embed, view=None)

    @validator_group.command(name="undelegate", aliases=["vundel", "vundelegate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vundelegate(self, ctx: DiscoContext, validator_member: discord.Member, network: str, amount: str = "all") -> None:
        """Withdraw your delegation from a PoS validator.

        Usage: .stake validator undelegate <@validator> <network> [amount|all]
        """
        network = _normalize_network(network)

        d = await self.bot.db.get_delegation(ctx.author.id, validator_member.id, ctx.guild.id, network)
        if not d or int(d["amount"] or 0) <= 0:
            await ctx.reply_error(f"You have no active delegation to {validator_member.mention} on **{network}**.")
            return

        d_amount_raw = int(d["amount"])
        d_amount = to_human(d_amount_raw)

        _lu = d["locked_until"]
        _lu_ts = _lu.timestamp() if hasattr(_lu, 'timestamp') else _lu
        if time.time() < _lu_ts:
            remaining = int(_lu_ts - time.time())
            hours = remaining // 3600
            mins  = (remaining % 3600) // 60
            await ctx.reply_error(f"Your delegation is locked for another **{hours}h {mins}m**.")
            return

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            amount_raw_undel = d_amount_raw
            amount_val = d_amount
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                _price_row = await self.bot.db.get_price(d["token"], ctx.guild.id)
                amount_val = _parsed / float(_price_row["price"]) if _price_row and _price_row.get("price", 0) > 0 else 0.0
            else:
                amount_val = _parsed
            amount_raw_undel = to_raw(amount_val)
            if amount_raw_undel > d_amount_raw:
                await ctx.reply_error(f"You only have **{d_amount:,.4f} {d['token']}** delegated.")
                return

        embed = (
            card(
                "🔓 Confirm Undelegation",
                description=f"Withdraw delegation from {validator_member.mention} on **{network}**?",
                color=C_WARNING,
            )
            .field("🌐 Network",    network,                                   True)
            .field("💸 Withdrawing", f"**{amount_val:,.4f} {d['token']}**",   True)
            .build()
        )
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Undelegation cancelled.", color=C_ERROR).build())
            return

        net_short = _NET_SHORT.get(network, "")
        await self.bot.db.remove_delegation(ctx.author.id, validator_member.id, ctx.guild.id, network, amount_raw_undel)
        if net_short:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, d["token"], amount_raw_undel)
        else:
            await self.bot.db.update_holding(ctx.author.id, ctx.guild.id, d["token"], amount_raw_undel)

        embed = (
            card("✅ Undelegation Confirmed", color=C_SUCCESS)
            .field("🌐 Network",   network,                                   True)
            .field("📥 Returned",  f"**{amount_val:,.4f} {d['token']}**",    True)
            .field("🏦 Wallet",    "DeFi wallet",                             True)
            .build()
        )
        await msg.edit(embed=embed, view=None)

    @validator_group.command(name="delegations", aliases=["mydels", "mydelegations"])
    @guild_only
    @no_bots
    @ensure_registered
    async def my_delegations(self, ctx: DiscoContext) -> None:
        """View your active validator delegations."""
        delegations = await self.bot.db.get_user_delegations(ctx.author.id, ctx.guild.id)
        if not delegations:
            await ctx.reply_error(f"You have no active delegations. Use `{ctx.prefix}stake validator delegate` to delegate to a validator.")
            return

        _b = card("🔒 Your Delegations", color=C_PURPLE)
        now = time.time()
        total_usd = 0.0

        for d in delegations:
            validator_mention = mention(d['validator_user_id'], ctx.guild)
            _lu2 = d["locked_until"]
            _lu2_ts = _lu2.timestamp() if hasattr(_lu2, 'timestamp') else _lu2
            lock_remaining = max(0, int(_lu2_ts - now))
            lock_str = (
                f"🔒 {lock_remaining // 3600}h {(lock_remaining % 3600) // 60}m left"
                if lock_remaining > 0
                else "✅ Unlocked"
            )

            price_row = await self.bot.db.get_price(d["token"], ctx.guild.id)
            price = float(price_row["price"]) if price_row else 0.0
            d_amount_human = d.h("amount")
            usd_val = d_amount_human * price
            sess_earned = d.h("session_earned") if d.get("session_earned") else d.h("total_earned")
            earned_usd = sess_earned * price
            total_usd += usd_val

            usd_str = f" ≈ ${usd_val:,.2f}" if price > 0 else ""
            earned_str = (
                f"**+{sess_earned:,.6f} {d['token']}**"
                + (f" ≈ ${earned_usd:,.2f}" if price > 0 else "")
            )

            _b.field(
                f"👤 {validator_mention}",
                f"🌐 {d['network']}",
                False,
            )
            _b.field("💎 Delegated",  f"**{d_amount_human:,.6f} {d['token']}**{usd_str}", True)
            _b.field("🏆 Earned",      earned_str,                                       True)
            _b.field("🔒 Lock",         lock_str,                                         True)

        if total_usd > 0:
            _b.footer(f"📊 Total delegated value ≈ ${total_usd:,.2f}")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @validator_group.command(name="list", aliases=["vlist", "vals"])
    @guild_only
    async def validators_list(self, ctx: DiscoContext, network: str | None = None) -> None:
        """Show active validators with stats for informed delegation decisions.

        Usage: .stake validator list [network]
        """
        all_v = await self.bot.db.get_pos_validators(ctx.guild.id)
        if network:
            network = _normalize_network(network)
            all_v = [v for v in all_v if network.lower() in v["network"].lower()]

        active = [v for v in all_v if v["is_active"]]

        if not active:
            await ctx.reply_error(f"No active validators. Be the first with `{ctx.prefix}stake validator register`!")
            return

        by_net: dict[str, list] = {}
        for v in active:
            by_net.setdefault(v["network"], []).append(v)

        # Max 3 validators per embed page (3 × 7 fields = 21 fields, safely under Discord's 25-field limit)
        _VALS_PER_PAGE = 3
        pages = []
        for net, net_validators in sorted(by_net.items()):
            net_validators.sort(key=lambda x: x["stake_amount"], reverse=True)
            total_stake_raw = sum(v["stake_amount"] for v in net_validators)
            total_stake_h = to_human(int(total_stake_raw))
            stake_token_label = net_validators[0]['stake_token'] if net_validators else ''

            chunks = [
                net_validators[i:i + _VALS_PER_PAGE]
                for i in range(0, len(net_validators), _VALS_PER_PAGE)
            ]

            for page_num, chunk in enumerate(chunks, 1):
                page_suffix = f" (Page {page_num}/{len(chunks)})" if len(chunks) > 1 else ""
                _b = card(
                    f"🌐 Validators  -  {net}{page_suffix}",
                    description=(
                        f"**{len(net_validators)}** active validator{'s' if len(net_validators) != 1 else ''}  ·  "
                        f"Total staked: **{total_stake_h:,.2f} {stake_token_label}**\n"
                        f"⚠️ Blocks process only when **{MIN_VALIDATORS}+** validators are active."
                    ),
                    color=C_PURPLE,
                )

                for v in chunk:
                    dels = await self.bot.db.get_delegations_for_validator(v["user_id"], ctx.guild.id, net)
                    del_count  = len(dels)
                    del_total  = sum(float(d["amount"]) for d in dels)
                    eff_stake  = float(v["stake_amount"]) + del_total
                    pct        = eff_stake / (total_stake_raw + del_total) * 100 if (total_stake_raw + del_total) > 0 else 0
                    weight_bar = FormatKit.bar(pct, 100.0, width=8)

                    commission = v.get("commission_rate", DELEGATION_VALIDATOR_KEEP)
                    del_share  = round((1.0 - commission) * 100)

                    lock_str = ""
                    _slu_d = v["stake_locked_until"]
                    _slu_d_ts = _slu_d.timestamp() if hasattr(_slu_d, "timestamp") else _slu_d
                    if _slu_d_ts and time.time() < _slu_d_ts:
                        remaining = int(_slu_d_ts - time.time())
                        lock_str = f"  🔒 {remaining//3600}h {(remaining%3600)//60}m locked"

                    slashes = v.get("slash_count", 0)
                    slash_str = f"⚠️ {slashes}/{MAX_SLASH_COUNT} slashes" if slashes > 0 else "✅ Clean record"

                    _b.field(
                        mention(v['user_id'], ctx.guild),
                        f"📊 Weight: `{weight_bar}`{lock_str}",
                        False,
                    )
                    _b.field("💎 Own Stake",    f"**{to_human(int(v['stake_amount'] or 0)):,.2f}** {v['stake_token']}", True)
                    _b.field("👥 Delegated",    f"**{to_human(int(del_total)):,.2f}** ({del_count} del.)",              True)
                    _b.field("⚙️ Commission",   f"Keeps **{commission*100:.0f}%** → you earn **{del_share}%**", True)
                    _b.field("🏆 Blocks",       f"**{v['total_blocks_validated']:,}** confirmed",    True)
                    _b.field("🛡 Slashes",      slash_str,                                            True)

                _b.footer(
                    "💡 Lower commission = more for delegators  ·  "
                    ".stake validator delegate @user <network> <amount>"
                )
                pages.append(_b.build())

        await send_paginated(ctx, pages)

    @validator_group.command(name="mempool")
    @guild_only
    async def mempool_view(self, ctx: DiscoContext, network: str | None = None) -> None:
        """View pending actions in the mempool.

        Usage: .stake validator mempool [network]
        """
        from core.framework.network import SHORT_TO_FULL as _NET_FULL
        _VALID_FULL = set(_NET_SHORT.keys())

        if network:
            if network.lower() in _NET_FULL:
                network = _NET_FULL[network.lower()]
            elif network not in _VALID_FULL:
                valid_list = ", ".join(
                    f"`{short}` ({full})" for full, short in _NET_SHORT.items()
                )
                await ctx.reply_error(
                    f"Unknown network **`{network}`**.\nValid options: {valid_list}"
                )
                return

        pending = await self.bot.db.get_pending_mempool(
            ctx.guild.id, network, limit=25
        )

        if not pending:
            embed = card(
                "📭 Mempool Empty",
                description="No pending actions" + (f" on **{network}**" if network else "") + ".",
                color=C_NEUTRAL,
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        by_net: dict[str, list] = {}
        for a in pending:
            by_net.setdefault(a["network"], []).append(a)

        _b = card(
            "⏳ Mempool" + (f"  -  {network}" if network else ""),
            description=f"🔗 **{len(pending)}** pending action{'s' if len(pending) != 1 else ''} · sorted by gas price",
            color=C_INFO,
        ).footer(f"⛽ High gas = faster inclusion  ·  New block every ~{VALIDATOR_TICK}s")

        now = time.time()

        for net, actions in by_net.items():
            lines = []
            for a in actions[:12]:
                tier_emoji = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\U0001f7e2"}.get(a["gas_price"], "\u26aa")

                try:
                    p = json.loads(a["payload"]) if isinstance(a["payload"], str) else a["payload"]
                except Exception:
                    p = {}

                atype = a["action_type"].upper()
                if a["action_type"] == "swap":
                    amt_in  = p.get("amount_in", "?")
                    tok_in  = p.get("token_in", "?")
                    tok_out = p.get("token_out", "?")
                    detail = f"`{amt_in} {tok_in} \u2192 {tok_out}`"
                elif a["action_type"] == "send":
                    amt    = p.get("amount", "?")
                    sym    = p.get("symbol", "?")
                    to_id  = p.get("to_user_id")
                    to_str = mention(to_id, ctx.guild) if to_id else "?"
                    detail = f"`{amt} {sym}` \u2192 {to_str}"
                else:
                    detail = ""

                age_s = int(now - a["submitted_at"]) if a.get("submitted_at") else 0
                if age_s < 60:
                    age_str = f"{age_s}s ago"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m ago"
                else:
                    age_str = f"{age_s // 3600}h ago"

                _net_coin = gas_coin_for_network(net)
                _nc_cfg = Config.TOKENS.get(_net_coin, {})
                _nc_emoji = _nc_cfg.get("emoji", "\u25cf")
                lines.append(
                    f"{tier_emoji} `#{a['id']}` **{atype}** {detail}\n"
                    f"   \u2514 {mention(a['user_id'], ctx.guild)} \u00b7 \u26fd **{to_human(int(a['gas_fee'])):.8f} {_nc_emoji}{_net_coin}** \u00b7 {age_str}"
                )

            if len(actions) > 12:
                lines.append(f"*\u2026and {len(actions) - 12} more*")

            # Discord embed fields are limited to 1024 chars
            body = ""
            for line in lines:
                if len(body) + len(line) + 1 > 1000:
                    body += "\n*…truncated*"
                    break
                body += ("\n" if body else "") + line

            _b.field(
                f"🔗 {net}  ·  {len(actions)} pending tx{'s' if len(actions) != 1 else ''}",
                body or " - ",
                False,
            )

        await ctx.reply(embed=_b.build(), mention_author=False)

    @validator_group.command(name="submit", aliases=["vsubmit"], hidden=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def vsubmit(
        self,
        ctx: DiscoContext,
        action_type: str,
        network: str,
        gas_price: str,
        *,
        payload_str: str = "{}",
    ) -> None:
        """Manually submit an action to the mempool.

        Usage: .stake validator submit <action_type> <network> <gas_price> [payload_json]
        Example: .stake validator submit send Arcadia high {"to_user_id": 123, "symbol": "ARC", "amount": 0.5}

        Note: Most actions are submitted automatically via normal commands.
        This is for advanced use or testing.
        """
        action_type = action_type.lower()
        gas_price   = gas_price.lower()

        if gas_price not in GAS_TIERS:
            tiers = ", ".join(f"`{t}`" for t in GAS_TIERS)
            await ctx.reply_error(f"Gas price must be {tiers}.")
            return

        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            await ctx.reply_error("Invalid JSON payload.")
            return

        gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild.id, action_type, gas_price, network)

        h = await ctx.db.get_holding(ctx.author.id, ctx.guild.id, gas_coin)
        gas_balance = to_human(h["amount"]) if h else 0.0
        if gas_balance < gas_fee:
            coin_cfg = Config.TOKENS.get(gas_coin, {})
            emoji = coin_cfg.get("emoji", "\u25cf")
            await ctx.reply_error(
                f"Need **{gas_fee:.8f} {emoji}{gas_coin}** for gas. You have **{gas_balance:.8f}**."
            )
            return

        await ctx.db.update_holding(ctx.author.id, ctx.guild.id, gas_coin, to_raw(-gas_fee))

        action_id = await ctx.db.add_to_mempool(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            network=network,
            action_type=action_type,
            payload=payload,
            gas_price=gas_price,
            gas_fee=to_raw(gas_fee),
        )

        coin_cfg = Config.TOKENS.get(gas_coin, {})
        emoji = coin_cfg.get("emoji", "\u25cf")
        embed = (
            card(
                "📨 Action Queued",
                description=f"Your `{action_type.upper()}` is in the **{network}** mempool.",
                color=C_INFO,
            )
            .field("🌐 Network",    network,                                     True)
            .field("⚡ Action",     f"`{action_type.upper()}`",                  True)
            .field("🔢 Mempool ID", f"`#{action_id}`",                           True)
            .field("⛽ Gas Price",  f"**{gas_price.title()}**",                  True)
            .field("💸 Gas Fee",   f"`{gas_fee:.8f}` {emoji}{gas_coin}",         True)
            .footer("⏳ Included in the next validator block")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @validator_group.command(name="networks", aliases=["vnetworks"])
    @guild_only
    async def vnetworks(self, ctx: DiscoContext) -> None:
        """Show all networks that support validator staking."""
        lines = []
        for net, token in Config.NETWORK_STAKE_TOKEN.items():
            net_validators = await self.bot.db.get_pos_validators_for_network(ctx.guild.id, net)
            active_count = sum(1 for v in net_validators if v["is_active"])
            total_stake = sum(v["stake_amount"] for v in net_validators if v["is_active"])
            gc = gas_coin_for_network(net)
            gc_cfg = Config.TOKENS.get(gc, {})
            gc_emoji = gc_cfg.get("emoji", "\u25cf")
            base = await self.bot.db.get_base_fee(ctx.guild.id, net)
            lines.append(
                f"**{net}**  -  Stake: `{token}` | Gas: {gc_emoji}`{gc}` | "
                f"Base fee: `{_fmt_fee(base)}` | "
                f"Validators: {active_count} | "
                f"Total Staked: {total_stake:,.2f}"
            )

        embed = (
            card(
                "🌐 Validator Networks",
                description="\n".join(lines) if lines else "No networks configured.",
                color=C_PURPLE,
            )
            .footer("Register: .stake validator register <network> <amount>")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @validator_group.command(name="stats", aliases=["vstats"])
    @guild_only
    @ensure_registered
    async def vstats(self, ctx: DiscoContext) -> None:
        """View your validator statistics."""
        net_validators = await self.bot.db.get_user_pos_validators(ctx.author.id, ctx.guild.id)
        if not net_validators:
            await ctx.reply_error(f"You are not registered as a validator. Use `{ctx.prefix}stake validator register` to start.")
            return

        lockstone = await self.bot.db.get_lockstone(ctx.author.id, ctx.guild.id)
        ls_bonus = _lockstone_stat(lockstone, "stake_bonus")

        _b = card("📊 Your Validator Stats", color=C_PURPLE)
        for v in net_validators:
            status_str = "✅ Active" if v["is_active"] else "❌ Inactive"
            lock_str = "✅ Unlocked"
            _slu_e = v["stake_locked_until"]
            _slu_e_ts = _slu_e.timestamp() if hasattr(_slu_e, "timestamp") else _slu_e
            if _slu_e_ts and time.time() < _slu_e_ts:
                remaining = int(_slu_e_ts - time.time())
                lock_str = f"🔒 {remaining//3600}h {(remaining%3600)//60}m left"
            slashes = v.get("slash_count", 0)
            slash_str = f"⚠️ {slashes}/{MAX_SLASH_COUNT}" if slashes > 0 else "✅ Clean"
            _b.field(
                f"🔗 {v['network']}",
                status_str,
                False,
            )
            _b.field("💎 Stake",        f"**{v['stake_amount']:,.4f}** {v['stake_token']}",  True)
            _b.field("🏆 Blocks",        f"**{v['total_blocks_validated']:,}** validated",   True)
            earned_str = fmt_bonus(f"{v['total_rewards_earned']:,.4f} USD", ls_bonus, "Lockstone")
            _b.field("💰 Earned",        f"**{earned_str}**",                                 True)
            _b.field("🛡 Slashes",       slash_str,                                            True)
            _b.field("🔒 Lock Status",   lock_str,                                             True)
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    # SAFETY MODULE SUBGROUPS  -  ,stake vtr / ,stake dsy
    # ══════════════════════════════════════════════════════════════════════════
    # VTR and DSY are single-token yield-staking positions modelled on the
    # real Vantor V2 Safety Module: stake VTR -> earn USDC, stake DSY -> earn
    # DSD, both with a 24h unstake cooldown. They live on ,stake so players
    # see all yield-bearing positions (yield farms, validators, safety
    # module) under one command tree.

    async def _sm_pending_yield(
        self, ctx: DiscoContext, symbol: str,
    ) -> tuple[float, float, bool]:
        """Return (pending_yield_h, daily_rate_h, is_auto_compound).

        Amounts are in yield-token units (USDC/DSD) even for auto-compound
        positions so callers can show a consistent USD-equivalent display.
        """
        row = await ctx.db.get_sm_stake(ctx.author.id, ctx.guild_id, symbol)
        if not row or int(row["amount"]) == 0 or row.get("cooldown_at"):
            return 0.0, 0.0, False
        cfg = _SM[symbol]
        yield_token = cfg["yield_token"]
        is_auto = bool(row.get("auto_compound", False))
        ly = row["last_yield"]
        last_ts = ly.timestamp() if hasattr(ly, "timestamp") else float(ly)
        elapsed_days = (time.time() - last_ts) / 86400.0
        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            return 0.0, 0.0, is_auto
        token_price = float(price_row["price"])
        if yield_token in ("USDC", "DSD", "USD"):
            yield_price = 1.0
        else:
            yp_row = await ctx.db.get_price(yield_token, ctx.guild_id)
            yield_price = float(yp_row["price"]) if yp_row else 1.0
        staked_h = to_human(int(row["amount"]))
        staked_usd = staked_h * token_price
        # Dynamic rate based on current guild TVL.
        total_raw = await ctx.db.get_sm_total_staked(ctx.guild_id, symbol)
        total_usd = to_human(total_raw) * token_price
        daily_rate = sm_current_daily_rate(total_usd, cfg)
        daily_h = staked_usd * daily_rate / yield_price if yield_price > 0 else 0.0
        pending_h = daily_h * elapsed_days
        return pending_h, daily_h, is_auto

    async def _sm_status_embed(self, ctx: DiscoContext, symbol: str) -> discord.Embed:
        cfg = _SM[symbol]
        net = cfg["network"]
        yield_token = cfg["yield_token"]
        emoji = Config.TOKENS[symbol]["emoji"]
        yield_emoji = Config.TOKENS.get(yield_token, {}).get("emoji", "")
        network_name = "Arcadia Network" if net == "arc" else "Discoin Network"
        cooldown_h = cfg["cooldown_secs"] / 3600
        max_apy = cfg.get("max_apy_pct", 10000.0)
        min_apy = cfg.get("min_apy_pct", 50.0)

        row = await ctx.db.get_sm_stake(ctx.author.id, ctx.guild_id, symbol)
        total_raw = await ctx.db.get_sm_total_staked(ctx.guild_id, symbol)
        sub = symbol.lower()

        # Fetch price once for APY + USD display.
        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        token_price = float(price_row["price"]) if price_row else 0.0
        total_h = to_human(total_raw)
        total_usd = total_h * token_price
        live_apy = sm_current_apy_pct(total_usd, cfg)

        if not row or int(row.get("amount", 0)) == 0:
            empty_total_usd_str = (
                f"  ({fmt_usd(total_usd)})"
                if total_h > 0 and token_price > 0 else ""
            )
            return (
                card(f"{emoji} {symbol} Safety Module")
                .color(C_TEAL)
                .description(
                    f"You have no {symbol} staked in the Safety Module.\n\n"
                    f"**How it works:**\n"
                    f"- Stake {symbol} to earn {yield_emoji}{yield_token} yield\n"
                    f"- APY is live: **{min_apy:.0f}% -- {max_apy:,.0f}%** "
                    f"(floor guaranteed, compresses from max as TVL grows)\n"
                    f"- Enable **auto-compound** to re-stake yield hourly "
                    f"instead of claiming manually\n"
                    f"- {cooldown_h:.0f}h unstake cooldown before withdrawal\n"
                    f"- Up to {cfg['slash_rate']*100:.0f}% can be slashed during a "
                    f"shortfall event\n"
                    f"- Network: {network_name}"
                )
                .field("Current APY", f"**{live_apy:,.1f}%**", True)
                .field(
                    "Total Staked (server)",
                    fmt_token(total_h, symbol, emoji) + empty_total_usd_str,
                    True,
                )
                .footer(
                    f"APY = ${cfg.get('emission_usd_per_day', 50000):.0f}/day emission / TVL -- "
                    f"{min_apy:.0f}% floor, {max_apy:,.0f}% max.  "
                    f"Unstake -> {cooldown_h:.0f}h cooldown -> withdraw.  "
                    f"Up to {cfg['slash_rate']*100:.0f}% slashable.  "
                    f"{ctx.prefix}stake {sub} deposit <amount> to begin"
                )
                .build()
            )

        staked_h = to_human(int(row["amount"]))
        pending_h, daily_h, is_auto = await self._sm_pending_yield(ctx, symbol)
        cd_remaining = sm_cooldown_remaining(row)
        color = C_WARNING if row.get("cooldown_at") else C_TEAL

        if yield_token in ("USDC", "DSD", "USD"):
            yield_price = 1.0
        else:
            yp_row = await ctx.db.get_price(yield_token, ctx.guild_id)
            yield_price = float(yp_row["price"]) if yp_row else 0.0

        def _with_usd(amount: float, price: float) -> str:
            return f"  ({fmt_usd(amount * price)})" if amount > 0 and price > 0 else ""

        pending_label = "Pending Compound" if is_auto else "Pending Yield"
        pending_note  = f"\n-# re-stakes as {symbol}" if is_auto else ""

        b = (
            card(f"{emoji} {symbol} Safety Module")
            .color(color)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field(
                "Staked",
                fmt_token(staked_h, symbol, emoji)
                + _with_usd(staked_h, token_price),
                True,
            )
            .field(
                pending_label,
                fmt_token(pending_h, yield_token, yield_emoji)
                + _with_usd(pending_h, yield_price)
                + pending_note,
                True,
            )
            .field(
                "Daily Rate",
                fmt_token(daily_h, yield_token, yield_emoji)
                + _with_usd(daily_h, yield_price)
                + f"/day",
                True,
            )
            .field("Current APY", f"**{live_apy:,.1f}%**", True)
            .field(
                "Auto-Compound",
                "**ON** (hourly re-stake)" if is_auto else
                f"**OFF** -- use `{ctx.prefix}stake {sub} autocompound` to enable",
                True,
            )
        )
        if row.get("cooldown_at"):
            if cd_remaining > 0:
                b.field(
                    "Cooldown",
                    f"Active -- {fmt_rel(row['cooldown_at'])} started\n"
                    f"{cd_remaining/3600:.1f}h remaining",
                    True,
                )
            else:
                b.field("Cooldown", f"Done! Use `{ctx.prefix}stake {sub} withdraw` to claim.", True)
        else:
            b.field("Cooldown", "None active", True)
        b.field(
            "Total Staked (server)",
            fmt_token(total_h, symbol, emoji) + _with_usd(total_h, token_price),
            True,
        )
        b.footer(
            f"APY = ${cfg.get('emission_usd_per_day', 500):.0f}/day emission / TVL -- "
            f"max {max_apy:,.0f}%.  "
            f"Unstake -> {cooldown_h:.0f}h cooldown -> withdraw.  "
            f"Up to {cfg['slash_rate']*100:.0f}% slashable.  "
            f"{ctx.prefix}stake {sub} autocompound / claim / unstake / withdraw"
        )
        b.timestamp()
        return b.build()

    async def _sm_deposit(self, ctx: DiscoContext, symbol: str, amount: str) -> None:
        cfg = _SM[symbol]
        if amount.lower() in ("status", "info", "pos"):
            await ctx.reply(embed=await self._sm_status_embed(ctx, symbol), mention_author=False)
            return
        # Track the user's raw balance separately for the ``all``/``max``
        # path so we can pass it straight to stake_sm() without going
        # through float -- raw -> to_human -> to_raw can drift by a few
        # raw units for balances that don't have an exact float
        # representation, producing "have 100, need 100" off-by-1s.
        amount_raw_override: int | None = None
        if amount.lower() in ("all", "max"):
            holding = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, cfg["network"], symbol,
            )
            bal_raw = int(holding["amount"]) if holding else 0
            amount_raw_override = bal_raw
            amount_f = to_human(bal_raw)
        else:
            try:
                amount_f = float(amount)
            except ValueError:
                await ctx.reply_error(f'"{amount}" is not a valid amount. Use a number or `all`.')
                return
        result = await stake_sm(
            ctx.db, ctx.guild_id, ctx.author.id, symbol, amount_f,
            amount_raw=amount_raw_override,
        )
        if not result.success:
            await ctx.reply_error(result.error)
            return
        emoji = Config.TOKENS[symbol]["emoji"]
        yield_token = cfg["yield_token"]
        sub = symbol.lower()
        # Compute live APY post-deposit so the confirmation shows real current rate.
        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        token_price = float(price_row["price"]) if price_row else 0.0
        total_raw = await ctx.db.get_sm_total_staked(ctx.guild_id, symbol)
        total_usd = to_human(total_raw) * token_price
        live_apy = sm_current_apy_pct(total_usd, cfg)
        embed = (
            card(f"{emoji} {symbol} Staked")
            .color(C_SUCCESS)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("Staked", fmt_token(amount_f, symbol, emoji), True)
            .field("Yield Token", yield_token, True)
            .field("Current APY", f"**{live_apy:,.1f}%**", True)
            .field("Cooldown", f"{cfg['cooldown_secs']//3600}h to unstake", True)
            .field("Slash Risk", f"{cfg['slash_rate']*100:.0f}% in shortfall event", True)
            .footer(
                f"{ctx.prefix}stake {sub} autocompound to enable auto re-staking  |  "
                f"{ctx.prefix}stake {sub} status to view position"
            )
            .timestamp()
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _sm_unstake(self, ctx: DiscoContext, symbol: str) -> None:
        result = await sm_begin_unstake(ctx.db, ctx.guild_id, ctx.author.id, symbol)
        if not result.success:
            await ctx.reply_error(result.error)
            return
        cfg = _SM[symbol]
        emoji = Config.TOKENS[symbol]["emoji"]
        sub = symbol.lower()
        embed = (
            card(f"{emoji} Unstake Cooldown Started")
            .color(C_WARNING)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .description(
                f"Your {fmt_token(result.amount, symbol, emoji)} will be available "
                f"to withdraw in **{cfg['cooldown_secs']//3600}h**.\n\n"
                "Note: yield accrual is paused during the cooldown period."
            )
            .footer(f"{ctx.prefix}stake {sub} withdraw after the cooldown expires")
            .timestamp()
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _sm_withdraw(self, ctx: DiscoContext, symbol: str) -> None:
        result = await withdraw_sm(ctx.db, ctx.guild_id, ctx.author.id, symbol)
        if not result.success:
            await ctx.reply_error(result.error)
            return
        cfg = _SM[symbol]
        emoji = Config.TOKENS[symbol]["emoji"]
        net_full = "Arcadia DeFi wallet" if cfg["network"] == "arc" else "Discoin Network DeFi wallet"
        embed = (
            card(f"{emoji} {symbol} Withdrawn")
            .color(C_SUCCESS)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("Returned to DeFi Wallet", fmt_token(result.amount, symbol, emoji), True)
            .footer(f"{symbol} returned to your {net_full}")
            .timestamp()
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _sm_claim(self, ctx: DiscoContext, symbol: str) -> None:
        results = await apply_sm_yield(
            ctx.db, ctx.guild_id, symbol, user_id=ctx.author.id,
        )
        user_result = next((r for r in results if r["user_id"] == ctx.author.id), None)
        if not user_result:
            row = await ctx.db.get_sm_stake(ctx.author.id, ctx.guild_id, symbol)
            if not row or int(row.get("amount", 0)) == 0:
                await ctx.reply_error(f"You have no {symbol} staked.")
            elif row.get("cooldown_at"):
                await ctx.reply_error("Yield is paused during the unstake cooldown.")
            else:
                await ctx.reply_error("Nothing to claim yet (yield accumulates over time).")
            return
        cfg = _SM[symbol]
        emoji = Config.TOKENS[symbol]["emoji"]
        if user_result.get("auto_compound"):
            compounded_h = to_human(user_result["compounded_raw"])
            embed = (
                card(f"{emoji} Yield Compounded")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("Re-staked", fmt_token(compounded_h, symbol, emoji), True)
                .footer(
                    f"Auto-compound is ON -- yield re-staked as {symbol} into your position.  "
                    f"{ctx.prefix}stake {symbol.lower()} autocompound to disable."
                )
                .timestamp()
                .build()
            )
        else:
            yield_token = cfg["yield_token"]
            yield_h = to_human(user_result["yield_amount_raw"])
            net_full = "Arcadia DeFi wallet" if cfg["network"] == "arc" else "Discoin Network DeFi wallet"
            embed = (
                card(f"{emoji} Yield Claimed")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("Received", fmt_token(yield_h, yield_token), True)
                .footer(f"{yield_token} deposited to your {net_full}")
                .timestamp()
                .build()
            )
        await ctx.reply(embed=embed, mention_author=False)

    async def _sm_autocompound(self, ctx: DiscoContext, symbol: str) -> None:
        row = await ctx.db.get_sm_stake(ctx.author.id, ctx.guild_id, symbol)
        if not row or int(row.get("amount", 0)) == 0:
            await ctx.reply_error(f"You have no {symbol} staked in the Safety Module.")
            return
        current = bool(row.get("auto_compound", False))
        new_val = not current
        await ctx.db.set_sm_auto_compound(ctx.author.id, ctx.guild_id, symbol, new_val)
        cfg = _SM[symbol]
        emoji = Config.TOKENS[symbol]["emoji"]
        yield_token = cfg["yield_token"]
        sub = symbol.lower()
        if new_val:
            title = f"{emoji} Auto-Compound Enabled"
            msg = (
                f"Yield is now **re-staked as {symbol}** every hour automatically.\n"
                f"Your position grows without any manual action.\n\n"
                f"-# Disable with `{ctx.prefix}stake {sub} autocompound` at any time."
            )
        else:
            title = f"{emoji} Auto-Compound Disabled"
            msg = (
                f"Yield will now be paid as **{yield_token}** to your DeFi wallet.\n"
                f"Use `{ctx.prefix}stake {sub} claim` to collect it manually.\n\n"
                f"-# Re-enable with `{ctx.prefix}stake {sub} autocompound` at any time."
            )
        await ctx.reply_success(msg, title=title)

    # ── ,stake vtr subgroup ───────────────────────────────────────────────

    @stake_group.group(name="vtr", invoke_without_command=True)
    @guild_only
    async def aave_group(self, ctx: DiscoContext) -> None:
        """VTR Safety Module: stake VTR to earn USDC yield."""
        if await suggest_subcommand(ctx, self.aave_group):
            return
        await ctx.reply(embed=await self._sm_status_embed(ctx, "VTR"), mention_author=False)

    @aave_group.command(name="deposit", aliases=["stake"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def aave_deposit(self, ctx: DiscoContext, amount: str) -> None:
        """Stake VTR in the Safety Module to earn USDC yield."""
        await self._sm_deposit(ctx, "VTR", amount)

    @aave_group.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def aave_unstake(self, ctx: DiscoContext) -> None:
        """Begin the 24h unstake cooldown. Yield stops during cooldown."""
        await self._sm_unstake(ctx, "VTR")

    @aave_group.command(name="withdraw")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def aave_withdraw(self, ctx: DiscoContext) -> None:
        """Withdraw staked VTR back to your DeFi wallet after the cooldown."""
        await self._sm_withdraw(ctx, "VTR")

    @aave_group.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(30)
    async def aave_claim(self, ctx: DiscoContext) -> None:
        """Manually trigger a yield distribution for your VTR stake."""
        await self._sm_claim(ctx, "VTR")

    @aave_group.command(name="autocompound", aliases=["ac", "compound"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def aave_autocompound(self, ctx: DiscoContext) -> None:
        """Toggle auto-compounding for your VTR stake (re-stakes yield hourly)."""
        await self._sm_autocompound(ctx, "VTR")

    @aave_group.command(name="status", aliases=["info", "pos"])
    @guild_only
    @no_bots
    @ensure_registered
    async def aave_status(self, ctx: DiscoContext) -> None:
        """View your VTR Safety Module position."""
        await ctx.reply(embed=await self._sm_status_embed(ctx, "VTR"), mention_author=False)

    # ── ,stake dsy subgroup ────────────────────────────────────────────────

    @stake_group.group(name="dsy", invoke_without_command=True)
    @guild_only
    async def dsy_group(self, ctx: DiscoContext) -> None:
        """DSY Safety Module: stake DSY to earn DSD yield."""
        if await suggest_subcommand(ctx, self.dsy_group):
            return
        await ctx.reply(embed=await self._sm_status_embed(ctx, "DSY"), mention_author=False)

    @dsy_group.command(name="deposit", aliases=["stake"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def dsy_deposit(self, ctx: DiscoContext, amount: str) -> None:
        """Stake DSY in the Safety Module to earn DSD yield."""
        await self._sm_deposit(ctx, "DSY", amount)

    @dsy_group.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def dsy_unstake(self, ctx: DiscoContext) -> None:
        """Begin the 24h unstake cooldown for DSY. Yield stops during cooldown."""
        await self._sm_unstake(ctx, "DSY")

    @dsy_group.command(name="withdraw")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def dsy_withdraw(self, ctx: DiscoContext) -> None:
        """Withdraw staked DSY back to your DeFi wallet after the cooldown."""
        await self._sm_withdraw(ctx, "DSY")

    @dsy_group.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(30)
    async def dsy_claim(self, ctx: DiscoContext) -> None:
        """Manually trigger a yield distribution for your DSY stake."""
        await self._sm_claim(ctx, "DSY")

    @dsy_group.command(name="autocompound", aliases=["ac", "compound"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def dsy_autocompound(self, ctx: DiscoContext) -> None:
        """Toggle auto-compounding for your DSY stake (re-stakes yield hourly)."""
        await self._sm_autocompound(ctx, "DSY")

    @dsy_group.command(name="status", aliases=["info", "pos"])
    @guild_only
    @no_bots
    @ensure_registered
    async def dsy_status(self, ctx: DiscoContext) -> None:
        """View your DSY Safety Module position."""
        await ctx.reply(embed=await self._sm_status_embed(ctx, "DSY"), mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    # HIDDEN PREFIX-ONLY ALIASES for backward compatibility
    # ══════════════════════════════════════════════════════════════════════════

    @commands.command(name="yield", hidden=True)
    async def _alias_farm(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Shorthand alias for ,stake farm (DeFi yield farming)."""
        parts = args.split() if args else []
        if len(parts) >= 2:
            await self.node_stake(ctx, parts[0], parts[1])
        else:
            await ctx.reply_error("Usage: `,yield <FARM_ID> <amount>` or `,stake farm <FARM_ID> <amount>`")

    @commands.command(name="node", hidden=True)
    async def _alias_node(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Backward-compat alias for ,stake farm (legacy)."""
        parts = args.split() if args else []
        if len(parts) >= 2:
            await self.node_stake(ctx, parts[0], parts[1])
        else:
            await ctx.reply_error("Usage: `,stake farm <FARM_ID> <amount>`")

    @commands.command(name="uyield", hidden=True)
    async def _alias_unfarm(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Shorthand alias for ,stake unstake (DeFi yield farming)."""
        parts = args.split() if args else []
        if len(parts) >= 2:
            await self.node_unstake(ctx, parts[0], parts[1])
        else:
            await ctx.reply_error("Usage: `,uyield <FARM_ID> <amount>` or `,stake unstake <FARM_ID> <amount>`")

    @commands.command(name="unnode", hidden=True)
    async def _alias_unnode(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Backward-compat alias for ,stake unstake (legacy)."""
        parts = args.split() if args else []
        if len(parts) >= 2:
            await self.node_unstake(ctx, parts[0], parts[1])
        else:
            await ctx.reply_error("Usage: `,stake unstake <FARM_ID> <amount>`")

    @commands.command(name="yieldlist", hidden=True)
    async def _alias_farmlist(self, ctx: DiscoContext) -> None:
        """Shorthand alias for ,stake list (DeFi yield farms)."""
        await self.node_list(ctx)

    @commands.command(name="nodelist", hidden=True)
    async def _alias_nodelist(self, ctx: DiscoContext) -> None:
        """Backward-compat alias for ,stake list (legacy)."""
        await self.node_list(ctx)

    @commands.command(name="myyields", hidden=True)
    async def _alias_myfarms(self, ctx: DiscoContext) -> None:
        """Shorthand alias for ,stake mine (your DeFi positions)."""
        await self.node_mystakes(ctx)

    @commands.command(name="mynodes", hidden=True)
    async def _alias_mynodes(self, ctx: DiscoContext) -> None:
        """Backward-compat alias for .stake mine (legacy)"""
        await self.node_mystakes(ctx)

    @commands.command(name="mystakes", hidden=True)
    async def _alias_mystakes(self, ctx: DiscoContext) -> None:
        """Top-level alias for .stake mine"""
        await self.node_mystakes(ctx)

    @commands.command(name="vregister", hidden=True)
    async def _alias_vregister(self, ctx: DiscoContext, network: str = "", amount: str = "") -> None:
        """Backward-compat alias for .stake validator register"""
        if not network or not amount:
            await ctx.reply_error("Usage: `.vregister <network> <amount>`")
            return
        await self.vregister(ctx, network, amount)

    @commands.command(name="vunregister", hidden=True)
    async def _alias_vunregister(self, ctx: DiscoContext, network: str = "") -> None:
        """Backward-compat alias for .stake validator unregister"""
        if not network:
            await ctx.reply_error("Usage: `.vunregister <network>`")
            return
        await self.vunregister(ctx, network)

    @commands.command(name="vdelegate", hidden=True)
    async def _alias_vdelegate(self, ctx: DiscoContext, validator_member: discord.Member = None, network: str = "", amount: str = "") -> None:
        """Backward-compat alias for .stake validator delegate"""
        if not validator_member or not network or not amount:
            await ctx.reply_error("Usage: `.vdelegate <@validator> <network> <amount>`")
            return
        await self.vdelegate(ctx, validator_member, network, amount)

    @commands.command(name="vundelegate", hidden=True)
    async def _alias_vundelegate(self, ctx: DiscoContext, validator_member: discord.Member = None, network: str = "", amount: str = "all") -> None:
        """Backward-compat alias for .stake validator undelegate"""
        if not validator_member or not network:
            await ctx.reply_error("Usage: `.vundelegate <@validator> <network> [amount]`")
            return
        await self.vundelegate(ctx, validator_member, network, amount)

    @commands.command(name="vnetworks", hidden=True)
    async def _alias_vnetworks(self, ctx: DiscoContext) -> None:
        """Backward-compat alias for .stake validator networks"""
        await self.vnetworks(ctx)

    @commands.command(name="vstats", hidden=True)
    async def _alias_vstats(self, ctx: DiscoContext) -> None:
        """Backward-compat alias for .stake validator stats"""
        await self.vstats(ctx)

    @commands.command(name="mempool", hidden=True)
    async def _alias_mempool(self, ctx: DiscoContext, network: str | None = None) -> None:
        """Backward-compat alias for .stake validator mempool"""
        await self.mempool_view(ctx, network)

    @commands.command(name="vcommission", hidden=True)
    async def _alias_vcommission(self, ctx: DiscoContext, network: str = "", rate: float = 0) -> None:
        """Backward-compat alias for .stake validator commission"""
        if not network or not rate:
            await ctx.reply_error("Usage: `.vcommission <network> <rate>`")
            return
        await self.vsetcommission(ctx, network, rate)

    @commands.command(name="vals", hidden=True)
    async def _alias_vals(self, ctx: DiscoContext, network: str | None = None) -> None:
        """Backward-compat alias for .stake validator list"""
        await self.validators_list(ctx, network)

    @commands.command(name="vsubmit", hidden=True)
    async def _alias_vsubmit(self, ctx: DiscoContext, action_type: str = "", network: str = "", gas_price: str = "", *, payload_str: str = "{}") -> None:
        """Backward-compat alias for .stake validator submit"""
        if not action_type or not network or not gas_price:
            await ctx.reply_error("Usage: `.vsubmit <action_type> <network> <gas_price> [payload_json]`")
            return
        await self.vsubmit(ctx, action_type, network, gas_price, payload_str=payload_str)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Stake(bot))
