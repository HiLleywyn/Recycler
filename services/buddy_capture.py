"""services/buddy_capture.py -- Arena capture flow.

Captures wild opponents (and rarely, bosses) from `,buddy map battle`
into the player's roster (cc_buddies, status='owned'). Mirrors the
dungeon delve capture path but writes into the arena buddy table
instead of dungeon_party.

Public surface:
    capture_chance(*, is_boss, hp_pct, luck_bonus) -> float
    attempt_arena_capture(db, gid, uid, *, species, level, rarity_tier,
                          is_boss, hp_pct, luck_bonus, zone_id) -> CaptureResult

Boss captures: at most one per zone forever (enforced via
cc_buddy_map_progress.captured_boss_zones).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from configs.buddies_config import boss_variant, roll_gender, xp_for_level

log = logging.getLogger(__name__)


# HP thresholds + chance windows.
WILD_HP_CEILING:   float = 0.20   # arena capture only available when
                                   # opponent HP <= 20% of its max
BOSS_HP_CEILING:   float = 0.05   # bosses must be sub-5%
WILD_BASE_CHANCE:  float = 0.35
WILD_LOW_BONUS:    float = 0.25   # +25% when opponent <= 5%
BOSS_BASE_CHANCE:  float = 0.03
BOSS_LOW_BONUS:    float = 0.05   # +5% when boss <= 2%
BOSS_LOW_THRESH:   float = 0.02
BOSS_MAX_CHANCE:   float = 0.15   # hard ceiling
LUCK_BONUS_RATE:   float = 0.10   # mastery luck.rare_catch contribution


@dataclass(slots=True)
class CaptureResult:
    success:        bool
    reason:         str            # short message; '' on success
    chance:         float          # actual roll target [0, 1]
    species:        str
    level:          int
    rarity_tier:    int
    is_boss_clear:  bool
    buddy_id:       int | None     # newly inserted cc_buddies.id, or None on miss


def capture_chance(
    *, is_boss: bool, hp_pct: float, luck_bonus: float = 0.0,
) -> float:
    """Return the success probability for the current capture attempt.

    Boss captures cap at 15% even with full luck stacks; the boss-as-
    pet fantasy stays prestigious rather than farmable.
    """
    hp = max(0.0, min(1.0, float(hp_pct)))
    luck = max(0.0, float(luck_bonus or 0.0))
    if is_boss:
        if hp > BOSS_HP_CEILING:
            return 0.0
        chance = BOSS_BASE_CHANCE
        if hp <= BOSS_LOW_THRESH:
            chance += BOSS_LOW_BONUS
        if luck > 0:
            chance += LUCK_BONUS_RATE * (luck / max(0.01, LUCK_BONUS_RATE))
        return min(BOSS_MAX_CHANCE, chance)
    if hp > WILD_HP_CEILING:
        return 0.0
    chance = WILD_BASE_CHANCE
    if hp <= 0.05:
        chance += WILD_LOW_BONUS
    chance += LUCK_BONUS_RATE * (luck / max(0.01, LUCK_BONUS_RATE)) * 0.5
    return min(0.85, chance)


async def _boss_already_captured(
    db: Any, gid: int, uid: int, zone_id: str,
) -> bool:
    row = await db.fetch_one(
        "SELECT captured_boss_zones FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    captured = list((row or {}).get("captured_boss_zones") or [])
    return zone_id in captured


async def _mark_boss_captured(
    db: Any, gid: int, uid: int, zone_id: str,
) -> None:
    await db.execute(
        "UPDATE cc_buddy_map_progress "
        "SET captured_boss_zones = "
        "    (SELECT ARRAY(SELECT DISTINCT unnest("
        "       COALESCE(captured_boss_zones, '{}') || ARRAY[$3::text]))) "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), str(zone_id),
    )


async def attempt_arena_capture(
    db: Any, gid: int, uid: int, *,
    species: str, level: int, rarity_tier: int,
    is_boss: bool, hp_pct: float, luck_bonus: float = 0.0,
    zone_id: str = "",
) -> CaptureResult:
    """Try to catch the active opponent. Inserts into cc_buddies on success."""
    sp = str(species or "").strip().lower() or "fox"
    lvl = max(1, int(level or 1))
    tier = max(1, int(rarity_tier or 1))

    # Gate: HP threshold
    chance = capture_chance(
        is_boss=is_boss, hp_pct=float(hp_pct), luck_bonus=float(luck_bonus),
    )
    if chance <= 0.0:
        thresh = BOSS_HP_CEILING if is_boss else WILD_HP_CEILING
        return CaptureResult(
            success=False,
            reason=f"Bring it under {int(thresh * 100)}% HP first.",
            chance=0.0, species=sp, level=lvl, rarity_tier=tier,
            is_boss_clear=False, buddy_id=None,
        )

    # Gate: bosses are one-per-zone forever
    if is_boss and zone_id:
        if await _boss_already_captured(db, int(gid), int(uid), str(zone_id)):
            return CaptureResult(
                success=False,
                reason="You've already tamed this boss -- it's one per zone.",
                chance=0.0, species=sp, level=lvl, rarity_tier=tier,
                is_boss_clear=False, buddy_id=None,
            )

    success = random.random() < chance
    if not success:
        return CaptureResult(
            success=False,
            reason=f"It thrashed free ({int(chance * 100)}% chance).",
            chance=chance, species=sp, level=lvl, rarity_tier=tier,
            is_boss_clear=False, buddy_id=None,
        )

    # Insert into cc_buddies. Bosses use the BOSS_VARIANTS display name
    # ("Meadow King") and stamp boss_zone_id on the row so the renderer
    # + battle engine can swap in the unique overlay + ability later
    # even if the player renames the buddy.
    #
    # The INSERT is wrapped in a defensive fallback: if migration 0263
    # hasn't been applied yet (column doesn't exist), drop the
    # boss_zone_id column from the INSERT and follow up with a separate
    # UPDATE that's allowed to fail. This way captures never silently
    # disappear just because the schema is one step behind the code.
    variant = boss_variant(zone_id) if is_boss else {}
    if is_boss and variant:
        name = str(variant.get("display_name") or f"Boss {sp.title()}")
    else:
        name = f"Wild {sp.title()}"
    bzid_value = str(zone_id) if (is_boss and zone_id) else None
    new_id: int = 0
    try:
        row = await db.fetch_one(
            """
            INSERT INTO cc_buddies
                (guild_id, owner_user_id, species, name,
                 status, is_active, rarity_tier, level, xp, gender,
                 boss_zone_id)
            VALUES ($1, $2, $3, $4, 'stored', FALSE, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            int(gid), int(uid),
            sp, name,
            int(tier), int(lvl), int(xp_for_level(lvl)),
            roll_gender(),
            bzid_value,
        )
        new_id = int((row or {}).get("id") or 0)
    except Exception as exc:
        # Falls here when boss_zone_id column doesn't exist yet (mig 0263
        # not applied) or any other unexpected INSERT-shape mismatch.
        # Retry without the boss-specific column so the buddy is at
        # least saved; cosmetic + ability override will turn back on
        # automatically once migration 0263 runs (the UPDATE below
        # also tries to backfill).
        log.warning("buddy_capture: full INSERT failed (%s); falling back", exc)
        row = await db.fetch_one(
            """
            INSERT INTO cc_buddies
                (guild_id, owner_user_id, species, name,
                 status, is_active, rarity_tier, level, xp, gender)
            VALUES ($1, $2, $3, $4, 'stored', FALSE, $5, $6, $7, $8)
            RETURNING id
            """,
            int(gid), int(uid),
            sp, name,
            int(tier), int(lvl), int(xp_for_level(lvl)),
            roll_gender(),
        )
        new_id = int((row or {}).get("id") or 0)
        if is_boss and bzid_value and new_id:
            try:
                await db.execute(
                    "UPDATE cc_buddies SET boss_zone_id = $1 WHERE id = $2",
                    bzid_value, new_id,
                )
            except Exception:
                log.debug(
                    "buddy_capture: boss_zone_id backfill skipped "
                    "(migration 0263 not applied)",
                )

    if is_boss and zone_id:
        await _mark_boss_captured(db, int(gid), int(uid), str(zone_id))

    return CaptureResult(
        success=True, reason="",
        chance=chance, species=sp, level=lvl, rarity_tier=tier,
        is_boss_clear=False, buddy_id=new_id,
    )
