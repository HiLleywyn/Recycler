"""
cogs/validators.py  -  Player-driven PoS Validator System

Architecture:
  - Players register as validators by staking the network's stake token
  - Every VALIDATOR_TICK seconds, one validator per network is selected (weighted by stake)
  - Selected validator processes all pending mempool actions into a validator_block
  - Gas fees are split: 90% to validator (+delegators), 10% to treasury (guild_treasury table)
  - Per-guild treasury_cut_pct in DB is the runtime value; VALIDATOR_REWARD/TREASURY_CUT
    constants are display-only aliases that must match the DB default (both = 0.10 cut).
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
import secrets

# Cryptographically secure RNG for validator selection (controls block
# production and gas fee distribution  -  must not be predictable).
_srng = secrets.SystemRandom()
import time
import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.heartbeat import pulse, register_interval
from core.framework.ui import send_paginated
from core.framework.ui import C_ERROR, C_INFO, C_NEUTRAL, C_PURPLE, C_SUCCESS, C_WARNING, ConfirmView, fmt_gas, mention
from core.framework.embed import card
from core.framework.utils import parse_amount
from core.framework.fuzzy import suggest_subcommand
from core.framework.scale import to_raw, to_human

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
from constants.validators import (
    VALIDATOR_TICK, VALIDATOR_REWARD, TREASURY_CUT, MIN_STAKE, MIN_VALIDATORS,
    STAKE_LOCK_SECS, MAX_SLASH_COUNT, SLASH_DECAY_SECS, MAX_MEMPOOL, DELEGATION_VALIDATOR_KEEP,
    DELEGATION_LOCK_SECS, MIN_DELEGATION, REJECTION_SLASH_RATE,
    GAS_TIERS, GAS_MIN_MULT as _GAS_MIN_MULT, GAS_MAX_MULT as _GAS_MAX_MULT, NET_SHORT,
)

# Local alias kept for backward compatibility (cog-internal usage)
_NET_SHORT = NET_SHORT

# Canonical alias normalization lives in :mod:`core.framework.network`. Keep a
# thin wrapper that preserves the "unknown input passes through unchanged"
# behavior so callers that pass already-canonical names (or typos) keep
# working.
from core.framework.network import normalize_full as _fw_normalize_full


def _normalize_network(name: str) -> str:
    """Map any user-supplied network alias to its canonical long-form name.

    Returns the input unchanged if it is not a recognized network alias, so
    callers can still fall through to their own error paths.
    """
    return _fw_normalize_full(name) or name


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
    "Sun Network":       1e-7,    # SUN-denominated, PoW
    "Moneta Chain":   1e-8,    # satoshi-scale, PoW
    "Arcadia Network":  1e-9,    # ~1 gwei per gas unit, PoS
    "Discoin Network":   1e-8,    # DSC-denominated, PoS (matches ARC scale; was 1e-6 causing huge gas at max drift)
}

# ── Helpers ────────────────────────────────────────────────────────────────────

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
    Caps effective stake at 25% of total network stake to prevent whale
    dominance (single validator can't control >25% of block production).
    """
    if not validators:
        return None

    delegated_totals = delegated_totals or {}

    # First pass: compute raw effective stakes and total
    raw_stakes: list[float] = []
    for v in validators:
        effective = float(v["stake_amount"]) + delegated_totals.get(v["user_id"], 0.0)
        raw_stakes.append(effective)
    total_stake = sum(raw_stakes)

    # Second pass: apply 25% cap and back-to-back penalty
    # Cap prevents any single validator from dominating block production
    # even if they hold a majority of staked tokens.
    stake_cap = total_stake * 0.25 if total_stake > 0 else 1.0
    weights = []
    for i, v in enumerate(validators):
        w = min(raw_stakes[i], stake_cap)
        if v["user_id"] == last_validator_id:
            w *= 0.01  # 99% back-to-back penalty (was 90%, too weak)
        weights.append(max(w, 0.001))

    total = sum(weights)
    probs = [w / total for w in weights]
    return _srng.choices(validators, weights=probs, k=1)[0]


# ── Cog ────────────────────────────────────────────────────────────────────────

