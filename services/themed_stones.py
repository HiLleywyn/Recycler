"""
services/themed_stones.py  -  XP grants + stat lookups for themed leveled stones.

Five stones earn XP from the five minigame surfaces:
    Tidestone   -- fishing
    Heartstone  -- buddy companionship
    Cryptstone  -- dungeon delving
    Bloodstone  -- buddy battles
    Bloomstone  -- farming

Each stone follows the same row shape (level, xp, staked_amount, lp_currency,
acquired_at) and reuses the generic CRUD on database/users.py. This module
adds the small business-logic shim each callsite needs:
    * grant_*_xp(...)  -- compute XP from an activity metric and credit it
    * stat_bonus(...)  -- look up the current stat-bonus % from level

Callsites pass the activity metric (catches landed, chat ticks, ore mined,
battle round count, ...) and this module reads the per-stone xp_per_*
constants out of items_config so a config tweak doesn't need a code change.
Failures are logged and swallowed -- a bookkeeping miss must never abort
the upstream action.
"""
from __future__ import annotations

import logging
from typing import Any

from configs.items_config import SHOP_ITEMS as _ITEMS

log = logging.getLogger(__name__)


# Shop keys, one per themed stone. Public so callers can be explicit.
TIDESTONE   = "tidestone"
HEARTSTONE  = "heartstone"
CRYPTSTONE  = "cryptstone"
BLOODSTONE  = "bloodstone"
BLOOMSTONE  = "bloomstone"
# Meta-economy stones (cross-cutting bot systems, USD-priced).
GAVELSTONE    = "gavelstone"     # auction house
ANVILSTONE    = "anvilstone"     # crafting
CHIMERASTONE  = "chimerastone"   # AMM swaps


def _meta(key: str) -> dict:
    return _ITEMS.get(key) or {}


def _xp_constant(key: str, name: str, default: float = 0.0) -> float:
    return float(_meta(key).get(name) or default)


def _stat(key: str, name: str) -> float:
    """Per-level stat value declared in items_config."""
    return float((_meta(key).get("stats") or {}).get(name) or 0.0)


async def _grant(
    db: Any, user_id: int, guild_id: int, key: str, xp: float,
    *, bot: Any = None, guild: Any = None,
) -> None:
    """Add ``xp`` to the user's stone of type ``key``. Best-effort.

    When ``bot`` and ``guild`` are passed, also runs the standard
    ``cogs.shop.notify_item_levelup_ready`` so the themed stones
    participate in the same auto-levelup + ready-DM machinery the
    legacy stones (hashstone / lockstone / vaultstone / liqstone)
    already use. Without this hook the four themed stones (Tidestone /
    Heartstone / Cryptstone / Bloodstone) silently sat at the same
    level forever even when ``,autolevelup on`` was enabled and the
    user had funds.
    """
    if xp <= 0:
        return
    try:
        getter = getattr(db, f"get_{key}", None)
        adder  = getattr(db, f"add_{key}_xp", None)
        updater = getattr(db, f"update_{key}_xp", None)
        if getter is None or adder is None:
            return
        row = await getter(user_id, guild_id)
        if not row:
            return
        item_cfg = _meta(key)
        max_lvl = int(item_cfg.get("max_level") or 100)
        if int(row.get("level") or 1) >= max_lvl:
            return
        old_xp = float(row.get("xp") or 0.0)
        result = await adder(user_id, guild_id, float(xp))
        # ``add_<stone>_xp`` returns (live_xp, live_level) -- use it to
        # call notify_item_levelup_ready on the fresh values. If the
        # adder didn't return a tuple (older signatures, mock DBs),
        # fall back to recomputing from the pre-grant row.
        if isinstance(result, tuple) and len(result) >= 2:
            live_xp, live_level = float(result[0]), int(result[1])
        else:
            live_xp = old_xp + float(xp)
            live_level = int(row.get("level") or 1)
        # Cap XP at the next-level threshold so themed stones obey the
        # same "you must pay to level up before banking more XP" rule
        # the legacy stones (hash/lock/vault/liq) enforce inline. Without
        # this, plant/harvest/cast/chat ticks bank unbounded XP past the
        # threshold so a single levelup pay-out instantly chains many
        # levels (the "outrageously fast" behaviour players were seeing
        # on bloomstone / heartstone / tidestone in particular).
        from cogs.shop import cap_xp as _cap_xp
        capped = _cap_xp(live_xp, live_level, item_cfg)
        if capped < live_xp and updater is not None:
            try:
                await updater(user_id, guild_id, capped, live_level)
                live_xp = capped
            except Exception:
                log.debug(
                    "themed_stones: cap_xp write %s uid=%s gid=%s failed",
                    key, user_id, guild_id, exc_info=True,
                )
        if bot is not None and guild is not None:
            try:
                from cogs.shop import notify_item_levelup_ready
                staked = float(row.get("staked_amount") or 0)
                await notify_item_levelup_ready(
                    bot, int(user_id), guild, key,
                    old_xp, live_xp, live_level, staked,
                )
            except Exception:
                log.debug(
                    "themed_stones: notify %s uid=%s gid=%s failed",
                    key, user_id, guild_id, exc_info=True,
                )
    except Exception:
        log.debug(
            "themed_stones: grant %s xp=%.2f uid=%s gid=%s failed",
            key, xp, user_id, guild_id, exc_info=True,
        )


