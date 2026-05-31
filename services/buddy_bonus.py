"""
services/buddy_bonus.py  -  Single source of truth for buddy-powered multipliers.

Every host feature that gets a buddy buff (chat XP, mining payout, trade
fees, faucet claim) calls ``buddy_bonus()`` from here. Never re-derive the
formula anywhere else -- if the numbers change they change in this file.

Formula:
    pct = per_level * rarity_bonus_mult * level
    pct = BONUS_SIG_PER_LEVEL when lane matches the species' signature lane,
          BONUS_OFF_PER_LEVEL otherwise.

So a fresh Common buddy grants a small but nonzero boost from level 1, a
mid-level Uncommon buddy grants a visible multi-percent boost in its
signature lane, and a max-level Legendary buddy in its signature lane caps
out at a large, noticeable bonus. Mood-broken buddies (hunger == 0 OR
happiness == 0) only get half the bonus they would otherwise receive.

Only the user's ACTIVE buddy contributes. A player with multiple owned
buddies picks their active via the buddy panel; the others are effectively
in storage and do not affect any multiplier.

A 30-second in-process cache keyed on (guild_id, user_id, lane) keeps the
hot path (chat XP on every message) from hammering the DB. The cache
self-invalidates via TTL, so no explicit cleanup is needed after hatch /
adopt / surrender / swap / set-active.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from configs.buddies_config import (
    BONUS_LANES,
    BONUS_OFF_PER_LEVEL,
    BONUS_SIG_PER_LEVEL,
    MAX_LEVEL,
    MOOD_PENALTY_MULTIPLIER,
    buddy_bonus_lanes_for,
    rarity_meta,
)

log = logging.getLogger(__name__)

# (guild_id, user_id, lane) -> (expires_at_epoch, multiplier)
_cache: dict[tuple[int, int, str], tuple[float, float]] = {}
_CACHE_TTL_S: float = 30.0


async def buddy_bonus(
    db: Any, guild_id: int, user_id: int, *, lane: str | None = None,
) -> float:
    """Return the buddy bonus multiplier for one user's active buddy.

    Callers should treat the return value as a pure scalar:

        amount = int(round(base_amount * await buddy_bonus(db, gid, uid, lane="chat")))

    For fee rebates, use ``1.0 / multiplier`` instead -- a 10% buddy buff
    becomes a ~9.1% fee rebate, which is the right direction.

    ``lane`` must be one of ``BONUS_LANES`` or ``None``. ``None`` uses the
    off-lane per-level rate with no specialty bump (safe default for
    legacy callers that haven't been updated yet).

    Returns 1.0 on any error so a buddy subsystem failure cannot break
    the host feature that asked.
    """
    if lane is not None and lane not in BONUS_LANES:
        log.debug("buddy_bonus: unknown lane %r, falling back to off-lane", lane)
        lane = None

    now = time.time()
    key = (guild_id, user_id, lane or "")
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    try:
        # LEFT JOIN excludes any buddy currently on a running expedition.
        # While the buddy is away, return 1.0 (no bonus) so the player
        # can't farm passive bonuses across every cog from a deployed
        # buddy. The cog-side guards refuse direct interactions; this
        # path is the silent no-op for chat / work / fish / farm / delve.
        row = await db.fetch_one(
            """
            SELECT b.species, b.level, b.hunger, b.happiness, b.rarity_tier
              FROM cc_buddies b
              LEFT JOIN buddy_expeditions e
                     ON e.buddy_id = b.id
                    AND e.status   = 'running'
             WHERE b.guild_id = $1 AND b.owner_user_id = $2
               AND b.status = 'owned' AND b.is_active
               AND e.expedition_id IS NULL
             LIMIT 1
            """,
            guild_id, user_id,
        )
    except Exception:
        log.debug(
            "buddy_bonus: lookup failed gid=%s uid=%s", guild_id, user_id,
            exc_info=True,
        )
        _cache[key] = (now + _CACHE_TTL_S, 1.0)
        return 1.0

    if not row:
        _cache[key] = (now + _CACHE_TTL_S, 1.0)
        return 1.0

    species   = str(row.get("species") or "")
    level     = int(row.get("level") or 1)
    hunger    = int(row.get("hunger") or 0)
    happiness = int(row.get("happiness") or 0)
    tier      = int(row.get("rarity_tier") or 1)

    # Signature lanes = species primary PLUS rarity-granted extras (Rare
    # gets +1, Epic +2, Legendary +3 -- chosen deterministically off the
    # BONUS_LANES rotation so a Legendary Zenny always buffs the same
    # four lanes for every player). Any of those lanes get the fast
    # ramp; everything else gets the slow off-lane ramp.
    sig_lanes = set(buddy_bonus_lanes_for(species, tier))
    per_level = (
        BONUS_SIG_PER_LEVEL
        if lane is not None and lane in sig_lanes
        else BONUS_OFF_PER_LEVEL
    )

    # Scale by rarity and clamp level to the cap so no one overshoots.
    level = max(1, min(MAX_LEVEL, level))
    bonus_mult = float(rarity_meta(tier).get("bonus_mult", 1.0))
    bonus = per_level * bonus_mult * level

    if hunger == 0 or happiness == 0:
        bonus *= MOOD_PENALTY_MULTIPLIER

    multiplier = 1.0 + bonus
    _cache[key] = (now + _CACHE_TTL_S, multiplier)
    return multiplier
