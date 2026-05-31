"""Group reserve tribute helper.

A single entry point, :func:`tribute_from_activity`, is called from every
productive cashout (fishing, farming, dungeon, crafting). When the user is in
a mining group whose Hall has the matching tribute upgrade purchased, a
*system-funded* grant equal to ``tribute_pct * gross_usd`` is added to the
group's ``reserve_usd`` bucket. The user keeps their full payout -- the grant
is a guild perk, not a tax.

This is the single source of truth for tribute mechanics. Cashout services
must never re-derive tribute logic inline.
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import Config

log = logging.getLogger(__name__)


_TRIBUTE_KEY: dict[str, str] = {
    "fishing":  "tribute_fishing_pct",
    "farming":  "tribute_farming_pct",
    "dungeon":  "tribute_dungeon_pct",
    "crafting": "tribute_crafting_pct",
}

_BONUS_KEY: dict[str, str] = {
    "fishing":  "member_fishing_bonus",
    "farming":  "member_farming_bonus",
    "dungeon":  "member_dungeon_bonus",
    "crafting": "member_crafting_bonus",
}


def _aggregate_effects(purchased_ids: list[str]) -> dict[str, float]:
    """Sum every numeric effect across the group's purchased upgrades.

    Returns a flat dict ``{effect_key: total}`` for keys this module cares
    about: tribute_*_pct, tribute_multiplier, member_*_bonus.
    """
    hall = Config.GROUP_HALL_UPGRADES
    out: dict[str, float] = {}
    for uid in purchased_ids:
        cfg = hall.get(uid)
        if not cfg:
            continue
        for k, v in (cfg.get("effect") or {}).items():
            if k in _TRIBUTE_KEY.values() or k in _BONUS_KEY.values() or k == "tribute_multiplier":
                try:
                    out[k] = out.get(k, 0.0) + float(v)
                except (TypeError, ValueError):
                    continue
    return out


async def member_activity_bonus(
    db: Any, guild_id: int, user_id: int, source: str,
) -> float:
    """Return the multiplicative bonus a group member earns on ``source`` cashouts.

    ``source`` is one of: ``"fishing"``, ``"farming"``, ``"dungeon"``,
    ``"crafting"``. Returns ``0.0`` when the user is not in a group, the
    group has no matching Industry upgrade, or ``source`` is unrecognised.

    The caller is expected to apply the bonus as ``payout * (1.0 + bonus)``.
    """
    bonus_key = _BONUS_KEY.get(source)
    if bonus_key is None:
        return 0.0
    try:
        membership = await db.get_user_mining_group(int(user_id), int(guild_id))
        if not membership:
            return 0.0
        upgrades = await db.get_group_upgrades(int(guild_id), membership["group_id"])
    except Exception:
        log.debug("member_activity_bonus lookup failed", exc_info=True)
        return 0.0
    purchased = [u["upgrade_id"] for u in (upgrades or [])]
    eff = _aggregate_effects(purchased)
    return float(eff.get(bonus_key, 0.0))


async def tribute_from_activity(
    db: Any, guild_id: int, user_id: int, gross_usd: float, source: str,
) -> float:
    """Mint a system-funded tribute into the user's group reserve.

    Called from every cashout-style payout (fishing, farming, dungeon,
    crafting). When the user belongs to a mining group whose Hall has the
    matching ``tribute_<source>_pct`` upgrade purchased, a USD grant of
    ``gross_usd * pct * (1.0 + tribute_multiplier)`` is added to the
    group's ``reserve_usd``. The grant is *not* deducted from the user's
    payout -- it's a guild perk funded by the system.

    Returns the USD value granted (``0.0`` when no group, no upgrade, or
    ``gross_usd <= 0``). Failures are logged but never raise; a tribute
    bookkeeping hiccup must never abort the upstream cashout.
    """
    if gross_usd <= 0:
        return 0.0
    pct_key = _TRIBUTE_KEY.get(source)
    if pct_key is None:
        return 0.0

    try:
        membership = await db.get_user_mining_group(int(user_id), int(guild_id))
        if not membership:
            return 0.0
        group_id = membership["group_id"]
        upgrades = await db.get_group_upgrades(int(guild_id), group_id)
    except Exception:
        log.debug("tribute_from_activity lookup failed", exc_info=True)
        return 0.0

    purchased = [u["upgrade_id"] for u in (upgrades or [])]
    eff = _aggregate_effects(purchased)
    pct = float(eff.get(pct_key, 0.0))
    if pct <= 0:
        return 0.0
    multiplier = 1.0 + float(eff.get("tribute_multiplier", 0.0))
    grant_usd = float(gross_usd) * pct * multiplier
    if grant_usd <= 0:
        return 0.0

    try:
        await db.add_group_reserve_usd(int(guild_id), group_id, grant_usd)
    except Exception:
        log.exception(
            "tribute_from_activity: reserve credit failed gid=%s gid=%s src=%s amt=%.6f",
            guild_id, group_id, source, grant_usd,
        )
        return 0.0
    return grant_usd