# ── Per-stone grant helpers ────────────────────────────────────────────────

async def grant_tidestone_xp(
    db: Any, user_id: int, guild_id: int, *,
    landed: bool = False, legendary: bool = False, combo: int = 0,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Tidestone XP for a fishing cast outcome.

    Pass ``bot`` and ``guild`` to opt the user into auto-levelup +
    "ready" DM. Callers without a bot reference (pure service-layer
    code) will skip the notify hook silently.
    """
    xp = 0.0
    if landed:
        xp += _xp_constant(TIDESTONE, "xp_per_cast", 20.0)
    if legendary:
        xp += _xp_constant(TIDESTONE, "xp_per_legendary", 400.0)
    if combo > 1:
        xp += _xp_constant(TIDESTONE, "xp_per_combo", 4.0) * combo
    await _grant(db, user_id, guild_id, TIDESTONE, xp, bot=bot, guild=guild)


async def grant_heartstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    chat_ticks: int = 0, fed: bool = False, leveled_up: bool = False,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Heartstone XP for buddy interactions."""
    xp = 0.0
    if chat_ticks > 0:
        xp += _xp_constant(HEARTSTONE, "xp_per_chat", 6.0) * chat_ticks
    if fed:
        xp += _xp_constant(HEARTSTONE, "xp_per_feed", 20.0)
    if leveled_up:
        xp += _xp_constant(HEARTSTONE, "xp_per_levelup", 150.0)
    await _grant(db, user_id, guild_id, HEARTSTONE, xp, bot=bot, guild=guild)


async def grant_cryptstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    kills: int = 0, captures: int = 0, mines: int = 0, bosses: int = 0,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Cryptstone XP for dungeon outcomes."""
    xp = 0.0
    if kills > 0:
        xp += _xp_constant(CRYPTSTONE, "xp_per_kill", 25.0) * kills
    if captures > 0:
        xp += _xp_constant(CRYPTSTONE, "xp_per_capture", 45.0) * captures
    if mines > 0:
        xp += _xp_constant(CRYPTSTONE, "xp_per_mine", 15.0) * mines
    if bosses > 0:
        xp += _xp_constant(CRYPTSTONE, "xp_per_boss", 500.0) * bosses
    await _grant(db, user_id, guild_id, CRYPTSTONE, xp, bot=bot, guild=guild)


async def grant_bloodstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    rounds: int = 0, won: bool = False, lost: bool = False,
    capture_battle: bool = False,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Bloodstone XP for a finished buddy battle."""
    xp = 0.0
    if rounds > 0:
        xp += _xp_constant(BLOODSTONE, "xp_per_battle_round", 5.0) * rounds
    if won:
        xp += _xp_constant(BLOODSTONE, "xp_per_battle_win", 200.0)
    elif lost:
        xp += _xp_constant(BLOODSTONE, "xp_per_battle_loss", 40.0)
    if capture_battle:
        xp += _xp_constant(BLOODSTONE, "xp_per_capture_battle", 80.0)
    await _grant(db, user_id, guild_id, BLOODSTONE, xp)


async def grant_bloomstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    planted: int = 0, harvested: int = 0,
    legendary: bool = False, recipe: int = 0, pest_kills: int = 0,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Bloomstone XP for farming actions.

    ``planted``      -- count of seed packets planted in this call (1 per slot)
    ``harvested``    -- count of plots harvested in this call
    ``legendary``    -- True if any of the harvested crops were legendary
    ``recipe``       -- count of recipes processed (bread, jam, ambrosia, etc.)
    ``pest_kills``   -- count of pests slain on the player's tiles
    """
    xp = 0.0
    if planted > 0:
        xp += _xp_constant(BLOOMSTONE, "xp_per_plant", 5.0) * planted
    if harvested > 0:
        xp += _xp_constant(BLOOMSTONE, "xp_per_harvest", 20.0) * harvested
    if legendary:
        xp += _xp_constant(BLOOMSTONE, "xp_per_legendary", 400.0)
    if recipe > 0:
        xp += _xp_constant(BLOOMSTONE, "xp_per_recipe", 50.0) * recipe
    if pest_kills > 0:
        xp += _xp_constant(BLOOMSTONE, "xp_per_pest_kill", 15.0) * pest_kills
    await _grant(db, user_id, guild_id, BLOOMSTONE, xp, bot=bot, guild=guild)


# ── Stat-bonus lookups ─────────────────────────────────────────────────────

async def _bonus(
    db: Any, user_id: int, guild_id: int, key: str, stat_name: str,
) -> float:
    """Return the current bonus multiplier (e.g. 0.15 for +15%) for the
    named stat on the user's stone of type ``key``. Returns 0.0 when the
    user doesn't own the stone or the stone is at level 0/no level row."""
    try:
        getter = getattr(db, f"get_{key}", None)
        if getter is None:
            return 0.0
        row = await getter(user_id, guild_id)
        if not row:
            return 0.0
        level = max(1, int(row.get("level") or 1))
        return _stat(key, stat_name) * level
    except Exception:
        log.debug(
            "themed_stones: bonus %s.%s uid=%s gid=%s failed",
            key, stat_name, user_id, guild_id, exc_info=True,
        )
        return 0.0


async def tidestone_payout_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, TIDESTONE, "fish_payout_bonus")