class Validators(commands.Cog):
    """Player-driven PoS validator system."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # Tracks the last validator selected per (guild_id, network)
        self._last_validator: dict[tuple, str] = {}
        self.validator_tick.start()
        register_interval("pos_validator_tick", VALIDATOR_TICK)

    def cog_unload(self) -> None:
        self.validator_tick.cancel()

    # ── Background loop ────────────────────────────────────────────────────────

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "validators")

    @tasks.loop(seconds=VALIDATOR_TICK)
    async def validator_tick(self) -> None:
        """Main slot loop  -  runs every VALIDATOR_TICK seconds across all guilds."""
        for guild in self.bot.guilds:
            try:
                await self._process_guild(guild)
            except Exception as e:
                # Never crash the loop
                log.exception("[validators] Error processing guild %s: %s", guild.id, e)
        pulse("pos_validator_tick")

    async def _process_guild(self, guild: discord.Guild) -> None:
        """Process one validator block cycle for every active network in this guild."""
        # Slash decay: reduce slash_count by 1 for validators whose last slash was > 7 days ago
        try:
            await self.bot.db.decay_validator_slashes(guild.id, SLASH_DECAY_SECS)
        except Exception:
            pass  # DB method may not exist yet; silently skip

        # Get all active player-validators in this guild
        all_validators = await self.bot.db.get_pos_validators(guild.id)
        if not all_validators:
            return

        # Group validators by network
        by_network: dict[str, list[dict]] = {}
        for v in all_validators:
            if v["is_active"]:
                by_network.setdefault(v["network"], []).append(v)

        for network, validators in by_network.items():
            if len(validators) < MIN_VALIDATORS:
                continue  # need at least MIN_VALIDATORS active validators per network
            await self._process_network_block(guild, network, validators)

    async def _process_network_block(
        self,
        guild: discord.Guild,
        network: str,
        validators: list[dict],
    ) -> None:
        """Process one validator block for a single network."""
        # Pull pending mempool actions for this network
        pending = await self.bot.db.get_pending_mempool(guild.id, network, limit=MAX_MEMPOOL)
        if not pending:
            return  # empty mempool  -  no block needed this slot

        # Sort by gas price priority (high → medium → low)
        pending.sort(key=lambda x: _gas_priority(x["gas_price"]), reverse=True)

        # Pre-fetch delegated totals for weighted selection
        delegated_totals: dict[int, float] = {}
        for v in validators:
            delegated_totals[v["user_id"]] = await self.bot.db.get_total_delegated_stake(
                v["user_id"], guild.id, network
            )

        # Select validator via weighted stake (own + delegated)
        last_vid = self._last_validator.get((guild.id, network))
        validator = _select_validator(validators, last_vid, delegated_totals)
        if not validator:
            return
        self._last_validator[(guild.id, network)] = validator["user_id"]

        # Create the block record
        block_id = await self.bot.db.create_validator_block(guild.id, network, validator["user_id"])

        # Set of user_ids who are active PoS validators on this network  -  used for slash eligibility
        _active_validator_ids: set[int] = {v["user_id"] for v in validators}

        # Process each action
        results: list[dict] = []
        total_gas: int = 0

        for action in pending:
            success, reason = await self._execute_action(guild, action)
            status = "confirmed" if success else "rejected"
            gas = int(action["gas_fee"]) if success else 0
            total_gas += gas

            # Refund locked tokens if rejected, and log to transactions table
            if not success:
                await self._refund_action(guild, action)
                _net_prefix = _NET_SHORT.get(network, "")
                await self.bot.db.log_tx(
                    guild.id, action["user_id"],
                    "REJECTED_" + action["action_type"].upper(),
                    network=_net_prefix,
                )
                # Slash the submitter if they are an active PoS validator on this network
                if action["user_id"] in _active_validator_ids:
                    # Check for Validator Guard  -  absorbs slash if available
                    _guard_used = await self.bot.db.use_validator_guard(action["user_id"], guild.id)
                    if _guard_used:
                        await self.bot.bus.publish(
                            "validator_guard_used",
                            guild=guild,
                            user_id=action["user_id"],
                            network=network,
                            reason=reason,
                        )
                        continue  # guard consumed, skip slash entirely
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
                            # Refund all delegators when validator is deactivated
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
                                # DM each affected delegator
                                _del_member = guild.get_member(d["delegator_id"])
                                if _del_member:
                                    try:
                                        _del_embed = (
                                            card("⛔ Validator Deactivated  -  Delegation Refunded", color=C_WARNING)
                                            .field("Network",   network,                             True)
                                            .field("Refunded",  f"{to_human(d['amount']):,.6f} {d['token']}",  True)
                                            .field("Reason",    f"Validator auto-deactivated after {slash_result.get('slash_count', MAX_SLASH_COUNT)} slashes.", False)
                                            .footer("Your funds have been returned to your wallet.")
                                            .build()
                                        )
                                        await _del_member.send(embed=_del_embed)
                                    except discord.HTTPException:
                                        pass  # DMs disabled  -  silent fail is intentional

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
        # All reward amounts are raw NUMERIC(36,0) ints -- must NOT be floats
        validator_reward_raw: int = int(round(total_gas * (1.0 - _t_cut)))
        treasury_cut_raw: int     = total_gas - validator_reward_raw  # exact complement avoids rounding loss

        # Split validator reward using validator's chosen commission rate
        commission = validator.get("commission_rate", DELEGATION_VALIDATOR_KEEP)
        commission = max(0.30, min(0.90, commission))  # clamp to valid range (lowered min from 0.60 for delegator-friendly validators)
        delegation_pool_raw: int           = int(round(validator_reward_raw * (1.0 - commission)))
        adjusted_validator_reward_raw: int = validator_reward_raw - delegation_pool_raw

        net_short = _NET_SHORT.get(network, "")
        if adjusted_validator_reward_raw > 0:
            if net_short:
                await self.bot.db.update_wallet_holding(validator["user_id"], guild.id, net_short, gas_coin, adjusted_validator_reward_raw)
            else:
                await self.bot.db.update_holding(validator["user_id"], guild.id, gas_coin, adjusted_validator_reward_raw)

        # Distribute delegation pool proportionally among delegators
        if delegation_pool_raw > 0:
            delegations = await self.bot.db.get_delegations_for_validator(
                validator["user_id"], guild.id, network
            )
            total_delegated = sum(int(d["amount"]) for d in delegations)
            if total_delegated > 0:
                for d in delegations:
                    share = int(d["amount"]) / total_delegated  # ratio -- scale-invariant
                    payout = int(round(delegation_pool_raw * share))
                    if payout > 0:
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
            else:
                # No delegators  -  give unclaimed pool back to validator
                if net_short:
                    await self.bot.db.update_wallet_holding(validator["user_id"], guild.id, net_short, gas_coin, delegation_pool_raw)
                else:
                    await self.bot.db.update_holding(validator["user_id"], guild.id, gas_coin, delegation_pool_raw)
                adjusted_validator_reward_raw += delegation_pool_raw

        if treasury_cut_raw > 0:
            await self.bot.db.add_to_treasury(guild.id, treasury_cut_raw)
            from services.vault import deposit_to_vault
            _vault_net = _NET_SHORT.get(network, "")
            if _vault_net:
                # deposit_to_vault expects human (native coin) units, not raw
                await deposit_to_vault(self.bot.db, guild.id, _vault_net, to_human(treasury_cut_raw), bot=self.bot)

        # Confirm the block
        await self.bot.db.confirm_validator_block(
            block_id, total_gas, adjusted_validator_reward_raw, treasury_cut_raw
        )

        # Update validator stats (track adjusted reward actually received)
        await self.bot.db.increment_validator_blocks(validator["user_id"], guild.id, adjusted_validator_reward_raw)

        # Lockstone XP: grant per validator block confirmed
        _LS_CFG = Config.SHOP_ITEMS.get("lockstone", {})
        if _LS_CFG:
            from cogs.shop import notify_item_levelup_ready as _nilr, cap_xp as _cap_xp
            lockstone = await self.bot.db.get_lockstone(validator["user_id"], guild.id)
            if lockstone and lockstone["level"] < _LS_CFG.get("max_level", 50):
                xp_gain = _LS_CFG.get("xp_per_block", 10.0)
                xp_result = await self.bot.db.add_lockstone_xp(validator["user_id"], guild.id, xp_gain)
                if xp_result:
                    live_xp, live_level = xp_result
                    capped_xp = _cap_xp(live_xp, live_level, _LS_CFG)
                    if capped_xp < live_xp:
                        await self.bot.db.update_lockstone_xp(validator["user_id"], guild.id, capped_xp, live_level)
                    await _nilr(self.bot, validator["user_id"], guild, "lockstone", live_xp - xp_gain, live_xp, live_level, lockstone["staked_amount"])

        # EIP-1559: adjust base fee for next block based on how full this block was
        confirmed_count = sum(1 for r in results if r["success"])
        is_valid = True  # block is always valid once created (mempool was non-empty)
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
                total_gas=to_human(total_gas),
                gas_coin=gas_coin,
                results=results,
            )

        # Publish event so trades.py can post the feed embed.
        # Values are converted to human units here -- fmt_gas() expects native coin amounts.
        await self.bot.bus.publish(
            "validator_block",
            guild=guild,
            network=network,
            validator=validator,
            block_id=block_id,
            results=results,
            total_gas=to_human(total_gas),
            gas_coin=gas_coin,
            validator_reward=to_human(adjusted_validator_reward_raw),
            delegation_pool=to_human(delegation_pool_raw),
            treasury_cut=to_human(treasury_cut_raw),
            is_valid=is_valid,
        )

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
                    # Refund to token_in's OWN network wallet. The submitter
                    # stamped net_in into the payload so vault-pair pools
                    # (where token_in may live on a different network from
                    # the mempool) refund cleanly instead of dumping the
                    # balance on the gas chain. Fall back to the mempool
                    # row's network for entries queued before the fix.
                    _payload_net_in = payload.get("net_in") or ""
                    _in_net_short = _NET_SHORT.get(_payload_net_in, "") or _action_net
                    if _in_net_short:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _in_net_short, token_in, to_raw(amount_in))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, token_in, to_raw(amount_in))
            elif action_type == "buy":
                # USD was deducted at submission  -  refund it
                amount_usd = float(payload.get("amount_usd", 0))
                if amount_usd > 0:
                    await self.bot.db.update_wallet(user_id, guild.id, to_raw(amount_usd))
            elif action_type == "sell":
                # Tokens were deducted from CeFi holdings at submission  -  refund there
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
            elif action_type == "stake":
                # Tokens were deducted from DeFi wallet at submission  -  refund there
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    if _action_net:
                        await self.bot.db.update_wallet_holding(user_id, guild.id, _action_net, symbol, to_raw(amount))
                    else:
                        await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
            elif action_type == "addlp":
                # Both tokens deducted from DeFi wallet at submission  -  refund both
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
                # LP shares deducted at submission  -  refund them via lp_position
                pool_id   = payload.get("pool_id", "")
                lp_shares = float(payload.get("lp_shares", 0))
                if pool_id and lp_shares > 0:
                    await self.bot.db.update_lp_position(user_id, guild.id, pool_id, to_raw(lp_shares))
            elif action_type == "contract_call":
                # Tokens locked by contract receive ops  -  contract engine handles rollback internally
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
        """Execute a wallet-to-wallet token send from mempool.
        NOTE: Tokens are already deducted from sender at submission time (locked).
        On confirm → credit recipient.
        On reject  → refund sender (handled by resolve path calling _refund_mempool_action).
        """
        to_id  = payload.get("to_user_id")
        symbol = payload.get("symbol", "").upper()
        amount = float(payload.get("amount", 0))

        if not to_id or not symbol or amount <= 0:
            return False, "Invalid send payload"

        # Credit recipient to their DeFi wallet
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
        """Execute a queued AMM swap from the mempool.
        token_in is already deducted from sender at submission time.
        On confirm → apply AMM math against live pool state, credit token_out.
        On reject  → refund token_in (handled by _refund_action).

        Uses live pool reserves at block time  -  mirrors how Arcadia executes
        swaps at the state of the block, not the state at submission.
        """
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
        # Read reserves as raw ints; do AMM math via human floats but apply
        # the debit/credit in raw int space so the pool row stays exact.
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

        # AMM: constant product with 0.3% fee, computed at block execution time
        FEE = 0.003
        amount_in_with_fee = amount_in * (1 - FEE)
        amount_out = reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee)

        if amount_out <= 0:
            return False, "Swap output too small"

        amount_in_raw = to_raw(amount_in)
        amount_out_raw = to_raw(amount_out)

        # Credit token_out to its OWN network's wallet. For vault-pair pools
        # the output token can live on a different network from the mempool
        # (e.g. CAT group token on Moon Network paired against MTA on
        # Moneta Chain) -- crediting to the mempool's network would park
        # the balance in the wrong wallet row and create the duplicate-
        # network display bug. Prefer the submitter-stamped net_out from the
        # payload; fall back to the live token registry for older entries
        # that were queued before the fix.
        net_short = _NET_SHORT.get(network, "")
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

        # Update pool reserves in raw int space
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
        """Execute a queued buy at live oracle price.
        USD is already deducted from sender at submission time.
        On confirm → credit tokens at block-time price.
        On reject  → refund USD (handled by _refund_action).
        """
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
        _eff_price = max(1e-15, float(price_row["price"]) * (1 + impact))
        qty = amount_usd / _eff_price
        await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(qty))

        await self.bot.db.log_tx(
            guild.id, user_id, "BUY",
            symbol_in="USD", amount_in=to_raw(amount_usd),
            symbol_out=symbol, amount_out=to_raw(qty),
            price_at=float(price_row["price"]),
        )
        return True, f"Bought {qty:.6f} {symbol} at ${_eff_price:.4f}"

    async def _exec_sell(
        self, guild: discord.Guild, user_id: int, payload: dict
    ) -> tuple[bool, str]:
        """Execute a queued sell at live oracle price.
        Tokens are already deducted from sender at submission time.
        On confirm → credit USD at block-time price.
        On reject  → refund tokens (handled by _refund_action).
        """
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
        _eff_price = max(1e-9, float(price_row["price"]) * (1 - impact))
        revenue = amount * _eff_price
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
        """Execute a queued stake.
        Tokens already deducted at submission time.
        On confirm → add to validator's stake record.
        On reject  → refund tokens (handled by _refund_action).
        """
        validator_id = payload.get("validator_id", "").upper()
        symbol       = payload.get("symbol", "").upper()
        amount       = float(payload.get("amount", 0))

        if not validator_id or not symbol or amount <= 0:
            return False, "Invalid stake payload"

        v = await self.bot.db.get_validator(validator_id, guild.id)
        if not v:
            return False, f"Validator {validator_id} not found"

        await self.bot.db.update_stake(user_id, guild.id, validator_id, symbol, to_raw(amount))
        await self.bot.db.log_tx(
            guild.id, user_id, "STAKE",
            symbol_in=symbol, amount_in=to_raw(amount),
            symbol_out=validator_id, amount_out=to_raw(amount),
            network=_NET_SHORT.get(network, ""),
        )
        return True, f"Staked {amount:.6f} {symbol} with {validator_id}"

    async def _exec_unstake(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued unstake.
        On confirm → remove from stake record, return tokens to user.
        Nothing locked at submission time for unstake (stake remains until confirmed).
        """
        validator_id = payload.get("validator_id", "").upper()
        symbol       = payload.get("symbol", "").upper()
        amount       = float(payload.get("amount", 0))

        if not validator_id or not symbol or amount <= 0:
            return False, "Invalid unstake payload"

        await self.bot.db.update_stake(user_id, guild.id, validator_id, symbol, to_raw(-amount))
        net_short = _NET_SHORT.get(network, "")
        if net_short:
            await self.bot.db.update_wallet_holding(user_id, guild.id, net_short, symbol, to_raw(amount))
        else:
            await self.bot.db.update_holding(user_id, guild.id, symbol, to_raw(amount))
        await self.bot.db.log_tx(
            guild.id, user_id, "UNSTAKE",
            symbol_in=validator_id, amount_in=to_raw(amount),
            symbol_out=symbol, amount_out=to_raw(amount),
            network=_NET_SHORT.get(network, ""),
        )
        return True, f"Unstaked {amount:.6f} {symbol} from {validator_id}"

    async def _exec_addlp(
        self, guild: discord.Guild, user_id: int, payload: dict, network: str = ""
    ) -> tuple[bool, str]:
        """Execute a queued add-liquidity.
        Both tokens already deducted at submission time.
        On confirm → mint LP shares, update pool reserves.
        On reject  → refund both tokens (handled by _refund_action).
        """
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

        # LP mint math in raw int space so pool reserves stay exact across mints.
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
        """Execute a queued remove-liquidity.
        LP shares already deducted at submission time.
        On confirm → burn LP shares, credit proportional tokens.
        On reject  → refund LP shares (handled by _refund_action).
        """
        pool_id   = payload.get("pool_id", "")
        token_a   = payload.get("token_a", "").upper()
        token_b   = payload.get("token_b", "").upper()
        lp_shares = float(payload.get("lp_shares", 0))

        if not pool_id or lp_shares <= 0:
            return False, "Invalid removelp payload"

        pool = await self.bot.db.get_pool(pool_id, guild.id)
        if not pool or pool["total_lp"] <= 0:
            return False, "Pool not found or empty"

        # LP exit math in raw int space so reserves stay exact across removals.
        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        total_lp_raw = int(pool["total_lp"])
        # Cap the deduction at the actual raw balance: to_raw(to_human(raw))
        # can overshoot and trigger "Insufficient LP shares" on full exits.
        _user_lp = await self.bot.db.get_user_lp(user_id, guild.id, pool_id)
        _user_lp_raw = int(_user_lp["lp_shares"]) if _user_lp else 0
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
            # Publish bus event so trades.py can post to the validators channel
            events = await self.bot.db.get_contract_events(guild.id, address, limit=5)
            # Filter to events logged for this block
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

    # ── Player commands ────────────────────────────────────────────────────────

    @commands.hybrid_group(name="validator", aliases=["val", "v"], with_app_command=False)
    @guild_only
    async def validator(self, ctx: DiscoContext) -> None:
        """Validator commands. Use a subcommand: register, delegate, etc."""
        if await suggest_subcommand(ctx, self.validator):
            return
        p = ctx.prefix or Config.PREFIX
        await ctx.reply(
            f"**Validator commands:** `{p}validator register` · `{p}validator delegate` · `{p}validator list`",
            mention_author=False,
        )

    @validator.command(name="register", aliases=["vreg", "vregister"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vregister(self, ctx: DiscoContext, network: str, amount: str) -> None:
        """Register as a validator by staking tokens on a network.

        Usage: .vregister <network> <amount|all>
        Example: .vregister Arcadia 500
        """
        network = _normalize_network(network)

        # Resolve stake token for this network
        stake_token = await self.bot.db.get_network_stake_token(ctx.guild.id, network)
        if not stake_token:
            await ctx.reply_error(f"Unknown network `{network}`. Check `.vnetworks` for options.")
            return

        # Require a DeFi wallet on this network
        net_short = _NET_SHORT.get(network, "")
        if not net_short or not await self.bot.db.has_defi_wallet(ctx.author.id, ctx.guild.id, net_short):
            await ctx.reply_error_action(
                f"You need a DeFi wallet on **{network}** to register as a validator.",
                f"Create {network} Wallet",
                f"wallet create {net_short or network.lower()}",
                rerun_original=True,
            )
            return

        # Resolve amount  -  "all" stakes everything in DeFi wallet
        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        holding = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token)
        have_raw = int(holding["amount"]) if holding else 0
        have = to_human(have_raw)
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
                # Convert USD to token quantity
                _price = await self.bot.db.get_price(stake_token, ctx.guild.id)
                if not _price or _price["price"] <= 0:
                    await ctx.reply_error("Price data unavailable for conversion.")
                    return
                amount_val = _parsed / float(_price["price"])
            else:
                amount_val = _parsed
            amount_raw = to_raw(amount_val)

        if amount_val < MIN_STAKE:
            await ctx.reply_error(f"Minimum stake is **{MIN_STAKE:,.0f} {stake_token}**.")
            return

        if not holding or amount_raw > have_raw:
            await ctx.reply_error(
                f"You need **{amount_val:,.4f} {stake_token}** in your DeFi wallet but only have **{have:,.4f}**."
            )
            return

        # One validator registration per player across all networks
        all_my_validators = await self.bot.db.get_pos_validators_for_user(ctx.author.id, ctx.guild.id)
        registered_on_other = [v for v in all_my_validators if v["network"] != network and v["is_active"]]
        if registered_on_other:
            nets = ", ".join(v["network"] for v in registered_on_other)
            await ctx.reply_error(
                f"You are already registered as a validator on **{nets}**.\n"
                "Each player may only run one validator. Unregister that one first with `.vunregister`."
            )
            return

        # Check if already registered on this network (top-up allowed)
        existing = await self.bot.db.get_pos_validator(ctx.author.id, ctx.guild.id, network)
        if existing:
            # Top up stake -- use create_pos_validator ON CONFLICT path so reactivation
            # and lock extension are handled atomically (sets is_active=TRUE, extends lock).
            new_total_raw = int(existing['stake_amount']) + amount_raw
            lock_until = time.time() + STAKE_LOCK_SECS
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -amount_raw)
            await self.bot.db.create_pos_validator(ctx.author.id, ctx.guild.id, network, stake_token, new_total_raw, lock_until)
            new_total = to_human(existing['stake_amount']) + amount_val
            was_inactive = not existing.get("is_active")
            desc = (
                f"Added **{amount_val:,.4f} {stake_token}** to your validator stake on **{network}**.\n"
                f"New total: **{new_total:,.4f} {stake_token}**"
            )
            if was_inactive:
                desc += "\n\nYour validator has been **reactivated** and is now eligible to process blocks."
            embed = card("🔐 Stake Increased", description=desc, color=C_SUCCESS).build()
        else:
            # Register fresh
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -amount_raw)
            lock_until = time.time() + STAKE_LOCK_SECS
            await self.bot.db.create_pos_validator(
                ctx.author.id, ctx.guild.id, network, stake_token, amount_raw, lock_until
            )
            embed = card(
                "✅ Validator Registered",
                description=(
                    f"You are now a validator on **{network}**!\n\n"
                    f"**Staked:** {amount_val:,.4f} {stake_token}\n"
                    f"**Locked for:** 24 hours\n\n"
                    f"You will be selected to process blocks proportional to your stake.\n"
                    f"Earn **{VALIDATOR_REWARD*100:.0f}%** of all gas fees from blocks you validate.\n"
                    f"*(remaining {TREASURY_CUT*100:.0f}% goes to the treasury)*"
                ),
                color=C_SUCCESS,
            ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @validator.command(name="unregister", aliases=["vunreg", "vunregister"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vunregister(self, ctx: DiscoContext, network: str) -> None:
        """Withdraw your validator stake from a network.

        Usage: .vunregister <network>
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

        # Refund all delegators before deactivating
        delegation_rows = await self.bot.db.wipe_delegations_for_validator(
            ctx.author.id, ctx.guild.id, network
        )
        net_short = _NET_SHORT.get(network, "")
        for d in delegation_rows:
            if net_short:
                await self.bot.db.update_wallet_holding(d["delegator_id"], ctx.guild.id, net_short, d["token"], d["amount"])
            else:
                await self.bot.db.update_holding(d["delegator_id"], ctx.guild.id, d["token"], d["amount"])

        # Refund stake to DeFi wallet
        amount = v["stake_amount"]
        token  = v["stake_token"]
        if net_short:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, token, amount)
        else:
            await self.bot.db.update_holding(ctx.author.id, ctx.guild.id, token, amount)
        await self.bot.db.deactivate_pos_validator(ctx.author.id, ctx.guild.id, network)

        desc = f"Returned **{to_human(amount):,.4f} {token}** to your wallet. You are no longer a validator on **{network}**."
        if delegation_rows:
            desc += f"\n\n**{len(delegation_rows)} delegator(s)** have been refunded their delegations."
        embed = card("🔓 Stake Withdrawn", description=desc, color=C_WARNING).build()
        await ctx.reply(embed=embed, mention_author=False)

    @validator.command(name="commission", aliases=["vcommission", "vcomm", "vsetcommission"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vsetcommission(self, ctx: DiscoContext, network: str, rate: float) -> None:
        """Set your validator's commission rate  -  the % of gas you keep (rest goes to delegators).

        Usage: .vsetcommission <network> <rate>
        Rate is a percentage: 60 - 90 (e.g. 80 = you keep 80%, delegators share 20%)
        Commission can only be changed once every 24 hours.
        """
        network = _normalize_network(network)
        v = await self.bot.db.get_pos_validator(ctx.author.id, ctx.guild.id, network)
        if not v or not v["is_active"]:
            await ctx.reply_error(f"You are not an active validator on **{network}**.")
            return

        # Commission change cooldown: prevent bait-and-switch on delegators.
        # Validators must wait 24h between commission changes so delegators
        # have time to react and undelegate if they disagree with the new rate.
        import time as _time
        _COMMISSION_CD = 86_400  # 24 hours
        last_change = v.get("last_commission_change")
        if last_change:
            _lc_epoch = last_change.timestamp() if hasattr(last_change, "timestamp") else float(last_change)
            _elapsed = _time.time() - _lc_epoch
            if _elapsed < _COMMISSION_CD:
                remaining = int(_COMMISSION_CD - _elapsed)
                hours, remainder = divmod(remaining, 3600)
                mins = remainder // 60
                await ctx.reply_error(
                    f"Commission can only be changed once every **24 hours**.\n"
                    f"Try again in **{hours}h {mins}m**."
                )
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
        embed = card(
            "⚙️ Commission Updated",
            description=(
                f"Your validator on **{network}** now keeps **{rate:.0f}%** of gas earnings.\n"
                f"Delegators share the remaining **{delegator_share:.0f}%** proportionally."
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Delegation Commands ────────────────────────────────────────────────────

    @validator.command(name="delegate", aliases=["vdel", "vdelegate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vdelegate(self, ctx: DiscoContext, validator: discord.Member, network: str, amount: str) -> None:
        """Delegate stake to a PoS validator and earn a share of their gas rewards.

        Usage: .vdelegate <@validator> <network> <amount|all>
        """
        network = _normalize_network(network)

        if validator.id == ctx.author.id:
            await ctx.reply_error("You cannot delegate to yourself.")
            return

        v = await self.bot.db.get_pos_validator(validator.id, ctx.guild.id, network)
        if not v or not v["is_active"]:
            await ctx.reply_error(f"{validator.mention} is not an active validator on **{network}**.")
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

        if amount.lower() == "all":
            h = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token)
            amount_val = h.h("amount") if h else 0.0
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                # Convert USD to token quantity
                _price = await self.bot.db.get_price(stake_token, ctx.guild.id)
                if not _price or _price["price"] <= 0:
                    await ctx.reply_error("Price data unavailable for conversion.")
                    return
                amount_val = _parsed / float(_price["price"])
            else:
                amount_val = _parsed

        if amount_val < MIN_DELEGATION:
            await ctx.reply_error(f"Minimum delegation is **{MIN_DELEGATION:,.0f} {stake_token}**.")
            return

        # Max 3 active delegations per player
        MAX_DELEGATIONS = 3
        existing_dels = await self.bot.db.get_user_delegations(ctx.author.id, ctx.guild.id)
        active_dels = [d for d in existing_dels if int(d["amount"] or 0) > 0 and d["validator_user_id"] != validator.id]
        if len(active_dels) >= MAX_DELEGATIONS:
            await ctx.reply_error(
                f"You already have **{len(active_dels)} active delegations** (max {MAX_DELEGATIONS}).\n"
                "Undelegate from one before delegating to another."
            )
            return

        # Gas fee
        gas_coin, gas_fee = await gas_fee_for_network(self.bot.db, ctx.guild.id, "delegate", "medium", network)
        gas_cfg = Config.TOKENS.get(gas_coin, {})
        gas_em = gas_cfg.get("emoji", "●")
        gas_h = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, gas_coin)
        gas_bal = gas_h.h("amount") if gas_h else 0.0

        holding = await self.bot.db.get_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token)
        have = holding.h("amount") if holding else 0.0
        need = amount_val + (gas_fee if gas_coin == stake_token else 0.0)

        if have < need:
            await ctx.reply_error(
                f"You need **{amount_val:,.4f} {stake_token}** + gas in your DeFi wallet but only have **{have:,.4f}**."
            )
            return
        if gas_coin != stake_token and gas_bal < gas_fee:
            await ctx.reply_error(
                f"Need **{fmt_gas(gas_fee, gas_coin, gas_em)}** for gas. You have **{fmt_gas(gas_bal, gas_coin, gas_em)}**."
            )
            return

        _b = card(
            "🔒 Confirm Delegation",
            description=(
                f"Delegate **{amount_val:,.4f} {stake_token}** to {validator.mention} on **{network}**?\n\n"
                f"• Locked for **24 hours**\n"
                f"• You'll earn the validator's delegator share of gas rewards proportionally\n"
                f"• If the validator is slashed, your delegation is also slashed"
            ),
            color=C_WARNING,
        )
        if gas_fee > 0:
            _b.field("Gas Fee", f"**{fmt_gas(gas_fee, gas_coin, gas_em)}**", True)
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=_b.build(), view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Delegation cancelled.", color=C_ERROR).build())
            return

        # Deduct gas
        if gas_fee > 0:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, gas_coin, -to_raw(gas_fee))

        lock_until = time.time() + DELEGATION_LOCK_SECS
        await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, stake_token, -to_raw(amount_val))
        new_total_raw = await self.bot.db.create_or_add_delegation(
            ctx.author.id, validator.id, ctx.guild.id, network, stake_token, to_raw(amount_val), lock_until
        )
        new_total = to_human(int(new_total_raw))
        embed = card(
            "✅ Delegation Confirmed",
            description=(
                f"Delegated **{amount_val:,.4f} {stake_token}** to {validator.mention} on **{network}**.\n"
                f"Your total delegation to this validator: **{new_total:,.4f} {stake_token}**"
            ),
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=embed, view=None)

    @validator.command(name="undelegate", aliases=["vundel", "vundelegate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def vundelegate(self, ctx: DiscoContext, validator: discord.Member, network: str, amount: str = "all") -> None:
        """Withdraw your delegation from a PoS validator.

        Usage: .vundelegate <@validator> <network> [amount|all]
        """
        network = _normalize_network(network)

        d = await self.bot.db.get_delegation(ctx.author.id, validator.id, ctx.guild.id, network)
        if not d or int(d["amount"] or 0) <= 0:
            await ctx.reply_error(f"You have no active delegation to {validator.mention} on **{network}**.")
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

        if amount.lower() == "all":
            amount_val = d_amount
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<usd>`, or `all`.")
                return
            if _usd_mode:
                # Convert USD to token quantity
                _price = await self.bot.db.get_price(d["token"], ctx.guild.id)
                if not _price or _price["price"] <= 0:
                    await ctx.reply_error("Price data unavailable for conversion.")
                    return
                amount_val = _parsed / float(_price["price"])
            else:
                amount_val = _parsed
            if amount_val > d_amount:
                await ctx.reply_error(f"You only have **{d_amount:,.4f} {d['token']}** delegated.")
                return

        embed = card(
            "🔓 Confirm Undelegation",
            description=f"Withdraw **{amount_val:,.4f} {d['token']}** from {validator.mention} on **{network}**?",
            color=C_WARNING,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Undelegation cancelled.", color=C_ERROR).build())
            return

        net_short = _NET_SHORT.get(network, "")
        amount_raw_undel = to_raw(amount_val)
        await self.bot.db.remove_delegation(ctx.author.id, validator.id, ctx.guild.id, network, amount_raw_undel)
        if net_short:
            await self.bot.db.update_wallet_holding(ctx.author.id, ctx.guild.id, net_short, d["token"], amount_raw_undel)
        else:
            await self.bot.db.update_holding(ctx.author.id, ctx.guild.id, d["token"], amount_raw_undel)

        embed = card(
            "✅ Undelegation Confirmed",
            description=f"Returned **{amount_val:,.4f} {d['token']}** to your DeFi wallet.",
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=embed, view=None)

    @validator.command(name="delegations", aliases=["mydels", "mydelegations"])
    @guild_only
    @no_bots
    @ensure_registered
    async def my_delegations(self, ctx: DiscoContext) -> None:
        """View your active validator delegations."""
        delegations = await self.bot.db.get_user_delegations(ctx.author.id, ctx.guild.id)
        if not delegations:
            await ctx.reply_error("You have no active delegations. Use `.vdelegate` to delegate to a validator.")
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
                f"🔒 {lock_remaining // 3600}h {(lock_remaining % 3600) // 60}m remaining"
                if lock_remaining > 0
                else "✅ Unlocked"
            )

            # USD value
            price_row = await self.bot.db.get_price(d["token"], ctx.guild.id)
            price = float(price_row["price"]) if price_row else 0.0
            _d_amt_h = to_human(int(d["amount"] or 0))
            _d_earned_h = to_human(int(d.get("total_earned") or 0))
            usd_val = _d_amt_h * price
            earned_usd = _d_earned_h * price
            total_usd += usd_val

            usd_str = f"≈ ${usd_val:,.2f}" if price > 0 else ""
            earned_str = (
                f"**+{_d_earned_h:,.6f} {d['token']}** earned"
                + (f" (≈ ${earned_usd:,.2f})" if price > 0 else "")
            )

            _b.field(
                f"{validator_mention}  -  {d['network']}",
                (
                    f"**{_d_amt_h:,.6f} {d['token']}** {usd_str}\n"
                    f"{earned_str}\n"
                    f"{lock_str}"
                ),
                False,
            )

        if total_usd > 0:
            _b.footer(f"Total delegated value ≈ ${total_usd:,.2f}")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @validator.command(name="list", aliases=["vlist", "vals"])
    @guild_only
    async def validators_list(self, ctx: DiscoContext, network: str | None = None) -> None:
        """Show active validators with stats for informed delegation decisions.

        Usage: .vals [network]
        """
        all_v = await self.bot.db.get_pos_validators(ctx.guild.id)
        if network:
            network = _normalize_network(network)
            all_v = [v for v in all_v if network.lower() in v["network"].lower()]

        active = [v for v in all_v if v["is_active"]]

        if not active:
            await ctx.reply_error("No active validators. Be the first with `.vregister`!")
            return

        # Group by network
        by_net: dict[str, list] = {}
        for v in active:
            by_net.setdefault(v["network"], []).append(v)

        pages = []
        for net, validators in sorted(by_net.items()):
            validators.sort(key=lambda x: x["stake_amount"], reverse=True)
            total_stake_raw = sum(v["stake_amount"] for v in validators)
            total_stake_h = to_human(int(total_stake_raw))

            _b = card(
                f"🌐 Validators  -  {net}",
                description=(
                    f"**{len(validators)}** active validator{'s' if len(validators) != 1 else ''}  •  "
                    f"Total stake: **{total_stake_h:,.2f} {validators[0]['stake_token'] if validators else ''}**\n"
                    f"⚠️ Blocks only process when **{MIN_VALIDATORS}+** validators are active on this network."
                ),
                color=C_PURPLE,
            )

            for v in validators:
                # Fetch delegation info
                dels = await self.bot.db.get_delegations_for_validator(v["user_id"], ctx.guild.id, net)
                del_count  = len(dels)
                del_total  = sum(float(d["amount"]) for d in dels)
                eff_stake  = float(v["stake_amount"]) + del_total
                pct        = eff_stake / (total_stake_raw + del_total) * 100 if (total_stake_raw + del_total) > 0 else 0

                commission = v.get("commission_rate", DELEGATION_VALIDATOR_KEEP)
                del_share  = round((1.0 - commission) * 100)

                lock_str = ""
                _slu2 = v["stake_locked_until"]
                _slu2_ts = _slu2.timestamp() if hasattr(_slu2, "timestamp") else _slu2
                if _slu2_ts and time.time() < _slu2_ts:
                    remaining = int(_slu2_ts - time.time())
                    lock_str = f"\n🔒 Stake locked **{remaining//3600}h {(remaining%3600)//60}m**"

                slashes = v.get("slash_count", 0)
                slash_str = f"⚠️ {slashes}/{MAX_SLASH_COUNT} slashes" if slashes > 0 else "✅ No slashes"

                _b.field(
                    f"{mention(v['user_id'], ctx.guild)} ({net})",
                    (
                        f"**Own stake:** {v.h('stake_amount'):,.2f} {v['stake_token']}\n"
                        f"**Delegated:** {to_human(del_total):,.2f} {v['stake_token']} ({del_count} delegator{'s' if del_count != 1 else ''})\n"
                        f"**Effective weight:** {pct:.1f}% of blocks\n"
                        f"**Commission:** {commission*100:.0f}% kept → you earn **{del_share}%** of their gas\n"
                        f"**Blocks confirmed:** {v['total_blocks_validated']:,}  •  {slash_str}"
                        f"{lock_str}"
                    ),
                    False,
                )

            _b.footer(
                "Lower commission = more earnings for delegators  •  "
                "More delegations = smaller share per delegator  •  "
                ".vdelegate @user <network> <amount>"
            )
            pages.append(_b.build())

        await send_paginated(ctx, pages)

    @validator.command(name="mempool")
    @guild_only
    async def mempool_view(self, ctx: DiscoContext, network: str | None = None) -> None:
        """View pending actions in the mempool.

        Usage: .mempool [network]
        """
        from core.framework.network import SHORT_TO_FULL as _NET_FULL
        _VALID_FULL = set(_NET_SHORT.keys())                # full names

        if network:
            # Normalise: accept both short codes ("arc") and full names ("Arcadia Network")
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
                description="No pending actions." + (f" on **{network}**" if network else ""),
                color=C_NEUTRAL,
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Group by network, already sorted high→low gas by DB query
        by_net: dict[str, list] = {}
        for a in pending:
            by_net.setdefault(a["network"], []).append(a)

        _b = card(
            "⏳ Mempool" + (f"  -  {network}" if network else ""),
            color=C_INFO,
        ).footer(f"Sorted by gas price (high→low) · New block every ~{VALIDATOR_TICK}s")

        now = time.time()

        for net, actions in by_net.items():
            lines = []
            for a in actions[:12]:
                tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(a["gas_price"], "⚪")

                # Parse payload for human-readable detail
                try:
                    p = json.loads(a["payload"]) if isinstance(a["payload"], str) else a["payload"]
                except Exception:
                    p = {}

                atype = a["action_type"].upper()
                if a["action_type"] == "swap":
                    amt_in  = p.get("amount_in", "?")
                    tok_in  = p.get("token_in", "?")
                    tok_out = p.get("token_out", "?")
                    detail = f"`{amt_in} {tok_in} → {tok_out}`"
                elif a["action_type"] == "send":
                    amt    = p.get("amount", "?")
                    sym    = p.get("symbol", "?")
                    to_id  = p.get("to_user_id")
                    to_str = mention(to_id, guild=ctx.guild, bot=self.bot) if to_id else "?"
                    detail = f"`{amt} {sym}` → {to_str}"
                else:
                    detail = ""

                # Age
                age_s = int(now - a["submitted_at"]) if a.get("submitted_at") else 0
                if age_s < 60:
                    age_str = f"{age_s}s ago"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m ago"
                else:
                    age_str = f"{age_s // 3600}h ago"

                _net_coin = gas_coin_for_network(net)
                _nc_cfg = Config.TOKENS.get(_net_coin, {})
                _nc_emoji = _nc_cfg.get("emoji", "●")
                lines.append(
                    f"{tier_emoji} `#{a['id']}` **{atype}** {detail}\n"
                    f"   └ {mention(a['user_id'], guild=ctx.guild, bot=self.bot)} · ⛽ **{to_human(int(a['gas_fee'])):.8f} {_nc_emoji}{_net_coin}** · {age_str}"
                )

            if len(actions) > 12:
                lines.append(f"*…and {len(actions) - 12} more*")

            # Discord embed fields are limited to 1024 chars
            body = ""
            for line in lines:
                if len(body) + len(line) + 1 > 1000:
                    body += "\n*…truncated*"
                    break
                body += ("\n" if body else "") + line

            _b.field(
                f"🔗 {net}  -  {len(actions)} pending",
                body or " - ",
                False,
            )

        await ctx.reply(embed=_b.build(), mention_author=False)

    @commands.command(name="gas", aliases=["gasfee", "gasprice"])
    @guild_only
    @no_bots
    async def gas_info(self, ctx: DiscoContext, network: str = "") -> None:
        """Show current gas fees, mempool depth, and tier recommendations.

        Usage: .gas [network]
        Examples: .gas arc   .gas discoin   .gas  (shows all networks)
        """
        target_networks: list[str] = []
        if network:
            canonical = _normalize_network(network)
            if canonical not in _NET_SHORT:
                valid_nets = ", ".join(f"`{s}`" for s in sorted(_NET_SHORT.values()))
                await ctx.reply_error(
                    f"Unknown network `{network}`. Valid: {valid_nets}"
                )
                return
            target_networks = [canonical]
        else:
            target_networks = list(_NET_SHORT.keys())

        _b = card("⛽ Gas Fees", color=C_INFO)

        for net in target_networks:
            gas_coin = gas_coin_for_network(net)
            base_fee = await ctx.db.get_base_fee(ctx.guild_id, net)
            pending = await ctx.db.get_pending_mempool(ctx.guild_id, net, limit=100)
            depth = len(pending)

            # Recommendation tier
            if depth <= 10:
                rec = "🟢 Low is fine"
            elif depth <= 30:
                rec = "🟡 Medium recommended"
            else:
                rec = "🔴 High recommended  -  mempool is busy"

            # Per-tier cost examples for send and swap
            def _est(action: str, tier: str) -> str:
                units = GAS_UNITS.get(action, GAS_UNITS.get("default", 1))
                multiplier = 1.0 + GAS_TIERS.get(tier, GAS_TIERS["medium"])
                cost = base_fee * multiplier * units
                return f"{cost:.6f} {gas_coin}"

            field_val = (
                f"**Mempool depth:** {depth} pending  ·  {rec}\n"
                f"**Base fee:** {base_fee:.2e} {gas_coin}\n"
                f"```\n"
                f"Tier    | Multiplier | Send cost      | Swap cost\n"
                f"--------|------------|----------------|-----------------\n"
                f"low     |   1.05x    | {_est('send','low'):<14} | {_est('swap','low')}\n"
                f"medium  |   1.20x    | {_est('send','medium'):<14} | {_est('swap','medium')}\n"
                f"high    |   1.50x    | {_est('send','high'):<14} | {_est('swap','high')}\n"
                f"```\n"
                f"Set tier: `gas high` or `gas low` on any transaction."
            )
            _b.field(f"🌐 {net}", field_val, False)

        _b.footer("For PoW mining electricity costs, see .chain mine status")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @validator.command(name="submit", aliases=["vsubmit"], hidden=False)
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

        Usage: .vsubmit <action_type> <network> <gas_price> [payload_json]
        Example: .vsubmit send Arcadia high {"to_user_id": 123, "symbol": "ARC", "amount": 0.5}

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

        # Check user can pay gas in network coin
        h = await ctx.db.get_holding(ctx.author.id, ctx.guild.id, gas_coin)
        gas_balance = to_human(h["amount"]) if h else 0.0
        if gas_balance < gas_fee:
            coin_cfg = Config.TOKENS.get(gas_coin, {})
            emoji = coin_cfg.get("emoji", "●")
            await ctx.reply_error(
                f"Need **{gas_fee:.8f} {emoji}{gas_coin}** for gas. You have **{gas_balance:.8f}**."
            )
            return

        # Deduct gas upfront in network coin
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
        emoji = coin_cfg.get("emoji", "●")
        embed = card(
            "📨 Action Submitted",
            description=(
                f"Your `{action_type.upper()}` has been added to the **{network}** mempool.\n\n"
                f"**Gas Price:** {gas_price.title()} | **Gas Fee:** {gas_fee:.8f} {emoji}{gas_coin}\n"
                f"**Mempool ID:** `#{action_id}`\n\n"
                f"It will be processed in the next validator block."
            ),
            color=C_INFO,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @validator.command(name="networks", aliases=["vnetworks"])
    @guild_only
    async def vnetworks(self, ctx: DiscoContext) -> None:
        """Show all networks that support validator staking."""
        lines = []
        for net, token in Config.NETWORK_STAKE_TOKEN.items():
            validators = await self.bot.db.get_pos_validators_for_network(ctx.guild.id, net)
            active_count = sum(1 for v in validators if v["is_active"])
            total_stake = to_human(sum(v["stake_amount"] for v in validators if v["is_active"]))
            gc = gas_coin_for_network(net)
            gc_cfg = Config.TOKENS.get(gc, {})
            gc_emoji = gc_cfg.get("emoji", "●")
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
            .footer("Register with .vregister <network> <amount>")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @validator.command(name="stats", aliases=["vstats"])
    @guild_only
    @ensure_registered
    async def vstats(self, ctx: DiscoContext) -> None:
        """View your validator statistics."""
        validators = await self.bot.db.get_user_pos_validators(ctx.author.id, ctx.guild.id)
        if not validators:
            await ctx.reply_error("You are not registered as a validator. Use `.vregister` to start.")
            return

        _b = card("📊 Your Validator Stats", color=C_PURPLE)
        for v in validators:
            lock_str = ""
            _slu3 = v["stake_locked_until"]
            _slu3_ts = _slu3.timestamp() if hasattr(_slu3, "timestamp") else _slu3
            if _slu3_ts and time.time() < _slu3_ts:
                remaining = int(_slu3_ts - time.time())
                lock_str = f"\n🔒 **Locked:** {remaining//3600}h {(remaining%3600)//60}m remaining"
            status = "✅ Active" if v["is_active"] else "❌ Inactive"
            _b.field(
                f"🔗 {v['network']}",
                (
                    f"**Status:** {status}\n"
                    f"**Stake:** {v.h('stake_amount'):,.4f} {v['stake_token']}\n"
                    f"**Blocks Validated:** {v['total_blocks_validated']}\n"
                    f"**Total Earned:** {v.h('total_rewards_earned'):,.4f} USD\n"
                    f"**Slash Count:** {v['slash_count']}"
                    f"{lock_str}"
                ),
                True,
            )
        await ctx.reply(embed=_b.build(), mention_author=False)


    # ── Top-level aliases ───────────────────────────────────────────────────

    @commands.command(name="mydelegations", hidden=True)
    async def _alias_mydelegations(self, ctx: DiscoContext) -> None:
        """Top-level alias for .validator delegations"""
        await self.my_delegations(ctx)

    @commands.command(name="mydels", hidden=True)
    async def _alias_mydels(self, ctx: DiscoContext) -> None:
        """Top-level alias for .validator delegations"""
        await self.my_delegations(ctx)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Validators(bot))
