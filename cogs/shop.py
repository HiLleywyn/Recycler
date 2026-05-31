"""cogs/shop.py  -  Item Shop and inventory system.

/shop                       -  list all shop items
/shop buy                   -  list buyable items
/shop buy hashstone          -  buy a Hashstone
/shop sell hashstone         -  sell a Hashstone
/shop transfer hashstone @user  -  transfer a Hashstone

The Hashstone (💎) is a stakeable item purchased with DSD (stablecoin).
- DSD is locked (staked) in the item, not burned.
- Levels 1 - 100, +1% per level per stat (max +100%).
- XP earned during mining, proportional to hashrate share.
- Transferable via Discoin Network (flat DSD gas fee).
- Buy/sell both charge a % fee sent to the guild treasury.
"""
from __future__ import annotations

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.network import (
    STABLE_NETWORK as _STABLE_NETWORK,
    stable_display as _stable_display,
    stable_emoji as _stable_emoji,
)
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_SUCCESS,
    C_WARNING, ConfirmView, FormatKit, fmt_token, fmt_usd,
)
from core.framework.fuzzy import suggest_subcommand
from core.framework.heartbeat import register_interval
from services.vault import deposit_to_vault

_SS  = Config.SHOP_ITEMS["hashstone"]
_LS  = Config.SHOP_ITEMS.get("lockstone", {})
_VS  = Config.SHOP_ITEMS.get("vaultstone", {})
_LQ  = Config.SHOP_ITEMS.get("liqstone", {})
# Themed minigame stones (fishing / buddy / dungeon / battle / farming).
_TI  = Config.SHOP_ITEMS.get("tidestone", {})
_HE  = Config.SHOP_ITEMS.get("heartstone", {})
_CR  = Config.SHOP_ITEMS.get("cryptstone", {})
_BL  = Config.SHOP_ITEMS.get("bloodstone", {})
_BM  = Config.SHOP_ITEMS.get("bloomstone", {})
# Meta-economy stones (auction / craft / swap, USD-priced).
_GV  = Config.SHOP_ITEMS.get("gavelstone", {})
_AV  = Config.SHOP_ITEMS.get("anvilstone", {})
_CH  = Config.SHOP_ITEMS.get("chimerastone", {})
_VG  = Config.SHOP_ITEMS.get("validator_guard", {})
_YG  = Config.SHOP_ITEMS.get("yield_guard", {})
_GK  = Config.SHOP_ITEMS.get("glamour_kit", {})
_NC  = Config.SHOP_ITEMS.get("night_crystal", {})
_AP  = Config.SHOP_ITEMS.get("aurora_pass", {})

_BUYABLE_ITEMS = ("hashstone", "lockstone", "vaultstone", "liqstone",
                  "tidestone", "heartstone", "cryptstone", "bloodstone",
                  "bloomstone", "gavelstone", "anvilstone", "chimerastone",
                  "validator_guard", "yield_guard")
_STONE_ITEMS = ("hashstone", "lockstone", "vaultstone", "liqstone",
                "tidestone", "heartstone", "cryptstone", "bloodstone",
                "bloomstone", "gavelstone", "anvilstone", "chimerastone")
_CONSUMABLE_ITEMS = ("validator_guard", "yield_guard")
# Cosmetics are CRAFT-ONLY -- they don't appear in ,shop, ,shop buy
# refuses them, and the items embed hides them. The only path in is the
# matching crafting recipe (see crafting_config.shimmer_dust /
# moon_essence / aurora_prism). ,inventory use still works on a crafted
# cosmetic; the role grant is now time-limited (default 1 hour).
_COSMETIC_ITEMS = ("glamour_kit", "night_crystal", "aurora_pass")

# Single source of truth for all stone configs. Used by the generic
# buy/sell/transfer/levelup dispatch so adding a new stone in
# items_config.py only needs DB getters/setters of the same name pattern.
_STONE_CFGS: dict[str, dict] = {
    "hashstone":    _SS,
    "lockstone":    _LS,
    "vaultstone":   _VS,
    "liqstone":     _LQ,
    "tidestone":    _TI,
    "heartstone":   _HE,
    "cryptstone":   _CR,
    "bloodstone":   _BL,
    "bloomstone":   _BM,
    "gavelstone":   _GV,
    "anvilstone":   _AV,
    "chimerastone": _CH,
}

# Aliases that resolve to canonical stone keys.  Buy/sell/transfer/levelup
# all run input through ``_resolve_stone_key`` so users can type either the
# full name or a short alias.
_STONE_ALIASES: dict[str, str] = {
    "hash": "hashstone",       "hashstone":  "hashstone",
    "lock": "lockstone",       "lockstone":  "lockstone",
    "vault": "vaultstone",     "vaultstone": "vaultstone",
    "liq": "liqstone",         "liqstone":   "liqstone",
    "lp": "liqstone",
    "tide": "tidestone",       "tidestone":  "tidestone",
    "heart": "heartstone",     "heartstone": "heartstone",
    "crypt": "cryptstone",     "cryptstone": "cryptstone",
    "blood": "bloodstone",     "bloodstone": "bloodstone",
    "bloom": "bloomstone",     "bloomstone": "bloomstone",
    "farm": "bloomstone",
    # Meta-economy stones.
    "gavel": "gavelstone",     "gavelstone":   "gavelstone",
    "ah":    "gavelstone",     "auction":      "gavelstone",
    "anvil": "anvilstone",     "anvilstone":   "anvilstone",
    "craft": "anvilstone",     "forge":        "anvilstone",
    "chimera": "chimerastone", "chimerastone": "chimerastone",
    "swap":  "chimerastone",
}


def _resolve_stone_key(name: str) -> str | None:
    """Map a user-typed stone name (or alias) to its canonical key.

    Returns ``None`` if the name does not resolve to any known stone.
    """
    if not name:
        return None
    key = _STONE_ALIASES.get(name.lower().strip())
    if key and _STONE_CFGS.get(key):
        return key
    return None


# Stat-key labels shared by the inventory + shop embeds.  Keeping them in
# one map means a new stat appears everywhere consistently.
_STONE_STAT_LABELS: dict[str, tuple[str, str]] = {
    "work_daily_bonus":      ("\U0001F4BC", "Work/Daily"),
    "mining_bonus":          ("\U000026CF", "Mining"),
    "stake_bonus":           ("\U0001F4C8", "Staking"),
    "interest_bonus":        ("\U0001F3E6", "Interest"),
    "swap_fee_discount":     ("\U0001F501", "Swap fee reduc"),
    "lp_reward_bonus":       ("\U0001F30A", "LP rewards"),
    "fish_payout_bonus":     ("\U0001F3A3", "Fish payout"),
    "fish_combo_bonus":      ("\U0001FA9D", "Fish combo"),
    "buddy_xp_bonus":        ("\U0001F43E", "Buddy XP"),
    "buddy_decay_resist":    ("\U0001F49E", "Mood decay resist"),
    "dungeon_mine_bonus":    ("\U000026CF", "Dungeon ore"),
    "dungeon_atk_bonus":     ("\U00002694", "Dungeon ATK"),
    "dungeon_capture_bonus": ("\U0001F9F2", "Capture chance"),
    "battle_atk_bonus":      ("\U00002694", "Battle ATK"),
    "battle_hp_bonus":       ("\U00002764", "Battle HP"),
    "battle_prize_bonus":    ("\U0001F4B0", "Battle prize"),
    "farm_yield_bonus":      ("\U0001F33E", "Crop yield"),
    "farm_seed_drop_bonus":  ("\U0001F331", "SEED drop"),
    # Meta-economy stones.
    "ah_buyer_rebate":       ("\U0001FA99", "AH buy rebate"),
    "ah_seller_bonus":       ("\U0001FA99", "AH sale bonus"),
    "craft_yield_bonus":     ("\U0001F528", "Craft yield"),
    "craft_xp_bonus":        ("\U0001F4DA", "Craft XP"),
    "swap_fee_bonus":        ("\U0001F501", "Swap fee bonus"),
}

_STONE_XP_SOURCES: dict[str, str] = {
    "hashstone":    "XP from: mining blocks",
    "lockstone":    "XP from: staking & validator blocks",
    "vaultstone":   "XP from: savings deposits & interest",
    "liqstone":     "XP from: providing LP (value x hold time)",
    "tidestone":    "XP from: ,fish casts (+ legendary, + combo)",
    "heartstone":   "XP from: buddy chats / feeds / level-ups",
    "cryptstone":   "XP from: dungeon kills / captures / mines / bosses",
    "bloodstone":   "XP from: buddy battle rounds + wins (+ captures)",
    "bloomstone":   "XP from: ,farm plant / harvest / process / pest kills",
    "gavelstone":   "XP from: ,ah buys + settled sales (NOT listings)",
    "anvilstone":   "XP from: ,craft actions (one per action, any qty)",
    "chimerastone": "XP from: ,swap actions (not bare ,buy / ,sell)",
}


def _stone_bonus_parts(cfg: dict, level: int) -> list[str]:
    """Return the formatted bonus list for a stone at ``level``.

    Filters out stats not configured for this stone and skips zero-value
    stats so the display only shows what actually applies.
    """
    out: list[str] = []
    for stat_key, (emoji, label) in _STONE_STAT_LABELS.items():
        val = (cfg.get("stats") or {}).get(stat_key, 0.0)
        if not val:
            continue
        out.append(f"{emoji} {label}: **+{val * level * 100:.1f}%**")
    return out


def _stone_db_ops(db, key: str):
    """Return the six DB methods for a stone key.

    ``(get, create, delete, transfer, update_xp, add_staked)`` -- all stones
    follow the same naming convention so we resolve them by ``getattr``
    instead of maintaining an explicit per-stone map.
    """
    return (
        getattr(db, f"get_{key}"),
        getattr(db, f"create_{key}"),
        getattr(db, f"delete_{key}"),
        getattr(db, f"transfer_{key}"),
        getattr(db, f"update_{key}_xp"),
        getattr(db, f"add_{key}_staked"),
    )


def _stone_table_name(key: str) -> str:
    """Stone-key (singular) -> table name (plural). Mirrors the convention
    used in database/users.py: hashstone -> hashstones, etc."""
    return f"{key}s"


async def _stone_price_map(db, guild_id: int) -> dict[str, float]:
    """Pre-fetch oracle prices for every non-stable currency referenced in
    ``_STONE_CFGS.accepted_currencies``.

    Returns ``{symbol: usd_price}`` so a render path can compute the USD
    value of a staked balance without scattering per-stone async price
    lookups through the embed builder. Stable / USD currencies aren't
    listed -- the caller treats them as $1:$1 implicitly.
    """
    needed: set[str] = set()
    for cfg in _STONE_CFGS.values():
        if not cfg:
            continue
        for sym in cfg.get("accepted_currencies") or ():
            sym = sym.upper()
            if sym == "USD" or sym in _STABLE_NETWORK:
                continue
            needed.add(sym)
    out: dict[str, float] = {}
    for sym in needed:
        try:
            row = await db.get_price(sym, guild_id)
            out[sym] = float(row["price"]) if row else 0.0
        except Exception:
            out[sym] = 0.0
    return out


def _stone_staked_usd(staked_human: float, currency: str, price_map: dict[str, float]) -> float:
    """USD value of a stone's staked balance.

    DSD/USDC/USD => 1:1; everything else => oracle from ``price_map``.
    Returns 0.0 when the oracle is missing so the caller can decide
    whether to render the USD parenthetical.
    """
    cur = (currency or "").upper()
    if cur in _STABLE_NETWORK or cur == "USD":
        return float(staked_human)
    px = price_map.get(cur, 0.0)
    return float(staked_human) * px if px > 0 else 0.0

# ── Stablecoin helpers ────────────────────────────────────────────────────────
# Canonical ``{symbol: short_network_code}`` map and display helpers live in
# :mod:`core.framework.network`; only tokens with ``stablecoin=True`` in
# ``Config.TOKENS`` appear in ``_STABLE_NETWORK``.
_DEFAULT_STABLE = "DSD"   # default currency for sell refunds and display


import logging
import math

log = logging.getLogger(__name__)


async def _item_lp_add(db, guild_id: int, user_id: int, currency: str, amount: float) -> float:
    """Add ``amount`` of stablecoin to the STABLE/USD LP pool on behalf of a stone holder.

    Splits 50/50 into both sides of the pool (both are $1 so the ratio is 1:1).
    Returns the LP shares minted, or 0.0 if the pool doesn't exist.
    """
    pool_id, ca, cb = db.make_pool_id(currency, "USD")
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        return 0.0

    half = amount / 2.0
    reserve_a = float(pool["reserve_a"])
    reserve_b = float(pool["reserve_b"])
    total_lp = float(pool["total_lp"])

    if total_lp <= 0:
        lp_minted = math.sqrt(half * half)
    else:
        share_a = half / reserve_a if reserve_a > 0 else 0
        share_b = half / reserve_b if reserve_b > 0 else 0
        lp_minted = min(share_a, share_b) * total_lp

    if lp_minted <= 0:
        return 0.0

    # Update pool reserves
    await db.execute(
        "UPDATE pools SET reserve_a = reserve_a + $1, reserve_b = reserve_b + $2, "
        "total_lp = total_lp + $3 WHERE pool_id = $4 AND guild_id = $5",
        half, half, lp_minted, pool_id, guild_id,
    )

    # Update user LP position and mark shares as item-locked (cannot be manually removed)
    await db.update_lp_position(user_id, guild_id, pool_id, lp_minted)
    await db.execute(
        "UPDATE lp_positions SET locked_lp_shares = locked_lp_shares + $1 "
        "WHERE user_id=$2 AND guild_id=$3 AND pool_id=$4",
        lp_minted, user_id, guild_id, pool_id,
    )

    # Update snapshot for fee tracking
    new_total_lp = total_lp + lp_minted
    new_ra = reserve_a + half
    new_rb = reserve_b + half
    await db.upsert_lp_snapshot(
        user_id, guild_id, pool_id,
        new_ra / new_total_lp if new_total_lp > 0 else 0,
        new_rb / new_total_lp if new_total_lp > 0 else 0,
    )

    log.info("[item_lp] user=%d guild=%d added %.2f %s -> %.4f LP in %s",
             user_id, guild_id, amount, currency, lp_minted, pool_id)
    return lp_minted


async def _drip_lp_all_pools(db, guild_id: int, total_amount_raw: int) -> int:
    """Distribute a small LP drip across *every* pool in the guild.

    Called on shop buy and stone level-up.  No user owns this LP -- it's
    anonymous treasury liquidity that deepens reserves on every pool so the
    whole game's market depth grows whenever items move.  Returns the per-pool
    drip amount actually applied (raw int), or 0 if pools or amount were too
    small to bother.
    """
    if total_amount_raw <= 0:
        return 0
    try:
        pools = await db.get_all_pools(guild_id)
    except Exception:
        log.debug("drip_lp: list pools failed gid=%s", guild_id, exc_info=True)
        return 0
    if not pools:
        return 0
    per_pool = total_amount_raw // max(1, len(pools))
    if per_pool <= 0:
        return 0
    half = per_pool // 2
    if half <= 0:
        return 0
    for p in pools:
        try:
            await db.execute(
                "UPDATE pools SET reserve_a = reserve_a + $1, "
                "reserve_b = reserve_b + $2 "
                "WHERE pool_id = $3 AND guild_id = $4",
                half, half, p["pool_id"], guild_id,
            )
        except Exception:
            log.debug(
                "drip_lp: update failed gid=%s pool=%s",
                guild_id, p.get("pool_id"), exc_info=True,
            )
    log.info(
        "[drip_lp] guild=%d -> %d pools x %.4f raw each",
        guild_id, len(pools), to_human(per_pool),
    )
    return per_pool


async def _item_lp_remove(db, guild_id: int, user_id: int, currency: str) -> float:
    """Remove ALL LP shares the user has in the STABLE/USD pool.

    Returns the total USD value withdrawn (both sides combined).
    Called when a stone is sold.
    """
    pool_id, ca, cb = db.make_pool_id(currency, "USD")
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        return 0.0

    lp_pos = await db.get_user_lp(user_id, guild_id, pool_id)
    if not lp_pos or int(lp_pos["lp_shares"] or 0) <= 0:
        return 0.0

    # Stay in raw-int space: lp_shares / total_lp / reserves are NUMERIC(36,0)
    # raw-scaled and exceed float64 precision once values grow past ~10^15.
    shares_raw = int(lp_pos["lp_shares"])
    total_lp_raw = int(pool["total_lp"])
    if total_lp_raw <= 0:
        return 0.0

    withdraw_a_raw = int(pool["reserve_a"]) * shares_raw // total_lp_raw
    withdraw_b_raw = int(pool["reserve_b"]) * shares_raw // total_lp_raw
    total_withdrawn = to_human(withdraw_a_raw) + to_human(withdraw_b_raw)

    # Update pool reserves
    await db.execute(
        "UPDATE pools SET reserve_a = reserve_a - $1, reserve_b = reserve_b - $2, "
        "total_lp = total_lp - $3 WHERE pool_id = $4 AND guild_id = $5",
        withdraw_a_raw, withdraw_b_raw, shares_raw, pool_id, guild_id,
    )

    # Remove LP position and release the item lock
    await db.update_lp_position(user_id, guild_id, pool_id, -shares_raw)
    await db.execute(
        "UPDATE lp_positions SET locked_lp_shares = GREATEST(0, locked_lp_shares - $1) "
        "WHERE user_id=$2 AND guild_id=$3 AND pool_id=$4",
        shares_raw, user_id, guild_id, pool_id,
    )
    await db.delete_lp_snapshot(user_id, guild_id, pool_id)

    log.info("[item_lp] user=%d guild=%d removed %.4f LP from %s -> %.2f USD",
             user_id, guild_id, to_human(shares_raw), pool_id, total_withdrawn)
    return total_withdrawn


def _item_stat(hashstone: dict | None, key: str) -> float:
    """Return the effective stat value for a player's Hashstone.

    For leveled items the bonus scales with level: ``stat_value × level``.
    For flat items the stat is fixed regardless of level: ``stat_value``.
    Returns 0.0 if the player has no Hashstone.
    """
    if hashstone is None:
        return 0.0
    base = _SS["stats"].get(key, 0.0)
    if _SS.get("leveled", False):
        return base * hashstone["level"]
    return base