async def heartstone_xp_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, HEARTSTONE, "buddy_xp_bonus")


async def heartstone_decay_resist(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, HEARTSTONE, "buddy_decay_resist")


async def cryptstone_mine_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, CRYPTSTONE, "dungeon_mine_bonus")


async def cryptstone_atk_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, CRYPTSTONE, "dungeon_atk_bonus")


async def cryptstone_capture_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, CRYPTSTONE, "dungeon_capture_bonus")


async def bloodstone_atk_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, BLOODSTONE, "battle_atk_bonus")


async def bloodstone_hp_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, BLOODSTONE, "battle_hp_bonus")


async def bloodstone_prize_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, BLOODSTONE, "battle_prize_bonus")


async def bloomstone_yield_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, BLOOMSTONE, "farm_yield_bonus")


async def bloomstone_seed_drop_bonus(db: Any, user_id: int, guild_id: int) -> float:
    return await _bonus(db, user_id, guild_id, BLOOMSTONE, "farm_seed_drop_bonus")


# ── Meta-economy stones (AH / crafting / swap) ────────────────────────────

async def grant_gavelstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    bought: bool = False, sold: bool = False,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Gavelstone XP for an AH outcome.

    ``bought`` -- the user just bought a listing (one grant per buy).
    ``sold``   -- one of the user's listings just settled (one per sale).
    Listing creation is intentionally NOT a grant -- otherwise spam-
    listing would farm XP.
    """
    xp = 0.0
    if bought:
        xp += _xp_constant(GAVELSTONE, "xp_per_buy", 20.0)
    if sold:
        xp += _xp_constant(GAVELSTONE, "xp_per_sale", 20.0)
    await _grant(db, user_id, guild_id, GAVELSTONE, xp, bot=bot, guild=guild)


async def grant_anvilstone_xp(
    db: Any, user_id: int, guild_id: int, *,
    crafted: bool = False,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Anvilstone XP for one ,craft action (any qty)."""
    xp = 0.0
    if crafted:
        xp += _xp_constant(ANVILSTONE, "xp_per_craft", 30.0)
    await _grant(db, user_id, guild_id, ANVILSTONE, xp, bot=bot, guild=guild)


