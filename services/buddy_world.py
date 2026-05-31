"""
services/buddy_world.py -- "escaped buddy" world events.

Periodically, a random buddy in the shelter "escapes" and appears in the
guild's bot channel with a public Battle prompt. The first player to win
the PvE fight adopts it for free. If nobody answers the prompt within
the timeout, the buddy slinks back to the shelter unharmed.

Design notes
-------------
* Only shelter buddies whose adoptable_after is in the past are eligible,
  so we never pluck a buddy the former owner is still inside their
  reclaim window on.
* The transition flips ``status`` to 'escaped' atomically. The partial
  index on (guild_id, escaped_at) WHERE status='escaped' lets us list
  currently-live events per guild without scanning the table.
* Reclaim-to-shelter is idempotent: if a Postgres concurrent writer
  already adopted the buddy, UPDATE returns no row and the caller
  moves on.
* All time windows run on the DB clock (NOW() + interval / EXTRACT
  EPOCH) to avoid container clock skew, per CLAUDE.md.
"""
from __future__ import annotations

import logging
from typing import Any

from configs.buddies_config import ADOPT_MOOD

log = logging.getLogger(__name__)


async def pick_escape_candidate(db: Any, guild_id: int) -> dict | None:
    """Return one random adoptable shelter buddy, or None if shelter is empty.

    Only rows that have cleared the adoptable_after grace are eligible;
    buddies whose former owner is still inside their reclaim window stay
    off-limits to the escape system.
    """
    return await db.fetch_one(
        "SELECT id, species, name, level, xp, rarity_tier, "
        "       hunger, happiness, energy, "
        "       former_owner_id "
        "FROM cc_buddies "
        "WHERE guild_id = $1 "
        "  AND status = 'shelter' "
        "  AND (adoptable_after IS NULL OR adoptable_after <= NOW()) "
        "ORDER BY random() "
        "LIMIT 1",
        guild_id,
    )


async def mark_escaped(db: Any, buddy_id: int) -> dict | None:
    """Flip a shelter buddy to 'escaped'. Returns the row or None on race.

    Atomic: if another writer adopted the buddy between pick_escape_candidate
    and this call, the UPDATE returns no row and we abort the event.
    """
    return await db.fetch_one(
        "UPDATE cc_buddies "
        "SET status = 'escaped', "
        "    escaped_at = NOW(), "
        "    updated_at = NOW() "
        "WHERE id = $1 AND status = 'shelter' "
        "RETURNING *",
        buddy_id,
    )


async def reclaim_to_shelter(db: Any, buddy_id: int) -> bool:
    """Return an 'escaped' buddy to the shelter (prompt timed out unclaimed).

    Idempotent: returns False if the buddy is no longer in 'escaped' state
    (e.g. a concurrent adoption already happened and this reclaim is
    stale).
    """
    row = await db.fetch_one(
        "UPDATE cc_buddies "
        "SET status = 'shelter', "
        "    escaped_at = NULL, "
        "    updated_at = NOW() "
        "WHERE id = $1 AND status = 'escaped' "
        "RETURNING id",
        buddy_id,
    )
    return row is not None


async def banish_defeated(db: Any, buddy_id: int) -> bool:
    """Hard-delete an escaped buddy after it lost a fight the winner
    couldn't adopt (usually the winner was at the MAX_OWNED_BUDDIES cap).

    Losing the fight is a final state: the buddy is beaten and doesn't
    get to slink back into the shelter's adoption pool, or the same
    row would keep re-escaping and the winner would face it again on
    the next world-event tick. Idempotent: returns False if the row
    was already adopted / cleaned up concurrently.
    """
    row = await db.fetch_one(
        "DELETE FROM cc_buddies WHERE id = $1 AND status = 'escaped' RETURNING id",
        buddy_id,
    )
    return row is not None


async def adopt_escaped(
    db: Any, guild_id: int, user_id: int, buddy_id: int,
) -> tuple[bool, str, dict | None]:
    """Player defeated the escaped buddy -- grant adoption.

    Mirrors ``services.buddy_lifecycle.try_adopt`` but transitions from
    'escaped' rather than 'shelter'. Returns ``(ok, error, row)``. Row
    is the newly-owned buddy on success.
    """
    count = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
        guild_id, user_id,
    )
    from services.buddy_economy import user_max_battle_slots as _max_battle
    cap = await _max_battle(db, guild_id, user_id)
    if int(count or 0) >= cap:
        return (
            False,
            f"You already have {cap} battle-active buddies. Store one "
            f"or buy a battle slot upgrade before claiming another.",
            None,
        )

    be_active = int(count or 0) == 0
    h, hp, e = ADOPT_MOOD
    try:
        row = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  status             = 'owned', "
            "  owner_user_id      = $2, "
            "  is_active          = $7, "
            "  hunger             = $4, "
            "  happiness          = $5, "
            "  energy             = $6, "
            "  abandoned_at       = NULL, "
            "  abandoned_reason   = NULL, "
            "  adoptable_after    = NULL, "
            "  escaped_at         = NULL, "
            "  last_interacted_at = NOW(), "
            "  last_decay_at      = NOW(), "
            "  updated_at         = NOW() "
            "WHERE guild_id = $1 AND id = $3 AND status = 'escaped' "
            "RETURNING *",
            guild_id, user_id, buddy_id, h, hp, e, be_active,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "cc_buddies_one_active_per_user" in msg or "unique" in msg:
            return False, "Adoption raced -- please try again.", None
        log.exception(
            "adopt_escaped failed gid=%s uid=%s buddy_id=%s",
            guild_id, user_id, buddy_id,
        )
        return False, "Adoption failed. Please try again.", None
    if not row:
        return False, "That buddy escaped to the shelter already.", None
    return True, "", row
