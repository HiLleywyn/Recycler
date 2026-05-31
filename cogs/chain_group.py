"""
cogs/chain_group.py  -  Unified Chain, Contracts & Mining command group

Restructures chain, contracts, and mining commands under a single `/chain` group:
  - /chain              Show latest block
  - /chain block        Show block info
  - /chain tx           Show transaction info
  - /chain contract ... Contract subgroup (deploy, call, info, list, events, fund, withdraw, txs, pause, resume)
  - /chain mine ...     Mining subgroup (rigs, buy, sell, status, history, solo, pool, group, network)

Architecture (contracts):
  - Contracts are JSON-defined programs deployed by players on a network
  - Each contract has: address, owner, persistent state, token balance, event log
  - Functions are sequences of ops (receive, send, swap, buy, sell, require, etc.)
  - Contract calls go through the mempool and execute during validator block processing
  - The canonical ContractEngine lives in cogs/contracts.py and handles op-by-op
    execution with atomic rollback on revert; this module only uses it for
    definition validation via ContractEngine.validate_definition().

Analogy to Arcadia:
  - contract_deploy mempool action  = CREATE transaction
  - contract_call mempool action    = CALL transaction
  - contract.state                  = contract storage
  - virtual_uid                     = contract account address (holds balances)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from core.framework.heartbeat import pulse, register_interval
import math
import random
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from cogs.shop import _item_stat, notify_item_levelup_ready, cap_xp
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.cooldowns import user_cooldown
from core.framework.embed import card
from core.framework.network import normalize_full as normalize_network_full
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.tx import set_tx
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_SUCCESS, ConfirmView, fmt_bonus,
    fmt_token, fmt_ts, fmt_usd, fmt_gas, FormatKit, mention,
)

from core.framework.fuzzy import suggest_subcommand
from core.framework.scale import to_raw, to_human
from constants.economy import CHAIN_SWITCH_COOLDOWN as _CHAIN_SWITCH_COOLDOWN
from constants.validators import NET_SHORT as _NET_SHORT_MAP

log = logging.getLogger(__name__)


async def _mint_group_vault_tokens(
    db,
    guild_id: int,
    group_id: str,
    grp_row: dict,
    blocks_won: int,
    bot=None,
    guild=None,
    mining_chain: str = "",
) -> None:
    """Mint ``blocks_won`` group tokens into the vault and update the vault LP.

    Called from both SUN and non-SUN block reward paths whenever a group wins
    blocks AND has a token + PoW network bound via ``.group token network``.

    Requires at least 2 active group members; skips minting otherwise.
    When bot + guild are provided, sends DM notifications to group members and
    posts a summary to the vault_feed_channel.

    Auto-binds the group's token to the currently-mined PoW network the
    first time a group with no token_network wins a block. This means
    founders never need to manually run ``.group token network <x>``.

    Silently skips if:
      - token_symbol is unset on the group (no tag set yet)
      - fewer than 2 group members (minimum to activate group token mining)
      - price rows are missing (uses 0.01/$0.10 fallbacks)
      - mining_chain is provided and does not match the token's network coin
    """
    sym = grp_row.get("token_symbol") or ""
    net_name = grp_row.get("token_network") or ""
    if not sym or blocks_won <= 0:
        return 0

    # ── Auto-bind unbound group tokens to the chain currently being mined ────
    # If a group has a token symbol but never ran `.group token network <x>`,
    # auto-assign it to the PoW network they're actively mining on. This means
    # groups don't have to know about the bind step - the first block they win
    # silently binds them and they start accumulating vault tokens immediately.
    if not net_name and mining_chain:
        auto_net_name = next(
            (n for n, c in Config.NETWORK_COINS.items()
             if c and c.upper() == mining_chain.upper()),
            "",
        )
        # Only auto-bind PoW networks (MTA/SUN), never stablecoin chains.
        # The token's ``guild_tokens.network`` stays on ``"Moon Network"`` --
        # only the mining chain / vault pairing is recorded here.
        if auto_net_name in ("Moneta Chain", "Sun Network"):
            try:
                await db.set_group_token_network(guild_id, group_id, sym, auto_net_name)
                await db.execute(
                    "UPDATE guild_tokens SET vault_locked=FALSE, trading_enabled=TRUE, "
                    "network='Moon Network' WHERE guild_id=$1 AND symbol=$2",
                    guild_id, sym,
                )
                net_name = auto_net_name
                grp_row = dict(grp_row)
                grp_row["token_network"] = auto_net_name
                log.info(
                    "[vault_mint] auto-bound group=%s sym=%s -> %s (mining=%s)",
                    group_id, sym, auto_net_name, mining_chain,
                )
            except Exception as exc:
                log.error(
                    "[vault_mint] auto-bind FAILED group=%s sym=%s: %s",
                    group_id, sym, exc, exc_info=True,
                )
                return 0

    if not net_name:
        return 0

    net_coin = Config.NETWORK_COINS.get(net_name, "")
    if not net_coin:
        return 0

    if mining_chain and net_coin and net_coin.upper() != mining_chain.upper():
        return 0

    # Require at least 2 members to mine group tokens
    members = await db.get_group_members(guild_id, group_id)
    if len(members) < 2:
        return 0

    _TOKENS_PER_BLOCK = 500  # group tokens minted per sealed block

    # Mint tokens into the vault balance
    minted = blocks_won * _TOKENS_PER_BLOCK
    await db.mint_vault_tokens(guild_id, group_id, float(minted))

    # Update circulating supply on the guild_tokens row (raw 10^18-scaled integer)
    from core.framework.scale import to_raw as _tr
    await db.execute(
        "UPDATE guild_tokens SET circulating_supply = circulating_supply + $1 "
        "WHERE guild_id=$2 AND symbol=$3",
        _tr(float(minted)), guild_id, sym,
    )

    # Fetch prices for LP ratio calculation
    tok_row = await db.get_price(sym, guild_id)
    net_row = await db.get_price(net_coin, guild_id)
    tok_price = float(tok_row["price"]) if tok_row else 0.01
    net_price = float(net_row["price"]) if net_row else 0.10

    # Each block adds _TOKENS_PER_BLOCK group tokens to the LP plus the
    # price-equivalent amount of network coin to keep the pool balanced.
    ratio = tok_price / max(net_price, 1e-12)
    net_delta = minted * ratio

    # Drain the group's reserve_btc bucket into the LP so the MTA the group
    # mines actually backs the LP instead of sitting unused. We drain up to
    # net_delta of real MTA on every mint and pair it with the freshly-minted
    # group tokens. Any leftover stays in reserve_btc for upgrades.
    if net_coin.upper() == "MTA":
        try:
            _rbrow = await db.fetch_one(
                "SELECT reserve_btc FROM mining_groups WHERE guild_id=$1 AND group_id=$2",
                guild_id, group_id,
            )
            reserve_btc_h = _rbrow.h("reserve_btc") if _rbrow else 0.0
        except Exception:
            reserve_btc_h = 0.0
        pulled = min(reserve_btc_h, net_delta)
        if pulled > 0:
            try:
                await db.add_group_reserve_btc(guild_id, group_id, -pulled)
                log.info(
                    "[vault_mint] drained %.8f MTA from reserve into LP (group=%s, ideal=%.8f)",
                    pulled, group_id, net_delta,
                )
            except Exception as exc:
                log.error(
                    "[vault_mint] reserve_btc drain FAILED group=%s: %s",
                    group_id, exc,
                )

    # Vault pool now pairs the group token with the Moon-Network WRAPPED
    # coin (MMTA / MSUN) instead of the raw PoW coin. The reserve_btc drain
    # above still burns native MTA proportionally so the group's treasury
    # bucket doesn't pile up -- each native coin drained is effectively
    # wrapped 1:1 into the MMTA reserves we add to the pool below.
    from constants.moons import wrapped_coin as _wrapped_coin
    wrapped_sym = _wrapped_coin(net_coin)
    wrapped_price_row = await db.get_price(wrapped_sym, guild_id)
    wrapped_price = float(wrapped_price_row["price"]) if wrapped_price_row else net_price

    # Ensure the vault pool exists before adding LP. Groups that had their
    # network set via migration (not the group token network command) may
    # not have had create_vault_pool called yet.
    await db.create_vault_pool(guild_id, sym, wrapped_sym, tok_price, wrapped_price)

    log.info(
        "[vault_mint] guild=%d group=%s sym=%s wrapped=%s minted=%s ratio=%.6f net_delta=%.6f",
        guild_id, group_id, sym, wrapped_sym, minted, ratio, net_delta,
    )
    try:
        result = await db.vault_add_to_pool(guild_id, sym, wrapped_sym, float(minted), net_delta)
    except Exception as exc:
        log.error(
            "[vault_mint] vault_add_to_pool FAILED guild=%d sym=%s wrapped=%s: %s",
            guild_id, sym, wrapped_sym, exc, exc_info=True,
        )
        result = None
    if result is None:
        log.warning(
            "[vault_mint] vault_add_to_pool returned None for %s/%s guild=%d  -  pool missing or update failed",
            sym, wrapped_sym, guild_id,
        )
    else:
        log.info(
            "[vault_mint] LP updated %s/%s guild=%d  reserve_a=%s reserve_b=%s",
            sym, wrapped_sym, guild_id, result.get("reserve_a"), result.get("reserve_b"),
        )

    # Notifications: DMs to group members + vault feed post
    if not bot or not guild:
        return int(minted)

    new_vault_bal = float(grp_row.get("vault_token_bal") or 0.0) + minted
    grp_name = grp_row.get("name", group_id)
    tok_usd = minted * tok_price
    usd_suffix = f" (~{fmt_usd(tok_usd)})" if tok_price > 0 else ""
    lp_net_str = f"+{net_delta:.4f} {net_coin}" if net_delta > 0 else "price pending"

    _tok_trading_row = await db.fetch_one(
        "SELECT trading_enabled FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
        guild_id, sym,
    )
    _tok_trading_enabled = bool((_tok_trading_row or {}).get("trading_enabled", False))

    notif_embed = (
        card(
            f"⛏️ {grp_name} - Vault Mint",
            description=f"**{blocks_won}** block{'s' if blocks_won != 1 else ''} mined - group token minted to vault.",
            color=C_GOLD,
        )
        .field("Minted",      f"**+{minted:,} {sym}**{usd_suffix}",             True)
        .field("Vault Total",  f"**{new_vault_bal:,.2f} {sym}**",                True)
        .field("Network",      f"{net_name} ({net_coin})",                       True)
        .field("LP Added",     f"+{minted:,} {sym} / {lp_net_str}",             True)
        .footer(
            "Token trading: enabled" if _tok_trading_enabled
            else "Token trading: locked -- admins can force-enable with .admin grouptoken enable"
        )
        .build()
    )

    # DM the group founder
    trades_cog = bot.get_cog("Trades")
    if trades_cog:
        founder_id = grp_row.get("founder_id")
        if founder_id:
            await trades_cog._dm(founder_id, guild, notif_embed, category="mining")

    # Post to vault_feed_channel if configured
    settings = await db.get_guild_settings(guild_id)
    feed_ch_id = settings.get("vault_feed_channel")
    if feed_ch_id:
        feed_ch = guild.get_channel(int(feed_ch_id))
        if feed_ch:
            try:
                await feed_ch.send(embed=notif_embed)
            except Exception:
                pass

    return int(minted)


# ══════════════════════════════════════════════════════════════════════════════
# Contract templates
# ══════════════════════════════════════════════════════════════════════════════

_TEMPLATE_LIMIT_ORDER: dict = {
    "functions": {
        "place": {
            "description": "Lock tokens and set a price target. Anyone can call execute() once met.",
            "params": {
                "token_in":     "string",
                "token_out":    "string",
                "amount":       "number",
                "target_price": "number",
                "pool_id":      "string",
            },
            "steps": [
                {"op": "receive",    "symbol": "$token_in",     "amount": "$amount"},
                {"op": "set_state",  "key": "token_in",          "value": "$token_in"},
                {"op": "set_state",  "key": "token_out",         "value": "$token_out"},
                {"op": "set_state",  "key": "amount",            "value": "$amount"},
                {"op": "set_state",  "key": "target_price",      "value": "$target_price"},
                {"op": "set_state",  "key": "pool_id",           "value": "$pool_id"},
                {"op": "set_state",  "key": "owner",             "value": "$caller"},
                {"op": "set_state",  "key": "filled",            "value": 0},
                {"op": "emit",       "event": "OrderPlaced",
                 "data": {"token_in": "$token_in", "amount": "$amount", "target": "$target_price"}},
            ],
        },
        "execute": {
            "description": "Execute the swap if the price condition is met. Callable by anyone (or the keeper).",
            "keeper": True,
            "params": {},
            "steps": [
                {"op": "require",       "lhs": "{{filled}}", "op_cmp": "eq", "rhs": 0},
                {"op": "get_state",     "key": "token_out",     "as": "token_out"},
                {"op": "get_state",     "key": "target_price",  "as": "target_price"},
                {"op": "require_price", "symbol": "{{token_out}}", "op_cmp": "lte", "value": "{{target_price}}"},
                {"op": "get_state",     "key": "token_in",      "as": "token_in"},
                {"op": "get_state",     "key": "amount",        "as": "amount"},
                {"op": "get_state",     "key": "pool_id",       "as": "pool_id"},
                {"op": "swap",          "pool_id": "{{pool_id}}",
                 "token_in": "{{token_in}}", "token_out": "{{token_out}}", "amount": "{{amount}}"},
                {"op": "get_state",     "key": "owner",         "as": "owner"},
                {"op": "send",          "to": "{{owner}}",      "symbol": "{{token_out}}", "amount": "$output"},
                {"op": "set_state",     "key": "filled",        "value": 1},
                {"op": "emit",          "event": "OrderExecuted", "data": {"received": "$output"}},
            ],
        },
        "cancel": {
            "description": "Cancel the order and refund tokens. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",    "lhs": "{{filled}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",  "key": "token_in",  "as": "token_in"},
                {"op": "get_state",  "key": "amount",    "as": "amount"},
                {"op": "get_state",  "key": "owner",     "as": "owner"},
                {"op": "send",       "to": "{{owner}}",  "symbol": "{{token_in}}", "amount": "{{amount}}"},
                {"op": "set_state",  "key": "filled",    "value": 1},
                {"op": "emit",       "event": "OrderCancelled"},
            ],
        },
    },
}

_TEMPLATE_ESCROW: dict = {
    "functions": {
        "deposit": {
            "description": "Deposit tokens into escrow. Owner only.",
            "params": {"symbol": "string", "amount": "number", "recipient": "string"},
            "steps": [
                {"op": "require_caller",  "user_id": "{{owner}}"},
                {"op": "receive",         "symbol": "$symbol",    "amount": "$amount"},
                {"op": "set_state",       "key": "symbol",        "value": "$symbol"},
                {"op": "set_state",       "key": "amount",        "value": "$amount"},
                {"op": "set_state",       "key": "recipient",     "value": "$recipient"},
                {"op": "set_state",       "key": "released",      "value": 0},
                {"op": "emit",            "event": "Deposited",
                 "data": {"symbol": "$symbol", "amount": "$amount", "recipient": "$recipient"}},
            ],
        },
        "release": {
            "description": "Release escrowed tokens to recipient. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",        "lhs": "{{released}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",      "key": "symbol",    "as": "symbol"},
                {"op": "get_state",      "key": "amount",    "as": "amount"},
                {"op": "get_state",      "key": "recipient", "as": "recipient"},
                {"op": "send",           "to": "{{recipient}}", "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "released",  "value": 1},
                {"op": "emit",           "event": "Released"},
            ],
        },
        "refund": {
            "description": "Refund tokens back to owner. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",        "lhs": "{{released}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",      "key": "symbol",  "as": "symbol"},
                {"op": "get_state",      "key": "amount",  "as": "amount"},
                {"op": "get_state",      "key": "owner",   "as": "owner"},
                {"op": "send",           "to": "{{owner}}", "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "released", "value": 1},
                {"op": "emit",           "event": "Refunded"},
            ],
        },
    },
}

_TEMPLATE_VESTING: dict = {
    "functions": {
        "fund": {
            "description": "Fund the vesting contract with the total allocation. Owner only.",
            "params": {"symbol": "string", "amount": "number", "recipient": "string",
                       "duration_days": "number", "cliff_days": "number"},
            "steps": [
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "receive",        "symbol": "$symbol",        "amount": "$amount"},
                {"op": "set_state",      "key": "symbol",            "value": "$symbol"},
                {"op": "set_state",      "key": "total",             "value": "$amount"},
                {"op": "set_state",      "key": "claimed",           "value": 0},
                {"op": "set_state",      "key": "recipient",         "value": "$recipient"},
                {"op": "set_state",      "key": "start_ts",          "value": "$now"},
                {"op": "set_state",      "key": "duration_secs",     "value": {"mul": ["$duration_days", 86400]}},
                {"op": "set_state",      "key": "cliff_secs",        "value": {"mul": ["$cliff_days", 86400]}},
                {"op": "emit",           "event": "Funded",
                 "data": {"symbol": "$symbol", "amount": "$amount", "recipient": "$recipient"}},
            ],
        },
        "claim": {
            "description": "Claim vested tokens. Recipient only.",
            "params": {},
            "steps": [
                {"op": "require_caller",  "user_id": "{{recipient}}"},
                {"op": "require_time",    "op_cmp": "gte", "ts": {"add": ["{{start_ts}}", "{{cliff_secs}}"]}},
                {"op": "get_state",       "key": "total",         "as": "total"},
                {"op": "get_state",       "key": "claimed",       "as": "claimed"},
                {"op": "get_state",       "key": "start_ts",      "as": "start_ts"},
                {"op": "get_state",       "key": "duration_secs", "as": "duration_secs"},
                {"op": "get_state",       "key": "symbol",        "as": "symbol"},
                {"op": "get_state",       "key": "recipient",     "as": "recipient"},
                {"op": "vested_claim",    "total": "{{total}}", "claimed": "{{claimed}}",
                 "start_ts": "{{start_ts}}", "duration_secs": "{{duration_secs}}"},
                {"op": "send",            "to": "{{recipient}}", "symbol": "{{symbol}}", "amount": "$output"},
                {"op": "set_state",       "key": "claimed",      "value": {"add": ["{{claimed}}", "$output"]}},
                {"op": "emit",            "event": "Claimed", "data": {"amount": "$output"}},
            ],
        },
    },
}

_TEMPLATE_MULTISIG: dict = {
    "functions": {
        "setup": {
            "description": "Initialize signers list and approval threshold.",
            "params": {"threshold": "number"},
            "steps": [
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "set_state",      "key": "threshold",  "value": "$threshold"},
                {"op": "set_state",      "key": "approvals",  "value": 0},
                {"op": "set_state",      "key": "executed",   "value": 0},
                {"op": "emit",           "event": "Initialized", "data": {"threshold": "$threshold"}},
            ],
        },
        "deposit": {
            "description": "Deposit tokens to be held until execution.",
            "params": {"symbol": "string", "amount": "number"},
            "steps": [
                {"op": "receive",   "symbol": "$symbol",  "amount": "$amount"},
                {"op": "set_state", "key": "symbol",      "value": "$symbol"},
                {"op": "set_state", "key": "amount",      "value": "$amount"},
                {"op": "emit",      "event": "Deposited", "data": {"symbol": "$symbol", "amount": "$amount"}},
            ],
        },
        "approve": {
            "description": "Add your approval. Increments approval count.",
            "params": {},
            "steps": [
                {"op": "require",   "lhs": "{{executed}}", "op_cmp": "eq", "rhs": 0},
                {"op": "set_state", "key": "approvals",    "value": {"add": ["{{approvals}}", 1]}},
                {"op": "emit",      "event": "Approved",   "data": {"by": "$caller", "total": "{{approvals}}"}},
            ],
        },
        "execute": {
            "description": "Execute if approval threshold met. Sends tokens to owner.",
            "params": {"to": "string"},
            "steps": [
                {"op": "require",        "lhs": "{{executed}}",  "op_cmp": "eq", "rhs": 0},
                {"op": "require",        "lhs": "{{approvals}}", "op_cmp": "gte", "rhs": "{{threshold}}"},
                {"op": "get_state",      "key": "symbol",  "as": "symbol"},
                {"op": "get_state",      "key": "amount",  "as": "amount"},
                {"op": "send",           "to": "$to",      "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "executed", "value": 1},
                {"op": "emit",           "event": "Executed", "data": {"to": "$to"}},
            ],
        },
    },
}

TEMPLATES: dict[str, dict] = {
    "limit_order": _TEMPLATE_LIMIT_ORDER,
    "escrow":      _TEMPLATE_ESCROW,
    "vesting":     _TEMPLATE_VESTING,
    "multisig":    _TEMPLATE_MULTISIG,
}


# ══════════════════════════════════════════════════════════════════════════════
# Keeper helpers (module-level)
# ══════════════════════════════════════════════════════════════════════════════

def _keeper_check_price(step: dict, prices: dict[str, float]) -> bool:
    """Cheaply evaluate a require_price op against a prices dict {SYMBOL: price}.
    Returns True if the condition is satisfied (keeper should trigger)."""
    sym = str(step.get("symbol", "")).upper()
    op_cmp = step.get("op_cmp", "lte")
    try:
        target = float(step.get("value", 0))
    except (TypeError, ValueError):
        return False
    current = prices.get(sym)
    if current is None:
        return False
    if op_cmp == "lte": return current <= target
    if op_cmp == "lt":  return current <  target
    if op_cmp == "gte": return current >= target
    if op_cmp == "gt":  return current >  target
    if op_cmp == "eq":  return abs(current - target) < 1e-9
    return False


def _keeper_check_time(step: dict) -> bool:
    """Cheaply evaluate a require_time op. Returns True if condition is satisfied."""
    op_cmp = step.get("op_cmp", "gte")
    try:
        ts = float(step.get("ts", 0))
    except (TypeError, ValueError):
        return False
    now = time.time()
    if op_cmp == "gte": return now >= ts
    if op_cmp == "lte": return now <= ts
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Mining helpers (module-level)
# ══════════════════════════════════════════════════════════════════════════════

_SUN = Config.POW_NETWORKS["SUN"]
_BTC = Config.POW_NETWORKS["MTA"]
_RIGS = Config.MINING_RIGS
_HS = Config.SHOP_ITEMS["hashstone"]   # Hashstone (renamed from Sunstone)
_SS = _HS  # backward compat alias

# Tick interval (seconds)
_TICK = 60


def _hashstone_level_from_xp(xp: float) -> int:
    """Compute Hashstone level from cumulative XP.
    Level N->N+1 costs N * xp_per_level_base. Total for level L = base*L*(L-1)/2.
    Inverse: L = floor((1 + sqrt(1 + 8*xp/base)) / 2), clamped to [1, max_level].
    """
    base = _HS["xp_per_level_base"]
    max_level = _HS["max_level"]
    if base <= 0 or xp <= 0:
        return 1
    import math as _math
    level = int((1 + _math.sqrt(1 + 8 * xp / base)) / 2)
    return max(1, min(level, max_level))


# backward compat alias
_sunstone_level_from_xp = _hashstone_level_from_xp


def _pow_current_reward(block_height: int, cfg: dict) -> float:
    """Compute current block reward for any PoW network given its config."""
    halvings = block_height // cfg["halving_blocks"]
    reward = cfg["initial_reward"] / (2 ** halvings)
    reward = max(reward, cfg["min_reward"])
    # Warmup: cubic ramp from 0→100% over the first N blocks.
    # Cubic curve means early blocks give almost nothing (block 25 out of
    # 200 = 0.2% of full reward), then rewards accelerate in the back half.
    # This prevents first-miner advantage while still reaching full reward.
    warmup = cfg.get("warmup_blocks", 0)
    if warmup > 0 and block_height < warmup:
        progress = block_height / warmup
        reward *= progress * progress * progress  # cubic: (h/W)^3
        reward = max(reward, cfg["min_reward"])   # re-enforce floor so height=0 never yields 0
    return reward


# Legacy single-network helpers (kept for any callers outside mining tick)
def _current_reward(block_height: int) -> float:
    return _pow_current_reward(block_height, _SUN)


def _current_btc_reward(block_height: int) -> float:
    return _pow_current_reward(block_height, _BTC)


def _blocks_until_halving(block_height: int) -> int:
    next_halving = (block_height // _SUN["halving_blocks"] + 1) * _SUN["halving_blocks"]
    return next_halving - block_height


def _poisson_sample(lam: float) -> int:
    """Knuth product-method Poisson sampler (normal approx for large lambda)."""
    if lam <= 0:
        return 0
    if lam < 30:
        L = math.exp(-lam)
        k = 0
        p = 1.0
        while p > L:
            p *= random.random()
            k += 1
        return k - 1
    # Normal approximation for large lambda
    return max(0, round(random.gauss(lam, math.sqrt(lam))))


async def _maybe_retarget(
    db,
    guild_id: int,
    network: dict,
    new_height: int,
) -> None:
    """Moneta-style difficulty retarget every 2016 blocks (clamped +/-4x)."""
    window = _SUN["difficulty_window"]
    last_retarget = network.get("last_retarget_height", 0) or 0
    if new_height - last_retarget < window:
        return

    last_ts = network.get("last_retarget_ts") or time.time()
    elapsed = time.time() - last_ts
    target_elapsed = window * _SUN["target_block_time"]  # 2016 * 600s = 1,209,600s

    # Clamp ratio to [0.25, 4.0] to prevent extreme swings
    ratio = max(0.25, min(4.0, elapsed / target_elapsed)) if elapsed > 0 else 1.0
    current_difficulty = network.get("difficulty") or _SUN["initial_difficulty"]
    new_difficulty = max(1.0, current_difficulty * ratio)

    await db.update_network_difficulty(guild_id, new_difficulty, time.time(), new_height)


# ══════════════════════════════════════════════════════════════════════════════
# ChainGroup Cog
# ══════════════════════════════════════════════════════════════════════════════

class ChainGroup(commands.Cog):
    """Unified chain, contracts, and mining commands."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.chain_tick.start()
        self._keeper_loop.start()
        self.mining_tick.start()
        register_interval("chain_tick", Config.CHAIN_BLOCK_INTERVAL)
        register_interval("keeper_loop", 60)
        register_interval("mining_tick", _TICK)

    def cog_unload(self) -> None:
        self.chain_tick.cancel()
        self._keeper_loop.cancel()
        self.mining_tick.cancel()

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "chain")

    # ══════════════════════════════════════════════════════════════════════════
    # Background task: Block bundling (from chain.py)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=Config.CHAIN_BLOCK_INTERVAL)
    async def chain_tick(self) -> None:
        for guild in self.bot.guilds:
            if not await self.bot.db.module_enabled(guild.id, "chain"):
                continue
            try:
                await self._bundle_block(guild)
            except Exception as exc:
                logging.getLogger(__name__).warning("chain_tick failed for %s: %s", guild.name, exc)
        pulse("chain_tick")

    @chain_tick.before_loop
    async def before_chain_tick(self) -> None:
        await self.bot.wait_until_ready()

    # Derive network short codes from Config.TOKENS (no more hardcoded list)
    _NETWORKS: tuple[str, ...] = tuple(sorted({
        _NET_SHORT_MAP[cfg["network"]]
        for cfg in Config.TOKENS.values()
        if cfg.get("network") and cfg["network"] in _NET_SHORT_MAP
    }))

    async def _bundle_block(self, guild: discord.Guild) -> None:
        from core.framework import session_log as _sl
        sl = _sl.get()
        # Each network has its own sequential block counter starting at 1.
        for network in self._NETWORKS:
            last_net = await self.bot.db.get_latest_chain_block(guild.id, network=network)
            since_ts = last_net["ts"] if last_net else 0.0
            next_num = (last_net["block_num"] if last_net else 0) + 1

            pending_txns = await self.bot.db.get_pending_txns_since(guild.id, since_ts, network=network)
            if sl:
                sl.info(
                    f"chain_tick  net={network}  last_block=#{last_net['block_num'] if last_net else 'none'}"
                    f"  since_ts={since_ts:.2f}  pending={len(pending_txns)}"
                )
            if not pending_txns:
                continue  # No activity on this network  -  skip block creation

            tx_hashes  = sorted(t["tx_hash"] for t in pending_txns)
            merkle     = hashlib.sha256("|".join(tx_hashes).encode()).hexdigest()
            raw        = f"{guild.id}:{network}:{next_num}:{merkle}"
            block_hash = "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]

            await self.bot.db.create_chain_block(guild.id, next_num, block_hash, len(tx_hashes), network=network)
            # Pass exact hashes so we never steal txns that arrived during bundling
            await self.bot.db.tag_transactions_with_block(guild.id, next_num, since_ts, network=network, tx_hashes=tx_hashes)
            # ARC/DSC are PoS chains  -  blocks auto-confirm (no PoW miner needed)
            if network not in ("sun", "mta"):
                await self.bot.db.mine_chain_block(guild.id, next_num, miner_id=None, network=network)

            # Log to session file
            if sl is not None:
                oracle_count = sum(
                    1 for t in pending_txns
                    if any(tag in t["tx_hash"].upper() for tag in ("_ARB", "_ORACLE"))
                    or t.get("tx_type", "") in ("ARB", "ORACLE_REBALANCE")
                )
                sl.chain_block(
                    guild_name=guild.name,
                    network=network,
                    block_num=next_num,
                    tx_count=len(tx_hashes),
                    oracle_count=oracle_count,
                    user_count=len(tx_hashes) - oracle_count,
                )

            await self.bot.bus.publish(
                "block_bundled",
                guild=guild,
                block_num=next_num,
                block_hash=block_hash,
                tx_count=len(tx_hashes),
                network=network,
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Background task: Keeper loop (from contracts.py)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=60)
    async def _keeper_loop(self) -> None:
        """Scan all active contracts for keeper-eligible functions and auto-execute them
        when their leading price/time condition is met."""
        for guild in self.bot.guilds:
            if not await self.bot.db.module_enabled(guild.id, "chain"):
                continue
            try:
                await self._keeper_run_guild(guild)
            except Exception as exc:
                log.error("Keeper loop error for guild %s: %s", guild.id, exc)
        pulse("keeper_loop")

    @_keeper_loop.before_loop
    async def _before_keeper(self) -> None:
        await self.bot.wait_until_ready()

    async def _keeper_run_guild(self, guild: discord.Guild) -> None:
        contracts = await self.bot.db.get_all_active_contracts(guild.id)
        if not contracts:
            return

        # Build a price snapshot once per guild tick for efficiency
        price_rows = await self.bot.db.get_all_prices(guild.id)
        prices: dict[str, float] = {r["symbol"]: float(r["price"]) for r in price_rows}

        settings = await self.bot.db.get_guild_settings(guild.id)
        contracts_channel_id = settings.get("contracts_channel")

        for contract in contracts:
            definition = contract.get("definition") or {}
            if isinstance(definition, str):
                try:
                    definition = json.loads(definition)
                except Exception:
                    continue
            functions = definition.get("functions", {})

            for fn_name, fn_def in functions.items():
                if not fn_def.get("keeper"):
                    continue
                steps = fn_def.get("steps", [])
                if not steps:
                    continue

                # Check if there's already a pending keeper call in the mempool
                pending = await self.bot.db.get_pending_mempool(
                    guild.id, contract["network"], limit=50
                )
                already_queued = any(
                    a.get("action_type") == "contract_call"
                    and json.loads(a.get("payload", "{}")).get("address") == contract["address"]
                    and json.loads(a.get("payload", "{}")).get("function") == fn_name
                    for a in pending
                )
                if already_queued:
                    continue

                # Load contract state to resolve state references in conditions
                state = contract.get("state") or {}
                if isinstance(state, str):
                    try:
                        state = json.loads(state)
                    except Exception:
                        state = {}

                # Walk the first ops to find and evaluate the leading condition
                condition_met = True
                for step in steps:
                    op = step.get("op", "")
                    if op == "require_price":
                        # Resolve state-templated symbol/value
                        sym = step.get("symbol", "")
                        if sym.startswith("{{") and sym.endswith("}}"):
                            sym = state.get(sym[2:-2].strip(), sym)
                        value = step.get("value")
                        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                            value = state.get(value[2:-2].strip(), value)
                        resolved_step = dict(step, symbol=sym, value=value)
                        if not _keeper_check_price(resolved_step, prices):
                            condition_met = False
                        break
                    elif op == "require_time":
                        if not _keeper_check_time(step):
                            condition_met = False
                        break
                    elif op in ("get_state", "set_state", "require"):
                        # Non-price/time ops: skip to next op looking for a condition
                        continue
                    else:
                        # Hit an action op before finding a price/time condition  -  nothing to auto-trigger
                        condition_met = False
                        break

                if not condition_met:
                    continue

                # Condition met  -  submit a keeper call to the mempool
                keeper_uid = Config.COMMUNITY_RESERVE_USER_ID
                payload = {
                    "address":  contract["address"],
                    "function": fn_name,
                    "args":     {},
                }
                try:
                    action_id = await self.bot.db.add_to_mempool(
                        guild_id=guild.id,
                        user_id=keeper_uid,
                        network=contract["network"],
                        action_type="contract_call",
                        payload=payload,
                        gas_price="medium",
                        gas_fee=0.0,
                    )
                    log.info(
                        "Keeper queued %s.%s() -> mempool #%s (guild %s)",
                        contract["address"], fn_name, action_id, guild.id,
                    )
                except Exception as exc:
                    log.error("Keeper failed to queue %s.%s: %s", contract["address"], fn_name, exc)
                    continue

                # Post notification to contracts channel if configured
                if contracts_channel_id:
                    ch = guild.get_channel(contracts_channel_id)
                    if ch:
                        embed = card(
                            "🤖 Keeper Triggered",
                            description=(
                                f"**{contract['name']}**.`{fn_name}()` condition met.\n"
                                f"`{contract['address']}`"
                            ),
                            color=C_INFO,
                        ).footer(f"Queued to {contract['network']} mempool • auto-executed by keeper").build()
                        try:
                            await ch.send(embed=embed)
                        except Exception:
                            pass

    # ══════════════════════════════════════════════════════════════════════════
    # Background task: Mining tick (from mining.py)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=_TICK)
    async def mining_tick(self) -> None:
        for guild in self.bot.guilds:
            if not await self.bot.db.module_enabled(guild.id, "chain"):
                continue
            for symbol, cfg in Config.POW_NETWORKS.items():
                try:
                    await self._process_pow_guild(guild, symbol, cfg)
                except Exception as exc:
                    logging.getLogger(__name__).error(
                        "mining_tick failed for %s/%s: %s", guild.name, symbol, exc, exc_info=True
                    )
        pulse("mining_tick")

    async def _process_pow_guild(self, guild: discord.Guild, symbol: str, cfg: dict) -> None:
        """Unified PoW mining tick for a single network (SUN, MTA, or any future chain).

        Uses pow_network_state for network state and rig_chain_assignments for per-user
        hashrate. SUN also processes solo/pool/group mining modes; other chains use
        proportional-distribution only (no solo/pool/group split  -  all miners in the
        pool-style payout from their assigned hashrate share).
        """
        is_sun = symbol == "SUN"

        # ── Fetch / seed network state ─────────────────────────────────────
        network = await self.bot.db.get_pow_network(guild.id, symbol)
        if not network:
            await self.bot.db.seed_pow_network(guild.id, symbol)
            return

        # ── Compute total network hashrate ────────────────────────────────
        all_rigs = await self.bot.db.get_all_guild_chain_rigs(guild.id, symbol)
        skipped_rigs = [r["rig_id"] for r in all_rigs if r["rig_id"] not in _RIGS]
        if skipped_rigs:
            logging.getLogger(__name__).warning(
                "mining_tick %s/%s: skipped %d rig(s) with unknown IDs: %s",
                guild.name, symbol, len(skipped_rigs), skipped_rigs[:5],
            )
        total_hr = sum(
            _RIGS[r["rig_id"]]["hashrate"] * r["quantity"]
            for r in all_rigs
            if r["rig_id"] in _RIGS
        )

        if total_hr <= 0:
            logging.getLogger(__name__).debug(
                "mining_tick %s/%s: total_hr=0, skipping", guild.name, symbol,
            )
            # No miners, but still drain any pending bundle blocks so the chain
            # doesn't freeze when everyone has their rigs on another network.
            net_key = symbol.lower()
            pending_unbundled = await self.bot.db.get_oldest_pending_chain_blocks(
                guild.id, limit=10, network=net_key,
            )
            for blk in pending_unbundled:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network=net_key)
            # Still process the mempool so pending transactions aren't stuck forever
            # when there are temporarily no miners. No fee distribution (total_hr=0).
            if symbol == "SUN":
                await self._process_sun_mempool(guild, 0.0)
            elif symbol == "MTA":
                await self._process_btc_mempool(guild, 0.0)
            return

        # ── Difficulty maintenance (bidirectional live tracking) ─────────
        # Tie difficulty directly to actual live hashrate so it adjusts
        # both UP (whale joins) and DOWN (whale leaves) every tick.
        # This prevents chain bricking: if a whale spikes difficulty then
        # leaves, remaining miners aren't stuck at impossible difficulty.
        difficulty = network.get("difficulty") or cfg["initial_difficulty"]
        live_target = max(
            total_hr * cfg["target_block_time"],
            cfg["initial_difficulty"],
        )

        if live_target > 0:
            # Only write to DB when difficulty drifts >5% from live target
            # to avoid unnecessary DB churn on every tick.
            drift = abs(difficulty - live_target) / max(difficulty, 1.0)
            if drift > 0.05:
                difficulty = live_target
                await self.bot.db.update_pow_network_difficulty(
                    guild.id, symbol, difficulty, time.time(), network["block_height"]
                )
        else:
            difficulty = max(difficulty, cfg["initial_difficulty"])

        # Emergency failsafe: if no block found for 3× target block time,
        # force difficulty to match live hashrate.  Catches edge cases
        # where difficulty got stuck (e.g. DB stale, rounding drift).
        last_block_dt = network.get("last_block_ts")
        if last_block_dt and live_target > 0:
            last_block_epoch = (
                last_block_dt.timestamp()
                if hasattr(last_block_dt, "timestamp")
                else float(last_block_dt)
            )
            stale_time = time.time() - last_block_epoch
            if stale_time > cfg["target_block_time"] * 3:
                difficulty = live_target
                await self.bot.db.update_pow_network_difficulty(
                    guild.id, symbol, difficulty, time.time(), network["block_height"]
                )

        block_height = network["block_height"]
        block_reward = _pow_current_reward(block_height, cfg)

        # Apply guild mining multiplier
        _g_settings = await self.bot.db.get_guild_settings(guild.id)
        _mining_mult = float(_g_settings.get("mining_multiplier") or 1.0)
        if _mining_mult != 1.0:
            block_reward *= _mining_mult

        # ── Electricity cost deduction ────────────────────────────────────
        elec_rate = cfg.get("electricity_rate", 0.0)
        elec_scaling = cfg.get("electricity_scaling", 1.0)  # superlinear scaling exponent
        elec_costs: dict[int, float] = {}
        if elec_rate > 0:
            uid_rigs: dict[int, list] = {}
            for rig_row in all_rigs:
                uid_rigs.setdefault(rig_row["user_id"], []).append(rig_row)
            for uid, rigs in uid_rigs.items():
                watts = sum(
                    _RIGS[r["rig_id"]]["power"] * r["quantity"]
                    for r in rigs if r["rig_id"] in _RIGS
                )
                rig_count = sum(r["quantity"] for r in rigs if r["rig_id"] in _RIGS)
                base_cost = watts * _TICK / 3600 / 1000 * elec_rate
                # Diminishing returns: cost scales superlinearly with rig count
                # e.g. with scaling=1.15: 10 rigs cost ~14x single rig instead of 10x
                cost = base_cost * (max(rig_count, 1) ** (elec_scaling - 1.0)) if elec_scaling > 1.0 else base_cost
                if cost > 0:
                    try:
                        await self.bot.db.update_wallet(uid, guild.id, -to_raw(cost))
                        elec_costs[uid] = cost
                    except ValueError:
                        pass

        # ── Poisson block sample ──────────────────────────────────────────
        lam_network = total_hr * _TICK / difficulty
        blocks_this_tick = _poisson_sample(lam_network)
        # Cap to 3× expected rate  -  prevents runaway block generation if
        # difficulty was stale/corrupt on the first tick before live-adjustment.
        _max_blocks = max(1, round(3 * _TICK / cfg["target_block_time"]))
        blocks_this_tick = min(blocks_this_tick, _max_blocks)

        if is_sun:
            # SUN runs the full solo/pool/group system
            await self._process_sun_guild(
                guild, cfg, network, all_rigs, total_hr, difficulty,
                block_height, block_reward, blocks_this_tick, elec_costs,
            )
        else:
            # Non-SUN chains: proportional distribution among all assigned miners
            await self._process_non_sun_guild(
                guild, symbol, cfg, network, all_rigs, total_hr, difficulty,
                block_height, block_reward, blocks_this_tick,
            )

    async def _process_non_sun_guild(
        self,
        guild: discord.Guild,
        symbol: str,
        cfg: dict,
        network: dict,
        all_rigs: list[dict],
        total_hr: float,
        difficulty: float,
        block_height: int,
        block_reward: float,
        blocks_this_tick: int,
    ) -> None:
        """Non-SUN PoW tick (e.g. MTA)  -  same solo/pool/group lottery as SUN.

        Block reward distribution rules (same as SUN):
          - Solo miner wins → that miner keeps the ENTIRE block reward.
          - Pool wins → reward split EVENLY among all pool miners.
          - Group wins → reward split among group members by internal weights ONLY.
        """
        if blocks_this_tick <= 0:
            # Drain any pending bundle blocks even when Poisson fires 0 new blocks.
            # They were already created by _bundle_block and just need confirmation.
            net_key = symbol.lower()
            pending = await self.bot.db.get_oldest_pending_chain_blocks(
                guild.id, limit=10, network=net_key,
            )
            for blk in pending:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network=net_key)
            await self.bot.db.update_pow_network(
                guild.id, symbol, block_height, total_hr, block_reward, network["last_block_ts"]
            )
            return

        net_key = symbol.lower()

        # ── Collect raw hashrates per user from rig assignments ───────────────
        uid_raw_hr: dict[int, float] = {}
        for rig_row in all_rigs:
            if rig_row["rig_id"] not in _RIGS:
                continue
            uid = rig_row["user_id"]
            uid_raw_hr[uid] = uid_raw_hr.get(uid, 0.0) + _RIGS[rig_row["rig_id"]]["hashrate"] * rig_row["quantity"]

        # Apply hashstone bonuses
        uid_boosted: dict[int, tuple[float, float, dict | None]] = {}  # uid → (boosted_hr, bonus_pct, hashstone)
        for uid, raw_hr in uid_raw_hr.items():
            if raw_hr <= 0:
                continue
            hashstone = await self.bot.db.get_hashstone(uid, guild.id)
            item_m_bonus = _item_stat(hashstone, "mining_bonus")
            boosted_hr = raw_hr * (1.0 + item_m_bonus) if item_m_bonus > 0 else raw_hr
            uid_boosted[uid] = (boosted_hr, item_m_bonus, hashstone)

        # ── Categorise miners by mode ─────────────────────────────────────────
        pool_ids = set(await self.bot.db.get_pool_miners(guild.id))
        group_ids = set(await self.bot.db.get_group_miners(guild.id))

        solo_data:   list[tuple[int, float, float, dict | None]] = []
        pool_data:   list[tuple[int, float, float, dict | None]] = []
        pool_total_hr = 0.0

        group_buckets: dict[str, list[tuple[int, float, float, dict | None]]] = {}
        group_total_hrs: dict[str, float] = {}

        for uid, (hr, bonus, hs) in uid_boosted.items():
            if uid in group_ids:
                grp = await self.bot.db.get_user_mining_group(uid, guild.id)
                if grp:
                    gid = grp["group_id"]
                    group_buckets.setdefault(gid, []).append((uid, hr, bonus, hs))
                    group_total_hrs[gid] = group_total_hrs.get(gid, 0.0) + hr
                    continue
            if uid in pool_ids:
                pool_data.append((uid, hr, bonus, hs))
                pool_total_hr += hr
            else:
                # solo (default)
                solo_data.append((uid, hr, bonus, hs))

        # ── Entity lottery ────────────────────────────────────────────────────
        entities: list[tuple] = []
        for uid, hr, _, _ in solo_data:
            entities.append(("solo", uid, hr))
        if pool_data:
            entities.append(("pool", None, pool_total_hr))
        for gid, grp_members in group_buckets.items():
            if len(grp_members) >= 2:
                entities.append(("group", gid, group_total_hrs[gid]))

        entity_won: dict[tuple, int] = {}
        if entities:
            ent_total = sum(hr for _, _, hr in entities)
            if ent_total > 0:
                # Cap each entity at 50% of total to prevent any single group monopolizing
                cap_hr = ent_total * 0.50
                entities = [(etype, ekey, min(hr, cap_hr)) for etype, ekey, hr in entities]
                ent_total = sum(hr for _, _, hr in entities)
                for _ in range(blocks_this_tick):
                    r = random.random() * ent_total
                    cumsum = 0.0
                    for etype, ekey, hr in entities:
                        cumsum += hr
                        if r < cumsum:
                            k = (etype, ekey)
                            entity_won[k] = entity_won.get(k, 0) + 1
                            break

        # ── Mine chain blocks (all at once) ───────────────────────────────────
        new_height = block_height + blocks_this_tick
        pending_chain = await self.bot.db.get_oldest_pending_chain_blocks(guild.id, limit=blocks_this_tick, network=net_key)
        for blk in pending_chain:
            await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network=net_key)
        for _ in range(blocks_this_tick - len(pending_chain)):
            last = await self.bot.db.get_latest_chain_block(guild.id, network=net_key)
            new_num = (last["block_num"] if last else 0) + 1
            raw = f"{guild.id}:{net_key}:{new_num}:{total_hr:.2f}:{time.time()}"
            bh = "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]
            await self.bot.db.create_and_mine_chain_block(guild.id, new_num, bh, None, network=net_key)

        async def _credit(uid: int, amount: float) -> None:
            raw = to_raw(amount)
            try:
                await self.bot.db.update_wallet_holding(uid, guild.id, net_key, symbol, raw)
            except Exception as exc1:
                try:
                    await self.bot.db.update_holding(uid, guild.id, symbol, raw)
                except Exception as exc2:
                    logging.getLogger(__name__).error(
                        "mining _credit FAILED for user=%d guild=%d symbol=%s amount=%.6f: "
                        "wallet_holding: %s | holding: %s",
                        uid, guild.id, symbol, amount, exc1, exc2,
                    )

        payouts: list[tuple[int, float, float, float, float]] = []
        group_info: list[dict] = []
        group_member_reserve: dict[int, float] = {}  # uid -> their MTA reserve cut
        # uid -> (group_name, vault_sym, vault_minted) for non-SUN group member DMs
        group_member_vault_info: dict[int, tuple[str, str, int]] = {}

        # Solo: winner-takes-all
        for uid, user_hr, bonus_pct, hashstone in solo_data:
            won = entity_won.get(("solo", uid), 0)
            if won == 0:
                continue
            earned = won * block_reward
            job_row = await self.bot.db.get_user_job(uid, guild.id)
            if job_row:
                jc = Config.JOBS.get(job_row["job_id"], {})
                mb = jc.get("perks", {}).get("mining_bonus", 0.0)
                if mb > 0:
                    earned *= (1.0 + mb)
            await _credit(uid, earned)
            payouts.append((uid, earned, 100.0, user_hr, bonus_pct))
            if hashstone and hashstone["level"] < _HS["max_level"]:
                xp_gain = won * _HS["xp_per_block_share"]
                xp_result = await self.bot.db.add_hashstone_xp(uid, guild.id, xp_gain)
                if xp_result:
                    live_xp, live_level = xp_result
                    capped_xp = cap_xp(live_xp, live_level, _HS)
                    if capped_xp < live_xp:
                        await self.bot.db.update_hashstone_xp(uid, guild.id, capped_xp, live_level)
                    await notify_item_levelup_ready(self.bot, uid, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])

        # Pool: equal split
        pool_won = entity_won.get(("pool", None), 0)
        if pool_won > 0 and pool_data:
            pool_total_reward = pool_won * block_reward
            equal_cut = pool_total_reward / len(pool_data)
            share_pct_each = 100.0 / len(pool_data)
            for uid, user_hr, bonus_pct, hashstone in pool_data:
                earned = equal_cut
                job_row = await self.bot.db.get_user_job(uid, guild.id)
                if job_row:
                    jc = Config.JOBS.get(job_row["job_id"], {})
                    mb = jc.get("perks", {}).get("mining_bonus", 0.0)
                    if mb > 0:
                        earned *= (1.0 + mb)
                await _credit(uid, earned)
                payouts.append((uid, earned, share_pct_each, user_hr, bonus_pct))
                if hashstone and hashstone["level"] < _HS["max_level"]:
                    hr_share = user_hr / pool_total_hr if pool_total_hr > 0 else 1.0 / len(pool_data)
                    xp_gain = hr_share * pool_won * _HS["xp_per_block_share"]
                    xp_result = await self.bot.db.add_hashstone_xp(uid, guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _HS)
                        if capped_xp < live_xp:
                            await self.bot.db.update_hashstone_xp(uid, guild.id, capped_xp, live_level)
                        await notify_item_levelup_ready(self.bot, uid, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])

        # Groups: full allocated reward, split by internal weights
        for gid, members in group_buckets.items():
            grp_won = entity_won.get(("group", gid), 0)
            if grp_won == 0 or len(members) < 2:
                continue
            grp_row = await self.bot.db.get_mining_group(guild.id, group_id=gid)
            if not grp_row:
                continue
            grp_reward = grp_won * block_reward
            weight_mode = grp_row.get("weight_mode", "hashrate")
            member_weights: dict[int, float] = {}
            if weight_mode == "equal":
                for uid, hr, _, _ in members:
                    member_weights[uid] = 1.0 if hr > 0 else 0.0
            elif weight_mode == "custom":
                weights_rows = await self.bot.db.get_group_weights(guild.id, gid)
                weight_map = {w["user_id"]: w["weight"] for w in weights_rows}
                for uid, hr, _, _ in members:
                    member_weights[uid] = weight_map.get(uid, 1.0)
            else:
                for uid, hr, _, _ in members:
                    if hr > 0:
                        member_weights[uid] = hr
            total_weight = sum(member_weights.values())
            if total_weight <= 0:
                continue
            ns_reserve_pct = float(grp_row.get("reserve_pct", 5.0))
            ns_total_reserve_cut = 0.0
            grp_member_payouts: list[tuple[int, float, float]] = []
            for uid, weight in member_weights.items():
                member_earned = grp_reward * (weight / total_weight)
                if member_earned <= 0:
                    continue
                job_row = await self.bot.db.get_user_job(uid, guild.id)
                if job_row:
                    jc = Config.JOBS.get(job_row["job_id"], {})
                    mb = jc.get("perks", {}).get("mining_bonus", 0.0)
                    if mb > 0:
                        member_earned *= (1.0 + mb)
                per_member_reserve = 0.0
                if ns_reserve_pct > 0:
                    cut = member_earned * (ns_reserve_pct / 100.0)
                    member_earned -= cut
                    ns_total_reserve_cut += cut
                    per_member_reserve = cut
                await _credit(uid, member_earned)
                payouts.append((uid, member_earned, weight / total_weight * 100, uid_boosted.get(uid, (0,))[0], uid_boosted.get(uid, (0, 0))[1]))
                grp_member_payouts.append((uid, member_earned, weight / total_weight * 100))
                group_member_reserve[uid] = per_member_reserve
                hashstone = uid_boosted.get(uid, (0, 0, None))[2]
                if hashstone and hashstone["level"] < _HS["max_level"]:
                    xp_gain = (weight / total_weight) * grp_won * _HS["xp_per_block_share"]
                    xp_result = await self.bot.db.add_hashstone_xp(uid, guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _HS)
                        if capped_xp < live_xp:
                            await self.bot.db.update_hashstone_xp(uid, guild.id, capped_xp, live_level)
                        await notify_item_levelup_ready(self.bot, uid, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])
            if ns_total_reserve_cut > 0:
                await self.bot.db.add_group_reserve_btc(guild.id, gid, ns_total_reserve_cut)

            # Mint group vault tokens: 1 per block won (requires 2+ members)
            vault_minted = await _mint_group_vault_tokens(self.bot.db, guild.id, gid, grp_row, grp_won, bot=self.bot, guild=guild, mining_chain=symbol)
            grp_name = grp_row.get("name", gid)
            grp_tok_sym = grp_row.get("token_symbol") or ""
            if vault_minted:
                for uid in member_weights:
                    group_member_vault_info[uid] = (grp_name, grp_tok_sym, vault_minted)

            group_info.append({
                "name":               grp_name,
                "total_reward":       grp_reward,
                "reserve_cut":        ns_total_reserve_cut,
                "members":            grp_member_payouts,
                "blocks":             grp_won,
                "vault_tokens_minted": vault_minted,
                "vault_token_sym":    grp_tok_sym,
            })

        await self.bot.db.update_pow_network(
            guild.id, symbol, new_height, total_hr,
            _pow_current_reward(new_height, cfg), time.time(),
        )

        # Supply-price adjustment: minting dilutes price (same as SUN)
        total_minted = sum(entry[1] for entry in payouts)
        if total_minted > 0:
            price_row = await self.bot.db.get_price(symbol, guild.id)
            if price_row and price_row.get("circulating_supply", 0) > 0:
                old_supply = float(price_row["circulating_supply"])
                # Enforce max_supply cap  -  stop minting beyond the hard cap
                max_supply = Config.TOKENS.get(symbol, {}).get("max_supply")
                if max_supply and old_supply + total_minted > max_supply:
                    total_minted = max(0.0, max_supply - old_supply)
                if total_minted <= 0:
                    return  # max supply reached, no more mining rewards
                new_supply = await self.bot.db.markets.update_builtin_circulating_supply(
                    guild.id, symbol, total_minted
                )
                if new_supply > 0:
                    adjusted = float(price_row["price"]) * (old_supply / new_supply)
                    await self.bot.db.update_price(symbol, guild.id, adjusted)

        # Publish mining event for feed embed + DMs
        if payouts:
            await self.bot.bus.publish(
                "pow_mining_tick",
                guild=guild,
                symbol=symbol,
                emoji=cfg.get("emoji", "⛏"),
                chain_name=cfg.get("name", symbol),
                block_height=new_height,
                block_reward=block_reward,
                blocks_mined=blocks_this_tick,
                total_hashrate=total_hr,
                payouts=payouts,
                group_info=group_info,
                group_member_reserve=group_member_reserve,
                group_member_vault_info=group_member_vault_info,
            )

        # Difficulty retarget (supplementary  -  primary tracking is bidirectional in _process_pow_guild)
        window = cfg.get("difficulty_window", 2016)
        last_retarget = network.get("last_retarget_height", 0) or 0
        if new_height - last_retarget >= window:
            last_ts = network.get("last_retarget_ts") or time.time()
            actual_time = time.time() - last_ts
            target_time = window * cfg["target_block_time"]
            ratio = max(0.25, min(4.0, actual_time / target_time)) if target_time > 0 else 1.0
            new_diff = difficulty * ratio
            # Anchor to live hashrate  -  never drift far from reality
            live_target = total_hr * cfg["target_block_time"]
            if live_target > 0:
                new_diff = max(new_diff, live_target * 0.5)
                new_diff = min(new_diff, live_target * 2.0)
            new_diff = max(1.0, new_diff)
            await self.bot.db.update_pow_network_difficulty(guild.id, symbol, new_diff, time.time(), new_height)

        # Process MTA mempool (like SUN mempool processing)
        if symbol == "MTA" and blocks_this_tick > 0:
            await self._process_btc_mempool(guild, total_hr)

    async def _process_sun_guild(
        self,
        guild: discord.Guild,
        cfg: dict,
        network: dict,
        all_rigs: list[dict],
        total_hr: float,
        difficulty: float,
        block_height: int,
        block_reward: float,
        blocks_this_tick: int,
        elec_costs: dict[int, float],
    ) -> None:
        """SUN mining tick  -  solo/pool/group split via entity lottery, Hashstone XP, mempool fees.

        Block reward distribution rules:
          - Solo miner wins a block → that miner gets the ENTIRE block reward (winner-takes-all).
          - Pool wins a block → split EVENLY among ALL pool miners (equal share, not by hashrate).
          - Group wins a block → split among group members by their internal weights ONLY.
        """
        # ── Tick summary ──────────────────────────────────────────────────────
        tick_summary: dict = {
            "block_height":      block_height,
            "block_reward":      block_reward,
            "total_hashrate":    total_hr,
            "solo_payouts":      [],
            "pool_blocks":       0,
            "pool_total":        0.0,
            "pool_payouts":      [],
            "groups":            [],
            "electricity_costs": elec_costs,
        }

        if blocks_this_tick <= 0:
            # Drain any pending bundle blocks even when Poisson fires 0 new blocks.
            pending = await self.bot.db.get_oldest_pending_chain_blocks(
                guild.id, limit=10, network="sun",
            )
            for blk in pending:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network="sun")
            await self.bot.db.update_pow_network(
                guild.id, "SUN", block_height, total_hr, block_reward, network["last_block_ts"]
            )
            return

        new_height = block_height

        # ── Gather per-category miner data ────────────────────────────────────
        # Start from all_rigs (already filtered to SUN chain assignments) so that
        # only miners who actually have SUN rigs participate.  Classify by mode
        # after, same pattern as _process_non_sun_guild.  The old approach queried
        # get_solo_miners() (which looks at mining_rigs total inventory, not chain
        # assignments) first and then filtered by SUN hashrate -- causing miners
        # who moved all rigs to MTA to still appear in the candidate list, drain
        # the entity set, and block SUN from ever producing blocks.

        uid_raw_hr: dict[int, float] = {}
        for rig_row in all_rigs:
            if rig_row["rig_id"] not in _RIGS:
                continue
            uid_raw_hr[rig_row["user_id"]] = (
                uid_raw_hr.get(rig_row["user_id"], 0.0)
                + _RIGS[rig_row["rig_id"]]["hashrate"] * rig_row["quantity"]
            )

        uid_boosted: dict[int, tuple[float, float, dict | None]] = {}
        for uid, raw_hr in uid_raw_hr.items():
            if raw_hr <= 0:
                continue
            hashstone = await self.bot.db.get_hashstone(uid, guild.id)
            item_m_bonus = _item_stat(hashstone, "mining_bonus")
            boosted_hr = raw_hr * (1.0 + item_m_bonus) if item_m_bonus > 0 else raw_hr
            uid_boosted[uid] = (boosted_hr, item_m_bonus, hashstone)

        pool_ids = set(await self.bot.db.get_pool_miners(guild.id))
        group_ids = set(await self.bot.db.get_group_miners(guild.id))

        solo_data: list[tuple[int, float, float, dict | None]] = []
        solo_total_hr = 0.0
        pool_miner_data: list[tuple[int, float, float, dict | None]] = []
        pool_total_hr = 0.0
        group_buckets: dict[str, list[tuple[int, float, float, dict | None]]] = {}
        group_total_hrs: dict[str, float] = {}

        for uid, (hr, bonus, hs) in uid_boosted.items():
            if uid in group_ids:
                grp = await self.bot.db.get_user_mining_group(uid, guild.id)
                if grp:
                    gid = grp["group_id"]
                    group_buckets.setdefault(gid, []).append((uid, hr, bonus, hs))
                    group_total_hrs[gid] = group_total_hrs.get(gid, 0.0) + hr
                    continue
            if uid in pool_ids:
                pool_miner_data.append((uid, hr, bonus, hs))
                pool_total_hr += hr
            else:
                solo_data.append((uid, hr, bonus, hs))
                solo_total_hr += hr

        # ── Entity lottery: assign each block to exactly one entity ───────────
        # Entities: individual solo miners, the pool (aggregate), individual groups (aggregate)
        entities: list[tuple] = []  # (type, key, total_hr)
        for uid, hr, _, _ in solo_data:
            entities.append(("solo", uid, hr))
        if pool_miner_data:
            entities.append(("pool", None, pool_total_hr))
        for grp_id, grp_members in group_buckets.items():
            if len(grp_members) >= 2:
                entities.append(("group", grp_id, group_total_hrs[grp_id]))

        entity_won: dict[tuple, int] = {}  # (type, key) → blocks_count
        if entities:
            ent_total = sum(hr for _, _, hr in entities)
            if ent_total > 0:
                # Cap each entity at 50% of total to prevent any single group monopolizing
                cap_hr = ent_total * 0.50
                entities = [(etype, ekey, min(hr, cap_hr)) for etype, ekey, hr in entities]
                ent_total = sum(hr for _, _, hr in entities)
                for _ in range(blocks_this_tick):
                    r = random.random() * ent_total
                    cumsum = 0.0
                    for etype, ekey, hr in entities:
                        cumsum += hr
                        if r < cumsum:
                            k = (etype, ekey)
                            entity_won[k] = entity_won.get(k, 0) + 1
                            break

        # ── No-miner fallback: advance chain without paying anyone ───────────────
        # If entities is empty (all SUN rigs exist but every owner is in a
        # single-person group, or some other edge case), still drain pending
        # bundle blocks and create new ones so the chain doesn't freeze.
        # Payout loops below are skipped when entity_won is empty.
        if not entities:
            pending_chain = await self.bot.db.get_oldest_pending_chain_blocks(
                guild.id, limit=blocks_this_tick, network="sun",
            )
            for blk in pending_chain:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network="sun")
                new_height += 1
            for _ in range(blocks_this_tick - len(pending_chain)):
                last = await self.bot.db.get_latest_chain_block(guild.id, network="sun")
                new_num = (last["block_num"] if last else 0) + 1
                raw = f"{guild.id}:sun:{new_num}:{total_hr:.2f}:{time.time()}"
                bh = "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]
                await self.bot.db.create_and_mine_chain_block(guild.id, new_num, bh, None, network="sun")
                new_height += 1

        # ── Solo payouts: winner-takes-all per block ───────────────────────────
        for user_id, user_hr, item_m_bonus, hashstone in solo_data:
            won_blocks = entity_won.get(("solo", user_id), 0)
            if won_blocks == 0:
                continue
            earned = won_blocks * block_reward
            job_row = await self.bot.db.get_user_job(user_id, guild.id)
            if job_row:
                job_cfg = Config.JOBS.get(job_row["job_id"], {})
                m_bonus = job_cfg.get("perks", {}).get("mining_bonus", 0.0)
                if m_bonus > 0:
                    earned *= (1.0 + m_bonus)
            await self.bot.db.update_wallet_holding(user_id, guild.id, "sun", "SUN", to_raw(earned))
            # Record chain blocks
            mined_nums: list[int] = []
            pending = await self.bot.db.get_oldest_pending_chain_blocks(guild.id, limit=won_blocks, network="sun")
            for blk in pending:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], user_id, network="sun")
                mined_nums.append(blk["block_num"])
            for _ in range(won_blocks - len(pending)):
                last = await self.bot.db.get_latest_chain_block(guild.id, network="sun")
                new_num = (last["block_num"] if last else 0) + 1
                raw = f"{guild.id}:solo:{new_num}:{user_id}:{time.time()}"
                bh = "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]
                await self.bot.db.create_and_mine_chain_block(guild.id, new_num, bh, user_id, network="sun")
                mined_nums.append(new_num)
            for _ in range(won_blocks):
                new_height += 1
                await self.bot.db.log_block(guild.id, new_height, user_id, block_reward, total_hr)
            tick_summary["solo_payouts"].append((user_id, won_blocks, earned, user_hr, mined_nums))
            # Hashstone XP
            if hashstone and hashstone["level"] < _HS["max_level"]:
                xp_gain = won_blocks * _HS["xp_per_block_share"]
                xp_result = await self.bot.db.add_hashstone_xp(user_id, guild.id, xp_gain)
                if xp_result:
                    live_xp, live_level = xp_result
                    capped_xp = cap_xp(live_xp, live_level, _HS)
                    if capped_xp < live_xp:
                        await self.bot.db.update_hashstone_xp(user_id, guild.id, capped_xp, live_level)
                    await notify_item_levelup_ready(self.bot, user_id, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])
            # DM notification
            trades_cog = self.bot.get_cog("Trades")
            if trades_cog:
                sun_price_row = await self.bot.db.get_price("SUN", guild.id)
                sun_p = float(sun_price_row["price"]) if sun_price_row else 0.0
                bonus_line = f"\n💎 Hashstone: **+{int(item_m_bonus * 100)}%** hashrate" if item_m_bonus > 0 else ""
                dm_embed = (
                    card("🏆 Block Found!", description=f"You mined **{won_blocks}** block{'s' if won_blocks > 1 else ''}! Solo mining success  -  you keep the full reward.{bonus_line}", color=C_GOLD)
                    .field("☀ SUN Reward",   f"**{fmt_token(earned, 'SUN', '☀')}**{' ≈ ' + fmt_usd(earned * sun_p) if sun_p > 0 else ''}", True)
                    .field("⛏ Hashrate",     f"**{user_hr:,.0f} MH/s**",                                             True)
                    .field("📦 Block Height", f"`#{new_height:,}`",                                                  True)
                    .field("🌐 Server",       guild.name,                                                            True)
                    .build()
                )
                await trades_cog._dm(user_id, guild, dm_embed, category="mining")

        # ── Pool payouts: equal split among all pool miners ────────────────────
        pool_won_blocks = entity_won.get(("pool", None), 0)
        if pool_won_blocks > 0 and pool_miner_data:
            pool_reward_total = pool_won_blocks * block_reward
            # Record chain blocks
            pending = await self.bot.db.get_oldest_pending_chain_blocks(guild.id, limit=pool_won_blocks, network="sun")
            for blk in pending:
                await self.bot.db.mine_chain_block(guild.id, blk["block_num"], None, network="sun")
            for _ in range(pool_won_blocks - len(pending)):
                last = await self.bot.db.get_latest_chain_block(guild.id, network="sun")
                new_num = (last["block_num"] if last else 0) + 1
                raw = f"{guild.id}:pool:{new_num}:{total_hr:.2f}:{time.time()}"
                bh = "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]
                await self.bot.db.create_and_mine_chain_block(guild.id, new_num, bh, None, network="sun")
            for _b in range(pool_won_blocks):
                new_height += 1
                await self.bot.db.log_block(guild.id, new_height, None, block_reward, total_hr)

            # Equal cut  -  every pool member gets the same USD-equivalent slice
            equal_cut = pool_reward_total / len(pool_miner_data)
            top_miners: list[tuple[int, float, float, float, float]] = []
            share_pct_each = 100.0 / len(pool_miner_data)
            for user_id, user_hr, bonus_pct, hashstone in pool_miner_data:
                earned = equal_cut
                job_row = await self.bot.db.get_user_job(user_id, guild.id)
                if job_row:
                    job_cfg = Config.JOBS.get(job_row["job_id"], {})
                    m_bonus = job_cfg.get("perks", {}).get("mining_bonus", 0.0)
                    if m_bonus > 0:
                        earned *= (1.0 + m_bonus)
                await self.bot.db.update_wallet_holding(user_id, guild.id, "sun", "SUN", to_raw(earned))
                top_miners.append((user_id, share_pct_each, earned, user_hr, bonus_pct))
                # Hashstone XP proportional to hashrate within pool
                if hashstone and hashstone["level"] < _HS["max_level"]:
                    hr_share = user_hr / pool_total_hr if pool_total_hr > 0 else 1.0 / len(pool_miner_data)
                    xp_gain = hr_share * pool_won_blocks * _HS["xp_per_block_share"]
                    xp_result = await self.bot.db.add_hashstone_xp(user_id, guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _HS)
                        if capped_xp < live_xp:
                            await self.bot.db.update_hashstone_xp(user_id, guild.id, capped_xp, live_level)
                        await notify_item_levelup_ready(self.bot, user_id, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])

            tick_summary["pool_blocks"] = pool_won_blocks
            tick_summary["pool_total"]  = pool_reward_total
            tick_summary["pool_payouts"] = top_miners[:5]
            # Full list of pool-miner user_ids (no truncation) so progression
            # listeners (achievements / quests / seasons / challenges) can
            # credit every paid miner, not just the top 5 shown in the feed.
            tick_summary["pool_miner_ids"] = [m[0] for m in top_miners]
            trades_cog = self.bot.get_cog("Trades")
            if trades_cog:
                sun_price_row = await self.bot.db.get_price("SUN", guild.id)
                sun_p = float(sun_price_row["price"]) if sun_price_row else 0.0
                for uid, s_pct, earned, boosted_hr, hs_bonus in top_miners:
                    bonus_line = f"\n💎 Hashstone: **+{int(hs_bonus * 100)}%** hashrate" if hs_bonus > 0 else ""
                    dm_embed = (
                        card("⛏ Pool Mining Payout", description=f"The pool found **{pool_won_blocks}** block{'s' if pool_won_blocks > 1 else ''}! Equal split among {len(pool_miner_data)} miners:{bonus_line}", color=C_GOLD)
                        .field("☀ Your Cut",    f"**{fmt_token(earned, 'SUN', '☀')}**{' ≈ ' + fmt_usd(earned * sun_p) if sun_p > 0 else ''}", True)
                        .field("⛏ Hashrate",    f"**{boosted_hr:,.0f} MH/s**",                                        True)
                        .field("📊 Pool Share",  f"**{s_pct:.1f}%** (equal)",                                          True)
                        .field("🌐 Server",      guild.name,                                                           True)
                        .build()
                    )
                    await trades_cog._dm(uid, guild, dm_embed, category="mining")
                    await asyncio.sleep(0.5)

        # ── Group payouts: full allocated blocks → distributed by internal weights ──
        for grp_id, members in group_buckets.items():
            grp_won_blocks = entity_won.get(("group", grp_id), 0)
            if grp_won_blocks == 0 or len(members) < 2:
                continue
            grp_row = await self.bot.db.get_mining_group(guild.id, group_id=grp_id)
            if not grp_row:
                continue

            grp_reward = grp_won_blocks * block_reward
            grp_net_hr = group_total_hrs.get(grp_id, 0.0)

            weight_mode = grp_row.get("weight_mode", "hashrate")
            member_weights: dict[int, float] = {}
            member_hashstones: dict[int, dict | None] = {uid: hs for uid, _, _, hs in members}

            if weight_mode == "equal":
                for uid, hr, _, _ in members:
                    member_weights[uid] = 1.0 if hr > 0 else 0.0
            elif weight_mode == "custom":
                weights_rows = await self.bot.db.get_group_weights(guild.id, grp_id)
                weight_map = {w["user_id"]: w["weight"] for w in weights_rows}
                for uid, hr, _, _ in members:
                    member_weights[uid] = weight_map.get(uid, 1.0)
            else:  # hashrate (default)
                for uid, hr, _, _ in members:
                    if hr > 0:
                        member_weights[uid] = hr

            total_weight = sum(member_weights.values())
            if total_weight <= 0:
                continue

            reserve_pct = float(grp_row.get("reserve_pct", 5.0))
            # Hall upgrades do not affect mining mechanics - only Hall thread bonuses
            hashrate_bonus = 0.0
            reserve_bonus = 0.0
            grp_xp_bonus = 0.0
            grp_reward_bonus = 0.0
            grp_electricity_reduc = 0.0

            total_reserve_cut = 0.0
            grp_member_payouts: list[tuple[int, float, float, float]] = []  # uid, earned, weight_pct, per_member_reserve

            for uid, weight in member_weights.items():
                member_earned = grp_reward * (weight / total_weight)
                if member_earned <= 0:
                    continue
                if hashrate_bonus > 0:
                    member_earned *= (1.0 + hashrate_bonus)
                if grp_reward_bonus > 0:
                    member_earned *= (1.0 + grp_reward_bonus)
                job_row = await self.bot.db.get_user_job(uid, guild.id)
                if job_row:
                    job_cfg = Config.JOBS.get(job_row["job_id"], {})
                    m_bonus = job_cfg.get("perks", {}).get("mining_bonus", 0.0)
                    if m_bonus > 0:
                        member_earned *= (1.0 + m_bonus)
                hashstone = member_hashstones[uid]
                per_member_reserve = 0.0
                if reserve_pct > 0:
                    cut = member_earned * (reserve_pct / 100.0)
                    member_earned -= cut
                    total_reserve_cut += cut * (1.0 + reserve_bonus)
                    per_member_reserve = cut
                await self.bot.db.update_wallet_holding(uid, guild.id, "sun", "SUN", to_raw(member_earned))
                grp_member_payouts.append((uid, member_earned, weight / total_weight * 100, per_member_reserve))
                # Hashstone XP proportional to member's weight share × blocks
                if hashstone and hashstone["level"] < _HS["max_level"]:
                    effective_share = weight / total_weight
                    xp_gain = effective_share * grp_won_blocks * _HS["xp_per_block_share"]
                    if grp_xp_bonus > 0:
                        xp_gain *= (1.0 + grp_xp_bonus)
                    xp_result = await self.bot.db.add_hashstone_xp(uid, guild.id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _HS)
                        if capped_xp < live_xp:
                            await self.bot.db.update_hashstone_xp(uid, guild.id, capped_xp, live_level)
                        await notify_item_levelup_ready(self.bot, uid, guild, "hashstone", live_xp - xp_gain, live_xp, live_level, hashstone["staked_amount"])

            if total_reserve_cut > 0:
                _sun_price_row = await self.bot.db.get_price("SUN", guild.id)
                _sun_usd = float(_sun_price_row["price"]) if _sun_price_row else 0.01
                await self.bot.db.add_group_reserve_usd(guild.id, grp_id, total_reserve_cut * _sun_usd)

            # Mint group vault tokens: 1 per block won (requires 2+ members).
            # mining_chain is hardcoded to "SUN" because this function only
            # ever runs from the is_sun branch in _process_pow_guild; the
            # symbol parameter never reached this scope.
            vault_minted = await _mint_group_vault_tokens(self.bot.db, guild.id, grp_id, grp_row, grp_won_blocks, bot=self.bot, guild=guild, mining_chain="SUN")

            for _b in range(grp_won_blocks):
                new_height += 1
                await self.bot.db.log_block(guild.id, new_height, None, block_reward, total_hr)

            # Collect active upgrade bonuses for embed display
            _active_bonuses: list[str] = []
            if hashrate_bonus > 0:
                _active_bonuses.append(f"⚡+{hashrate_bonus*100:.0f}% HR")
            if grp_reward_bonus > 0:
                _active_bonuses.append(f"💰+{grp_reward_bonus*100:.0f}% Reward")
            if grp_xp_bonus > 0:
                _active_bonuses.append(f"📡+{grp_xp_bonus*100:.0f}% XP")
            if grp_electricity_reduc > 0:
                _active_bonuses.append(f"☀️-{grp_electricity_reduc*100:.0f}% Elec")

            tick_summary["groups"].append({
                "name":                grp_row.get("name", grp_id),
                "net_hr":              grp_net_hr,
                "total_reward":        grp_reward,
                "reserve_cut":         total_reserve_cut,
                "members":             grp_member_payouts,
                "blocks":              grp_won_blocks,
                "upgrades":            _active_bonuses,
                "vault_tokens_minted": vault_minted,
                "vault_token_sym":     grp_row.get("token_symbol") or "",
            })

        # ── Advance network block height ──────────────────────────────────────
        final_height = new_height
        await self.bot.db.update_pow_network(guild.id, "SUN", final_height, total_hr, block_reward, time.time())

        # ── Supply-price adjustment: minting SUN dilutes price ───────────────
        total_sun_minted = sum(e for (_, _, e, _, _) in tick_summary["solo_payouts"])
        total_sun_minted += tick_summary.get("pool_total", 0.0)
        for grp in tick_summary.get("groups", []):
            total_sun_minted += grp.get("total_reward", 0.0)
        if total_sun_minted > 0:
            price_row = await self.bot.db.get_price("SUN", guild.id)
            if price_row and price_row.get("circulating_supply", 0) > 0:
                old_supply = float(price_row["circulating_supply"])
                # Enforce max_supply cap  -  SUN has a 21M hard cap like Moneta
                max_supply = Config.TOKENS.get("SUN", {}).get("max_supply")
                if max_supply and old_supply + total_sun_minted > max_supply:
                    total_sun_minted = max(0.0, max_supply - old_supply)
                if total_sun_minted > 0:
                    new_supply = await self.bot.db.markets.update_builtin_circulating_supply(
                        guild.id, "SUN", total_sun_minted
                    )
                    if new_supply > 0:
                        adjusted = float(price_row["price"]) * (old_supply / new_supply)
                        await self.bot.db.update_price("SUN", guild.id, adjusted)

        # ── Difficulty retarget (supplementary  -  primary tracking is bidirectional in _process_pow_guild)
        window = cfg.get("difficulty_window", 144)
        last_retarget = network.get("last_retarget_height", 0) or 0
        if final_height - last_retarget >= window:
            last_ts = network.get("last_retarget_ts") or time.time()
            elapsed = time.time() - last_ts
            target_elapsed = window * cfg["target_block_time"]
            ratio = max(0.25, min(4.0, elapsed / target_elapsed)) if elapsed > 0 else 1.0
            current_diff = network.get("difficulty") or cfg["initial_difficulty"]
            new_diff = current_diff * ratio
            # Anchor to live hashrate  -  never drift far from reality
            live_target = total_hr * cfg["target_block_time"]
            if live_target > 0:
                new_diff = max(new_diff, live_target * 0.5)
                new_diff = min(new_diff, live_target * 2.0)
            new_diff = max(1.0, new_diff)
            await self.bot.db.update_pow_network_difficulty(guild.id, "SUN", new_diff, time.time(), final_height)

        # ── Emit tick summary ─────────────────────────────────────────────────
        any_payout = (
            tick_summary["solo_payouts"]
            or tick_summary["pool_total"] > 0
            or tick_summary["groups"]
        )
        if any_payout:
            await self.bot.bus.publish("mining_tick_complete", guild=guild, summary=tick_summary)

        # ── Sun Network mempool ───────────────────────────────────────────────
        total_mined = max(final_height - block_height, blocks_this_tick)
        if total_mined > 0:
            await self._process_sun_mempool(guild, total_hr)

    async def _process_sun_mempool(self, guild: discord.Guild, total_hr: float) -> None:
        """Process pending Sun Network mempool transactions (MTA-style fee collection).
        Miners collect tx fees proportional to their hashrate share."""
        from cogs.validators import (
            adjust_base_fee, MAX_MEMPOOL,
        )

        _SUN_NETWORK = "Sun Network"
        pending = await self.bot.db.get_pending_mempool(guild.id, _SUN_NETWORK, limit=MAX_MEMPOOL)
        if not pending:
            return

        # Get the Validators cog to reuse its action execution logic
        validators_cog = self.bot.cogs.get("Validators")
        if not validators_cog:
            return

        # Execute each pending action
        total_gas = 0
        confirmed_count = 0
        for action in pending:
            try:
                success, _ = await validators_cog._execute_action(guild, action)
                if success:
                    total_gas += int(action["gas_fee"])
                    confirmed_count += 1
                    await self.bot.db.resolve_mempool_action(action["id"], "confirmed", None)
                else:
                    await validators_cog._refund_action(guild, action)
                    await self.bot.db.resolve_mempool_action(action["id"], "rejected", None)
            except Exception as _exc:
                log.exception(
                    "_process_sun_mempool: action %s failed unexpectedly, rejecting: %s",
                    action.get("id"), _exc,
                )
                try:
                    await validators_cog._refund_action(guild, action)
                    await self.bot.db.resolve_mempool_action(action["id"], "rejected", None)
                except Exception:
                    pass

        if total_gas <= 0:
            return

        # Distribute fees: miners get (1-treasury_cut), treasury gets treasury_cut
        _fee_cfg = await self.bot.db.guilds.get_fee_config(guild.id)
        _t_cut = _fee_cfg["treasury_cut_pct"]
        miner_pool   = total_gas * (1.0 - _t_cut)
        treasury_cut = total_gas * _t_cut

        if treasury_cut > 0:
            await self.bot.db.add_to_treasury(guild.id, int(treasury_cut))
            from services.vault import deposit_to_vault
            from core.framework.scale import to_human as _th
            await deposit_to_vault(self.bot.db, guild.id, "sun", _th(int(treasury_cut)), bot=self.bot)

        if miner_pool > 0 and total_hr > 0:
            # Pay all active SUN miners proportionally by hashrate
            all_rigs = await self.bot.db.get_all_guild_chain_rigs(guild.id, "SUN")
            paid_users: set[int] = set()
            for rig_row in all_rigs:
                uid = rig_row["user_id"]
                if uid in paid_users:
                    continue
                paid_users.add(uid)
                user_hr = await self.bot.db.get_user_chain_hashrate(uid, guild.id, "SUN")
                if user_hr <= 0:
                    continue
                share = user_hr / total_hr
                fee_share = miner_pool * share
                if fee_share > 0:
                    await self.bot.db.update_wallet_holding(uid, guild.id, "sun", "SUN", int(round(fee_share)))
        # EIP-1559-style base fee adjustment for Sun Network
        current_base = await self.bot.db.get_base_fee(guild.id, _SUN_NETWORK)
        new_base = adjust_base_fee(current_base, confirmed_count, MAX_MEMPOOL, _SUN_NETWORK)
        await self.bot.db.set_base_fee(guild.id, _SUN_NETWORK, new_base)

    async def _process_btc_mempool(self, guild: discord.Guild, total_hr: float) -> None:
        """Process pending Moneta Chain mempool transactions (fee collection).
        Mirrors SUN mempool processing  -  miners collect tx fees proportional to hashrate."""
        from cogs.validators import (
            adjust_base_fee, MAX_MEMPOOL,
        )

        _BTC_NETWORK = "Moneta Chain"
        pending = await self.bot.db.get_pending_mempool(guild.id, _BTC_NETWORK, limit=MAX_MEMPOOL)
        if not pending:
            return

        validators_cog = self.bot.cogs.get("Validators")
        if not validators_cog:
            return

        total_gas = 0
        confirmed_count = 0
        for action in pending:
            try:
                success, _ = await validators_cog._execute_action(guild, action)
                if success:
                    total_gas += int(action["gas_fee"])
                    confirmed_count += 1
                    await self.bot.db.resolve_mempool_action(action["id"], "confirmed", None)
                else:
                    await validators_cog._refund_action(guild, action)
                    await self.bot.db.resolve_mempool_action(action["id"], "rejected", None)
            except Exception as _exc:
                log.exception(
                    "_process_btc_mempool: action %s failed unexpectedly, rejecting: %s",
                    action.get("id"), _exc,
                )
                try:
                    await validators_cog._refund_action(guild, action)
                    await self.bot.db.resolve_mempool_action(action["id"], "rejected", None)
                except Exception:
                    pass

        if total_gas <= 0:
            return

        _fee_cfg = await self.bot.db.guilds.get_fee_config(guild.id)
        _t_cut = _fee_cfg["treasury_cut_pct"]
        miner_pool   = total_gas * (1.0 - _t_cut)
        treasury_cut = total_gas * _t_cut

        if treasury_cut > 0:
            await self.bot.db.add_to_treasury(guild.id, int(treasury_cut))
            from services.vault import deposit_to_vault
            from core.framework.scale import to_human as _th
            await deposit_to_vault(self.bot.db, guild.id, "mta", _th(int(treasury_cut)), bot=self.bot)

        if miner_pool > 0 and total_hr > 0:
            all_rigs = await self.bot.db.get_all_guild_chain_rigs(guild.id, "MTA")
            paid_users: set[int] = set()
            for rig_row in all_rigs:
                uid = rig_row["user_id"]
                if uid in paid_users:
                    continue
                paid_users.add(uid)
                user_hr = await self.bot.db.get_user_chain_hashrate(uid, guild.id, "MTA")
                if user_hr <= 0:
                    continue
                share = user_hr / total_hr
                fee_share = miner_pool * share
                if fee_share > 0:
                    await self.bot.db.update_wallet_holding(uid, guild.id, "mta", "MTA", int(round(fee_share)))

        current_base = await self.bot.db.get_base_fee(guild.id, _BTC_NETWORK)
        new_base = adjust_base_fee(current_base, confirmed_count, MAX_MEMPOOL, _BTC_NETWORK)
        await self.bot.db.set_base_fee(guild.id, _BTC_NETWORK, new_base)

    @mining_tick.before_loop
    async def before_mining_tick(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.bot.db.seed_network(guild.id)
            # Backfill any mining_rigs rows missing from rig_chain_assignments
            await self.bot.db.backfill_chain_assignments(guild.id)
            # Reset users stuck in group mode with no group (orphaned miners)
            await self.bot.db.fix_orphaned_group_miners(guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.bot.db.seed_network(guild.id)

    # ══════════════════════════════════════════════════════════════════════════
    # $chain group (top-level)  -  from chain.py
    # ══════════════════════════════════════════════════════════════════════════

    @commands.hybrid_group(name="chain", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def chain(self, ctx: DiscoContext) -> None:
        """Show the most recent block. Use subcommands for more options."""
        if await suggest_subcommand(ctx, self.chain):
            return
        blk = await ctx.db.get_latest_chain_block(ctx.guild_id, network=None)
        if not blk:
            await ctx.reply_error("No blocks found yet. Blocks bundle every 30 minutes when there is activity.")
            return
        await self._send_block_embed(ctx, blk)

    # ── $chain block ──────────────────────────────────────────────────────────

    @chain.command(name="block")
    @guild_only
    async def block_cmd(self, ctx: DiscoContext, number_or_network: str = "", network: str = "") -> None:
        """Show chain block details. Usage: .block [number|network] [network]
        network: arc, sol, bnb, sun
        Examples: .block arc  |  .block 5 arc  |  .block (latest overall)"""
        # Allow .block arc  OR  .block 5 arc  OR  .block 0 arc
        arg = number_or_network.lower().strip()
        if arg in self._NETWORKS:
            # .block arc -> latest block for that network
            net = arg
            number = 0
        elif arg == "":
            net = network.lower().strip() if network else None
            number = 0
        else:
            try:
                number = int(arg)
            except ValueError:
                await ctx.reply_error(
                    f"Unknown network or block number `{arg}`. Valid networks: {', '.join(self._NETWORKS)}"
                )
                return
            net = network.lower().strip() if network else None

        if net and net not in self._NETWORKS:
            await ctx.reply_error(f"Unknown network `{net}`. Valid: {', '.join(self._NETWORKS)}")
            return

        if number != 0 and not net:
            # Block numbers are per-network  -  need a network to look up by number
            await ctx.reply_error(
                f"Block numbers are per-network. Specify a network:\n"
                + "\n".join(f"`.block {number} {n}`" for n in self._NETWORKS)
            )
            return

        if number == 0:
            blk = await ctx.db.get_latest_chain_block(ctx.guild_id, network=net)
        else:
            blk = await ctx.db.get_chain_block(ctx.guild_id, number, network=net)

        if not blk:
            msg = f"No blocks found for **{net.upper()}** yet." if net else "No blocks found yet."
            await ctx.reply_error(msg + " Blocks bundle every 30 minutes when there is activity.")
            return

        await self._send_block_embed(ctx, blk)

    async def _send_block_embed(self, ctx: DiscoContext, blk: dict) -> None:
        ts       = fmt_ts(blk["ts"], "%Y-%m-%d %H:%M:%S UTC")
        net      = blk.get("network") or None
        txns_all = await ctx.db.get_chain_block_txns(ctx.guild_id, blk["block_num"], limit=50, network=net)
        # Filter oracle rebalances from Discord display (still recorded on-chain)
        txns_show = [t for t in txns_all if t["tx_type"] not in ("ARB", "ORACLE_REBALANCE")]
        arb_count = sum(1 for t in txns_all if t["tx_type"] in ("ARB", "ORACLE_REBALANCE"))

        network_name = blk.get("network", "")
        status = blk.get("status", "pending")
        if status == "mined":
            miner_id = blk.get("miner_id")
            mined_at = blk.get("mined_at")
            if miner_id:
                status_str = f"✅ Mined by {mention(miner_id, ctx.guild)}"
            elif network_name and network_name not in ("sun", "mta"):
                status_str = "✅ Confirmed (PoS validators)"
            else:
                status_str = "✅ Mined (pool)"
            if mined_at:
                mined_ts = fmt_ts(mined_at, "%Y-%m-%d %H:%M:%S UTC")
                status_str += f" @ {mined_ts}"
        else:
            status_str = "⏳ Pending  -  awaiting a miner"

        net_label = blk.get("network", "").upper()
        title = f"📦 Block #{blk['block_num']:,}"
        if net_label:
            title += f"  [{net_label}]"
        _b = card(title, color=C_NAVY)
        _b.field("🔗 Block Hash", f"`{blk['block_hash']}`", False)
        if net_label:
            _b.field("🌐 Network",   net_label,               True)
        _b.field("⏱ Bundled At",     ts,                      True)
        _b.field("🧱 Transactions",  f"**{blk['tx_count']}** total", True)
        _b.field("✅ Status",         status_str,              False)
        if arb_count:
            _b.field("🔄 Oracle Rebalances", f"**{arb_count}** internal rebalance(s)", True)

        if txns_show:
            lines = [f"🔹 `{t['tx_hash'][:20]}…`  **{t['tx_type']}**" for t in txns_show[:8]]
            label = f"📋 User Transactions ({len(txns_show)}{'+' if len(txns_show) > 8 else ''})"
            _b.field(label, "\n".join(lines), False)
        else:
            _b.field("📋 User Transactions", "*None in this bundle*", False)

        net_nav = net_label.lower() if net_label else ""
        footer = "Bundles seal every 30 min · mined to confirm  ·  .chain tx <hash> for tx details"
        if blk["block_num"] > 1 and net_nav:
            footer = f".chain block {blk['block_num'] - 1} {net_nav} for previous  ·  " + footer
        embed = _b.footer(footer).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── $chain tx / txinfo ────────────────────────────────────────────────────

    @chain.command(name="tx", aliases=["txinfo"])
    @guild_only
    async def txinfo(self, ctx: DiscoContext, tx_hash: str) -> None:
        """Show transaction or block details by hash. Usage: .txinfo <hash>
        Accepts transaction hashes (64-char hex) and block hashes (0x...)."""

        # 1. Transactions table (works for both 16-char legacy and 64-char new hashes)
        tx = await ctx.db.get_transaction(ctx.guild_id, tx_hash)
        if tx:
            await self._send_tx_embed(ctx, tx, tx_hash)
            return

        # 2. Chain block hash (0x-prefixed)
        blk = await ctx.db.get_chain_block_by_hash(ctx.guild_id, tx_hash)
        if blk:
            await self._send_block_embed(ctx, blk)
            return

        await ctx.reply_error(
            f"Hash `{tx_hash[:48]}{'…' if len(tx_hash) > 48 else ''}` not found.\n"
            "Tip: transaction hashes are 64 hex characters; block hashes start with `0x`."
        )

    async def _send_tx_embed(self, ctx: DiscoContext, tx: dict, tx_hash: str) -> None:
        ts        = fmt_ts(tx["ts"], "%Y-%m-%d %H:%M:%S UTC")
        member    = ctx.guild.get_member(tx["user_id"]) if tx.get("user_id") else None
        user_str  = member.mention if member else (f"User {tx['user_id']}" if tx.get("user_id") else "System")
        block_str = f"Block #{tx['block_num']:,}" if tx.get("block_num") else "⏳ Pending"

        status_icon = "✅" if tx.get("block_num") else "⏳"
        is_oracle = tx["tx_type"] in ("ARB", "ORACLE_REBALANCE")
        desc = f"🔗 **Tx Hash**\n`{tx_hash}`"
        if is_oracle:
            desc += "\n\n> 🔄 Oracle rebalance  -  internal system transaction"
        _b = card(f"📋 Transaction  -  {tx['tx_type']}", description=desc, color=C_NAVY)
        _b.field("⚡ Action",     tx["tx_type"],   True)
        _b.field("👤 User",       user_str,         True)
        _b.field("📦 Block",      block_str,         True)

        if tx.get("symbol_in") and tx.get("amount_in") is not None:
            _b.field("📤 Input",  f"`{tx['amount_in']:,.6f}` {tx['symbol_in']}",  True)
        if tx.get("symbol_out") and tx.get("amount_out") is not None:
            _b.field("📥 Output", f"`{tx['amount_out']:,.6f}` {tx['symbol_out']}", True)
        if tx.get("price_at"):
            _b.field("💲 Price",  f"`${tx['price_at']:,.6f}`",                     True)
        if tx.get("gas_fee") and tx["gas_fee"] > 0:
            _b.field("⛽ Gas Fee", f"`{tx['gas_fee']:,.8f}` {tx.get('gas_coin', '')}", True)

        # Fee/burn from token contract (for SEND/TRANSFER type txns)
        sym = tx.get("symbol_in")
        if sym and sym not in ("USD", None) and tx["tx_type"] in ("SEND", "TRANSFER", "BUY", "SELL"):
            contract = await ctx.db.get_token_contract(ctx.guild_id, sym)
            if contract:
                fee_rate  = float(contract.get("transfer_fee") or 0)
                burn_rate = float(contract.get("burn_rate") or 0)
                raw_amt   = tx.get("amount_in") or 0
                fee_amt   = raw_amt * fee_rate
                burn_amt  = raw_amt * burn_rate
                if fee_amt > 0:
                    _b.field("💸 Protocol Fee", f"`{fee_amt:,.6f}` {sym}", True)
                if burn_amt > 0:
                    _b.field("🔥 Burned",        f"`{burn_amt:,.6f}` {sym}", True)

        _b.field(f"{status_icon} Status",  "Confirmed" if tx.get("block_num") else "Pending", True)
        _b.field("🕐 Timestamp",  ts,                                                           True)
        embed = _b.build()
        set_tx(embed, ctx.guild_id, tx_hash)
        await ctx.reply(embed=embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    # Contract subgroup  -  $chain contract ...
    # ══════════════════════════════════════════════════════════════════════════

    @chain.group(name="contract", aliases=["ct"], invoke_without_command=True)
    @guild_only
    async def contract(self, ctx: DiscoContext) -> None:
        """On-chain smart contracts: deploy, call, fund, withdraw, info, list, events, pause, resume.

        Quick examples:
          .chain contract deploy MyEscrow arc escrow
          .chain contract deploy LimitBot "Arcadia Network" limit_order desc "Auto sells ARC at $5000"
          .chain contract list
          .chain contract info 0x<address>
          .chain contract call 0x<address> place arg token=ARC arg amount=1.0 arg price=3500
          .chain contract call 0x<address> execute
          .chain contract call 0x<address> cancel
          .chain contract fund 0x<address> ARC 0.5
          .chain contract events 0x<address>

        Contract types: limit_order | escrow | vesting | multisig | custom
        OpSet (steps in custom contracts): TRANSFER, SWAP, LOCK, UNLOCK, EMIT, REQUIRE, SET_STATE, GET_STATE
        """
        if await suggest_subcommand(ctx, self.contract):
            return
        await ctx.send_help(ctx.command)

    # ── Contract helpers ──────────────────────────────────────────────────────

    async def _queue_contract_action(
        self,
        ctx: DiscoContext,
        action_type: str,
        network: str,
        payload: dict,
        gas_price: str = "medium",
    ) -> int | None:
        """Deduct gas and submit a contract action to the mempool."""
        from cogs.validators import gas_fee_for_network
        gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, action_type, gas_price, network)
        gas_cfg   = Config.TOKENS.get(gas_coin, {})
        gas_emoji = gas_cfg.get("emoji", "●")

        h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, gas_coin)
        gas_bal = to_human(h["amount"]) if h else 0.0
        if gas_bal < gas_fee:
            await ctx.reply_error(
                f"Need **{fmt_gas(gas_fee, gas_coin, gas_emoji)}** for gas. "
                f"You have **{fmt_gas(gas_bal, gas_coin, gas_emoji)}**."
            )
            return None

        await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-gas_fee))
        action_id = await ctx.db.add_to_mempool(
            guild_id=ctx.guild_id,
            user_id=ctx.author.id,
            network=network,
            action_type=action_type,
            payload=payload,
            gas_price=gas_price,
            gas_fee=to_raw(gas_fee),
        )
        return action_id

    def _parse_flags(self, flags: str) -> tuple[str, dict]:
        """Parse gas tier and arg key=val from flags string.
        Returns (gas_price, {arg_key: arg_value})."""
        gas_price = "medium"
        args: dict = {}
        parts = flags.split()
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in ("gas", "gas-price") and i + 1 < len(parts):
                gas_price = parts[i + 1].lower()
                i += 2
                continue
            if p == "high":
                gas_price = "high"; i += 1; continue
            if p == "low":
                gas_price = "low"; i += 1; continue
            if p == "arg" and i + 1 < len(parts):
                kv = parts[i + 1]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    # Try numeric coercion
                    try:
                        args[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        args[k] = v
                i += 2; continue
            i += 1
        return gas_price, args

    # ── Contract commands ─────────────────────────────────────────────────────

    @contract.command(name="deploy")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_deploy(
        self, ctx: DiscoContext, name: str, network: str, ctype: str = "custom", *, flags: str = ""
    ) -> None:
        """Deploy a smart contract.

        Usage: .chain contract deploy <name> <network> [type] [flags]
        Types: limit_order | escrow | vesting | multisig | custom
        Flags:
          desc "description"   Set a human-readable description for this contract
          def {json}           Provide a custom JSON definition (for type=custom)

        Examples:
          .chain contract deploy MyEscrow arc escrow desc "Holds funds until both parties confirm"
          .chain contract deploy LimitBot arc limit_order desc "Auto sell ARC above $5000"

        Gas is required to submit the deploy transaction.
        """
        # Resolve network shorthand via the canonical normalizer.
        full_network = normalize_network_full(network) or network

        # Resolve template or custom definition
        if ctype in TEMPLATES:
            definition = TEMPLATES[ctype]
        else:
            # Try to parse def JSON from flags
            import re as _re
            m = _re.search(r'def\s+(\{.+\})', flags)
            if m:
                try:
                    definition = json.loads(m.group(1))
                except json.JSONDecodeError as e:
                    await ctx.reply_error(f"Invalid JSON definition: {e}")
                    return
            else:
                definition = {"functions": {}}

        try:
            from cogs.contracts import ContractEngine
            ContractEngine.validate_definition(definition)
        except ValueError as e:
            await ctx.reply_error(f"Invalid contract definition: {e}")
            return

        gas_price, _ = self._parse_flags(flags)

        # Parse desc flag
        import re as _re2
        desc_match = _re2.search(r'desc\s+"([^"]*)"', flags) or _re2.search(r"desc\s+'([^']*)'", flags) or _re2.search(r'desc\s+([^\s]+(?:\s+[^\s]+)*?)(?:\s+(?:def|gas|arg)\b|$)', flags)
        description = desc_match.group(1).strip() if desc_match else ""

        payload = {
            "name":        name,
            "network":     full_network,
            "type":        ctype,
            "definition":  definition,
            "description": description,
        }
        action_id = await self._queue_contract_action(
            ctx, "contract_deploy", full_network, payload, gas_price
        )
        if action_id is None:
            return

        tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[gas_price]
        functions = list(definition.get("functions", {}).keys())
        _b = card(
            "📜 Contract Deploy Queued",
            description=(
                f"**{name}** · `{ctype}` contract on **{full_network}**\n"
                f"Queued at mempool `#{action_id}`  -  deploys in the next validator block."
            ),
            color=C_AMBER,
        )
        if description:
            _b.field("📝 Description", description, False)
        _b.field("🌐 Network",    full_network,                                          True)
        _b.field("⛽ Gas Price",  f"{tier_emoji} **{gas_price.title()}**",               True)
        _b.field("🔢 Mempool ID", f"`#{action_id}`",                                     True)
        _b.field("⚙️ Functions",  ", ".join(f"`{f}`" for f in functions) or "*none*",    False)
        embed = _b.footer("📋 .chain contract list  -  find your address after confirmation").build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="call")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_call(
        self, ctx: DiscoContext, address: str, function: str, *, flags: str = ""
    ) -> None:
        """Call a function on a deployed contract.

        Usage: .chain contract call <address> <function> [arg key=val ...] [gas high|low]

        Example:
          .chain contract call 0xabc123 place arg token_in=ARC arg token_out=USDC \\
                                        arg amount=0.5 arg target_price=1800 \\
                                        arg pool_id=ETH_USDC
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found on this server.")
            return
        if ct["is_paused"]:
            await ctx.reply_error(f"Contract `{address}` is currently paused.")
            return

        fns = ct["definition"].get("functions", {})
        if function not in fns:
            available = ", ".join(f"`{f}`" for f in fns)
            await ctx.reply_error(f"Function `{function}` not found. Available: {available}")
            return

        gas_price, args = self._parse_flags(flags)

        payload = {
            "address":  address,
            "function": function,
            "args":     args,
        }
        action_id = await self._queue_contract_action(
            ctx, "contract_call", ct["network"], payload, gas_price
        )
        if action_id is None:
            return

        tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[gas_price]
        fn_def = fns[function]
        _b = card(
            "⚡ Contract Call Queued",
            description=(
                f"**{ct['name']}**.`{function}()`\n"
                f"🔗 `{address}`"
            ),
            color=C_AMBER,
        )
        if fn_def.get("description"):
            _b.field("📝 Function", fn_def["description"], False)
        _b.field("🌐 Network",    ct["network"],                    True)
        _b.field("⛽ Gas Price",  f"{tier_emoji} **{gas_price.title()}**", True)
        _b.field("🔢 Mempool ID", f"`#{action_id}`",                True)
        if args:
            args_text = "\n".join(f"`{k}` = `{v}`" for k, v in args.items())
            if len(args_text) > 1024:
                args_text = args_text[:1010] + "\n*(…)*"
            _b.field("🔧 Arguments", args_text, False)
        embed = _b.footer(f"⏳ Executes in next {ct['network']} validator block").build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="info")
    @guild_only
    async def contract_info(self, ctx: DiscoContext, address: str) -> None:
        """Show detailed info about a deployed contract.
        Usage: .chain contract info <address>
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        vuid = ct["virtual_uid"]
        fns  = ct["definition"].get("functions", {})

        # Gather contract balances
        balances: list[str] = []
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        for sym in list(all_tokens.keys()) + ["USD"]:
            if sym == "USD":
                user_row = await ctx.db.get_user(vuid, ctx.guild_id)
                bal = user_row.h("wallet") if user_row else 0.0
            else:
                h = await ctx.db.get_holding(vuid, ctx.guild_id, sym)
                bal = h.h("amount") if h else 0.0
            if bal > 0:
                cfg = Config.TOKENS.get(sym, {})
                balances.append(f"{cfg.get('emoji','●')}`{sym}` {fmt_token(bal, sym)}")

        owner = ctx.guild.get_member(ct["owner_id"])
        owner_str = owner.display_name if owner else mention(ct['owner_id'], ctx.guild)

        desc_text = ct.get("description", "")
        dep_ts = fmt_ts(ct["deployed_at"], "%Y-%m-%d %H:%M UTC")
        fn_list = "\n".join(
            f"`{fname}`  -  {fdef.get('description','')[:60]}"
            for fname, fdef in fns.items()
        )
        _b = card(
            f"📜 {ct['name']}",
            description=(
                f"🔗 `{address}`"
                + (f"\n\n*{desc_text}*" if desc_text else "")
            ),
            color=C_INFO,
        )
        _b.field("🏷 Type",     ct["type"],                                                  True)
        _b.field("🌐 Network",  ct["network"],                                               True)
        _b.field("👤 Owner",    owner_str,                                                   True)
        _b.field("📞 Calls",    f"**{ct['call_count']:,}**",                                 True)
        _b.field("📊 Status",   "⏸ Paused" if ct["is_paused"] else "✅ Active",              True)
        _b.field("🗓 Deployed", dep_ts,                                                      True)
        _b.field("⚙️ Functions", fn_list or "*none*",                                       False)

        if balances:
            _b.field("Balances", "\n".join(balances), False)

        state = ct["state"]
        if state:
            state_lines = [f"`{k}` = `{v}`" for k, v in list(state.items())[:10]]
            if len(state) > 10:
                state_lines.append(f"…and {len(state)-10} more")
            _b.field("State", "\n".join(state_lines), False)

        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="list", aliases=["ls"])
    @guild_only
    async def contract_list(self, ctx: DiscoContext, network: str = "") -> None:
        """List all deployed contracts on this server.
        Usage: .chain contract list [network]
        """
        net_filter = (normalize_network_full(network) or network) if network else None
        contracts = await ctx.db.get_contracts(ctx.guild_id, net_filter)

        if not contracts:
            msg = f"No contracts on **{net_filter}**." if net_filter else "No contracts deployed yet."
            await ctx.reply_error(msg)
            return

        _b = card(
            "📜 Smart Contracts",
            description=(
                f"🌐 {'**' + net_filter + '**' if net_filter else 'All networks'}  ·  "
                f"**{len(contracts)}** contract(s)"
            ),
            color=C_INFO,
        )
        for c in contracts[:15]:
            owner = ctx.guild.get_member(c["owner_id"])
            owner_str = owner.display_name if owner else mention(c['owner_id'], ctx.guild)
            status = "⏸ Paused" if c["is_paused"] else "✅ Active"
            fns = list(c["definition"].get("functions", {}).keys())
            _b.field(
                f"{'⏸' if c['is_paused'] else '✅'} {c['name']}  [{c['type']}]",
                (
                    f"🔗 `{c['address']}`\n"
                    f"🌐 {c['network']}  ·  👤 {owner_str}  ·  📞 {c['call_count']} calls\n"
                    f"⚙️ {', '.join(f'`{f}`' for f in fns) or '*none*'}"
                ),
                False,
            )
        if len(contracts) > 15:
            _b.footer(f"Showing 15 of {len(contracts)} contracts.")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="events", aliases=["log"])
    @guild_only
    async def contract_events(self, ctx: DiscoContext, address: str, limit: int = 10) -> None:
        """Show recent events emitted by a contract.
        Usage: .chain contract events <address> [limit]
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        events = await ctx.db.get_contract_events(ctx.guild_id, address, min(limit, 25))
        if not events:
            await ctx.reply_error("No events emitted yet.")
            return

        _b = card(
            f"📋 Contract Events  -  {ct['name']}",
            description=f"🔗 `{address}`  ·  {len(events)} event(s)",
            color=C_INFO,
        )
        for ev in events:
            ts_str = fmt_ts(ev["ts"], "%m/%d %H:%M")
            data_str = ", ".join(f"`{k}`=`{v}`" for k, v in ev["data"].items()) if ev["data"] else ""
            _b.field(
                f"📡 **{ev['event']}**  ·  {ts_str}",
                data_str if data_str else "*no data*",
                False,
            )
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="fund")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_fund(
        self, ctx: DiscoContext, address: str, symbol: str, amount: float
    ) -> None:
        """Send tokens directly to a contract's balance (owner or anyone).
        Usage: .chain contract fund <address> <symbol> <amount>
        This is an instant transfer  -  no gas required beyond normal tx.
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if ct["is_paused"]:
            await ctx.reply_error("Contract is paused.")
            return

        symbol = symbol.upper()
        if amount <= 0:
            await ctx.reply_error("Amount must be positive.")
            return

        vuid = ct["virtual_uid"]
        if symbol == "USD":
            if ctx.user_row.h("wallet") < amount:
                await ctx.reply_error(f"Insufficient USD. You have **{fmt_usd(ctx.user_row.h('wallet'))}**.")
                return
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -to_raw(amount))
            await ctx.db.ensure_user(vuid, ctx.guild_id)
            await ctx.db.update_wallet(vuid, ctx.guild_id, to_raw(amount))
        else:
            h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
            bal = h.h("amount") if h else 0.0
            if bal < amount:
                await ctx.reply_error(f"Insufficient {symbol}. You have **{fmt_token(bal, symbol)}**.")
                return
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, -to_raw(amount))
            await ctx.db.update_holding(vuid, ctx.guild_id, symbol, to_raw(amount))

        cfg = Config.TOKENS.get(symbol, {})
        await ctx.reply_success(
            f"Sent **{fmt_token(amount, symbol, cfg.get('emoji', ''))}** to contract **{ct['name']}**.",
            title="💸 Contract Funded",
        )
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=ct,
            action="fund",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={"symbol": symbol, "amount": amount},
        )

    @contract.command(name="withdraw")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_withdraw(
        self, ctx: DiscoContext, address: str, symbol: str, amount: float
    ) -> None:
        """Withdraw tokens from a contract's balance. Owner only.
        Usage: .chain contract withdraw <address> <symbol> <amount>
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if ct["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can withdraw.")
            return

        symbol = symbol.upper()
        vuid   = ct["virtual_uid"]

        if symbol == "USD":
            user_row = await ctx.db.get_user(vuid, ctx.guild_id)
            bal = user_row.h("wallet") if user_row else 0.0
            if bal < amount:
                await ctx.reply_error(f"Contract only has **{fmt_usd(bal)}** USD.")
                return
            await ctx.db.update_wallet(vuid, ctx.guild_id, -to_raw(amount))
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(amount))
        else:
            h = await ctx.db.get_holding(vuid, ctx.guild_id, symbol)
            bal = h.h("amount") if h else 0.0
            if bal < amount:
                await ctx.reply_error(f"Contract only has **{fmt_token(bal, symbol)}**.")
                return
            await ctx.db.update_holding(vuid, ctx.guild_id, symbol, -to_raw(amount))
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(amount))

        cfg = Config.TOKENS.get(symbol, {})
        await ctx.reply_success(
            f"Withdrew **{fmt_token(amount, symbol, cfg.get('emoji', ''))}** from **{ct['name']}**.",
            title="💰 Withdrawn",
        )
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=ct,
            action="withdraw",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={"symbol": symbol, "amount": amount},
        )

    @contract.command(name="txs", aliases=["transactions", "history"])
    @guild_only
    async def contract_txs(self, ctx: DiscoContext, address: str, limit: int = 15) -> None:
        """Show transaction history for a contract's balance address.
        Usage: .chain contract txs <address> [limit]
        """
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        vuid = ct["virtual_uid"]
        txs  = await ctx.db.get_user_tx_history(vuid, ctx.guild_id, min(limit, 25))

        if not txs:
            await ctx.reply_error("No transactions recorded for this contract yet.")
            return

        _b = card(
            f"📋 Contract Transactions  -  {ct['name']}",
            description=f"🔗 `{address}`  ·  showing **{len(txs)}** tx(s)",
            color=C_INFO,
        )
        for tx in txs:
            ts_str = fmt_ts(tx["ts"], "%m/%d %H:%M")
            sym_in   = tx.get("symbol_in", "")
            amt_in   = tx.get("amount_in", 0) or 0
            sym_out  = tx.get("symbol_out", "")
            amt_out  = tx.get("amount_out", 0) or 0
            tx_type  = tx.get("tx_type", "?")
            block    = f"  📦 blk `#{tx['block_num']}`" if tx.get("block_num") else ""

            if sym_in and sym_out and sym_in != sym_out:
                detail = f"`{fmt_token(amt_in, sym_in)}` → `{fmt_token(amt_out, sym_out)}`"
            elif sym_in:
                detail = f"`{fmt_token(amt_in, sym_in)}`"
            elif sym_out:
                detail = f"→ `{sym_out}`"
            else:
                detail = ""

            _b.field(
                f"⚡ `{tx_type}`  ·  {ts_str}{block}",
                detail or "*no detail*",
                False,
            )

        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="pause")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_pause(self, ctx: DiscoContext, address: str) -> None:
        """Pause a contract, preventing all calls. Owner only."""
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if ct["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can pause it.")
            return
        await ctx.db.pause_contract(address, True)
        await ctx.reply_success(f"Contract **{ct['name']}** is now paused.", title="⏸ Paused")
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=ct,
            action="pause",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={},
        )

    @contract.command(name="resume")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_resume(self, ctx: DiscoContext, address: str) -> None:
        """Resume a paused contract. Owner only."""
        ct = await ctx.db.get_contract(ctx.guild_id, address)
        if not ct:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if ct["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can resume it.")
            return
        await ctx.db.pause_contract(address, False)
        await ctx.reply_success(f"Contract **{ct['name']}** is now active.", title="▶️ Resumed")
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=ct,
            action="resume",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={},
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Mine subgroup  -  $chain mine ...
    # ══════════════════════════════════════════════════════════════════════════

    @chain.group(name="mine", invoke_without_command=True)
    @guild_only
    async def mine(self, ctx: DiscoContext) -> None:
        """Mining commands. Subcommands: rigs, buy, sell, status, history, solo, pool, group, network"""
        if await suggest_subcommand(ctx, self.mine):
            return
        await ctx.send_group_help(self.mine, title="⛏ Mining Commands", color=C_AMBER)

    @mine.command(name="rigs")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_rigs(self, ctx: DiscoContext) -> None:
        """List all available mining rigs and your quantities."""
        rows = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        # Sum quantities per rig_id across all chains
        your_rigs: dict[str, int] = {}
        for r in rows:
            your_rigs[r["rig_id"]] = your_rigs.get(r["rig_id"], 0) + r["quantity"]

        job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_id = job_row["job_id"] if job_row else "HOMELESS"
        job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
        max_slots = job_cfg.get("rig_slots", 2)
        used_slots = sum(your_rigs.values())

        _b = card(
            "⛏ SUN Mining  -  Rig Catalog",
            description=f"📦 **{used_slots}/{max_slots}** rig slots used  ·  Job: **{job_cfg['title']}**",
            color=C_AMBER,
        )
        for rig_id, rig in sorted(_RIGS.items(), key=lambda x: x[1]["tier"]):
            qty = your_rigs.get(rig_id, 0)
            owned_str = f"✅ **{qty}x owned**" if qty > 0 else f"*not owned*"
            elec_rate = _SUN.get("electricity_rate", 0.0)
            elec_hr = rig["power"] / 1000 * elec_rate  # W -> kW x $/kWh
            _b.field(
                f"{rig['emoji']} {rig['name']}  [Tier {rig['tier']}]",
                (
                    f"⛏ Hashrate: **{rig['hashrate']:,} MH/s**  ·  ⚡ {rig['power']}W ({fmt_usd(elec_hr)}/hr)\n"
                    f"💵 Price: **{fmt_usd(to_human(rig['price']))}**  ·  {owned_str}\n"
                    f"`{ctx.prefix}chain mine buy {rig_id}`  ·  `{ctx.prefix}chain mine sell {rig_id}`"
                ),
                False,
            )
        embed = _b.footer(f"☀ SUN halves every 210,000 blocks  ·  {ctx.prefix}chain mine network for global stats").build()
        await ctx.reply(embed=embed, mention_author=False)

    @mine.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def mine_buy(self, ctx: DiscoContext, rig_id: str, qty_or_chain: str = "1", chain: str = "SUN") -> None:
        """Buy mining rigs. Usage: $chain mine buy RIG_ID [qty] [mta|sun]
        Chain can be passed as the second arg: `mine buy RTX4090 mta` or `mine buy RTX4090 2 mta`"""
        rig_id = rig_id.upper()
        rig = _RIGS.get(rig_id)
        if not rig:
            await ctx.reply_error(f"Unknown rig `{rig_id}`. Use `{ctx.prefix}chain mine rigs` to see options.")
            return

        # Allow flexible ordering: "buy RTX4090 mta" or "buy RTX4090 2 mta"
        valid_chains = list(Config.POW_NETWORKS.keys())
        if qty_or_chain.upper() in valid_chains:
            chain = qty_or_chain.upper()
            qty = 1
        else:
            try:
                qty = int(qty_or_chain)
            except ValueError:
                chain_list = " | ".join(c.lower() for c in valid_chains)
                await ctx.reply_error(f"Usage: `{ctx.prefix}chain mine buy <rig> [qty] [{chain_list}]`")
                return
            chain = chain.upper()
            if chain not in valid_chains:
                chain_list = " | ".join(c.lower() for c in valid_chains)
                await ctx.reply_error(f"Invalid chain. Choose: **{chain_list}**")
                return

        if qty <= 0:
            await ctx.reply_error("Quantity must be positive.")
            return

        total_cost = rig["price"] * qty  # raw int
        if ctx.user_row["wallet"] < total_cost:
            await ctx.reply_error(
                f"That costs **{fmt_usd(to_human(total_cost))}** but you only have **{fmt_usd(to_human(ctx.user_row['wallet']))}**."
            )
            return

        # ── Slot limit enforcement ────────────────────────────────────────────
        job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_id = job_row["job_id"] if job_row else "HOMELESS"
        job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
        max_slots = job_cfg.get("rig_slots", 2)
        existing_chain_rigs = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        _rig_totals: dict[str, int] = {}
        for _r in existing_chain_rigs:
            _rig_totals[_r["rig_id"]] = _rig_totals.get(_r["rig_id"], 0) + _r["quantity"]
        current_total = sum(_rig_totals.values())
        if current_total + qty > max_slots:
            await ctx.reply_error(
                f"**Slot limit reached.** Your job (**{job_cfg['title']}**) allows "
                f"**{max_slots}** rig slot{'s' if max_slots != 1 else ''}. "
                f"You're using **{current_total}** and can only add **{max_slots - current_total}** more."
            )
            return

        # ── Estimate ROI ─────────────────────────────────────────────────────
        _chain_cfg_est = Config.POW_NETWORKS[chain]
        _chain_sym_est = _chain_cfg_est["symbol"]
        pow_state_est  = await ctx.db.get_pow_network(ctx.guild_id, chain)
        _net_hr_est    = float(pow_state_est["total_hashrate"]) if pow_state_est else 1.0
        _height_est    = int(pow_state_est["block_height"]) if pow_state_est else 0
        _reward_est    = _pow_current_reward(_height_est, _chain_cfg_est)
        added_hr       = rig["hashrate"] * qty
        new_net_hr     = max(_net_hr_est + added_hr, 1.0)
        blocks_per_hour = (added_hr / new_net_hr) * (3600 / _chain_cfg_est["target_block_time"])
        earnings_hr    = blocks_per_hour * _reward_est
        total_cost_h   = to_human(total_cost)
        breakeven_hrs  = (total_cost_h / earnings_hr) if earnings_hr > 0 else float("inf")

        chain_price_row = await ctx.db.get_price(_chain_sym_est, ctx.guild_id)
        chain_price = float(chain_price_row["price"]) if chain_price_row else 0.0
        earnings_hr_usd = earnings_hr * chain_price

        # ── Confirmation ─────────────────────────────────────────────────────
        if math.isfinite(breakeven_hrs):
            be_str = f"**{breakeven_hrs:,.0f} hrs** ({breakeven_hrs / 24:,.1f} days)"
        else:
            be_str = "**∞** (no network hashrate yet)"
        _res_usd = total_cost_h / 2.0
        _conf_hs = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        _conf_hs_bonus = _item_stat(_conf_hs, "mining_bonus")
        chain_cfg = Config.POW_NETWORKS[chain]
        chain_sym = chain_cfg["symbol"]
        chain_emoji = chain_cfg.get("emoji", "")
        conf_embed = (
            card(
                f"⚠️ Confirm Rig Purchase",
                description=f"{rig['emoji']} **{rig['name']}** × {qty}  -  review before buying.",
                color=C_AMBER,
            )
            .field("🛒 Rig",          f"**{rig['name']}** × {qty}\n⛏ {fmt_bonus(f'+{added_hr:,} MH/s', _conf_hs_bonus)}",  True)
            .field("💵 Cost",          f"**{fmt_usd(total_cost_h)}**\n💳 After: {fmt_usd(to_human(ctx.user_row['wallet']) - total_cost_h)}", True)
            .field("⛓ Chain",          f"{chain_emoji} **{chain}**", True)
            .field("⚡ Est. Earn/hr",  f"**{fmt_token(earnings_hr, chain_sym, chain_emoji)}** ≈ {fmt_usd(earnings_hr_usd)}\n⏱ Break-even: {be_str}", True)
            .field("🏦 Vault",         f"≈ ${_res_usd:,.4f} → USD Vault",                                True)
            .footer("📊 Estimates based on current difficulty and price")
            .build()
        )
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card(description="Purchase cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -total_cost)
        new_qty = await ctx.db.update_rig(ctx.author.id, ctx.guild_id, rig_id, qty)
        await ctx.db.set_rig_chain_quantity(ctx.author.id, ctx.guild_id, rig_id, chain, qty)
        await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", total_cost)

        # Auto-create a DeFi wallet for the chosen chain if needed
        chain_net_map = {"SUN": ("sun", "Sun Network"), "MTA": ("mta", "Moneta Chain")}
        _net_prefix, _net_name = chain_net_map.get(chain, ("sun", "Sun Network"))
        if not await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, _net_prefix):
            addresses = await ctx.db.get_user_addresses(ctx.author.id, ctx.guild_id)
            if len(addresses) < 10:
                await ctx.db.create_wallet_address(
                    ctx.author.id, ctx.guild_id,
                    label="Mining Wallet", is_temp=False,
                    network=_net_name, address_prefix=_net_prefix,
                )

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "MINE_BUY",
            symbol_in="USD", amount_in=total_cost,
            symbol_out=rig_id, amount_out=qty,
            network="usd",
        )
        await ctx.bot.bus.publish(
            "mine_rig_bought",
            guild=ctx.guild, user=ctx.author,
            rig_id=rig_id, qty=qty, total_cost=total_cost, tx_hash=tx_hash,
        )

        _res_usd2 = total_cost_h / 2.0
        hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        _hs_bonus = _item_stat(hashstone, "mining_bonus")
        _hr_str = fmt_bonus(f"+{rig['hashrate'] * qty:,} MH/s", _hs_bonus)
        embed = (
            card(f"🎉 Rig Purchased!", color=C_SUCCESS)
            .field("⛏ Rig",           f"**{rig['name']}** × {qty}\n⚡ {_hr_str}", True)
            .field("💵 Cost",          f"**{fmt_usd(total_cost_h)}**",               True)
            .field("⛓ Chain",          f"{chain_cfg.get('emoji', '')} **{chain}**  -  use `,chain mine assign` to change", True)
            .field("📦 Owned",         f"**{new_qty}** total\n🏦 ${_res_usd2:,.2f} → Vault", True)
            .build()
        )
        set_tx(embed, ctx.guild_id, tx_hash)
        await ctx.reply(embed=embed, mention_author=False)

    @mine.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def mine_sell(self, ctx: DiscoContext, rig_id: str, qty: str = "1") -> None:
        """Sell mining rigs back at 50% value. Usage: $mine sell RIG_ID [quantity|all]"""
        rig_id = rig_id.upper()
        rig = _RIGS.get(rig_id)
        if not rig:
            # Try swapping: user may have typed 'mine sell 1 GTX1080'
            if qty.upper() in _RIGS:
                rig_id, qty = qty.upper(), rig_id
                rig = _RIGS.get(rig_id)
        if not rig:
            await ctx.reply_error(f"Unknown rig `{rig_id}`.")
            return

        all_chain_rows = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        owned = sum(r["quantity"] for r in all_chain_rows if r["rig_id"] == rig_id)

        if qty.lower() == "all":
            sell_qty = owned
        else:
            try:
                sell_qty = int(qty)
            except ValueError:
                await ctx.reply_error("Quantity must be a number or `all`.")
                return

        if sell_qty <= 0 or owned == 0:
            await ctx.reply_error(f"You have no **{rig['name']}** to sell.")
            return
        if sell_qty > owned:
            await ctx.reply_error(f"You only own **{owned}** {rig['name']}(s).")
            return

        resale_raw = rig["price"] * sell_qty // 2
        original_cost = rig["price"] * sell_qty
        resale_h = to_human(resale_raw)
        original_cost_h = to_human(original_cost)
        loss_h = original_cost_h - resale_h

        # ── Confirmation ─────────────────────────────────────────────────────
        conf_embed = (
            card(
                f"⚠️ Confirm Rig Sale",
                description=f"{rig['emoji']} **{rig['name']}** × {sell_qty}  -  rigs sell at **50% of purchase price**.",
                color=C_AMBER,
            )
            .field("💵 Resale Value",   f"**{fmt_usd(resale_h)}**\n📉 Cost: {fmt_usd(original_cost_h)}  ·  Loss: -{fmt_usd(loss_h)}", True)
            .field("⛏ Hashrate Lost",   f"**-{rig['hashrate'] * sell_qty:,} MH/s**", True)
            .footer("⚠️ This action cannot be undone")
            .build()
        )
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card(description="Sale cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        # Atomic: re-verify ownership + deduct rigs + credit wallet in one transaction
        _sell_failed = False
        async with ctx.db.atomic():
            # Re-check inside transaction to close the race window
            _fresh_rows = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
            _fresh_owned = sum(r["quantity"] for r in _fresh_rows if r["rig_id"] == rig_id)
            if sell_qty > _fresh_owned:
                _sell_failed = True
            else:
                await ctx.db.update_rig(ctx.author.id, ctx.guild_id, rig_id, -sell_qty)
                # Remove from rig_chain_assignments: drain from SUN first, then other chains
                _sell_remain = sell_qty
                _rig_chain_rows = sorted(
                    [r for r in _fresh_rows if r["rig_id"] == rig_id],
                    key=lambda x: (0 if x["chain_symbol"] == "SUN" else 1, x["chain_symbol"]),
                )
                for _rc in _rig_chain_rows:
                    if _sell_remain <= 0:
                        break
                    _remove = min(_sell_remain, _rc["quantity"])
                    if _remove > 0:
                        await ctx.db.remove_rig_chain_quantity(
                            ctx.author.id, ctx.guild_id, rig_id, _rc["chain_symbol"], _remove
                        )
                        _sell_remain -= _remove
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, resale_raw)

        if _sell_failed:
            await ctx.reply_error(f"You now only own **{_fresh_owned}** {rig['name']}(s).")
            return

        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "MINE_SELL",
            symbol_in=rig_id, amount_in=sell_qty,
            symbol_out="USD", amount_out=resale_raw,
        )
        await ctx.reply_success(
            f"Sold **{sell_qty}× {rig['name']}** for **{fmt_usd(resale_h)}** (50% resale value).",
            title=f"{rig['emoji']} Rigs Sold",
        )

    @mine.command(name="assign")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_assign(self, ctx: DiscoContext, qty: str, rig_id: str, chain: str = "") -> None:
        """Assign rigs to MTA or SUN mining (each rig mines only one chain at a time).

        Usage:
          .chain mine assign <qty|all> <RIG_ID> mta    -  move rigs to Moneta mining
          .chain mine assign <qty|all> <RIG_ID> sun    -  move rigs back to SUN mining
        """
        rig_id = rig_id.upper()
        chain = chain.upper() if chain else ""
        valid_chains = list(Config.POW_NETWORKS.keys())
        if chain not in valid_chains:
            chain_list = " | ".join(c.lower() for c in valid_chains)
            await ctx.reply_error(
                f"Specify a chain: **{chain_list}**\nExample: `.chain mine assign 2 RTX4090 mta`"
            )
            return

        rig = _RIGS.get(rig_id)
        if not rig:
            await ctx.reply_error(f"Unknown rig `{rig_id}`. Use `.chain mine rigs` to see options.")
            return

        # Check what we own across all chains
        all_assign_rows = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        total_owned = sum(r["quantity"] for r in all_assign_rows if r["rig_id"] == rig_id)
        if total_owned == 0:
            await ctx.reply_error("You don't own any of those rigs.")
            return

        # Determine source chain (whichever chain has rigs that isn't the destination)
        other_chains = [r for r in all_assign_rows if r["rig_id"] == rig_id and r["chain_symbol"] != chain]
        if not other_chains:
            await ctx.reply_error(f"All your **{rig['name']}** rigs are already on **{chain}**.")
            return

        # Chain-switch cooldown: prevent rapid hopping exploits.
        # Applies equally to solo, pool, and group miners.
        last_switch = await ctx.db.get_last_chain_switch(ctx.author.id, ctx.guild_id)
        if last_switch:
            _sw_epoch = (
                last_switch.timestamp()
                if hasattr(last_switch, "timestamp")
                else float(last_switch)
            )
            _sw_elapsed = time.time() - _sw_epoch
            if _sw_elapsed < _CHAIN_SWITCH_COOLDOWN:
                remaining = int(_CHAIN_SWITCH_COOLDOWN - _sw_elapsed)
                mins, secs = divmod(remaining, 60)
                await ctx.reply_error(
                    f"Chain switching is on cooldown. Try again in **{mins}m {secs}s**.\n"
                    "This prevents chain-hopping exploitation."
                )
                return

        if qty.lower() == "all":
            move_qty = sum(r["quantity"] for r in other_chains)
        else:
            try:
                move_qty = int(qty)
                if move_qty <= 0:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Quantity must be a positive integer or 'all'.")
                return

        if move_qty == 0:
            await ctx.reply_error("No rigs to reassign.")
            return

        # Move rigs from other chains to target chain (drain source chains in order)
        _move_remain = move_qty
        _sources = sorted(other_chains, key=lambda x: (0 if x["chain_symbol"] == "SUN" else 1, x["chain_symbol"]))
        # Validate total available
        total_avail = sum(r["quantity"] for r in _sources)
        if move_qty > total_avail:
            await ctx.reply_error(
                f"You only have **{total_avail}** rigs available to move (not on **{chain}**)."
            )
            return

        try:
            for _src in _sources:
                if _move_remain <= 0:
                    break
                _take = min(_move_remain, _src["quantity"])
                if _take > 0:
                    await ctx.db.assign_rig_to_chain(
                        ctx.author.id, ctx.guild_id, rig_id,
                        _src["chain_symbol"], chain, _take,
                    )
                    _move_remain -= _take
        except ValueError as e:
            await ctx.reply_error(str(e))
            return

        # Record chain switch for cooldown enforcement
        await ctx.db.record_chain_switch(ctx.author.id, ctx.guild_id)

        # Auto-create wallet for the destination chain if missing
        chain_cfg = Config.POW_NETWORKS.get(chain, {})
        chain_name = chain_cfg.get("name", chain)
        has_wallet = await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, chain.lower())
        if not has_wallet:
            await ctx.db.create_wallet_address(
                ctx.author.id, ctx.guild_id,
                label=f"{chain_name} Wallet",
                network=f"{chain_name} Network",
            )

        chain_emoji = chain_cfg.get("emoji", "⛏")
        _b = card(
            f"{chain_emoji} {chain} Rigs Assigned",
            description=(
                f"Moved **{move_qty}× {rig['name']}** to **{chain_name} ({chain})** mining.\n"
                f"Each rig mines only one chain at a time.\n"
                f"Use `.chain mine status` to see your full rig breakdown."
            ),
            color=C_AMBER,
        )
        await ctx.reply(embed=_b.build(), mention_author=False)

    @mine.command(name="status")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_status(self, ctx: DiscoContext) -> None:
        """Show your mining setup and estimated earnings across all PoW chains."""
        user_hr = await ctx.db.get_user_chain_hashrate(ctx.author.id, ctx.guild_id, "SUN")
        user_btc_hr = await ctx.db.get_user_chain_hashrate(ctx.author.id, ctx.guild_id, "MTA")
        # Build rig display from rig_chain_assignments
        all_chain_rigs = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        # Group by rig_id for display
        rig_chains: dict[str, dict[str, int]] = {}
        for r in all_chain_rigs:
            rig_chains.setdefault(r["rig_id"], {})[r["chain_symbol"]] = r["quantity"]
        rows = [{"rig_id": k, **{f"qty_{sym}": q for sym, q in v.items()}} for k, v in rig_chains.items()]
        mining_cfg = await ctx.db.get_user_mining_config(ctx.author.id, ctx.guild_id)
        mode = mining_cfg.get("mode", "pool").title()
        # Show group name if in group mode
        if mode.lower() == "group":
            grp = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
            mode = f"Group ({grp['name']})" if grp else "Group"
        network = await ctx.db.get_pow_network(ctx.guild_id, "SUN")
        block_reward = _current_reward(network["block_height"]) if network else _SUN["initial_reward"]
        net_hr = (network["total_hashrate"] or 1) if network else 1
        difficulty = (network.get("difficulty") or _SUN["initial_difficulty"]) if network else _SUN["initial_difficulty"]

        sun_holding = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "sun", "SUN")
        sun_bal = to_human(sun_holding["amount"]) if sun_holding else 0.0

        # Estimated hourly SUN: blocks per hour at this hashrate
        lam_per_tick = (user_hr * _TICK / difficulty) if difficulty > 0 else 0
        blocks_per_hr = lam_per_tick * (3600 / _TICK)
        est_hr = blocks_per_hr * block_reward

        sun_price_row = await ctx.db.get_price("SUN", ctx.guild_id)
        sun_usd = float(sun_price_row["price"]) if sun_price_row else 0.0
        usd_per_day = est_hr * 24 * sun_usd

        # Slot usage, electricity, and ROI  -  computed from rig_chain_assignments
        job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_id  = job_row["job_id"] if job_row else "HOMELESS"
        job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
        max_slots = job_cfg.get("rig_slots", 2)
        all_chain_rigs_raw = await ctx.db.get_user_all_chain_rigs(ctx.author.id, ctx.guild_id)
        # Total slots = unique (rig_id) quantities across all chains (each physical rig counts once)
        rig_total_qty: dict[str, int] = {}
        for r in all_chain_rigs_raw:
            rig_total_qty[r["rig_id"]] = rig_total_qty.get(r["rig_id"], 0) + r["quantity"]
        used_slots = sum(rig_total_qty.values())
        rig_resale = sum(
            to_human(_RIGS[rid]["price"]) * qty * 0.5
            for rid, qty in rig_total_qty.items() if rid in _RIGS
        )

        # Estimated hourly MTA
        btc_network = await ctx.db.get_pow_network(ctx.guild_id, "MTA")
        btc_net_hr = (btc_network["total_hashrate"] or 1) if btc_network else 1
        btc_difficulty = (btc_network.get("difficulty") or _BTC["initial_difficulty"]) if btc_network else _BTC["initial_difficulty"]
        btc_block_reward = _current_btc_reward(btc_network["block_height"]) if btc_network else _BTC["initial_reward"]
        btc_lam_per_tick = (user_btc_hr * _TICK / btc_difficulty) if btc_difficulty > 0 else 0
        btc_blocks_per_hr = btc_lam_per_tick * (3600 / _TICK)
        est_btc_hr = btc_blocks_per_hr * btc_block_reward
        btc_price_row = await ctx.db.get_price("MTA", ctx.guild_id)
        btc_usd = float(btc_price_row["price"]) if btc_price_row else 0.0
        btc_usd_per_day = est_btc_hr * 24 * btc_usd

        total_usd_per_day = usd_per_day + btc_usd_per_day
        roi_days = rig_resale / total_usd_per_day if total_usd_per_day > 0 else float("inf")

        # Per-chain electricity cost
        elec_per_hr = 0.0
        for sym, pow_cfg in Config.POW_NETWORKS.items():
            sym_rigs = [r for r in all_chain_rigs_raw if r["chain_symbol"] == sym]
            watts = sum(_RIGS[r["rig_id"]]["power"] * r["quantity"] for r in sym_rigs if r["rig_id"] in _RIGS)
            elec_per_hr += watts / 1000 * pow_cfg.get("electricity_rate", 0.0)
        net_usd_hr  = est_hr * sun_usd + est_btc_hr * btc_usd - elec_per_hr

        hr_bar   = FormatKit.bar(user_hr + user_btc_hr, max(net_hr, user_hr + user_btc_hr, 1), width=12)
        slot_bar = FormatKit.bar(used_slots, max(max_slots, 1), width=8, show_pct=False)

        _b = card("⛏ Mining Dashboard", color=C_AMBER)
        _b.author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        _b.description(
            f"```\nHashrate  {hr_bar}\n"
            f"Rig Slots {slot_bar} {used_slots}/{max_slots}\n```"
            f"⚙️ Mode: **{mode}**  ·  Job: **{job_cfg['title']}**"
        )

        # ── Active Rigs ───────────────────────────────────────────────────
        if not rows:
            _b.field("🖥 Active Rigs", "No rigs  -  use `.chain mine buy` to get started", False)
        else:
            rig_lines = []
            for r in rows:
                rig_id = r["rig_id"]
                if rig_id not in _RIGS:
                    continue
                rcfg = _RIGS[rig_id]
                for sym, token_cfg in Config.POW_NETWORKS.items():
                    q = r.get(f"qty_{sym}", 0)
                    if q > 0:
                        rig_lines.append(
                            f"{rcfg['emoji']} **{rcfg['name']}** ×{q}"
                            f"  →  {token_cfg['emoji']} {sym}  `{rcfg['hashrate'] * q:,} MH/s`"
                        )
            _b.field("🖥 Active Rigs", "\n".join(rig_lines) or "None assigned", False)

        # ── Hashrate & Group Bonuses ──────────────────────────────────────
        hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        mining_bonus = _item_stat(hashstone, "mining_bonus")
        mining_bonus += job_cfg.get("perks", {}).get("mining_bonus", 0.0)

        _sun_hr_str = fmt_bonus(f"**{user_hr:,} MH/s**", mining_bonus)
        _btc_hr_str = f"**{user_btc_hr:,} MH/s**" if user_btc_hr else " - "

        hr_lines = [
            f"☀ SUN  {_sun_hr_str}",
            f"🟡 MTA  {_btc_hr_str}",
        ]

        if mode.lower().startswith("group"):
            _grp = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
            if _grp:
                _upgrades = await ctx.db.get_group_upgrades(ctx.guild_id, _grp["group_id"])
                _uids = {u["upgrade_id"] for u in _upgrades}
                # Hall upgrades do not affect mining stats - only Hall thread bonuses
                _hr_bonus = 0.0
                _rw_bonus = 0.0
                _xp_bonus = 0.0
                _elec_reduc = 0.0
                _luck_bonus = 0.0
                _parts = []
                if _hr_bonus > 0:
                    _parts.append(f"⚡ +{_hr_bonus*100:.0f}% HR")
                if _rw_bonus > 0:
                    _parts.append(f"💰 +{_rw_bonus*100:.0f}% Reward")
                if _xp_bonus > 0:
                    _parts.append(f"📡 +{_xp_bonus*100:.0f}% XP")
                if _elec_reduc > 0:
                    _parts.append(f"☀️ -{_elec_reduc*100:.0f}% Elec")
                if _luck_bonus > 0:
                    _parts.append(f"🍀 +{_luck_bonus*100:.0f}% Luck")
                if _parts:
                    hr_lines.append(f"👥 {' · '.join(_parts)}")

        _b.field("⛏ Hashrate", "\n".join(hr_lines), False)

        # ── Balances ──────────────────────────────────────────────────────
        btc_holding = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "mta", "MTA")
        btc_bal = to_human(btc_holding["amount"]) if btc_holding else 0.0

        _b.field("💰 Balances",
            f"☀ **{fmt_token(sun_bal, 'SUN')}**  ≈ {fmt_usd(sun_bal * sun_usd)}\n"
            f"🟡 **{btc_bal:.8f} MTA**  ≈ {fmt_usd(btc_bal * btc_usd)}",
            True)

        # ── Earnings ──────────────────────────────────────────────────────
        earn_lines = [f"☀ SUN  **{fmt_token(est_hr, 'SUN')}**/hr"]
        if user_btc_hr:
            earn_lines.append(f"🟡 MTA  **{est_btc_hr:.8f}**/hr")
        earn_lines.append(f"💵 **{fmt_usd(total_usd_per_day)}**/day")
        _b.field("📊 Earnings", "\n".join(earn_lines), True)

        # ── Costs & ROI ───────────────────────────────────────────────────
        cost_lines = [
            f"⚡ Electricity  {fmt_usd(elec_per_hr)}/hr",
            f"💵 Net profit  **{fmt_usd(net_usd_hr)}**/hr",
        ]
        if roi_days < float("inf"):
            cost_lines.append(f"📈 ROI  **{roi_days:.1f}** days")
        _b.field("🧾 Costs & ROI", "\n".join(cost_lines), True)

        embed = _b.footer(
            "💡 .chain mine assign · .chain mine solo | pool | group"
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @mine.command(name="history", aliases=["hist"])
    @guild_only
    async def mine_history(self, ctx: DiscoContext) -> None:
        """Show the last 10 blocks mined on this server's network."""
        blocks = await ctx.db.get_recent_blocks(ctx.guild_id, limit=10)
        if not blocks:
            await ctx.reply_error(f"No blocks found yet. Get some rigs with `{ctx.prefix}chain mine buy`.")
            return
        _SYMBOL_EMOJI = {"SUN": "☀", "MTA": "🟡", "ARC": "🔵", "DSC": "◈"}
        lines = []
        for b in blocks:
            miner = mention(b['miner_id'], ctx.guild) if b.get("miner_id") else "Pool"
            sym = b.get("symbol") or "SUN"
            emoji = _SYMBOL_EMOJI.get(sym, "⛏")
            t = fmt_ts(b["block_ts"], "%H:%M:%S")
            lines.append(
                f"`#{b['block_height']:,}` {miner}  -  **{fmt_token(b['reward'], sym, emoji)}** @ {t} UTC"
            )
        embed = (
            card(
                "📜 Block History",
                description="\n".join(lines),
                color=C_AMBER,
            )
            .footer("⛏ Most recent blocks first  ·  .chain mine network for global stats")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @mine.command(name="solo")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_solo(self, ctx: DiscoContext) -> None:
        """Switch to solo mining mode (individual Poisson block rolls  -  full reward, high variance)."""
        await ctx.db.set_user_mining_mode(ctx.author.id, ctx.guild_id, "solo")
        await ctx.reply_success(
            "Switched to **solo mining**. Full block rewards, but high variance.",
            title="⛏ Solo Mining",
        )

    @mine.command(name="pool")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_pool(self, ctx: DiscoContext) -> None:
        """Switch to pool mining mode (steady proportional payouts). This is the default."""
        await ctx.db.set_user_mining_mode(ctx.author.id, ctx.guild_id, "pool")
        await ctx.reply_success(
            "Switched to **pool mining**. Steady proportional ☀ SUN every tick.",
            title="🏊 Pool Mining",
        )

    @mine.command(name="group")
    @guild_only
    @no_bots
    @ensure_registered
    async def mine_group(self, ctx: DiscoContext) -> None:
        """Switch to group mining mode. Rewards are distributed to your mining group
        by the group's configured weight mode (hashrate / equal / custom)."""
        grp = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not grp:
            await ctx.reply_error(
                "You're not in a mining group. Join one first with `.group join <name>`, "
                "or create one with `.group create <name>`."
            )
            return
        await ctx.db.set_user_mining_mode(ctx.author.id, ctx.guild_id, "group")
        await ctx.reply_success(
            f"Switched to **group mining** in **{grp['name']}**.\n"
            f"Rewards distributed by `{grp.get('weight_mode', 'hashrate')}` mode.",
            title="👥 Group Mining",
        )

    @mine.command(name="network", aliases=["net"])
    @guild_only
    async def mine_network(self, ctx: DiscoContext) -> None:
        """Show global mining network stats for all PoW chains on this server."""
        embeds: list[discord.Embed] = []

        for symbol, cfg in Config.POW_NETWORKS.items():
            network = await ctx.db.get_pow_network(ctx.guild_id, symbol)
            if not network:
                continue

            block_height = network["block_height"]
            difficulty = network.get("difficulty") or cfg["initial_difficulty"]
            block_reward = _pow_current_reward(block_height, cfg)
            halving_total = cfg.get("halving_blocks", 210_000)
            next_halving = (block_height // halving_total + 1) * halving_total
            blocks_to_halving = next_halving - block_height
            net_hr = network["total_hashrate"] or 0

            lam_10min = (net_hr * 600 / difficulty) if difficulty > 0 else 0

            user_hr = 0
            if hasattr(ctx, "user_row"):
                user_hr = await ctx.db.get_user_chain_hashrate(ctx.author.id, ctx.guild_id, symbol)
            share_pct = (user_hr / net_hr * 100) if net_hr > 0 else 0

            next_retarget = (
                (network.get("last_retarget_height") or 0) + cfg["difficulty_window"]
            )
            blocks_to_retarget = max(0, next_retarget - block_height)

            emoji = cfg.get("emoji", "⛏")
            halving_bar = FormatKit.bar(halving_total - blocks_to_halving, halving_total, width=10)
            _b = card(
                f"{emoji} {cfg['name']} Mining Network",
                description=(
                    f"{emoji} **Halving progress:** `{halving_bar}`\n"
                    f"Next halving in **{blocks_to_halving:,}** blocks"
                ),
                color=C_AMBER,
            )
            _b.field("📦 Block Height",  f"**{block_height:,}**",                                     True)
            _b.field("⛏ Net Hashrate",   f"**{net_hr:,.1f} MH/s**",                                   True)
            _b.field("🏆 Block Reward",  f"**{block_reward:,.8f} {emoji} {symbol}**",                  True)
            _b.field("🔢 Difficulty",    f"**{difficulty:,.0f}**",                                     True)
            _b.field("📊 Avg Blk/10m",  f"**{lam_10min:.3f}**",                                       True)
            _b.field("🔄 Retarget In",   f"**{blocks_to_retarget:,}** blocks",                         True)
            if user_hr:
                hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
                mining_bonus = _item_stat(hashstone, "mining_bonus")
                _b.field("👤 Your Share", fmt_bonus(f"**{share_pct:.3f}%**  ({user_hr:,} MH/s)", mining_bonus), True)
            embed = _b.footer(
                f"Halves every {halving_total:,} blocks  ·  Retargets every {cfg['difficulty_window']:,} blocks"
            ).build()
            embeds.append(embed)

        if not embeds:
            await ctx.reply_error("Mining network not initialized yet.")
            return

        await ctx.reply(embeds=embeds, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    # Hidden prefix-only aliases for backward compatibility
    # ══════════════════════════════════════════════════════════════════════════

    @commands.command(name="mine", hidden=True)
    async def _alias_mine(self, ctx: DiscoContext) -> None:
        """Backward-compat alias  -  redirects to $chain mine."""
        await ctx.send_help(self.mine)

    @commands.command(name="block", hidden=True)
    async def _alias_block(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Backward-compat alias  -  redirects to $chain block."""
        parts = args.split() if args else []
        number_or_network = parts[0] if parts else ""
        network = parts[1] if len(parts) > 1 else ""
        await self.block_cmd(ctx, number_or_network=number_or_network, network=network)

    @commands.command(name="txinfo", hidden=True)
    async def _alias_txinfo(self, ctx: DiscoContext, tx_hash: str = "") -> None:
        """Backward-compat alias  -  redirects to $chain tx."""
        if not tx_hash:
            await ctx.reply_error("Usage: `.txinfo <hash>`")
            return
        await self.txinfo(ctx, tx_hash=tx_hash)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChainGroup(bot))