async def grant_chimerastone_xp(
    db: Any, user_id: int, guild_id: int, *,
    swapped: bool = False,
    bot: Any = None, guild: Any = None,
) -> None:
    """Grant Chimerastone XP for one ,swap (or ,trade swap) action."""
    xp = 0.0
    if swapped:
        xp += _xp_constant(CHIMERASTONE, "xp_per_swap", 25.0)
    await _grant(db, user_id, guild_id, CHIMERASTONE, xp, bot=bot, guild=guild)


async def gavelstone_buyer_rebate(db: Any, user_id: int, guild_id: int) -> float:
    """Per-level rebate fraction the buyer earns on every AH purchase."""
    return await _bonus(db, user_id, guild_id, GAVELSTONE, "ah_buyer_rebate")


async def gavelstone_seller_bonus(db: Any, user_id: int, guild_id: int) -> float:
    """Per-level bonus fraction the seller earns on every settled sale."""
    return await _bonus(db, user_id, guild_id, GAVELSTONE, "ah_seller_bonus")


async def anvilstone_yield_bonus(db: Any, user_id: int, guild_id: int) -> float:
    """Per-level fractional bonus to crafted-item output qty."""
    return await _bonus(db, user_id, guild_id, ANVILSTONE, "craft_yield_bonus")


async def anvilstone_xp_bonus(db: Any, user_id: int, guild_id: int) -> float:
    """Per-level fractional bonus to crafting-skill XP."""
    return await _bonus(db, user_id, guild_id, ANVILSTONE, "craft_xp_bonus")


async def chimerastone_swap_fee_bonus(db: Any, user_id: int, guild_id: int) -> float:
    """Per-level fractional swap-fee discount, stacks on top of Liqstone."""
    return await _bonus(db, user_id, guild_id, CHIMERASTONE, "swap_fee_bonus")


__all__ = (
    "TIDESTONE", "HEARTSTONE", "CRYPTSTONE", "BLOODSTONE", "BLOOMSTONE",
    "GAVELSTONE", "ANVILSTONE", "CHIMERASTONE",
    "grant_tidestone_xp", "grant_heartstone_xp",
    "grant_cryptstone_xp", "grant_bloodstone_xp",
    "grant_bloomstone_xp",
    "grant_gavelstone_xp", "grant_anvilstone_xp", "grant_chimerastone_xp",
    "tidestone_payout_bonus",
    "heartstone_xp_bonus", "heartstone_decay_resist",
    "cryptstone_mine_bonus", "cryptstone_atk_bonus", "cryptstone_capture_bonus",
    "bloodstone_atk_bonus", "bloodstone_hp_bonus", "bloodstone_prize_bonus",
    "bloomstone_yield_bonus", "bloomstone_seed_drop_bonus",
    "gavelstone_buyer_rebate", "gavelstone_seller_bonus",
    "anvilstone_yield_bonus", "anvilstone_xp_bonus",
    "chimerastone_swap_fee_bonus",
)