def _lockstone_stat(lockstone: dict | None, key: str) -> float:
    """Return the effective stat value for a player's Lockstone."""
    if lockstone is None or not _LS:
        return 0.0
    base = _LS.get("stats", {}).get(key, 0.0)
    if _LS.get("leveled", False):
        return base * lockstone["level"]
    return base


def _vaultstone_stat(vaultstone: dict | None, key: str) -> float:
    """Return the effective stat value for a player's Vaultstone."""
    if vaultstone is None or not _VS:
        return 0.0
    base = _VS.get("stats", {}).get(key, 0.0)
    if _VS.get("leveled", False):
        return base * vaultstone["level"]
    return base


def _liqstone_stat(liqstone: dict | None, key: str) -> float:
    """Return the effective stat value for a player's Liqstone."""
    if liqstone is None or not _LQ:
        return 0.0
    base = _LQ.get("stats", {}).get(key, 0.0)
    if _LQ.get("leveled", False):
        return base * liqstone["level"]
    return base


def _stone_level_from_xp(xp: float, item_cfg: dict) -> int:
    """Generic level calculator for any stone item with xp_per_level_base / max_level."""
    base = item_cfg.get("xp_per_level_base", 100)
    max_level = item_cfg.get("max_level", 100)
    if base <= 0 or xp <= 0:
        return 1
    level = int((1 + math.sqrt(1 + 8 * xp / base)) / 2)
    return max(1, min(level, max_level))


# ── Level math ────────────────────────────────────────────────────────────────

def _xp_for_level(level: int) -> float:
    """Total XP required to reach `level` from scratch."""
    base = _SS["xp_per_level_base"]
    return float(base * level * (level - 1) // 2)


def cap_xp(new_xp: float, cur_level: int, item_cfg: dict) -> float:
    """Clamp XP so it cannot exceed the next level-up threshold.

    Items must not gain more XP once they've hit the threshold  - 
    the player has to pay DSD and level up first.
    """
    base = item_cfg.get("xp_per_level_base", 100)
    max_level = item_cfg.get("max_level", 100)
    if cur_level >= max_level:
        return new_xp  # already maxed, no cap needed
    threshold = float(base * (cur_level + 1) * cur_level // 2)
    return min(new_xp, threshold)


def _xp_to_next(current_level: int) -> float:
    """XP needed to go from current_level → current_level+1."""
    return float(_SS["xp_per_level_base"] * current_level)


def _level_from_xp(xp: float) -> int:
    """Compute level from cumulative XP. Inverse of _xp_for_level."""
    base = _SS["xp_per_level_base"]
    if base <= 0 or xp <= 0:
        return 1
    level = int((1 + math.sqrt(1 + 8 * xp / base)) / 2)
    return max(1, min(level, _SS["max_level"]))


def _xp_progress_bar(xp: float, level: int, width: int = 12) -> str:
    """ASCII progress bar showing XP within current level."""
    if level >= _SS["max_level"]:
        return "▓" * width + " MAX"
    current_level_start = _xp_for_level(level)
    next_level_start    = _xp_for_level(level + 1)
    span = next_level_start - current_level_start
    filled = int(width * (xp - current_level_start) / span) if span > 0 else 0
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def _levelup_cost(cfg: dict, cur_level: int, staked_stable: int = 0) -> int:
    """Cost in DSD (raw int) to level up from cur_level to cur_level+1.

    Cost = 10% of the stone's current total staked DSD (raw int).
    """
    return int(staked_stable * 0.10) if staked_stable > 0 else int(cfg["cost_stable"] * 0.10)


async def _send_item_dm(bot, user_id: int, guild, embed) -> None:
    """Send an embed DM to a guild member. Silently swallows errors."""
    try:
        member = guild.get_member(user_id) or bot.get_user(user_id)
        if not member:
            member = await guild.fetch_member(user_id)
        if member and not member.bot:
            from core.framework.links import sanitize_embed
            sanitize_embed(embed)
            await member.send(embed=embed)
    except Exception:
        pass


async def _perform_auto_levelup(
    bot, user_id: int, guild, item_name: str, *, send_dm: bool = True,
) -> bool:
    """Spend the next-level cost out of DSD/USDC (DeFi first, CeFi top-up)
    and level the stone up if the player can afford it.

    Returns True if the stone was leveled, False otherwise. Used by both
    ``notify_item_levelup_ready`` (in-tick fast path) and the
    ``_auto_levelup_poller`` background task (catch-up path).
    """
    cfg = _STONE_CFGS.get(item_name)
    if not cfg:
        return False
    max_level = cfg.get("max_level", 100)
    try:
        getter, _create, _delete, _xfer, updater, staked_adder = _stone_db_ops(bot.db, item_name)
        stone = await getter(user_id, guild.id)
    except Exception:
        return False
    if not stone:
        return False
    cur_level = int(stone.get("level") or 0)
    if cur_level >= max_level:
        return False
    base = cfg.get("xp_per_level_base", 100)
    threshold = float(base * (cur_level + 1) * cur_level // 2)
    cur_xp = float(stone.get("xp") or 0)
    if cur_xp < threshold:
        return False  # not eligible

    staked_stable = float(stone.get("staked_amount") or 0)
    # ``cost_in_stake`` is the level-up charge in the stone's stake
    # currency (10% of staked, same units as ``staked_amount``). For
    # cross-currency selection below we'll convert through USD.
    cost_in_stake = _levelup_cost(cfg, cur_level, staked_stable)
    lp_cur = (stone.get("lp_currency") or "DSD").upper()

    # Build the candidate currency list: try the stone's stored
    # lp_currency FIRST so the auto-levelup keeps using whatever the
    # player paid in originally, then fall through every other entry
    # in accepted_currencies if the primary balance is short. Without
    # this fallback, a stone bought in MTA silently fails to auto-
    # levelup the moment the player runs their MTA down even if they
    # have plenty of SUN to cover the same USD-equivalent cost.
    accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
    candidates: list[str] = []
    if lp_cur in accepted:
        candidates.append(lp_cur)
    for c in accepted:
        if c not in candidates:
            candidates.append(c)
    # Legacy DSD/USDC tail in case the stone was bought before the
    # accepted_currencies tightening shipped and the per-stone
    # migration hasn't reached this row yet.
    for legacy in ("DSD", "USDC"):
        if legacy not in candidates:
            candidates.append(legacy)

    from core.framework.network import normalize_short

    # ``cost_in_stake`` is in the stone's stake currency (MTA for a
    # MTA hashstone, DSD for a DSD vaultstone, etc.). To pay in any
    # other accepted currency we convert through USD via two oracle
    # lookups: stake_oracle (USD per stake_token) -> usd amount ->
    # target_oracle (USD per target_token) -> target amount.
    if lp_cur == "USD" or lp_cur in _STABLE_NETWORK:
        stake_oracle = 1.0
    else:
        _so_row = await bot.db.get_price(lp_cur, guild.id)
        stake_oracle = float(_so_row["price"]) if _so_row else 0.0

    async def _resolve(currency: str) -> tuple[int, str | None] | None:
        """Return (cost_in_currency_raw, network) or None on price miss."""
        # Same-currency level-up: no oracle needed.
        if currency == lp_cur:
            if currency == "USD":
                return int(cost_in_stake), None
            if currency in _STABLE_NETWORK:
                return int(cost_in_stake), _STABLE_NETWORK[currency]
            tok_meta = Config.TOKENS.get(currency, {})
            network = normalize_short(tok_meta.get("network") or "")
            if not network:
                return None
            return int(cost_in_stake), network
        # Cross-currency: route through USD-equivalent.
        if stake_oracle <= 0:
            return None
        usd_human = to_human(int(cost_in_stake)) * stake_oracle
        if currency == "USD":
            return to_raw(usd_human), None
        if currency in _STABLE_NETWORK:
            return to_raw(usd_human), _STABLE_NETWORK[currency]
        tok_meta = Config.TOKENS.get(currency, {})
        network = normalize_short(tok_meta.get("network") or "")
        if not network:
            return None
        oracle_row = await bot.db.get_price(currency, guild.id)
        oracle = float(oracle_row["price"]) if oracle_row else 0.0
        if oracle <= 0:
            return None
        return to_raw(usd_human / oracle), network

    async def _balance(currency: str, network: str | None) -> tuple[int, int]:
        if currency == "USD":
            user_row = await bot.db.get_user(user_id, guild.id)
            return int((user_row or {}).get("wallet") or 0), 0
        wh = await bot.db.get_wallet_holding(user_id, guild.id, network, currency)
        defi_bal = int(wh["amount"]) if wh else 0
        cefi_row = await bot.db.get_holding(user_id, guild.id, currency)
        cefi_bal = int(cefi_row["amount"]) if cefi_row else 0
        return defi_bal, cefi_bal

    chosen: tuple[str, int, str | None, int, int] | None = None
    try:
        for cand in candidates:
            resolved = await _resolve(cand)
            if resolved is None:
                continue
            cost, network = resolved
            try:
                defi_bal, cefi_bal = await _balance(cand, network)
            except Exception:
                continue
            if (defi_bal + cefi_bal) >= cost:
                chosen = (cand, cost, network, defi_bal, cefi_bal)
                break
    except Exception:
        return False

    if chosen is None:
        return False  # no accepted currency has sufficient balance

    currency, cost, network, defi_bal, cefi_bal = chosen
    new_level = cur_level + 1
    take_defi = min(defi_bal, cost)
    take_cefi = cost - take_defi
    try:
        if currency == "USD":
            await bot.db.update_wallet(user_id, guild.id, -cost)
        else:
            if take_defi > 0:
                await bot.db.update_wallet_holding(
                    user_id, guild.id, network, currency, -take_defi,
                )
            if take_cefi > 0:
                await bot.db.update_holding(
                    user_id, guild.id, currency, -take_cefi,
                )
        await bot.db.add_to_treasury(guild.id, cost)
        if network is not None:
            await deposit_to_vault(bot.db, guild.id, network, cost, bot=bot)
        await updater(user_id, guild.id, stone["xp"], new_level)
        await staked_adder(user_id, guild.id, cost)
        if currency in _STABLE_NETWORK:
            await _item_lp_add(bot.db, guild.id, user_id, currency, cost)
            await _drip_lp_all_pools(bot.db, guild.id, int(cost * 0.05))
        # Update the stone's lp_currency to the currency we actually
        # debited, so subsequent ,inv / ,bal / shop displays reflect
        # the latest spend and the next auto-levelup tries this
        # currency first.
        if currency != lp_cur:
            try:
                await bot.db.execute(
                    f"UPDATE {_stone_table_name(item_name)} "
                    "SET lp_currency=$1 WHERE user_id=$2 AND guild_id=$3",
                    currency, user_id, guild.id,
                )
            except Exception:
                log.debug(
                    "auto-levelup lp_currency update failed item=%s",
                    item_name, exc_info=True,
                )
        await bot.db.log_tx(
            guild.id, user_id, "STONE_LEVELUP",
            symbol_in=currency, amount_in=cost,
            network=(network or "usd"),
        )
    except Exception as exc:
        log.warning(
            "Auto levelup spend failed user=%d guild=%d item=%s: %s",
            user_id, guild.id, item_name, exc,
        )
        return False

    try:
        await bot.bus.publish(
            "stone_leveled",
            guild=guild, user_id=int(user_id),
            item_type=item_name, new_level=int(new_level),
            auto=True,
        )
    except Exception:
        log.debug("stone_leveled publish failed gid=%s uid=%s",
                  guild.id, user_id, exc_info=True)

    if send_dm:
        try:
            prefs = await bot.db.get_user_prefs(user_id, guild.id)
            if prefs.get("dm_autolevelup", 0):
                emoji = cfg.get("emoji", "")
                dm_embed = (
                    card(
                        f"{emoji} Item Auto-Leveled Up!",
                        description=(
                            f"Your **{item_name.title()}** automatically leveled up to "
                            f"**Level {new_level}** / {max_level}!"
                        ),
                        color=C_SUCCESS,
                    )
                    .footer(f"{guild.name} - toggle: ,notify autolevelup off")
                    .build()
                )
                await _send_item_dm(bot, user_id, guild, dm_embed)
        except Exception:
            log.debug("auto-levelup DM failed uid=%s", user_id, exc_info=True)
    return True


async def notify_item_levelup_ready(bot, user_id: int, guild, item_name: str, old_xp: float, new_xp: float, cur_level: int, staked_stable: float = 0.0) -> None:
    """Called after any XP update. If auto_levelup is on and funds are available,
    levels the item automatically and DMs the result. Otherwise DMs a ready notice.
    Silently ignored if the item is at max level or this tick didn't cross the threshold.
    """
    cfg = _STONE_CFGS.get(item_name)
    if not cfg:
        return
    max_level = cfg.get("max_level", 100)
    if cur_level >= max_level:
        return
    base = cfg.get("xp_per_level_base", 100)
    threshold = float(base * (cur_level + 1) * cur_level // 2)
    # Only fire when the threshold is crossed in this exact update -- the
    # ``_auto_levelup_poller`` background task picks up any eligible
    # stones the in-tick path missed (e.g. funds arrived later, or auto-
    # levelup was toggled on after the threshold was already crossed).
    if old_xp >= threshold or new_xp < threshold:
        return

    emoji = cfg.get("emoji", "")
    new_level = cur_level + 1
    cost = _levelup_cost(cfg, cur_level, staked_stable)

    # Check if auto-levelup is enabled for this user
    try:
        settings_row = await bot.db.fetch_one(
            "SELECT auto_levelup FROM user_settings WHERE user_id=$1 AND guild_id=$2",
            user_id, guild.id,
        )
        auto_up = bool(settings_row.get("auto_levelup")) if settings_row else False
    except Exception:
        auto_up = False

    if auto_up:
        leveled = await _perform_auto_levelup(bot, user_id, guild, item_name, send_dm=True)
        if leveled:
            return
        # Insufficient funds -- fall through to "ready" DM

    # "Ready to level up" DM
    try:
        prefs = await bot.db.get_user_prefs(user_id, guild.id)
        if not prefs.get("dm_itemlevelup", 0):
            return
    except Exception:
        return
    # Fetch stone currency for the DM display. Convert the USD-equivalent
    # ``cost`` to the actual currency the stone is staked in -- otherwise
    # the DM tells a Tidestone holder to pay 6500 REEL when REEL trades
    # at $0.40 (the actual cost would be ~16,250 REEL, not 6500).
    _dm_cur = "DSD"
    _dm_cost_raw = int(cost)
    try:
        getter, *_ = _stone_db_ops(bot.db, item_name)
        _stone_row = await getter(user_id, guild.id)
        _dm_cur = (_stone_row.get("lp_currency") or "DSD") if _stone_row else "DSD"
        _dm_cur = _dm_cur.upper()
        if _dm_cur not in _STABLE_NETWORK and _dm_cur != "USD":
            try:
                oracle_row = await bot.db.get_price(_dm_cur, guild.id)
                oracle = float(oracle_row["price"]) if oracle_row else 0.0
                if oracle > 0:
                    _dm_cost_raw = to_raw(to_human(int(cost)) / oracle)
            except Exception:
                pass
    except Exception:
        _dm_cur = "DSD"
    _dm_emoji = _stable_emoji(_dm_cur) if _dm_cur in _STABLE_NETWORK else (
        Config.TOKENS.get(_dm_cur, {}).get("emoji", "")
    )
    dm_embed = (
        card(
            f"{emoji} Item Ready to Level Up!",
            description=(
                f"Your **{item_name.title()}** has enough XP to reach **Level {new_level}**!\n\n"
                f"Pay **{fmt_token(to_human(_dm_cost_raw), _dm_cur, _dm_emoji)}** "
                f"(~{fmt_usd(to_human(int(cost)))}) to level up:\n"
                f"`,inventory levelup {item_name}`"
            ),
            color=C_GOLD,
        )
        .footer(f"{guild.name} - toggle: ,notify itemlevelup off")
        .build()
    )
    await _send_item_dm(bot, user_id, guild, dm_embed)


def _stone_listing_field(cfg: dict, key: str, owned: dict | None) -> str:
    """One field body for the shop listing -- consistent across all stones.

    Compact form: cost + accepted, bonuses-per-level, XP source, plus an
    "Owned" line when the user holds one. The verbose flavor description
    moved off this surface to keep the items embed safely under Discord's
    6000-char-per-embed cap as the stone roster grew past 9. Full
    description still surfaces in the ``,shop buy <stone>`` receipt and
    in ``,inventory <stone>``.
    """
    cost_human = to_human(cfg["cost_stable"])
    max_level = cfg.get("max_level", 100)
    bonus_lvl1 = _stone_bonus_parts(cfg, level=1)
    bonus_per_lv = ""
    if bonus_lvl1:
        bonus_per_lv = " · ".join(b.replace("**+", "**+").replace("%", "%/lv") for b in bonus_lvl1)
    xp_src = _STONE_XP_SOURCES.get(key, "")
    accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
    accepted_inline = ", ".join(f"`{c}`" for c in accepted)
    body = (
        f"**Cost:** **{fmt_usd(cost_human)}** · pay in {accepted_inline} · "
        f"Max Lv {max_level}"
    )
    if bonus_per_lv:
        body += f"\n{bonus_per_lv}"
    if xp_src:
        body += f"\n*{xp_src}*"
    if owned:
        owned_bonus = _stone_bonus_parts(cfg, level=owned["level"])
        owned_cur = (owned.get("lp_currency") or accepted[0]).upper()
        # Owned-state row uses the same stat-row style as the dashboard so
        # players can see at a glance how close their stone is to maxing.
        # Bonuses get appended to the same line so the total field length
        # doesn't grow vs. the pre-restyle layout (the items page is tight
        # against the 6000-char embed limit with all 9 stones listed).
        owned_line = FormatKit.stat_row(
            int(owned["level"]), int(max_level),
            f"*Owned  -  {owned_cur}*",
            count_width=2,
        )
        if owned_bonus:
            owned_line += "  ·  " + " · ".join(owned_bonus)
        body += "\n" + owned_line
    return body


def _item_list_embeds(
    hashstone: dict | None,
    lockstone: dict | None = None,
    vaultstone: dict | None = None,
    liqstone: dict | None = None,
    validator_guard_count: int = 0,
    yield_guard_count: int = 0,
    *,
    tidestone: dict | None = None,
    heartstone: dict | None = None,
    cryptstone: dict | None = None,
    bloodstone: dict | None = None,
    bloomstone: dict | None = None,
    gavelstone: dict | None = None,
    anvilstone: dict | None = None,
    chimerastone: dict | None = None,
) -> list[discord.Embed]:
    """Build the /shop item listing embeds  -  items page + consumables page.

    Iterates the canonical ``_STONE_CFGS`` so adding a stone is a config-only
    change and the shop, inventory, and DM embeds stay in lockstep.
    """
    embeds: list[discord.Embed] = []

    owned_map: dict[str, dict | None] = {
        "hashstone":    hashstone,
        "lockstone":    lockstone,
        "vaultstone":   vaultstone,
        "liqstone":     liqstone,
        "tidestone":    tidestone,
        "heartstone":   heartstone,
        "cryptstone":   cryptstone,
        "bloodstone":   bloodstone,
        "bloomstone":   bloomstone,
        "gavelstone":   gavelstone,
        "anvilstone":   anvilstone,
        "chimerastone": chimerastone,
    }

    # ══════════════════════════════════════════════════════════════════════════
    # PAGE 1  -  Leveled Items (gear that gains XP)
    # ══════════════════════════════════════════════════════════════════════════
    _b = card("\U0001F6D2 Item Shop  -  Stones", color=C_AMBER).description(
        "*Leveled gear that scales with activity. "
        "`,shop buy <stone>` for the full description.*"
    )

    for skey, cfg in _STONE_CFGS.items():
        if not cfg or cfg.get("disabled"):
            continue
        cost_human = to_human(cfg["cost_stable"])
        accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
        accepted_str = " · ".join(f"`{c}`" for c in accepted)
        _b.field(
            f"{cfg.get('emoji', '')} {cfg.get('name', skey.title())}  -  "
            f"{fmt_usd(cost_human)}  |  pays in {accepted_str}",
            _stone_listing_field(cfg, skey, owned_map.get(skey)),
            False,
        )

    _b.footer(
        "Quick Buy below  ·  ,shop buy <item>  ·  ,shop sell <item>  ·  "
        ",shop transfer <item> @user"
    )
    embeds.append(_b.build())

    # ══════════════════════════════════════════════════════════════════════════
    # PAGE 2  -  Consumables (single-use stackable items)
    # ══════════════════════════════════════════════════════════════════════════
    _c = card("\U0001F6D2 Item Shop  -  Consumables", color=C_WARNING).description(
        "*Single-use items consumed automatically or manually. Stackable, no "
        "sell or transfer.*"
    )

    # ── Validator Guard ───────────────────────────────────────────────────────
    if _VG:
        vg_cost = to_human(_VG["cost_stable"])
        _c.field(
            f"{_VG['emoji']} Validator Guard  -  {fmt_usd(vg_cost)}",
            (
                f"{_VG['description']}\n"
                f"**Cost:** {fmt_token(vg_cost, 'DSD', '💵')} ({fmt_usd(vg_cost)}) · "
                f"**Stack:** up to {_VG.get('max_stack', 50)} · "
                f"**Fee:** {_VG['buy_fee_pct']*100:.0f}%"
                + (f"\n*You have {validator_guard_count} in inventory.*" if validator_guard_count else "")
            ),
            False,
        )

    # ── Yield Guard ───────────────────────────────────────────────────────────
    if _YG:
        yg_cost = to_human(_YG["cost_stable"])
        _c.field(
            f"{_YG['emoji']} Yield Guard  -  {fmt_usd(yg_cost)}",
            (
                f"{_YG['description']}\n"
                f"**Cost:** {fmt_token(yg_cost, 'DSD', '💵')} ({fmt_usd(yg_cost)}) · "
                f"**Stack:** up to {_YG.get('max_stack', 50)} · "
                f"**Fee:** {_YG['buy_fee_pct']*100:.0f}%"
                + (f"\n*You have {yield_guard_count} in inventory.*" if yield_guard_count else "")
            ),
            False,
        )

    # Cosmetic consumables used to render here. They are now CRAFT-ONLY
    # (see crafting_config.shimmer_dust / moon_essence / aurora_prism)
    # and intentionally hidden from the shop -- the only path is through
    # the matching recipe; ,inventory use then grants the linked role
    # for the duration declared on the cosmetic (default 1 hour).

    _c.footer("Use /shop buy <item> [qty] to purchase consumables  ·  cosmetics: ,craft list")
    embeds.append(_c.build())

    return embeds


# Legacy compat wrapper  -  returns the first embed for any callers expecting a single Embed
def _item_list_embed(
    hashstone: dict | None,
    lockstone: dict | None = None,
    vaultstone: dict | None = None,
    liqstone: dict | None = None,
    validator_guard_count: int = 0,
    yield_guard_count: int = 0,
    *,
    tidestone: dict | None = None,
    heartstone: dict | None = None,
    cryptstone: dict | None = None,
    bloodstone: dict | None = None,
    bloomstone: dict | None = None,
) -> discord.Embed:
    """Build the /shop item listing embed  -  returns first page for legacy callers."""
    return _item_list_embeds(
        hashstone, lockstone, vaultstone, liqstone,
        validator_guard_count, yield_guard_count,
        tidestone=tidestone, heartstone=heartstone,
        cryptstone=cryptstone, bloodstone=bloodstone,
        bloomstone=bloomstone,
    )[0]


async def _fetch_all_stones(db, user_id: int, guild_id: int) -> dict[str, dict | None]:
    """Load every owned stone for a user.  Single trip per stone via ``getattr``."""
    out: dict[str, dict | None] = {}
    for skey in _STONE_CFGS:
        if not _STONE_CFGS.get(skey):
            out[skey] = None
            continue
        try:
            getter = getattr(db, f"get_{skey}")
            out[skey] = await getter(user_id, guild_id)
        except Exception:
            out[skey] = None
    return out


# ── Inventory dropdown view ───────────────────────────────────────────────────
# ,inventory used to dump items + consumables into one fixed pair of embeds.
# The dropdown view here lets the user flip the SAME message between four
# sections: Items (default), Fishing tackle, Held buddy eggs, and Captured
# buddies. Each section is rendered on demand so a user with zero fishing
# state doesn't pay for that data on every open. The Select stays attached
# across page swaps so the user can hop sections without re-running the
# command.

# Owner-locked: only the user who ran ,inv can drive the dropdown. Other
# clickers get an ephemeral "this isn't your inventory" reply.

_INV_VIEW_TIMEOUT_S: int = 180


class _InventoryView(discord.ui.View):
    """Dropdown view for ,inventory that swaps embeds in-place."""

    def __init__(self, ctx: DiscoContext, owner_id: int) -> None:
        super().__init__(timeout=_INV_VIEW_TIMEOUT_S)
        self.ctx = ctx
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        # Add the section selector. Default selection ("Items") matches
        # the embed the cog opens with so the dropdown shows the right
        # state on first render.
        self._select = discord.ui.Select(
            placeholder="Switch inventory section...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="Items + Consumables",
                    value="items",
                    emoji="\U0001F9F0",
                    default=True,
                    description="Stones + validator/yield guards",
                ),
                discord.SelectOption(
                    label="Fishing Tackle",
                    value="fishing",
                    emoji="\U0001F3A3",
                    description="Caught fish, junk, bait, crab traps",
                ),
                discord.SelectOption(
                    label="Buddy Eggs",
                    value="eggs",
                    emoji="\U0001F95A",
                    description="Held eggs (hatch later, sell, or trade)",
                ),
                discord.SelectOption(
                    label="Captured Buddies",
                    value="buddies",
                    emoji="\U0001F436",
                    description="Your shelter (active + resting)",
                ),
                discord.SelectOption(
                    label="Dungeon Bag",
                    value="dungeon",
                    emoji="\U0001F5FA",
                    description="Class, gear, potions, dungeon party",
                ),
            ],
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your inventory. Run `,inv` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        choice = self._select.values[0] if self._select.values else "items"
        # Reflect the new selection as the dropdown's default option so
        # the chip stays in sync with what the embed shows.
        for opt in self._select.options:
            opt.default = (opt.value == choice)

        # Defer immediately; some sections do a couple of DB hits and
        # we don't want to blow the 3s interaction window.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        try:
            embeds = await _render_inventory_section(self.ctx, choice)
        except Exception:
            log.exception("inventory dropdown: %s render failed", choice)
            embeds = [card(
                "\U0001F6AB Couldn't load section",
                color=C_ERROR,
            ).description(
                "Something broke while loading that section. Try `,inv` again."
            ).build()]

        try:
            if self.message is not None:
                await self.message.edit(embeds=embeds, view=self)
        except discord.HTTPException:
            log.debug("inventory dropdown: edit failed", exc_info=True)

    async def on_timeout(self) -> None:
        # Disable the dropdown so the user knows it's expired without
        # blowing away the displayed embeds.
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _render_inventory_section(
    ctx: DiscoContext, section: str,
) -> list[discord.Embed]:
    """Build the embeds for the chosen inventory section.

    Pulls only the data the chosen section needs -- the section the
    user isn't viewing pays no DB cost.
    """
    if section == "fishing":
        return await _render_fishing_section(ctx)
    if section == "eggs":
        return await _render_eggs_section(ctx)
    if section == "buddies":
        return await _render_buddies_section(ctx)
    if section == "dungeon":
        return await _render_dungeon_section(ctx)
    return await _render_items_section(ctx)


async def _render_items_section(ctx: DiscoContext) -> list[discord.Embed]:
    """Render the legacy items + consumables pair of embeds.

    Mirrors the original ,inventory body. Kept as a function so the
    dropdown view can re-render on demand without re-invoking the
    command itself.
    """
    hashstone   = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
    lockstone  = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
    vaultstone = await ctx.db.get_vaultstone(ctx.author.id, ctx.guild_id)
    liqstone   = await ctx.db.get_liqstone(ctx.author.id, ctx.guild_id)
    # Themed minigame stones.
    tidestone   = await ctx.db.get_tidestone(ctx.author.id, ctx.guild_id)
    heartstone  = await ctx.db.get_heartstone(ctx.author.id, ctx.guild_id)
    cryptstone  = await ctx.db.get_cryptstone(ctx.author.id, ctx.guild_id)
    bloodstone  = await ctx.db.get_bloodstone(ctx.author.id, ctx.guild_id)
    bloomstone  = await ctx.db.get_bloomstone(ctx.author.id, ctx.guild_id)
    vg_count = await ctx.db.get_validator_guard_count(ctx.author.id, ctx.guild_id)
    yg_count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)

    _STAT_LABELS = {
        "work_daily_bonus":   ("\U0001F4BC", "Work/Daily"),
        "mining_bonus":       ("\U000026CF", "Mining"),
        "stake_bonus":        ("\U0001F4C8", "Staking"),
        "interest_bonus":     ("\U0001F3E6", "Interest"),
        "swap_fee_discount":  ("\U0001F501", "Swap fee reduc"),
        "lp_reward_bonus":    ("\U0001F30A", "LP rewards"),
        # Themed minigame stats.
        "fish_payout_bonus":  ("\U0001F3A3", "Fish payout"),
        "fish_combo_bonus":   ("\U0001FA9D", "Fish combo"),
        "buddy_xp_bonus":     ("\U0001F43E", "Buddy XP"),
        "buddy_decay_resist": ("\U0001F49E", "Mood decay resist"),
        "dungeon_mine_bonus": ("\U000026CF", "Dungeon ore"),
        "dungeon_atk_bonus":  ("\U00002694", "Dungeon ATK"),
        "dungeon_capture_bonus": ("\U0001F9F2", "Capture chance"),
        "battle_atk_bonus":   ("\U00002694", "Battle ATK"),
        "battle_hp_bonus":    ("\U00002764", "Battle HP"),
        "battle_prize_bonus": ("\U0001F4B0", "Battle prize"),
    }
    _XP_SOURCES = {
        "hashstone":   "XP from: mining blocks",
        "lockstone":  "XP from: staking & validator blocks",
        "vaultstone": "XP from: savings deposits & interest",
        "liqstone":   "XP from: providing LP (value x hold time)",
        "tidestone":  "XP from: ,fish casts (+ legendary, + combo)",
        "heartstone": "XP from: buddy chats / feeds / level-ups",
        "cryptstone": "XP from: dungeon kills / captures / mines / bosses",
        "bloodstone": "XP from: buddy battle rounds + wins (+ captures)",
    }

    def _stone_field(stone, cfg, name_key):
        desc_full = cfg.get("description", "")
        desc = (desc_full.split(".")[0] + ".") if desc_full else ""
        accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
        if not stone:
            lines = [f"*{desc}*"] if desc else []
            usd_cost = fmt_usd(to_human(cfg['cost_stable']))
            accepted_str = " · ".join(f"`{c}`" for c in accepted)
            lines.append(
                f"Not owned  -  buy with `/shop buy {name_key}` for "
                f"**{usd_cost}** -- pay in {accepted_str}."
            )
            return "\n".join(lines)
        level  = stone["level"]
        xp     = stone["xp"]
        staked = stone["staked_amount"]
        max_lv = cfg["max_level"]
        base   = cfg["xp_per_level_base"]
        fill = int(12 * min((xp - base * level * (level - 1) // 2) / max(1, base * level), 1))
        bar = "█" * fill + "░" * (12 - fill)
        # lp_currency is the stone's actual paid-in currency; fall back
        # to the first accepted currency if the row is legacy/missing
        # (keeps display in lockstep with the buy + levelup paths).
        stone_cur = (stone.get("lp_currency") or "").upper()
        if not stone_cur or (accepted and stone_cur not in accepted):
            stone_cur = accepted[0] if accepted else "DSD"
        stone_emoji = (
            _stable_emoji(stone_cur)
            if stone_cur in _STABLE_NETWORK or stone_cur == "USD"
            else (Config.TOKENS.get(stone_cur, {}).get("emoji", ""))
        )
        lines = []
        if level < max_lv:
            xp_start = base * level * (level - 1) // 2
            xp_next  = base * (level + 1) * level // 2
            xp_str = f"{xp - xp_start:,.1f} / {xp_next - xp_start:,.0f} XP"
            ready = xp >= xp_next
            lines.append(
                f"**Level {level} / {max_lv}** · "
                f"{fmt_token(to_human(staked), stone_cur, stone_emoji)} staked"
            )
            lines.append(f"`{bar}` {xp_str}  <- next level")
            if ready:
                lup_cost = _levelup_cost(cfg, level, staked)
                lines.append(
                    f"⬆️ **Ready to level up!** Pay "
                    f"{fmt_token(to_human(lup_cost), stone_cur, stone_emoji)} "
                    f"-> `/inventory levelup {name_key}`"
                )
        else:
            lines.append(
                f"**Level {level} / {max_lv} ✦ MAX** · "
                f"{fmt_token(to_human(staked), stone_cur, stone_emoji)} staked"
            )
        bonus_parts = []
        for stat_key, (emoji, label) in _STAT_LABELS.items():
            val = cfg["stats"].get(stat_key, 0.0)
            if val == 0.0:
                continue
            effective = val * level
            bonus_parts.append(f"{emoji} {label}: **+{effective*100:.1f}%**")
        if bonus_parts:
            lines.append(" · ".join(bonus_parts))
        if level < max_lv and name_key in _XP_SOURCES:
            lines.append(f"*{_XP_SOURCES[name_key]}*")
        return "\n".join(lines)

    _items = card(
        f"\U000026CF️ {ctx.author.display_name} - Items",
        color=C_GOLD,
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

    _items.field(f"{_SS['emoji']} Hashstone",   _stone_field(hashstone,   _SS, "hashstone"),   False)
    if _LS:
        _items.field(f"{_LS['emoji']} Lockstone",  _stone_field(lockstone,  _LS, "lockstone"),  False)
    if _VS:
        _items.field(f"{_VS['emoji']} Vaultstone", _stone_field(vaultstone, _VS, "vaultstone"), False)
    if _LQ and not _LQ.get("disabled"):
        _items.field(f"{_LQ['emoji']} Liqstone",   _stone_field(liqstone,   _LQ, "liqstone"),   False)
    # Themed minigame stones. Each is gated on its config entry being
    # present so adding/removing a stone is a config-only change.
    if _TI:
        _items.field(f"{_TI['emoji']} Tidestone",  _stone_field(tidestone,  _TI, "tidestone"),  False)
    if _HE:
        _items.field(f"{_HE['emoji']} Heartstone", _stone_field(heartstone, _HE, "heartstone"), False)
    if _CR:
        _items.field(f"{_CR['emoji']} Cryptstone", _stone_field(cryptstone, _CR, "cryptstone"), False)
    if _BL:
        _items.field(f"{_BL['emoji']} Bloodstone", _stone_field(bloodstone, _BL, "bloodstone"), False)
    if _BM:
        _items.field(f"{_BM['emoji']} Bloomstone", _stone_field(bloomstone, _BM, "bloomstone"), False)
    _items.footer("/inventory levelup <stone> · /shop buy <item> · /shop sell <item>")

    _cons = card(
        f"\U0001F9F0 {ctx.author.display_name} - Consumables",
        color=C_WARNING,
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    if _VG:
        _cons.field(
            f"{_VG['emoji']} Validator Guard",
            f"**{vg_count}** / {_VG.get('max_stack', 50)}\nAbsorbs a validator slash",
            True,
        )
    if _YG:
        _cons.field(
            f"{_YG['emoji']} Yield Guard",
            f"**{yg_count}** / {_YG.get('max_stack', 50)}\nAbsorbs a savings loss",
            True,
        )
    _cons.footer("/shop buy <consumable> [qty]")
    return [_items.build(), _cons.build()]


async def _render_fishing_section(ctx: DiscoContext) -> list[discord.Embed]:
    """Show fishing tackle: fish + junk + bait + traps.

    Reuses cogs.fishing._inventory_embed and services.fishing.inventory_summary
    + ensure_state so the section matches what ,fish inv shows exactly.
    """
    try:
        from services import fishing as fish_svc
        from cogs.fishing import _inventory_embed as _fish_inv_embed
    except Exception:
        log.exception("inventory dropdown: fishing import failed")
        return [card(
            "\U0001F3A3 Fishing", color=C_INFO,
        ).description(
            "Fishing module unavailable -- try `,fish inv` directly."
        ).build()]
    state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
    summary = fish_svc.inventory_summary(dict(state))
    return [_fish_inv_embed(ctx.author, summary)]


async def _render_eggs_section(ctx: DiscoContext) -> list[discord.Embed]:
    """Show held buddy eggs."""
    try:
        from services import fishing as fish_svc
        from cogs.fishing import _egg_status_embed
    except Exception:
        log.exception("inventory dropdown: eggs import failed")
        return [card(
            "\U0001F95A Held Eggs", color=C_GOLD,
        ).description(
            "Egg system unavailable -- try `,fish egg` directly."
        ).build()]
    summary = await fish_svc.list_held_eggs(
        ctx.db, ctx.guild_id, ctx.author.id,
    )
    # Live LURE oracle for the USD-equivalent column on the egg panel.
    lure_oracle = 0.0
    try:
        lp_row = await ctx.db.get_price("LURE", ctx.guild_id)
        lure_oracle = float(lp_row["price"]) if lp_row else 0.0
    except Exception:
        pass
    return [_egg_status_embed(
        ctx.author, summary, lure_oracle=lure_oracle,
    )]


async def _render_buddies_section(ctx: DiscoContext) -> list[discord.Embed]:
    """Show every owned buddy in the user's shelter.

    Renders one field per buddy with species emoji + name + level + tier,
    plus an Active marker on whichever is currently the active buddy and
    a 'for sale' marker on any buddies listed on the market.
    """
    try:
        from services import buddy_market as bm
        from configs.buddies_config import SPECIES, rarity_meta
    except Exception:
        log.exception("inventory dropdown: buddies import failed")
        return [card(
            "\U0001F436 Buddies", color=C_INFO,
        ).description(
            "Buddy system unavailable -- try `,buddy` directly."
        ).build()]

    rows = await bm.get_owned_buddies(ctx.db, ctx.guild_id, ctx.author.id)
    if not rows:
        return [card(
            f"\U0001F436 {ctx.author.display_name}'s Shelter",
            color=C_INFO,
        ).description(
            "_(no buddies in your shelter)_\n\n"
            "Hatch one with `,buddy hatch` (first 3 are free) or buy "
            "one off the auction house with `,ah browse buddy`."
        ).build()]

    builder = card(
        f"\U0001F436 {ctx.author.display_name}'s Shelter  -  "
        f"{len(rows)} buddy(ies)",
        color=C_INFO,
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    from configs.buddies_config import gender_glyph as _gender_glyph
    for r in rows:
        sp = str(r.get("species") or "")
        emoji = str(SPECIES.get(sp, {}).get("emoji") or "\U0001F436")
        tier_n = int(r.get("rarity_tier") or 1)
        tier_name = rarity_meta(tier_n).get("name", "Common")
        lvl = int(r.get("level") or 1)
        nm = str(r.get("name") or "Unnamed")
        glyph = _gender_glyph(r.get("gender"))
        glyph_part = f" {glyph}" if glyph else ""
        flags = []
        if r.get("is_active"):
            flags.append("⚡ *active*")
        if r.get("for_sale"):
            flags.append("\U0001F3F7️ *listed*")
        flag_line = ("  " + " · ".join(flags)) if flags else ""
        builder = builder.field(
            f"{emoji} **{nm}**{glyph_part}  (id `{r['id']}`)",
            f"Lv.{lvl} {tier_name} {sp.title()}{flag_line}",
            False,
        )
    builder = builder.footer(
        "Promote: ,buddy active <id>  -  Battle: ,buddy battle fight @user  -  "
        "List for sale: ,ah list buddy <id_or_name> <price>"
    )
    return [builder.build()]


async def _render_dungeon_section(ctx: DiscoContext) -> list[discord.Embed]:
    """Show the user's Delve crawler loadout: class + gear + potions + party.

    Mirrors what ,delve inv renders. Stays best-effort; if the dungeon
    module is unavailable (cog disabled, import fails) the section
    degrades to a single "module unavailable" embed instead of crashing.
    """
    try:
        from services import dungeon as dng_svc
        import configs.dungeon_config as _dc
    except Exception:
        log.exception("inventory dropdown: dungeon import failed")
        return [card(
            "\U0001F5FA Dungeon Bag", color=C_INFO,
        ).description(
            "Dungeon module unavailable -- try `,delve inv` directly."
        ).build()]

    state = await dng_svc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
    if not state or not state.get("class_key"):
        return [card(
            "\U0001F5FA Dungeon Bag", color=C_NEUTRAL,
        ).description(
            "You haven't started the dungeon yet. "
            "Pick a class with `,delve class warrior|mage|rogue` to begin."
        ).build()]

    weapons = state.get("weapons_owned") or {}
    armor = state.get("armor_owned") or {}
    cons = state.get("consumables") or {}
    eq_w = str(state.get("equipped_weapon") or "")
    eq_a = str(state.get("equipped_armor") or "")

    def _wline(k: str) -> str:
        m = _dc.weapon_meta(k) or {}
        star = " *(eq)*" if k == eq_w else ""
        rdot = _dc.rarity_dot(_dc.item_rarity(m))
        return (
            f"{rdot} {m.get('emoji', '')} `{k}` -- T{m.get('tier', 0)} "
            f"+{_dc.effective_atk_bonus(m)} ATK{star}"
        )

    def _aline(k: str) -> str:
        m = _dc.armor_meta(k) or {}
        star = " *(eq)*" if k == eq_a else ""
        rdot = _dc.rarity_dot(_dc.item_rarity(m))
        return (
            f"{rdot} {m.get('emoji', '')} `{k}` -- T{m.get('tier', 0)} "
            f"+{_dc.effective_def_bonus(m)} DEF{star}"
        )

    cons_lines = [
        f"x{int(qty):>3}  {(_dc.consumable_meta(k) or {}).get('emoji', '')} "
        f"`{k}` -- {(_dc.consumable_meta(k) or {}).get('blurb', '')}"
        for k, qty in cons.items() if int(qty) > 0
    ] or ["_(empty)_"]

    cmeta = _dc.class_meta(state.get("class_key") or "warrior") or {}
    fmeta = _dc.floor_meta(int(state.get("current_floor") or 0))
    color = int(fmeta.get("color_hex") or C_NAVY)

    builder = (
        card(
            f"\U0001F5FA {ctx.author.display_name}'s Delve Loadout",
            color=color,
        )
        .author(
            ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url,
        )
        .field(
            f"{cmeta.get('emoji', '')} {cmeta.get('name', '?')}",
            f"Lv.**{int(state.get('level') or 1)}**  -  "
            f"HP **{int(state.get('current_hp') or 0)}/"
            f"{int(state.get('hp_max') or 0)}**  -  "
            f"Deepest **F{int(state.get('deepest_floor') or 0)}**",
            False,
        )
        .field(
            "Weapons",
            "\n".join(_wline(k) for k in weapons) or "_(none)_",
            False,
        )
        .field(
            "Armor",
            "\n".join(_aline(k) for k in armor) or "_(none)_",
            False,
        )
        .field("Consumables", "\n".join(cons_lines), False)
        .field(
            "Stats",
            f"Kills **{int(state.get('total_kills') or 0):,}**  -  "
            f"Tames **{int(state.get('total_captures') or 0):,}**  -  "
            f"Bosses **{int(state.get('bosses_slain') or 0):,}**",
            False,
        )
    )
    # Captured buddies (party) -- compact summary so the section stays
    # under one embed.
    try:
        roster = await dng_svc.list_party(ctx.db, ctx.guild_id, ctx.author.id)
    except Exception:
        roster = []
    if roster:
        active_id = state.get("active_buddy_id")
        roster_lines = []
        for r in roster[:8]:
            sm = _dc.mob_meta(r.get("species_key") or "") or {}
            star = " *(active)*" if r.get("party_id") == active_id else ""
            roster_lines.append(
                f"`#{r.get('party_id')}` {sm.get('emoji', '')} "
                f"**{r.get('name') or sm.get('name', r.get('species_key'))}** "
                f"-- T{sm.get('tier', '?')}  Lv.{r.get('level', 1)}{star}"
            )
        if len(roster) > 8:
            roster_lines.append(f"_(+{len(roster) - 8} more)_")
        builder = builder.field(
            f"Party ({len(roster)}/{_dc.MAX_PARTY_SIZE})",
            "\n".join(roster_lines),
            False,
        )
    builder = builder.footer(
        "`,delve` to view current room  -  `,delve inv` for full breakdown"
    )
    return [builder.build()]


# ── Shop hub view ─────────────────────────────────────────────────────────────
# ``,shop`` opens a single dropdown that flips between every storefront in the
# game: the Discoin item shop (stones + consumables), the buddy market, the
# delve gear shop, and the fishing tackle shop. Each section is rendered on
# demand by reusing the section's own existing embed code so prices, owned
# state, and inventory counts stay in lockstep with each domain's commands.

_SHOP_HUB_TIMEOUT_S: int = 180


class _ShopBuyModal(discord.ui.Modal, title="Quick Shop Buy"):
    """Modal: ask for an item key + optional currency to buy from any
    of the shop sections without leaving the hub. Submission re-dispatches
    a synthetic ``,shop buy <key> [currency]`` message through the bot's
    full command pipeline so every decorator (`@guild_only`,
    `@ensure_registered`, cooldowns) runs exactly as if the player had
    typed the command themselves.
    """

    item = discord.ui.TextInput(
        label="Item to buy",
        placeholder="hashstone, lockstone, validator_guard, specialty_slot...",
        required=True,
        max_length=40,
    )
    currency = discord.ui.TextInput(
        label="Pay with (optional)",
        placeholder="DSD, USDC, MTA, REEL... (leave blank for the default)",
        required=False,
        max_length=10,
    )

    # Stash the parent view under a non-conflicting name. ``self.view`` is
    # used internally by ``discord.ui`` to wire items <-> their owning
    # View, so shadowing it on a Modal subclass breaks the interaction
    # lifecycle and surfaces as "this interaction failed" on submit.
    def __init__(self, view: "_ShopHubView") -> None:
        super().__init__()
        self._owner_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        item_key = str(self.item.value or "").strip().lower()
        cur = str(self.currency.value or "").strip().upper()
        if not item_key:
            await interaction.response.send_message(
                "Item key required.", ephemeral=True,
            )
            return

        ctx = self._owner_view.ctx
        prefix = ctx.prefix or Config.PREFIX
        cmd_tail = f"{item_key} {cur}".strip()
        full_command = f"{prefix}shop buy {cmd_tail}"

        # Acknowledge first so Discord doesn't fire the 3s timeout while
        # process_commands runs the full ,shop buy flow (which itself
        # mounts a ConfirmView and waits on the player). The ack is
        # ephemeral so it doesn't clutter the channel.
        try:
            await interaction.response.send_message(
                f"\U0001F6D2 Running `{full_command}`...",
                ephemeral=True,
            )
        except discord.HTTPException:
            log.debug("shop quick buy: ack failed", exc_info=True)

        # Re-dispatch as a synthetic message so the FULL command pipeline
        # runs (decorators, cooldowns, ensure_registered, the ConfirmView
        # interaction loop in ,shop buy). Direct-invoking the Command
        # object bypasses every decorator and crashes on missing state.
        try:
            import copy
            new_msg = copy.copy(ctx.message)
            new_msg.content = full_command  # type: ignore[attr-defined]
            await ctx.bot.process_commands(new_msg)
        except Exception as e:
            log.exception("shop quick buy failed item=%s", item_key)
            try:
                await interaction.followup.send(
                    f"Quick buy hit an error: `{type(e).__name__}: {e}`. "
                    f"Try `{full_command}` directly.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass


class _ShopHubView(discord.ui.View):
    """Dropdown view for ``,shop`` that swaps embeds in-place."""

    def __init__(self, ctx: DiscoContext, owner_id: int) -> None:
        super().__init__(timeout=_SHOP_HUB_TIMEOUT_S)
        self.ctx = ctx
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self._select = discord.ui.Select(
            placeholder="Switch shop...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="Discoin Items", value="discoin",
                    emoji="\U0001F6D2", default=True,
                    description="Stones + consumables (pay with stablecoin)",
                ),
                discord.SelectOption(
                    label="Buddies", value="buddies",
                    emoji="\U0001F436",
                    description="Buddies + eggs marketplace",
                ),
                discord.SelectOption(
                    label="Delves", value="delves",
                    emoji="\U0001F5FA",
                    description="Weapons, armor, potions (pay with RUNE)",
                ),
                discord.SelectOption(
                    label="Fishing", value="fishing",
                    emoji="\U0001F3A3",
                    description="Bait, lures, rods, traps",
                ),
            ],
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

        # Quick Buy button: pops a modal asking for item key + optional
        # currency. The modal's on_submit re-dispatches a synthetic
        # ",shop buy <key>" message through the bot's command pipeline so
        # the underlying _buy_stone / _buy_consumable / _buy_specialty_slot
        # ConfirmView flow runs exactly as if the player had typed it.
        async def _on_buy(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This isn't your shop.", ephemeral=True,
                )
                return
            try:
                await interaction.response.send_modal(_ShopBuyModal(self))
            except Exception as e:
                # Surface the failure as an ephemeral instead of letting
                # Discord show its generic "this interaction failed".
                log.exception("shop quick buy: send_modal failed")
                try:
                    await interaction.response.send_message(
                        f"Couldn't open the Quick Buy modal: "
                        f"`{type(e).__name__}: {e}`. Try `,shop buy <item>` "
                        f"directly.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass

        buy_btn = discord.ui.Button(
            label="Quick Buy",
            emoji="\U0001F6D2",
            style=discord.ButtonStyle.success,
            row=1,
        )
        buy_btn.callback = _on_buy
        self.add_item(buy_btn)

        # Refresh button: re-renders the current section in case the
        # player just bought something elsewhere or a stone level changed.
        async def _on_refresh(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This isn't your shop.", ephemeral=True,
                )
                return
            choice = (
                self._select.values[0] if self._select.values else "discoin"
            )
            try:
                embeds = await _render_shop_section(self.ctx, choice)
            except Exception:
                embeds = [card(
                    "\U0001F6AB Couldn't load shop", color=C_ERROR,
                ).description("Try `,shop` again.").build()]
            try:
                await interaction.response.edit_message(
                    embeds=embeds, view=self,
                )
            except discord.HTTPException:
                pass

        refresh_btn = discord.ui.Button(
            label="Refresh",
            emoji="\U0001F504",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        refresh_btn.callback = _on_refresh
        self.add_item(refresh_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your shop. Run `,shop` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        choice = self._select.values[0] if self._select.values else "discoin"
        for opt in self._select.options:
            opt.default = (opt.value == choice)
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            embeds = await _render_shop_section(self.ctx, choice)
        except Exception:
            log.exception("shop hub: %s render failed", choice)
            embeds = [card(
                "\U0001F6AB Couldn't load shop",
                color=C_ERROR,
            ).description("Try `,shop` again.").build()]
        try:
            if self.message is not None:
                await self.message.edit(embeds=embeds, view=self)
        except discord.HTTPException:
            log.debug("shop hub: edit failed", exc_info=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _wallet_field_for_section(
    ctx: DiscoContext, section: str,
) -> tuple[str, str]:
    """Build a ``("Your wallet", "<balance lines>")`` for the given
    shop-hub section so each tab shows the player how much of the relevant
    currency they hold without bouncing back to ``,balance``.

    Each balance is rendered as a ``FormatKit.stat_row`` line so the
    panel matches the polished progress-bar style used elsewhere on
    ``,today`` / ``,start``. The bar fills against the cheapest item the
    section sells, giving a visual read on "can I afford the entry SKU?"
    at a glance.

    Returned as a ``(label, value)`` tuple instead of mutated onto the
    items embed because the items page already runs close to the 6000
    char-per-embed cap; ``_render_shop_section`` puts these into a
    separate header embed prepended to the message.

    Currencies per section:
      discoin (default)  USD wallet + DSD + USDC + FGD
      buddies            USD (Buddy Market is denominated in USD)
      delves             RUNE (gear is priced in RUNE)
      fishing            REEL (rods + bait are priced in REEL)

    Errors are swallowed -- a wallet read failure should never block the
    shop view from rendering.
    """
    lines: list[str] = []
    try:
        if section == "delves":
            from services import dungeon as _dsvc
            import configs.dungeon_config as _dc
            rune_human = to_human(int(
                await _dsvc.get_rune_wallet_raw(
                    ctx.db, ctx.guild_id, ctx.author.id,
                ) or 0
            ))
            entry_cost = min(
                (float(c.get("price_rune") or 0) for c in _dc.CONSUMABLES.values()),
                default=100.0,
            ) or 100.0
            lines.append(FormatKit.stat_row(
                rune_human, max(rune_human, entry_cost),
                f"{_dc.RUNE_EMOJI}{_dc.RUNE_SYMBOL}  **{fmt_token(rune_human, _dc.RUNE_SYMBOL)}**",
                count_width=1,
            ))
        elif section == "fishing":
            from services import fishing as _fish_svc
            reel_human = to_human(
                await _fish_svc.get_reel_wallet_raw(
                    ctx.db, ctx.guild_id, ctx.author.id,
                )
            )
            lines.append(FormatKit.stat_row(
                reel_human, max(reel_human, 100.0),
                f"\U0001F3A3 REEL  **{fmt_token(reel_human, 'REEL')}**",
                count_width=1,
            ))
        elif section == "buddies":
            user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            usd_h = to_human(int((user_row or {}).get("wallet") or 0))
            lines.append(FormatKit.stat_row(
                usd_h, max(usd_h, 1_000.0),
                f"\U0001F4B5 USD  **{fmt_usd(usd_h)}**",
                count_width=1,
            ))
        else:  # discoin (default)
            user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            usd_h = to_human(int((user_row or {}).get("wallet") or 0))
            cheapest_stone = min(
                (to_human(int(cfg.get("cost_stable") or 0))
                 for cfg in _STONE_CFGS.values()
                 if cfg and not cfg.get("disabled")),
                default=1_000.0,
            ) or 1_000.0
            lines.append(FormatKit.stat_row(
                usd_h, max(usd_h, cheapest_stone),
                f"\U0001F4B5 USD  **{fmt_usd(usd_h)}**",
                count_width=1,
            ))
            for sym in ("DSD", "USDC", "FGD"):
                net = _STABLE_NETWORK.get(sym)
                if not net:
                    continue
                wh = await ctx.db.get_wallet_holding(
                    ctx.author.id, ctx.guild_id, net, sym,
                )
                amt = to_human(int((wh or {}).get("amount") or 0))
                if amt > 0:
                    lines.append(FormatKit.stat_row(
                        amt, max(amt, cheapest_stone),
                        f"{_stable_emoji(sym)}{sym}  **{fmt_token(amt, sym)}**",
                        count_width=1,
                    ))
    except Exception:
        log.debug("shop hub: wallet field render failed", exc_info=True)
    return ("\U0001F4B0 Your wallet", "\n".join(lines) if lines else "_(unavailable)_")


async def _render_shop_section(
    ctx: DiscoContext, section: str,
) -> list[discord.Embed]:
    """Build the embeds for the chosen shop hub section.

    Composition: a small wallet-header embed (stat-row balances for the
    section's currencies) followed by the section's listing embeds. The
    wallet lives in its own embed so the listing pages stay safely under
    Discord's 6000-char-per-embed cap even with all 9 stones rendered.
    """
    if section == "buddies":
        embeds = await _render_buddy_shop_section(ctx)
    elif section == "delves":
        embeds = await _render_delve_shop_section(ctx)
    elif section == "fishing":
        embeds = await _render_fishing_shop_section(ctx)
    else:
        embeds = await _render_discoin_shop_section(ctx)
    # Build the wallet header. Failures are swallowed -- a wallet read
    # error should never block the listing from rendering.
    header: discord.Embed | None = None
    try:
        label, value = await _wallet_field_for_section(ctx, section)
        header_b = card(label, color=C_NAVY).description(value)
        try:
            bot_user = getattr(ctx.bot, "user", None)
            avatar = getattr(bot_user, "display_avatar", None) if bot_user else None
            if avatar is not None:
                header_b = header_b.thumbnail(avatar.url)
        except Exception:
            log.debug("shop hub: thumbnail attach failed", exc_info=True)
        header = header_b.build()
    except Exception:
        log.debug("shop hub: wallet header build failed", exc_info=True)
    if header is not None:
        embeds = [header, *embeds]
    return embeds


async def _render_discoin_shop_section(
    ctx: DiscoContext,
) -> list[discord.Embed]:
    """Discoin item shop -- stones + consumables, prices in USD."""
    stones = await _fetch_all_stones(ctx.db, ctx.author.id, ctx.guild_id)
    vg_count = await ctx.db.get_validator_guard_count(ctx.author.id, ctx.guild_id)
    yg_count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)
    return _item_list_embeds(
        stones["hashstone"], stones["lockstone"], stones["vaultstone"], stones["liqstone"],
        vg_count, yg_count,
        tidestone=stones["tidestone"], heartstone=stones["heartstone"],
        cryptstone=stones["cryptstone"], bloodstone=stones["bloodstone"],
        bloomstone=stones["bloomstone"],
        gavelstone=stones.get("gavelstone"),
        anvilstone=stones.get("anvilstone"),
        chimerastone=stones.get("chimerastone"),
    )


async def _render_buddy_shop_section(
    ctx: DiscoContext,
) -> list[discord.Embed]:
    """Buddy market summary -- listings + egg market quick links."""
    try:
        from configs.buddies_config import SPECIES, rarity_meta
    except Exception:
        log.exception("shop hub: buddies import failed")
        return [card(
            "\U0001F436 Buddy Shop",
            description="Buddy module unavailable.",
            color=C_ERROR,
        ).build()]
    listings: list[dict] = []
    try:
        listings = await ctx.db.fetch_all(
            "SELECT id, owner_user_id, species, name, level, rarity_tier, "
            "for_sale_price FROM cc_buddies "
            "WHERE guild_id=$1 AND for_sale=TRUE "
            "ORDER BY for_sale_price ASC LIMIT 10",
            ctx.guild_id,
        ) or []
    except Exception:
        log.debug("shop hub: buddy listings query failed", exc_info=True)
    builder = card(
        "\U0001F436 Buddy Market",
        color=C_PURPLE,
        description=(
            "Browse buddies listed by other players, hatch fresh eggs, or trade "
            "in the egg market -- all on the auction house now.\n\n"
            "**Commands:**\n"
            "`,ah browse buddy` -- buddy listings\n"
            "`,ah browse egg` -- egg listings\n"
            "`,ah search <text>` -- find by name / species / token id\n"
            "`,buddy hatch <species>` -- hatch a held egg\n"
            "`,ah buy <listing_id>` -- buy any listing"
        ),
    )
    if listings:
        lines = []
        for r in listings:
            sp = str(r.get("species") or "")
            sm = SPECIES.get(sp, {}) if SPECIES else {}
            tier_n = int(r.get("rarity_tier") or 1)
            try:
                tier_name = rarity_meta(tier_n).get("name", "Common")
            except Exception:
                tier_name = "Common"
            price = float(r.get("for_sale_price") or 0)
            sp_emoji = sm.get("emoji") or "\U0001F436"
            nm = r.get("name") or "Unnamed"
            lines.append(
                f"`#{r['id']}` {sp_emoji} **{nm}** -- "
                f"Lv.{r.get('level', 1)} {tier_name} -- {fmt_usd(price)}"
            )
        builder = builder.field("Featured listings", "\n".join(lines), False)
    else:
        builder = builder.field(
            "Featured listings",
            "_(no buddies listed right now)_",
            False,
        )
    builder = builder.footer("Use `,ah` for the full auction house")
    return [builder.build()]


async def _render_delve_shop_section(
    ctx: DiscoContext,
) -> list[discord.Embed]:
    """Delve gear shop -- weapons / armor / consumables priced in RUNE.

    Returned as one embed per category so the catalog (44 weapons /
    25 armor / 29 consumables) stays under Discord's 1024-char-per-field
    and 6000-char-per-embed caps. A single combined embed silently 400s
    on edit, leaving the shop dropdown looking like it did nothing.
    """
    try:
        import configs.dungeon_config as _dc
    except Exception:
        log.exception("shop hub: dungeon import failed")
        return [card(
            "\U0001F5FA Delve Shop",
            description="Dungeon module unavailable.",
            color=C_ERROR,
        ).build()]

    def _rune_str(amount: float) -> str:
        return fmt_token(amount, _dc.RUNE_SYMBOL, _dc.RUNE_EMOJI)

    # Pack lines into <=1024-char chunks so each chunk fits a single
    # embed field. Joining with "\n" so we have to budget for the
    # separator on every line except the first.
    def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
        if not lines:
            return []
        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for ln in lines:
            add = len(ln) + (1 if cur else 0)
            if cur and cur_len + add > limit:
                chunks.append("\n".join(cur))
                cur = [ln]
                cur_len = len(ln)
            else:
                cur.append(ln)
                cur_len += add
        if cur:
            chunks.append("\n".join(cur))
        return chunks

    def _category_embed(
        title: str, color: int, lines: list[str], footer: str,
    ) -> discord.Embed:
        b = card(title, color=color)
        chunks = _chunk_lines(lines)
        if not chunks:
            b = b.field("Catalog", "_(none)_", False)
        else:
            for i, body in enumerate(chunks, start=1):
                name = title if len(chunks) == 1 else f"{title} ({i}/{len(chunks)})"
                b = b.field(name, body, False)
        return b.footer(footer).build()

    weapon_lines = [
        f"`{w['key']:<18}` T{w['tier']}  +{w['atk_bonus']:>2} ATK  "
        f"{_rune_str(w['price_rune'])}"
        for w in _dc.WEAPONS.values()
    ]
    armor_lines = [
        f"`{a['key']:<18}` T{a['tier']}  +{a['def_bonus']:>2} DEF  "
        f"{_rune_str(a['price_rune'])}"
        for a in _dc.ARMOR.values()
    ]
    cons_lines = [
        f"`{c['key']:<14}` {c['emoji']} {c['blurb']}  "
        f"{_rune_str(c['price_rune'])}"
        for c in _dc.CONSUMABLES.values()
    ]

    intro = (
        card("\U0001F5FA Delve Shop", color=C_NAVY)
        .description(
            "Buy with RUNE earned from delving. "
            "Use `,delve buy weapon|armor|consumable <key>`."
        )
        .footer("RUNE drops from chests + boss kills in `,delve`")
        .build()
    )
    return [
        intro,
        _category_embed(
            "\U00002694 Weapons", C_NAVY, weapon_lines,
            "Equip with `,delve equip weapon <key>`",
        ),
        _category_embed(
            "\U0001F6E1 Armor", C_NAVY, armor_lines,
            "Equip with `,delve equip armor <key>`",
        ),
        _category_embed(
            "\U0001F9EA Consumables", C_NAVY, cons_lines,
            "Use with `,delve use <key>`",
        ),
    ]


async def _render_fishing_shop_section(
    ctx: DiscoContext,
) -> list[discord.Embed]:
    """Fishing tackle shop -- bait, lures, traps."""
    try:
        from cogs.fishing import _shop_embed as _fish_shop_embed
        from services import fishing as fish_svc
    except Exception:
        log.exception("shop hub: fishing import failed")
        return [card(
            "\U0001F3A3 Fishing Shop",
            description="Fishing module unavailable -- try `,fish shop`.",
            color=C_ERROR,
        ).build()]
    try:
        state = await fish_svc.ensure_state(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        return [_fish_shop_embed(dict(state))]
    except Exception:
        log.debug("shop hub: fishing render failed", exc_info=True)
        return [card(
            "\U0001F3A3 Fishing Shop",
            description="Try `,fish shop` directly.",
            color=C_INFO,
        ).build()]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Shop(commands.Cog):
    # How often the catch-up poller scans every owned stone for users
    # with auto_levelup=on. Five minutes is plenty -- stones level once
    # per threshold-cross which usually takes hours to days, so a short
    # interval just trims the worst-case "I have funds, why didn't it
    # level?" delay.
    _AUTO_LEVELUP_POLL_S: int = 300
    # Sweep cosmetic_role_grants for expired rows once a minute. Tighter
    # than auto-levelup because the role grants are user-visible (1h
    # duration; a slow sweep would leave the role on for noticeable
    # extra minutes).
    _COSMETIC_GRANT_POLL_S: int = 60

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.auto_levelup_poller.start()
        self.cosmetic_grant_sweeper.start()
        register_interval("auto_levelup_poll", self._AUTO_LEVELUP_POLL_S)
        register_interval(
            "cosmetic_grant_sweep", self._COSMETIC_GRANT_POLL_S,
        )

    def cog_unload(self) -> None:
        self.auto_levelup_poller.cancel()
        self.cosmetic_grant_sweeper.cancel()

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "shop")

    @tasks.loop(seconds=_AUTO_LEVELUP_POLL_S)
    async def auto_levelup_poller(self) -> None:
        """Catch-up scan for stones that became eligible for auto-levelup
        outside an XP-tick context.

        ``notify_item_levelup_ready`` only fires when the level threshold
        is crossed in the exact tick it's called. That misses two cases:

        1. The player crossed the threshold without funds; topping up
           later doesn't re-trigger anything.
        2. The player toggled auto-levelup ON after the threshold was
           already crossed.

        This loop scans every user with ``auto_levelup=true``, walks
        every stone type, and calls ``_perform_auto_levelup`` on any
        currently-eligible stone with sufficient funds. ``_perform_auto_levelup``
        no-ops if the stone isn't eligible or funds are short, so the
        sweep is idempotent and safe to run frequently.
        """
        try:
            for guild in list(self.bot.guilds):
                try:
                    rows = await self.bot.db.fetch_all(
                        "SELECT user_id FROM user_settings "
                        "WHERE guild_id = $1 AND auto_levelup = TRUE",
                        guild.id,
                    )
                except Exception:
                    log.debug(
                        "auto_levelup_poller: settings fetch failed gid=%s",
                        guild.id, exc_info=True,
                    )
                    continue
                for srow in rows or []:
                    uid = int(srow.get("user_id") or 0)
                    if uid <= 0:
                        continue
                    for item_name in _STONE_CFGS.keys():
                        try:
                            await _perform_auto_levelup(
                                self.bot, uid, guild, item_name, send_dm=True,
                            )
                        except Exception:
                            log.debug(
                                "auto_levelup_poller: per-stone failed "
                                "uid=%s gid=%s item=%s",
                                uid, guild.id, item_name, exc_info=True,
                            )
        except Exception:
            log.warning("auto_levelup_poller: top-level error", exc_info=True)

    @auto_levelup_poller.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=_COSMETIC_GRANT_POLL_S)
    async def cosmetic_grant_sweeper(self) -> None:
        """Revoke cosmetic roles whose ``expires_at`` has passed.

        ,inventory use <cosmetic> grants the linked Discord role for a
        fixed duration (default 1h, see items_config.duration_seconds)
        and stamps a row in cosmetic_role_grants. This loop pulls every
        expired row, removes the role from the member if they still
        have it, and deletes the grant. Best-effort -- a closed DM /
        missing role / member-cache miss never blocks subsequent rows.
        """
        try:
            rows = await self.bot.db.list_expired_cosmetic_role_grants(
                limit=200,
            )
        except Exception:
            log.debug(
                "cosmetic_grant_sweeper: list query failed", exc_info=True,
            )
            return
        for row in rows or []:
            try:
                gid = int(row.get("guild_id") or 0)
                uid = int(row.get("user_id") or 0)
                role_id = int(row.get("role_id") or 0)
                grant_id = int(row.get("id") or 0)
                if gid <= 0 or uid <= 0 or role_id <= 0 or grant_id <= 0:
                    if grant_id > 0:
                        await self.bot.db.delete_cosmetic_role_grant(grant_id)
                    continue
                guild = self.bot.get_guild(gid)
                if guild is not None:
                    member = guild.get_member(uid)
                    if member is None:
                        try:
                            member = await guild.fetch_member(uid)
                        except Exception:
                            member = None
                    role = guild.get_role(role_id)
                    if member is not None and role is not None and role in member.roles:
                        try:
                            await member.remove_roles(
                                role,
                                reason="Cosmetic role expired",
                            )
                        except discord.Forbidden:
                            log.debug(
                                "cosmetic sweeper: missing perms gid=%s uid=%s",
                                gid, uid,
                            )
                        except discord.HTTPException:
                            log.debug(
                                "cosmetic sweeper: remove_roles failed gid=%s uid=%s",
                                gid, uid, exc_info=True,
                            )
                await self.bot.db.delete_cosmetic_role_grant(grant_id)
            except Exception:
                log.debug(
                    "cosmetic_grant_sweeper: per-row failed",
                    exc_info=True,
                )

    @cosmetic_grant_sweeper.before_loop
    async def _wait_for_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    # ── /shop ─────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="shop", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def shop(self, ctx: DiscoContext) -> None:
        """Open the unified Shop dropdown -- Discoin items, Buddies, Delve, Fishing."""
        if await suggest_subcommand(ctx, self.shop):
            return
        view = _ShopHubView(ctx, ctx.author.id)
        embeds = await _render_shop_section(ctx, "discoin")
        sent = await ctx.reply(embeds=embeds, view=view, mention_author=False)
        view.message = sent

    # ── /shop list ────────────────────────────────────────────────────────────

    @shop.command(name="list")
    @guild_only
    @no_bots
    @ensure_registered
    async def shop_list(self, ctx: DiscoContext) -> None:
        """List the Discoin item shop only (no dropdown)."""
        stones = await _fetch_all_stones(ctx.db, ctx.author.id, ctx.guild_id)
        vg_count = await ctx.db.get_validator_guard_count(ctx.author.id, ctx.guild_id)
        yg_count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)
        embeds = _item_list_embeds(
            stones["hashstone"], stones["lockstone"], stones["vaultstone"], stones["liqstone"],
            vg_count, yg_count,
            tidestone=stones["tidestone"], heartstone=stones["heartstone"],
            cryptstone=stones["cryptstone"], bloodstone=stones["bloodstone"],
            bloomstone=stones["bloomstone"],
            gavelstone=stones.get("gavelstone"),
            anvilstone=stones.get("anvilstone"),
            chimerastone=stones.get("chimerastone"),
        )
        await ctx.reply(embeds=embeds, mention_author=False)

    # ── /shop buy [item] ──────────────────────────────────────────────────────

    @shop.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    async def shop_buy(self, ctx: DiscoContext, item: str | None = None, currency: str = "") -> None:
        """Buy an item from the shop. Usage: /shop buy <item> [currency]

        ``currency`` is OPTIONAL. When omitted:
        * Stones default to their first ``accepted_currencies`` entry
          (Bloomstone -> HRV, Hashstone -> MTA, Tidestone -> REEL,
          Bloodstone -> BBT, etc.) so players don't have to memorise
          which token a given stone takes.
        * Consumables default to DSD (the original behaviour).
        """
        if item is None:
            stones = await _fetch_all_stones(ctx.db, ctx.author.id, ctx.guild_id)
            vg_count = await ctx.db.get_validator_guard_count(ctx.author.id, ctx.guild_id)
            yg_count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)
            embeds = _item_list_embeds(
                stones["hashstone"], stones["lockstone"], stones["vaultstone"], stones["liqstone"],
                vg_count, yg_count,
                tidestone=stones["tidestone"], heartstone=stones["heartstone"],
                cryptstone=stones["cryptstone"], bloodstone=stones["bloodstone"],
                bloomstone=stones["bloomstone"],
            )
            await ctx.reply(embeds=embeds, mention_author=False)
            return

        # Stones with per-stone accepted_currencies (Hashstone in MTA,
        # Tidestone in REEL, Bloodstone in BBT, Bloomstone in HRV, ...)
        # validate their own currency arg inside ``_buy_stone`` -- which
        # already defaults to the stone's first accepted currency when
        # the arg is empty. Pass the empty string straight through so
        # ``,shop buy bloomstone`` works without forcing the player to
        # remember the token.
        item_lower = item.lower().strip()
        stone_key = _resolve_stone_key(item_lower)
        if stone_key:
            await self._buy_stone(ctx, stone_key, (currency or "").strip())
            return

        # Consumables / extras (validator_guard / yield_guard /
        # specialty_slot) keep the legacy DSD/USDC default.
        currency = (currency or "DSD").upper()
        if currency not in _STABLE_NETWORK:
            await ctx.reply_error(
                f"`{currency}` is not an accepted stablecoin. Accepted: {_stable_display()}."
            )
            return
        if item_lower in ("validator_guard", "vguard"):
            await self._buy_consumable(ctx, "validator_guard", _VG, ctx.db.add_validator_guard, currency)
            return
        if item_lower in ("yield_guard", "yguard"):
            await self._buy_consumable(ctx, "yield_guard", _YG, ctx.db.add_yield_guard, currency)
            return
        if item_lower in _COSMETIC_ITEMS:
            # Cosmetics are craft-only since the cosmetic-rework -- the
            # only path in is the matching recipe in crafting_config.py.
            await ctx.reply_error(
                f"`{item_lower}` is craft-only. Make one with `,craft "
                f"<recipe>`:\n"
                f"  -  glamour_kit  -> `,craft shimmer_dust`\n"
                f"  -  night_crystal -> `,craft moon_essence`\n"
                f"  -  aurora_pass  -> `,craft aurora_prism`\n"
                f"Then `,inventory use {item_lower}` to grant the role for 1 hour."
            )
            return
        if item_lower in ("specialty_slot", "specialtyslot", "extra_slot", "slot"):
            await self._buy_specialty_slot(ctx)
            return
        available = ", ".join(f"`{i}`" for i in _BUYABLE_ITEMS)
        await ctx.reply_error(f"Unknown item `{item}`. Available: {available}")

    # ── /shop sell [item] ─────────────────────────────────────────────────────

    @shop.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    async def shop_sell(self, ctx: DiscoContext, item: str | None = None) -> None:
        """Sell an item back to the shop. Usage: /shop sell <hashstone|lockstone|vaultstone>"""
        if item is None:
            await ctx.reply_error("Specify the item to sell. Usage: `/shop sell hashstone`")
            return

        item_lower = item.lower().strip()
        stone_key = _resolve_stone_key(item_lower)
        if stone_key:
            await self._sell_stone(ctx, stone_key)
            return
        if item_lower in ("validator_guard", "vguard", "yield_guard", "yguard"):
            await ctx.reply_error("Consumable items cannot be sold back.")
            return
        sellable = ", ".join(f"`{i}`" for i in _STONE_ITEMS)
        await ctx.reply_error(f"Unknown item `{item}`. Sellable: {sellable}")

    # ── /shop transfer [item] @user ────────────────────────────────────────────

    @shop.command(name="transfer")
    @guild_only
    @no_bots
    @ensure_registered
    async def shop_transfer(self, ctx: DiscoContext, item: str | None = None, member: discord.Member | None = None) -> None:
        """Transfer an item to another user. Usage: /shop transfer <hashstone|lockstone|vaultstone> @user"""
        if item is None or member is None:
            await ctx.reply_error("Usage: `/shop transfer <item> @user`")
            return

        item_lower = item.lower().strip()
        stone_key = _resolve_stone_key(item_lower)
        if stone_key:
            await self._transfer_stone(ctx, stone_key, member)
            return
        if item_lower in ("validator_guard", "vguard", "yield_guard", "yguard"):
            await ctx.reply_error("Consumable items cannot be transferred.")
            return
        transferable = ", ".join(f"`{i}`" for i in _STONE_ITEMS)
        await ctx.reply_error(f"Unknown item `{item}`. Transferable: {transferable}")

    # ── Generic stone buy / sell / transfer ───────────────────────────────────
    # One implementation handles all 8 stones (hash, lock, vault, liq, tide,
    # heart, crypt, blood). Behavior is parameterised by the config in
    # ``_STONE_CFGS`` and the per-stone DB methods discovered via
    # ``_stone_db_ops``. Adding a new stone is a config + DB-method change
    # with zero new branches in this file.

    async def _buy_stone(
        self, ctx: DiscoContext, key: str, currency: str = "",
    ) -> None:
        cfg = _STONE_CFGS.get(key)
        if not cfg:
            await ctx.reply_error(f"`{key}` is not configured.")
            return
        if cfg.get("disabled"):
            await ctx.reply_error(f"{cfg.get('name', key.title())} is currently disabled.")
            return
        get_fn, create_fn, _del, _xfer, _upd, _add = _stone_db_ops(ctx.db, key)
        existing = await get_fn(ctx.author.id, ctx.guild_id)
        emoji = cfg.get("emoji", "")
        name = cfg.get("name", key.title())
        if existing:
            await ctx.reply_error(
                f"You already own a {name} {emoji}. Sell or transfer it first."
            )
            return

        # accepted_currencies came in via items_config in Phase C.1.
        # Default to the first accepted symbol if the user omitted the
        # currency arg (e.g. ``,shop buy hashstone`` falls back to MTA).
        # If the field's missing, fall back to the legacy DSD/USDC pair
        # so older, un-migrated stone configs keep working.
        accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
        currency = (currency or "").upper().strip() or accepted[0]
        if currency not in accepted:
            await ctx.reply_error(
                f"{name} accepts: `{', '.join(accepted)}`. Got `{currency}`."
            )
            return

        # Resolve the network + cost-in-currency. Three branches:
        #   USD            -- bare wallet (users.wallet); cost_stable is
        #                     the raw scaled USD amount itself.
        #   stable in
        #     STABLE_NETWORK
        #                  -- existing path; $1 peg, no oracle convert.
        #   any other      -- non-stable token (MTA / SUN / ARC / DSC /
        #                     REEL / BUD / RUNE / BBT / HRV). cost_stable
        #                     is the USD-equivalent target; we convert
        #                     to token amount at the live oracle.
        from core.framework.network import normalize_short
        usd_cost = to_human(cfg["cost_stable"])
        network: str | None
        if currency == "USD":
            network = None  # bare wallet path
            cost = cfg["cost_stable"]
        elif currency in _STABLE_NETWORK:
            network = _STABLE_NETWORK[currency]
            cost = cfg["cost_stable"]
        else:
            tok_meta = Config.TOKENS.get(currency, {})
            network = normalize_short(tok_meta.get("network") or "")
            if not network:
                await ctx.reply_error(
                    f"Unknown network for `{currency}`. Configure `{currency}` in Config.TOKENS."
                )
                return
            oracle_row = await ctx.db.get_price(currency, ctx.guild_id)
            oracle = float(oracle_row["price"]) if oracle_row else 0.0
            if oracle <= 0:
                await ctx.reply_error(
                    f"`{currency}` oracle is currently zero -- cannot price the stone. Try again later."
                )
                return
            cost = to_raw(usd_cost / oracle)

        tok_emoji = (
            _stable_emoji(currency)
            if currency in _STABLE_NETWORK or currency == "USD"
            else (Config.TOKENS.get(currency, {}).get("emoji", ""))
        )
        fee = int(cost * cfg["buy_fee_pct"])
        staked = cost - fee

        # Wallet check. USD reads users.wallet; everything else reads
        # wallet_holdings on the resolved network.
        if currency == "USD":
            user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            bal = int((user_row or {}).get("wallet") or 0)
        else:
            wh = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, network, currency,
            )
            bal = wh["amount"] if wh else 0
        if bal < cost:
            net_part = (
                f" on `{network}`" if network and currency != "USD" else ""
            )
            oracle_part = ""
            if currency != "USD" and currency not in _STABLE_NETWORK:
                try:
                    oracle_row = await ctx.db.get_price(currency, ctx.guild_id)
                    oracle = float((oracle_row or {}).get("price") or 0.0)
                    if oracle > 0:
                        oracle_part = (
                            f"\n-# Oracle: 1 {currency} = {fmt_usd(oracle)} -- "
                            f"need ~{fmt_token(to_human(cost), currency, tok_emoji)} "
                            f"to cover the {fmt_usd(usd_cost)} target."
                        )
                except Exception:
                    pass
            await ctx.reply_error(
                f"You need {fmt_token(to_human(cost), currency, tok_emoji)} "
                f"({fmt_usd(usd_cost)}) to acquire {name}.\n"
                f"Your {currency} balance{net_part}: "
                f"{fmt_token(to_human(bal), currency, tok_emoji)}"
                f"{oracle_part}"
            )
            return

        embed = card(
            f"{emoji} Stake {currency} to Acquire {name}",
            description=(
                f"Stake **{fmt_token(to_human(cost), currency, tok_emoji)}** "
                f"(**{fmt_usd(usd_cost)}**) to acquire a **{name}**?\n\n"
                f"• **{fmt_token(to_human(fee), currency, tok_emoji)}** -> guild treasury (buy fee)\n"
                f"• **{fmt_token(to_human(staked), currency, tok_emoji)}** -> locked in your {name} as stake\n\n"
                f"{cfg.get('description', '')}\n\n"
                f"💧 A small drip of LP is added to **every pool** in the game on purchase."
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Purchase cancelled.", color=C_ERROR).build())
            return

        try:
            if currency == "USD":
                # Bare wallet debit; reserves the staked + fee atomically.
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -cost)
            else:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, network, currency, -cost,
                )
        except ValueError:
            await msg.edit(embed=card(description=f"Insufficient {currency} balance.", color=C_ERROR).build())
            return

        await ctx.db.add_to_treasury(ctx.guild_id, fee)
        # Vault deposit only makes sense for the bot's per-network vaults.
        # USD goes straight to treasury, no vault deposit.
        if network is not None:
            await deposit_to_vault(ctx.db, ctx.guild_id, network, fee, bot=self.bot)
        await create_fn(ctx.author.id, ctx.guild_id, staked, lp_currency=currency)
        # NFT layer: mint a stone token. Best-effort.
        try:
            from services import items as _items
            await _items.mint_unit(
                ctx.db,
                guild_id=ctx.guild_id,
                contract_address=_items.contract_address("stone", str(key)),
                owner_user_id=ctx.author.id,
                metadata={
                    "stone_key": str(key),
                    "staked":    int(staked),
                    "currency":  str(currency),
                    "level":     1,
                },
                mint_source="shop.buy_stone",
                source_table=f"{key}stones",
                source_id=f"{ctx.author.id}:{key}:{int(__import__('time').time())}",
            )
        except Exception:
            log.debug(
                "nft stone mint sync failed gid=%s uid=%s key=%s",
                ctx.guild_id, ctx.author.id, key, exc_info=True,
            )
        # _item_lp_add only handles stables today; for MTA/REEL/BBT/etc
        # the call is a soft no-op (returns 0). Future enhancement: add
        # the stake into the relevant TOKEN/USD pool the same way.
        lp_added = (
            await _item_lp_add(ctx.db, ctx.guild_id, ctx.author.id, currency, staked)
            if currency in _STABLE_NETWORK else 0.0
        )
        drip_total = int(staked * 0.05) if currency in _STABLE_NETWORK else 0
        per_pool_drip = (
            await _drip_lp_all_pools(ctx.db, ctx.guild_id, drip_total)
            if drip_total > 0 else 0
        )
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SHOP_BUY",
            symbol_in=currency, amount_in=cost,
            network=(network or "usd"),
        )
        try:
            await self.bot.bus.publish(
                "stone_bought", guild=ctx.guild, user_id=int(ctx.author.id),
                item_type=key,
            )
        except Exception:
            pass

        bonus_parts = _stone_bonus_parts(cfg, level=1)
        bonus_line = "  ·  ".join(bonus_parts) if bonus_parts else ""
        lp_note = (
            f"\n💧 **{fmt_token(to_human(staked), currency)}** added to your "
            f"{currency}/USD LP pool"
            if lp_added > 0 else ""
        )
        drip_note = (
            f"\n💧 **{fmt_usd(to_human(drip_total))}** total drip "
            f"(~{fmt_usd(to_human(per_pool_drip))}/pool) seeded across every pool"
            if per_pool_drip > 0 else ""
        )
        result = (
            card(
                f"{emoji} {name} Acquired!",
                description=(
                    f"You now own a **Level 1 {name}**.\n"
                    f"{cfg.get('description', '')}"
                    + (f"\n{bonus_line}" if bonus_line else "")
                    + lp_note + drip_note
                ),
                color=C_SUCCESS,
            )
            .field(f"Staked", fmt_token(to_human(staked), currency, tok_emoji), True)
            .field("USD value", fmt_usd(to_human(staked)), True)
            .field("Fee paid", fmt_token(to_human(fee), currency, tok_emoji), True)
            .build()
        )
        await msg.edit(embed=result)

    async def _sell_stone(self, ctx: DiscoContext, key: str) -> None:
        cfg = _STONE_CFGS.get(key)
        if not cfg:
            await ctx.reply_error(f"`{key}` is not configured.")
            return
        get_fn, _create, delete_fn, _xfer, _upd, _add = _stone_db_ops(ctx.db, key)
        stone = await get_fn(ctx.author.id, ctx.guild_id)
        emoji = cfg.get("emoji", "")
        name = cfg.get("name", key.title())
        if not stone:
            await ctx.reply_error(
                f"You don't own a {name}. Use `/shop buy {key}` to get one."
            )
            return

        staked = int(stone["staked_amount"])
        sell_currency = stone.get("lp_currency") or "DSD"
        lp_value = int(await _item_lp_remove(ctx.db, ctx.guild_id, ctx.author.id, sell_currency))
        base_value = max(lp_value, staked)
        fee = int(base_value * cfg["sell_fee_pct"])
        refund = base_value - fee
        lp_profit = max(0, lp_value - staked)
        sell_emoji = _stable_emoji(sell_currency)
        profit_str = (
            f"\n💧 LP fee earnings: **+{fmt_token(to_human(lp_profit), sell_currency, sell_emoji)}** "
            f"(~{fmt_usd(to_human(lp_profit))})"
            if to_human(lp_profit) > 0.01 else ""
        )
        embed = card(
            f"{emoji} Sell {name}",
            description=(
                f"Sell your **Level {stone['level']} {name}**?\n\n"
                f"• **{fmt_token(to_human(fee), sell_currency, sell_emoji)}** -> guild treasury (sell fee)\n"
                f"• **{fmt_token(to_human(refund), sell_currency, sell_emoji)}** "
                f"(~{fmt_usd(to_human(refund))}) -> returned to your wallet"
                f"{profit_str}\n\n"
                f"⚠️ You will lose your Level {stone['level']} {name} permanently."
            ),
            color=C_WARNING,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Sell cancelled.", color=C_ERROR).build())
            return

        sell_net = _STABLE_NETWORK.get(sell_currency, "dsc")
        await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, sell_net, sell_currency, refund)
        await ctx.db.add_to_treasury(ctx.guild_id, fee)
        await deposit_to_vault(ctx.db, ctx.guild_id, sell_net, fee, bot=self.bot)
        await delete_fn(ctx.author.id, ctx.guild_id)
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SHOP_SELL",
            symbol_out=sell_currency, amount_out=refund, network=sell_net,
        )
        await msg.edit(embed=card(
            f"{emoji} {name} Sold",
            description=(
                f"Your {name} has been dissolved.\n"
                f"**{fmt_token(to_human(refund), sell_currency, sell_emoji)}** "
                f"(~{fmt_usd(to_human(refund))}) returned to your wallet."
                f"{profit_str}"
            ),
            color=C_SUCCESS,
        ).field("Fee paid", fmt_token(to_human(fee), sell_currency, sell_emoji), True).build())

    async def _transfer_stone(
        self, ctx: DiscoContext, key: str, member: discord.Member,
    ) -> None:
        cfg = _STONE_CFGS.get(key)
        if not cfg:
            await ctx.reply_error(f"`{key}` is not configured.")
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.reply_error("Invalid transfer target.")
            return
        get_fn, _create, _delete, transfer_fn, _upd, _add = _stone_db_ops(ctx.db, key)
        stone = await get_fn(ctx.author.id, ctx.guild_id)
        emoji = cfg.get("emoji", "")
        name = cfg.get("name", key.title())
        if not stone:
            await ctx.reply_error(f"You don't own a {name} to transfer.")
            return
        await ctx.db.ensure_user(member.id, ctx.guild_id)
        if await get_fn(member.id, ctx.guild_id):
            await ctx.reply_error(f"{member.display_name} already owns a {name}.")
            return

        gas_fee = cfg["transfer_fee_stable"]
        wh = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "dsc", "DSD")
        bal = wh["amount"] if wh else 0
        if bal < gas_fee:
            await ctx.reply_error(
                f"You need {fmt_token(to_human(gas_fee), 'DSD', '💵')} "
                f"({fmt_usd(to_human(gas_fee))}) for the transfer gas fee.\n"
                f"Your DSD balance: {fmt_token(to_human(bal), 'DSD', '💵')}"
            )
            return

        stone_lp_cur = stone.get("lp_currency") or "DSD"
        embed = card(
            f"{emoji} Transfer {name}",
            description=(
                f"Transfer your **Level {stone['level']} {name}** to "
                f"**{member.display_name}**?\n\n"
                f"• Gas fee: {fmt_token(to_human(gas_fee), 'DSD', '💵')} "
                f"(~{fmt_usd(to_human(gas_fee))}) -> guild treasury\n"
                f"• {name} (Level {stone['level']}, "
                f"{fmt_token(to_human(stone['staked_amount']), stone_lp_cur, _stable_emoji(stone_lp_cur))} "
                f"staked, ~{fmt_usd(to_human(stone['staked_amount']))}) "
                f"moves to {member.display_name}"
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Transfer cancelled.", color=C_ERROR).build())
            return

        try:
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, "dsc", "DSD", -gas_fee)
        except ValueError:
            await msg.edit(embed=card(description="Insufficient DSD for gas.", color=C_ERROR).build())
            return

        await ctx.db.add_to_treasury(ctx.guild_id, gas_fee)
        await deposit_to_vault(ctx.db, ctx.guild_id, "dsc", gas_fee, bot=self.bot)
        await transfer_fn(ctx.author.id, member.id, ctx.guild_id)
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SHOP_TRANSFER",
            symbol_in="DSD", amount_in=gas_fee, network="dsc",
        )
        await msg.edit(embed=card(
            f"{emoji} Transfer Complete",
            description=(
                f"Your **Level {stone['level']} {name}** has been transferred to "
                f"**{member.display_name}**.\n"
                f"Gas fee: {fmt_token(to_human(gas_fee), 'DSD', '💵')} "
                f"({fmt_usd(to_human(gas_fee))})"
            ),
            color=C_SUCCESS,
        ).build())

    # ── Generic consumable buy (for new stackable consumables) ────────────

    async def _buy_consumable(self, ctx: DiscoContext, item_id: str, cfg: dict, add_fn, currency: str = "DSD") -> None:
        if not cfg:
            await ctx.reply_error(f"{item_id} is not configured.")
            return
        network   = _STABLE_NETWORK[currency]
        tok_emoji = _stable_emoji(currency)
        cost = cfg["cost_stable"]  # raw int
        fee  = int(cost * cfg["buy_fee_pct"])
        wh   = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, network, currency)
        bal = wh["amount"] if wh else 0
        if bal < cost:
            await ctx.reply_error(
                f"You need {fmt_token(to_human(cost), currency, tok_emoji)} in your wallet.\n"
                f"Your {currency} balance: {fmt_token(to_human(bal), currency, tok_emoji)}"
            )
            return
        embed = card(
            f"{cfg['emoji']} Buy {cfg['name']}",
            description=(
                f"Buy a **{cfg['name']}** for {fmt_token(to_human(cost), currency, tok_emoji)}?\n\n"
                f"{cfg['description']}"
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Purchase cancelled.", color=C_ERROR).build())
            return
        try:
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, network, currency, -cost)
        except ValueError:
            await msg.edit(embed=card(description=f"Insufficient {currency} balance.", color=C_ERROR).build())
            return
        await ctx.db.add_to_treasury(ctx.guild_id, fee)
        await deposit_to_vault(ctx.db, ctx.guild_id, network, fee, bot=self.bot)
        new_count = await add_fn(ctx.author.id, ctx.guild_id)
        # NFT layer: mint one shop token. Best-effort.
        try:
            from services import items as _items
            await _items.mint_unit(
                ctx.db,
                guild_id=ctx.guild_id,
                contract_address=_items.contract_address("shop", str(item_id)),
                owner_user_id=ctx.author.id,
                metadata={"item_id": str(item_id), "currency": str(currency)},
                mint_source="shop.buy_consumable",
                source_table="users.consumables",
                source_id=(
                    f"{ctx.author.id}:{item_id}:"
                    f"{int(__import__('time').time())}"
                ),
            )
        except Exception:
            log.debug(
                "nft shop consumable mint sync failed gid=%s uid=%s key=%s",
                ctx.guild_id, ctx.author.id, item_id, exc_info=True,
            )
        await ctx.db.log_tx(ctx.guild_id, ctx.author.id, "SHOP_BUY", symbol_in=currency, amount_in=cost, network=network)
        result = card(
            f"{cfg['emoji']} {cfg['name']} Added!",
            description=f"You now have **{new_count}** {cfg['name']}(s) in inventory.",
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=result)

    # ── ,shop buy specialty_slot ──────────────────────────────────────────────
    # One-time premium unlock: bumps the user's crafting-specialty cap
    # from 2 to 3. Charged in USD off the user's wallet (no stablecoin
    # currency choice). Refuses if the user already bought one.

    SPECIALTY_SLOT_USD: float = 1_000_000.0

    async def _buy_specialty_slot(self, ctx: DiscoContext) -> None:
        from core.framework.scale import to_raw as _to_raw
        cost_usd = float(self.SPECIALTY_SLOT_USD)
        cost_raw = int(_to_raw(cost_usd))

        # Make sure the user_crafting row exists + read current extras.
        await ctx.db.execute(
            "INSERT INTO user_crafting (user_id, guild_id) "
            "VALUES ($2, $1) ON CONFLICT (guild_id, user_id) DO NOTHING",
            ctx.guild_id, ctx.author.id,
        )
        cur_extra = int(await ctx.db.fetch_val(
            "SELECT COALESCE(extra_specialty_slots, 0) "
            "FROM user_crafting WHERE guild_id = $1 AND user_id = $2",
            ctx.guild_id, ctx.author.id,
        ) or 0)
        if cur_extra >= 1:
            await ctx.reply_error(
                f"You already own the premium third specialty slot "
                f"(active extras: **{cur_extra}**). "
                f"Activate up to **{2 + cur_extra}** specialties via "
                f"`,craft specialize`."
            )
            return

        # Wallet check.
        ur = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        bal_raw = int((ur or {}).get("wallet") or 0)
        if bal_raw < cost_raw:
            from core.framework.scale import to_human as _to_human
            await ctx.reply_error(
                f"Premium third-slot unlock costs "
                f"**${cost_usd:,.0f}** USD. "
                f"Your USD wallet: **${_to_human(bal_raw):,.2f}**."
            )
            return

        # Confirm + charge.
        embed = card(
            "\U0001F48E Buy Third Specialty Slot?",
            description=(
                f"Unlock a **third** active crafting specialty slot for "
                f"**${cost_usd:,.0f}** USD?\n\n"
                f"Right now you can hold 2 specialties at once "
                f"(`,craft specialize <key>`). After this purchase you "
                f"can hold 3. One-time purchase, non-refundable."
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(
                description="Purchase cancelled.", color=C_ERROR,
            ).build())
            return

        # Atomic: debit USD + bump extra_specialty_slots in one transaction.
        try:
            async with ctx.db.atomic():
                debited = await ctx.db.fetch_val(
                    "UPDATE users SET wallet = wallet - $3::numeric "
                    "WHERE user_id = $2 AND guild_id = $1 "
                    "  AND wallet >= $3::numeric "
                    "RETURNING wallet",
                    ctx.guild_id, ctx.author.id, str(cost_raw),
                )
                if debited is None:
                    raise ValueError("Insufficient USD balance.")
                await ctx.db.execute(
                    "UPDATE user_crafting "
                    "  SET extra_specialty_slots = LEAST("
                    "        COALESCE(extra_specialty_slots, 0) + 1, 5"
                    "      ), "
                    "      updated_at = NOW() "
                    "WHERE guild_id = $1 AND user_id = $2",
                    ctx.guild_id, ctx.author.id,
                )
                await ctx.db.add_to_treasury(ctx.guild_id, cost_raw)
        except ValueError as e:
            await msg.edit(embed=card(
                description=str(e), color=C_ERROR,
            ).build())
            return
        except Exception:
            log.exception(
                "specialty slot buy failed gid=%s uid=%s",
                ctx.guild_id, ctx.author.id,
            )
            await msg.edit(embed=card(
                description="Purchase failed -- try again.",
                color=C_ERROR,
            ).build())
            return

        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SHOP_BUY",
            symbol_in="USD", amount_in=cost_raw, network="usd",
        )
        try:
            await self.bot.bus.publish(
                "specialty_slot_bought",
                guild=ctx.guild, user_id=int(ctx.author.id),
            )
        except Exception:
            log.debug("specialty_slot_bought publish failed", exc_info=True)

        result = card(
            "\U0001F48E Third Specialty Slot Unlocked",
            description=(
                f"Charged **${cost_usd:,.0f}** USD. You can now hold "
                f"**3** active crafting specialties at once. "
                f"Pick the third with `,craft specialize <key>`."
            ),
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=result)

    # ── /inventory ────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="inventory", aliases=["inv"], invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def inventory(self, ctx: DiscoContext) -> None:
        """View your inventory."""
        if await suggest_subcommand(ctx, self.inventory):
            return
        # Pre-fetch every stone row through the canonical _STONE_CFGS map
        # so adding a stone is a config-only change. Each row resolves
        # via the stone-key naming convention (``get_<key>``) used by
        # ``_stone_db_ops``.
        owned: dict[str, dict | None] = {}
        for _skey in _STONE_CFGS:
            try:
                getter = getattr(ctx.db, f"get_{_skey}")
            except AttributeError:
                owned[_skey] = None
                continue
            owned[_skey] = await getter(ctx.author.id, ctx.guild_id)
        # Pre-fetch oracle prices once for every non-stable currency the
        # roster accepts, so the per-stone field can show a USD value
        # alongside the staked token without re-running price lookups.
        price_map = await _stone_price_map(ctx.db, ctx.guild_id)
        vg_count = await ctx.db.get_validator_guard_count(ctx.author.id, ctx.guild_id)
        yg_count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)

        _STAT_LABELS = {
            "work_daily_bonus":   ("💼", "Work/Daily"),
            "mining_bonus":       ("⛏", "Mining"),
            "stake_bonus":        ("📈", "Staking"),
            "interest_bonus":     ("🏦", "Interest"),
            "swap_fee_discount":  ("🔄", "Swap fee reduc"),
            "lp_reward_bonus":    ("🌊", "LP rewards"),
            # Themed minigame stats.
            "fish_payout_bonus":  ("🎣", "Fish payout"),
            "fish_combo_bonus":   ("🪝", "Fish combo"),
            "buddy_xp_bonus":     ("🐾", "Buddy XP"),
            "buddy_decay_resist": ("💖", "Mood decay resist"),
            "dungeon_mine_bonus": ("⛏", "Dungeon ore"),
            "dungeon_atk_bonus":  ("⚔", "Dungeon ATK"),
            "dungeon_capture_bonus": ("🧲", "Capture chance"),
            "battle_atk_bonus":   ("⚔", "Battle ATK"),
            "battle_hp_bonus":    ("❤", "Battle HP"),
            "battle_prize_bonus": ("💰", "Battle prize"),
        }
        _XP_SOURCES = {
            "hashstone":   "XP from: mining blocks",
            "lockstone":  "XP from: staking & validator blocks",
            "vaultstone": "XP from: savings deposits & interest",
            "liqstone":   "XP from: providing LP (value x hold time)",
            "tidestone":  "XP from: ,fish casts (+ legendary, + combo)",
            "heartstone": "XP from: buddy chats / feeds / level-ups",
            "cryptstone": "XP from: dungeon kills / captures / mines / bosses",
            "bloodstone": "XP from: buddy battle rounds + wins (+ captures)",
        }

        def _stone_field(stone, cfg, name_key):
            desc_full = cfg.get("description", "")
            desc = (desc_full.split(".")[0] + ".") if desc_full else ""
            accepted = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
            if not stone:
                lines = [f"*{desc}*"] if desc else []
                accepted_str = " / ".join(f"`{c}`" for c in accepted)
                lines.append(
                    f"Not owned  -  buy with `/shop buy {name_key}` for "
                    f"**{fmt_usd(to_human(cfg['cost_stable']))}** -- "
                    f"pay in {accepted_str}."
                )
                return "\n".join(lines)
            level  = stone["level"]
            xp     = stone["xp"]
            staked = stone["staked_amount"]
            max_lv = cfg["max_level"]
            base   = cfg["xp_per_level_base"]
            fill = int(12 * min((xp - base * level * (level - 1) // 2) / max(1, base * level), 1))
            bar = "█" * fill + "░" * (12 - fill)
            # Display fallback: if lp_currency is set but isn't one of the
            # stone's currently-accepted currencies (legacy DSD on a
            # themed stone whose accepted_currencies tuple was tightened),
            # fall back to the canonical first accepted currency so the
            # display always matches the stone's true network token.
            stone_cur = (stone.get("lp_currency") or "").upper()
            if not stone_cur or (accepted and stone_cur not in accepted):
                stone_cur = accepted[0] if accepted else "DSD"
            stone_emoji = (
                _stable_emoji(stone_cur)
                if stone_cur in _STABLE_NETWORK or stone_cur == "USD"
                else (Config.TOKENS.get(stone_cur, {}).get("emoji", ""))
            )
            staked_h = to_human(staked)
            staked_usd = _stone_staked_usd(staked_h, stone_cur, price_map)
            usd_str = f" (≈ {fmt_usd(staked_usd)})" if staked_usd > 0 else ""
            staked_str = f"{fmt_token(staked_h, stone_cur, stone_emoji)}{usd_str}"
            lines = []
            if level < max_lv:
                xp_start = base * level * (level - 1) // 2
                xp_next  = base * (level + 1) * level // 2
                xp_str = f"{xp - xp_start:,.1f} / {xp_next - xp_start:,.0f} XP"
                ready = xp >= xp_next
                lines.append(f"**Level {level} / {max_lv}** · {staked_str} staked")
                lines.append(f"`{bar}` {xp_str}  <- next level")
                if ready:
                    lup_cost = _levelup_cost(cfg, level, staked)
                    lup_h = to_human(lup_cost)
                    lup_usd = _stone_staked_usd(lup_h, stone_cur, price_map)
                    lup_usd_str = f" (≈ {fmt_usd(lup_usd)})" if lup_usd > 0 else ""
                    lines.append(
                        f"⬆️ **Ready to level up!** Pay "
                        f"{fmt_token(lup_h, stone_cur, stone_emoji)}{lup_usd_str} "
                        f"-> `/inventory levelup {name_key}`"
                    )
            else:
                lines.append(f"**Level {level} / {max_lv} ✦ MAX** · {staked_str} staked")
            bonus_parts = []
            for stat_key, (emoji, label) in _STAT_LABELS.items():
                val = cfg["stats"].get(stat_key, 0.0)
                if val == 0.0:
                    continue
                effective = val * level
                bonus_parts.append(f"{emoji} {label}: **+{effective*100:.1f}%**")
            if bonus_parts:
                lines.append(" · ".join(bonus_parts))
            xp_src = _STONE_XP_SOURCES.get(name_key) or _XP_SOURCES.get(name_key)
            if level < max_lv and xp_src:
                lines.append(f"*{xp_src}*")
            return "\n".join(lines)

        # ── Page 1: Leveled Items ─────────────────────────────────────────────
        _items = card(
            f"⛏️ {ctx.author.display_name} - Items",
            color=C_GOLD,
        ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        # Iterate _STONE_CFGS so every stone (including the meta-economy
        # additions) renders without per-stone if-branches drifting out
        # of sync with the buy/levelup flow.
        for _skey, _cfg in _STONE_CFGS.items():
            if not _cfg or _cfg.get("disabled"):
                continue
            _items.field(
                f"{_cfg.get('emoji', '')} {_cfg.get('name', _skey.title())}",
                _stone_field(owned.get(_skey), _cfg, _skey),
                False,
            )

        _items.footer(
            "/inventory levelup <stone>  ·  /shop buy <item>  ·  "
            "/shop sell <item>  ·  ,items for the new NFT browser"
        )

        # ── Page 2: Consumables ───────────────────────────────────────────────
        _cons = card(
            f"🧰 {ctx.author.display_name} - Consumables",
            color=C_WARNING,
        ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        # Stackable consumables
        if _VG:
            _cons.field(f"{_VG['emoji']} Validator Guard", f"**{vg_count}** / {_VG.get('max_stack', 50)}\nAbsorbs a validator slash", True)
        if _YG:
            _cons.field(f"{_YG['emoji']} Yield Guard", f"**{yg_count}** / {_YG.get('max_stack', 50)}\nAbsorbs a savings loss", True)

        _cons.footer("/shop buy <consumable> [qty]")

        # Open with the legacy items + consumables view; the dropdown
        # lets the user flip to fishing tackle / eggs / buddies / dungeon
        # without re-running the command.
        view = _InventoryView(ctx, ctx.author.id)
        sent = await ctx.reply(
            embeds=[_items.build(), _cons.build()],
            view=view,
            mention_author=False,
        )
        view.message = sent

    @inventory.command(name="use")
    @guild_only
    @no_bots
    @ensure_registered
    async def inventory_use(self, ctx: DiscoContext, item: str) -> None:
        """Use a consumable item from your inventory. Usage: /inventory use yield_guard"""
        if item.lower() in ("yield_guard", "yguard"):
            if not _YG:
                await ctx.reply_error("Yield Guard is not configured.")
                return
            count = await ctx.db.get_yield_guard_count(ctx.author.id, ctx.guild_id)
            if count <= 0:
                await ctx.reply_error(
                    f"You have no Yield Guards in your inventory.\n"
                    f"Buy one with `/shop buy yield_guard`  -  costs {fmt_token(to_human(_YG.get('cost_stable', 0)), 'DSD', '💵')}."
                )
                return
            embed = card(
                f"{_YG['emoji']} Yield Guard Active",
                description=(
                    f"Your **{count}** Yield Guard(s) are standing by.\n\n"
                    f"They are **auto-consumed** when a lending loss or validator slash would reduce your holdings  -  "
                    f"no manual activation needed. One guard absorbs one event."
                ),
                color=C_SUCCESS,
            ).footer(f"{count} guard(s) in inventory · auto-triggers on next loss event").build()
            await ctx.reply(embed=embed, mention_author=False)
        elif item.lower() in _COSMETIC_ITEMS:
            await self._use_cosmetic(ctx, item.lower())
        else:
            all_usable = list(_CONSUMABLE_ITEMS) + list(_COSMETIC_ITEMS)
            usable_str = ", ".join(f"`{i}`" for i in all_usable)
            await ctx.reply_error(f"Unknown consumable `{item}`. Available: {usable_str}")

    async def _use_cosmetic(self, ctx: DiscoContext, item_key: str) -> None:
        """Consume one cosmetic and grant its linked role for a fixed
        duration (default 1h). Re-using the same cosmetic before the
        previous grant expires REFRESHES the timer instead of toggling
        the role off -- the sweeper revokes the role when the deadline
        in cosmetic_role_grants passes.
        """
        cfg_map = {"glamour_kit": _GK, "night_crystal": _NC, "aurora_pass": _AP}
        cfg = cfg_map.get(item_key, {})
        if not cfg:
            await ctx.reply_error(f"`{item_key}` is not configured.")
            return

        uid = ctx.author.id
        gid = ctx.guild_id
        count = await ctx.db.get_cosmetic_count(uid, gid, item_key)
        overrides = await ctx.db.get_cosmetic_role_overrides(gid)
        role_name: str = overrides.get(item_key) or cfg.get("role_name", "")
        duration_s = int(cfg.get("duration_seconds", 3600))

        if count <= 0:
            await ctx.reply_error(
                f"You have no **{cfg['name']}** in your inventory.\n"
                f"Cosmetics are craft-only -- make one with `,craft "
                f"{ {'glamour_kit': 'shimmer_dust', 'night_crystal': 'moon_essence', 'aurora_pass': 'aurora_prism'}.get(item_key, '<recipe>') }` first."
            )
            return

        member = ctx.guild.get_member(uid)
        if not member:
            try:
                member = await ctx.guild.fetch_member(uid)
            except Exception:
                member = None
        if not member:
            await ctx.reply_error(
                "Could not find you in this guild's member cache."
            )
            return

        existing_role = (
            discord.utils.get(ctx.guild.roles, name=role_name)
            if role_name else None
        )
        if not existing_role and role_name:
            try:
                existing_role = await ctx.guild.create_role(
                    name=role_name,
                    mentionable=False,
                    reason=f"Discoin cosmetic role: {item_key}",
                )
            except discord.Forbidden:
                await ctx.reply_error(
                    "I don't have permission to create roles. "
                    f"Ask an admin to create a role named **{role_name}** "
                    "and give me Manage Roles."
                )
                return
        if existing_role is None:
            await ctx.reply_error(
                f"`{item_key}` has no role_name configured."
            )
            return

        consumed = await ctx.db.remove_cosmetic(uid, gid, item_key)
        if not consumed:
            await ctx.reply_error("Could not consume item -- inventory error.")
            return

        try:
            await member.add_roles(
                existing_role, reason=f"Cosmetic ({duration_s}s): {item_key}",
            )
        except discord.Forbidden:
            # Refund the consumed item so the player isn't out an
            # uncraftable resource for a bot-side permission gap.
            await ctx.db.add_cosmetic(uid, gid, item_key, 1)
            await ctx.reply_error(
                "I don't have permission to assign roles. Ask an admin "
                "to give me Manage Roles."
            )
            return

        try:
            await ctx.db.upsert_cosmetic_role_grant(
                uid, gid, item_key, int(existing_role.id), duration_s,
            )
        except Exception:
            log.debug(
                "cosmetic role grant upsert failed uid=%s gid=%s key=%s",
                uid, gid, item_key, exc_info=True,
            )

        mins = max(1, duration_s // 60)
        remaining = await ctx.db.get_cosmetic_count(uid, gid, item_key)
        await ctx.reply_success(
            f"{cfg['emoji']} **{cfg['name']}** activated!\n"
            f"You now have the **{role_name}** role for **{mins} minute"
            f"{'s' if mins != 1 else ''}**. Use it again before it expires "
            f"to refresh the timer.\n"
            f"-# {remaining} {cfg['name']}(s) remaining in inventory.",
            title="Cosmetic Activated",
        )

    @inventory.command(name="levelup", aliases=["lvlup", "upgrade"])
    @guild_only
    @no_bots
    @ensure_registered
    async def inventory_levelup(self, ctx: DiscoContext, item: str, currency: str = "") -> None:
        """Level up a stone once XP threshold is met. Pay stablecoin to confirm."""
        stone_key = _resolve_stone_key(item)
        if not stone_key:
            lvl_items = ", ".join(f"`{k}`" for k in _STONE_CFGS if _STONE_CFGS[k])
            await ctx.reply_error(f"Can level up: {lvl_items}.")
            return

        item_lower = stone_key
        cfg = _STONE_CFGS[stone_key]
        # Currency resolution mirrors ``_buy_stone``:
        #   USD                -- bare wallet path
        #   stable in
        #     _STABLE_NETWORK  -- $1 peg, no oracle convert
        #   any other          -- token in Config.TOKENS (FORGE / RUNE /
        #                         REEL / BUD / ...) -- ``cost_stable`` is
        #                         the USD-equivalent target; convert to
        #                         token amount at the live oracle.
        # Default currency: stone's lp_currency (so the auto path keeps
        # using whatever the player paid in originally), else the first
        # entry in accepted_currencies, else DSD for un-migrated rows.
        accepted = list(cfg.get("accepted_currencies") or ("DSD",))
        # We need the stone row first to read lp_currency for defaulting,
        # but we also want to reject early on bogus currency input. Fetch
        # the stone now so the default + validation can both consult it.
        getter, _create, _delete, _xfer, updater, staked_adder = _stone_db_ops(ctx.db, stone_key)
        stone = await getter(ctx.author.id, ctx.guild_id)
        if not stone:
            buy_hint_cur = accepted[0] if accepted else "DSD"
            await ctx.reply_error(
                f"You don't own a {item_lower.title()}. "
                f"Stake {buy_hint_cur} to acquire one with `/shop buy {item_lower}`."
            )
            return
        if not currency:
            stored = (stone.get("lp_currency") or "").upper()
            if stored in accepted:
                currency = stored
            else:
                currency = accepted[0] if accepted else "DSD"
        currency = currency.upper()
        # Validation: USD must be on the accepted list; non-stable tokens
        # must live in Config.TOKENS so the oracle convert works; stables
        # must be a real stable.
        if currency == "USD":
            if "USD" not in accepted:
                await ctx.reply_error(
                    f"{cfg.get('name', stone_key)} doesn't accept USD. "
                    f"Pay in: `{', '.join(accepted)}`."
                )
                return
        elif currency in _STABLE_NETWORK:
            pass  # accepted
        elif currency in Config.TOKENS:
            if currency not in accepted and accepted != ["DSD", "USDC"]:
                # Reject when the stone explicitly enumerates accepted
                # tokens and this one isn't on the list.
                await ctx.reply_error(
                    f"{cfg.get('name', stone_key)} doesn't accept "
                    f"`{currency}`. Pay in: `{', '.join(accepted)}`."
                )
                return
        else:
            await ctx.reply_error(
                f"`{currency}` is not a recognised currency. "
                f"Accepted for {cfg.get('name', stone_key)}: "
                f"`{', '.join(accepted)}`."
            )
            return

        max_lv = cfg["max_level"]
        cur_level = stone["level"]
        if cur_level >= max_lv:
            await ctx.reply_error(
                f"Your {item_lower.title()} is already at max level (**{max_lv}**). ✦ MAX"
            )
            return

        # ── XP gate: must have earned enough XP before paying to level up ──
        base = cfg["xp_per_level_base"]
        xp_threshold = float(base * (cur_level + 1) * cur_level // 2)
        if stone["xp"] < xp_threshold:
            xp_have  = stone["xp"]
            xp_start = float(base * cur_level * (cur_level - 1) // 2)
            xp_need  = xp_threshold - xp_start
            xp_prog  = xp_have - xp_start
            await ctx.reply_error(
                f"Not enough XP to level up your **{item_lower.title()}** yet.\n\n"
                f"Progress: **{xp_prog:,.1f} / {xp_need:,.0f} XP** toward Level {cur_level + 1}.\n"
                f"Keep earning XP  -  then return here to pay and level up."
            )
            return

        new_level = cur_level + 1
        # Level-up cost is 10% of the stone's current staked balance,
        # denominated in the stone's stake currency. For a Hashstone
        # bought in MTA, ``staked_amount`` is MTA raw, so 10% of staked
        # is also MTA raw -- no oracle convert is needed (and the old
        # path's USD round-trip via oracle was double-converting,
        # producing a near-zero charge that let stones "level for free").
        from core.framework.network import normalize_short
        stone_currency = (stone.get("lp_currency") or "").upper() or (
            accepted[0] if accepted else "DSD"
        )
        cost_in_stake = _levelup_cost(cfg, cur_level, stone["staked_amount"])

        # USD-equivalent for display (best-effort): stables map 1:1,
        # non-stables read the live oracle.
        if stone_currency == "USD" or stone_currency in _STABLE_NETWORK:
            usd_cost_human = to_human(cost_in_stake)
        else:
            try:
                _ocr = await ctx.db.get_price(stone_currency, ctx.guild_id)
                _ocr_px = float(_ocr["price"]) if _ocr else 0.0
            except Exception:
                _ocr_px = 0.0
            usd_cost_human = to_human(cost_in_stake) * _ocr_px if _ocr_px > 0 else 0.0

        if currency == stone_currency:
            cost_stable = cost_in_stake
            if currency == "USD":
                lv_network: str | None = None
            elif currency in _STABLE_NETWORK:
                lv_network = _STABLE_NETWORK[currency]
            else:
                tok_meta = Config.TOKENS.get(currency, {})
                lv_network = normalize_short(tok_meta.get("network") or "")
                if not lv_network:
                    await ctx.reply_error(
                        f"Unknown network for `{currency}`. Configure it in Config.TOKENS."
                    )
                    return
        else:
            # Cross-currency level-up: convert the staked-currency cost to
            # USD via the stake-currency oracle, then to the target
            # currency via its oracle. Stables/USD short-circuit at $1.
            stake_oracle = 1.0
            if stone_currency != "USD" and stone_currency not in _STABLE_NETWORK:
                _so_row = await ctx.db.get_price(stone_currency, ctx.guild_id)
                stake_oracle = float(_so_row["price"]) if _so_row else 0.0
                if stake_oracle <= 0:
                    await ctx.reply_error(
                        f"`{stone_currency}` oracle is currently zero -- cannot "
                        f"price the level-up. Try again later."
                    )
                    return
            usd_cost_human = to_human(cost_in_stake) * stake_oracle
            if currency == "USD":
                cost_stable = to_raw(usd_cost_human)
                lv_network = None
            elif currency in _STABLE_NETWORK:
                cost_stable = to_raw(usd_cost_human)
                lv_network = _STABLE_NETWORK[currency]
            else:
                tok_meta = Config.TOKENS.get(currency, {})
                lv_network = normalize_short(tok_meta.get("network") or "")
                if not lv_network:
                    await ctx.reply_error(
                        f"Unknown network for `{currency}`. Configure it in Config.TOKENS."
                    )
                    return
                oracle_row = await ctx.db.get_price(currency, ctx.guild_id)
                oracle = float(oracle_row["price"]) if oracle_row else 0.0
                if oracle <= 0:
                    await ctx.reply_error(
                        f"`{currency}` oracle is currently zero -- cannot price the level-up. Try again later."
                    )
                    return
                cost_stable = to_raw(usd_cost_human / oracle)

        lv_emoji = (
            _stable_emoji(currency)
            if currency in _STABLE_NETWORK
            else (
                "\U0001F4B5"
                if currency == "USD"
                else (Config.TOKENS.get(currency, {}).get("emoji", ""))
            )
        )

        if currency == "USD":
            user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            bal = int((user_row or {}).get("wallet") or 0)
        else:
            wh = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, lv_network, currency,
            )
            bal = wh["amount"] if wh else 0.0
        if bal < cost_stable:
            move_hint = (
                ""
                if currency == "USD"
                else f"\nUse `/bank move <amount> {currency} bank wallet` to move funds first."
            )
            await ctx.reply_error(
                f"You need {fmt_token(to_human(cost_stable), currency, lv_emoji)} "
                f"({fmt_usd(usd_cost_human)}) to level up.\n"
                f"Your {currency} balance: {fmt_token(to_human(bal), currency, lv_emoji)}"
                f"{move_hint}"
            )
            return

        _STAT_LABELS = {
            "work_daily_bonus":  ("💼", "Work/Daily"),
            "mining_bonus":      ("⛏", "Mining"),
            "stake_bonus":       ("📈", "Staking"),
            "interest_bonus":    ("🏦", "Interest"),
            "swap_fee_discount": ("🔄", "Swap fee reduc"),
            "lp_reward_bonus":   ("🌊", "LP rewards"),
        }
        bonus_parts = []
        for stat_key, (emo, label) in _STAT_LABELS.items():
            val = cfg["stats"].get(stat_key, 0.0)
            if val == 0.0:
                continue
            effective = val * new_level
            bonus_parts.append(f"{emo} {label}: **+{effective*100:.0f}%**")

        bonus_line = "  ·  ".join(bonus_parts) if bonus_parts else ""
        new_staked = int(stone["staked_amount"]) + cost_stable
        embed = card(
            f"{cfg['emoji']} Level Up {item_lower.title()}?",
            description=(
                f"Pay {fmt_token(to_human(cost_stable), currency, lv_emoji)} to level up your **{item_lower.title()}**?\n\n"
                f"**Level {cur_level}** → **Level {new_level}** / {max_lv}"
                + (f"\n{bonus_line}" if bonus_line else "")
                + f"\n\nYour staked total will become {fmt_token(to_human(new_staked), currency, lv_emoji)} (sell value increases)."
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(description="Level up cancelled.", color=C_ERROR).build())
            return

        try:
            if currency == "USD":
                # USD-priced stones debit the bare wallet directly; no
                # network-routed wallet_holdings row. ``users`` is keyed
                # on ``(user_id, guild_id)`` -- using ``id`` here raises
                # ``column "id" does not exist`` and the player sees the
                # generic error after confirming.
                await ctx.db.update_wallet(
                    ctx.author.id, ctx.guild_id, -int(cost_stable),
                )
                lv_network = ""  # used by tx log + skipped vault deposit
            else:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, lv_network, currency,
                    -cost_stable,
                )
        except ValueError:
            await msg.edit(embed=card(description=f"Insufficient {currency} balance.", color=C_ERROR).build())
            return

        await ctx.db.add_to_treasury(ctx.guild_id, cost_stable)
        if currency != "USD":
            await deposit_to_vault(
                ctx.db, ctx.guild_id, lv_network, cost_stable, bot=self.bot,
            )
        await updater(ctx.author.id, ctx.guild_id, stone["xp"], new_level)
        await staked_adder(ctx.author.id, ctx.guild_id, cost_stable)
        # NFT layer: refresh stone token metadata (level + staked).
        # Stone tokens aren't burned on level-up -- they're the same
        # underlying NFT, just upgraded. Best-effort.
        try:
            from services import items as _items
            owned_stones = await _items.list_owned(
                ctx.db,
                guild_id=ctx.guild_id, user_id=ctx.author.id,
                contract_address=_items.contract_address("stone", str(item_lower)),
                limit=1,
            )
            if owned_stones:
                tok = owned_stones[0]
                import json as _json
                cur_md = tok.get("metadata") or {}
                if isinstance(cur_md, str):
                    try:
                        cur_md = _json.loads(cur_md)
                    except Exception:
                        cur_md = {}
                cur_md.update({
                    "level":  int(new_level),
                    "staked": int((stone or {}).get("staked_amount", 0)) + int(cost_stable),
                })
                await ctx.db.execute(
                    "UPDATE item_instances SET metadata = $2::jsonb, "
                    "updated_at = NOW() WHERE token_id = $1",
                    str(tok["token_id"]), _json.dumps(cur_md),
                )
        except Exception:
            log.debug(
                "nft stone level metadata sync failed gid=%s uid=%s key=%s",
                ctx.guild_id, ctx.author.id, item_lower, exc_info=True,
            )
        # LP goes to the pool matching the currency used at purchase
        stone_lp_cur = stone.get("lp_currency") or currency
        await _item_lp_add(ctx.db, ctx.guild_id, ctx.author.id, stone_lp_cur, cost_stable)
        await _drip_lp_all_pools(ctx.db, ctx.guild_id, int(cost_stable * 0.05))
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "STONE_LEVELUP",
            symbol_in=currency, amount_in=cost_stable,
            network=(lv_network or ""),
        )
        try:
            await self.bot.bus.publish(
                "stone_leveled",
                guild=ctx.guild, user_id=int(ctx.author.id),
                item_type=item_lower, new_level=int(new_level),
                auto=False,
            )
        except Exception:
            log.debug("stone_leveled publish failed gid=%s uid=%s",
                      ctx.guild_id, ctx.author.id, exc_info=True)

        result = card(
            f"{cfg['emoji']} {item_lower.title()} Leveled Up!",
            description=(
                f"**Level {cur_level}** → **Level {new_level}** / {max_lv}"
                + (f"\n{bonus_line}" if bonus_line else "")
            ),
            color=C_SUCCESS,
        ).field("Cost", fmt_token(to_human(cost_stable), currency, lv_emoji), True).field("Level", f"**{new_level}** / {max_lv}", True).field("Staked Total", fmt_token(to_human(new_staked), currency, lv_emoji), True).build()
        await msg.edit(embed=result)


    @commands.hybrid_command(name="autolevelup", aliases=["autolvlup"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def autolevelup(self, ctx: DiscoContext, state: str = "") -> None:
        """Toggle automatic item level-ups when XP is ready and you have funds.

        Usage:
          ,autolevelup         - show current status
          ,autolevelup on      - enable auto level-up
          ,autolevelup off     - disable auto level-up
        """
        state_lower = state.lower()

        if not state_lower:
            row = await ctx.db.fetch_one(
                "SELECT auto_levelup FROM user_settings WHERE user_id=$1 AND guild_id=$2",
                ctx.author.id, ctx.guild_id,
            )
            enabled = bool(row.get("auto_levelup")) if row else False
            status = "**On**" if enabled else "**Off**"
            embed = card(
                "Auto Level-Up",
                description=(
                    f"Current status: {status}\n\n"
                    "When enabled, items automatically level up the moment XP is ready "
                    "and your wallet has sufficient funds. You get a DM when it happens.\n\n"
                    "Toggle: `,autolevelup on` / `,autolevelup off`\n"
                    "DM notifications: `,notify autolevelup on/off`"
                ),
                color=C_SUCCESS if enabled else C_NEUTRAL,
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        if state_lower in ("on", "enable", "1", "true"):
            new_val = True
        elif state_lower in ("off", "disable", "0", "false"):
            new_val = False
        else:
            await ctx.reply_error(f"Unknown state `{state}`. Use `on` or `off`.")
            return

        await ctx.db.execute(
            """INSERT INTO user_settings (user_id, guild_id, auto_levelup)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id, guild_id)
               DO UPDATE SET auto_levelup = $3""",
            ctx.author.id, ctx.guild_id, new_val,
        )

        if new_val:
            await ctx.reply_success(
                "Auto level-up is now **on**. Your items will level up automatically when XP is ready and your wallet has the funds.",
                title="Auto Level-Up Enabled",
            )
        else:
            await ctx.reply_success(
                "Auto level-up is now **off**. You'll still get a DM when an item is ready - use `,inventory levelup <item>` to level up manually.",
                title="Auto Level-Up Disabled",
            )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Shop(bot))
