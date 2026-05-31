"""
services/buddy_lifecycle.py  -  Shelter + adoption flow for CC Buddies.

All time comparisons run on the DB clock (EXTRACT(EPOCH FROM NOW() - ts))
per the project's DB-side-clocks rule. Python-side comparisons against
Postgres timestamps are never used here.

Public API:
    to_shelter(db, gid, uid, reason, *, buddy_id=None) -> list[dict]
    list_shelter(db, gid, limit, offset)               -> list[dict]
    count_shelter(db, gid)                             -> int
    try_adopt(db, gid, uid, buddy_id)                  -> tuple[bool, str, dict | None]
    try_reclaim(db, gid, uid)                          -> tuple[bool, str, dict | None]
    set_active_buddy(db, gid, uid, buddy_id)           -> tuple[bool, str, dict | None]
    sweep_decay(db)                                    -> None           (background task)
    sweep_runaway(db)                                  -> list[dict]    (freshly run-away)
"""
from __future__ import annotations

import logging
from typing import Any

from configs.buddies_config import (
    ADOPT_MOOD,
    ENERGY_DECAY_PER_HOUR,
    ENERGY_REGEN_PER_HOUR,
    ENERGY_REGEN_THRESHOLD,
    HAPPINESS_DECAY_PER_HOUR,
    HUNGER_DECAY_PER_HOUR,
    RARITY_TIERS,
    RECLAIM_MOOD,
    RUNAWAY_IDLE_HOURS,
    SHELTER_GRACE_HOURS,
)

log = logging.getLogger(__name__)


# =============================================================================
# Shelter transition
# =============================================================================

# reason -> adoptable_after grace in hours. Surrender / runaway are
# adoptable immediately; leave / ban give the former owner a reclaim window.
_GRACE_HOURS_BY_REASON: dict[str, int] = {
    "surrendered": 0,
    "ran_away":    0,
    "left_guild":  SHELTER_GRACE_HOURS,
    "banned":      SHELTER_GRACE_HOURS,
}


async def to_shelter(
    db: Any, guild_id: int, user_id: int, reason: str,
    *, buddy_id: int | None = None, display_name: str | None = None,
) -> list[dict]:
    """Move one or all of the user's owned buddies to the shelter.

    ``buddy_id``:
        - ``None``   -- move ALL owned buddies (used for guild leave / ban,
                        where the user is gone and should not keep pets).
        - specific id -- move just that one (used for explicit surrender).

    ``display_name`` is the leaving user's display name at transition time
    (e.g. Discord member.display_name). Stored in the closed
    ``previous_owners`` entry so the AI can name-check ex-owners even
    after they leave / are banned. Pass ``None`` when unknown.

    Returns the list of rows that actually transitioned. Empty if the user
    owned nothing, or if the id didn't match. Idempotent.
    """
    grace_h = _GRACE_HOURS_BY_REASON.get(reason, SHELTER_GRACE_HOURS)

    # One UPDATE closes out the ownership tenure and appends it to the
    # append-only previous_owners history so both writes land atomically
    # for each row. We rely on hatched_at as a rough lower bound for
    # from_ts when the buddy has no prior history entries, which holds
    # for every buddy created by the standard hatch path.
    def _history_append(dn_param: str) -> str:
        return (
            "previous_owners || jsonb_build_array(jsonb_build_object("
            "  'user_id',      owner_user_id, "
            f" 'display_name', {dn_param}::text, "
            "  'from_ts',      COALESCE("
            "                     (SELECT (elem->>'to_ts')::bigint "
            "                        FROM jsonb_array_elements(previous_owners) elem "
            "                       ORDER BY (elem->>'to_ts')::bigint DESC NULLS LAST "
            "                       LIMIT 1),"
            "                     EXTRACT(EPOCH FROM hatched_at)::bigint,"
            "                     EXTRACT(EPOCH FROM NOW())::bigint"
            "                  ), "
            "  'to_ts',        EXTRACT(EPOCH FROM NOW())::bigint, "
            "  'reason',       $3::text"
            "))"
        )
    try:
        if buddy_id is None:
            # Guild-leave / ban path: also pull nesting buddies (parents
            # in the user's daycare slots) and clear those nest rows so
            # we don't leave orphaned cc_buddy_daycare entries pointing
            # at sheltered cc_buddies. Eggs in those nests are forfeit
            # along with the owner, same as any other deposit.
            await db.execute(
                "DELETE FROM cc_buddy_daycare "
                "WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
            rows = await db.fetch_all(
                f"UPDATE cc_buddies SET "
                f"  status          = 'shelter', "
                f"  former_owner_id = owner_user_id, "
                f"  previous_owners = {_history_append('$5')}, "
                f"  owner_user_id   = NULL, "
                f"  is_active       = FALSE, "
                f"  abandoned_at    = NOW(), "
                f"  abandoned_reason = $3, "
                f"  adoptable_after = NOW() + ($4::int * INTERVAL '1 hour'), "
                f"  updated_at      = NOW() "
                f"WHERE guild_id = $1 AND owner_user_id = $2 "
                f"  AND status IN ('owned', 'nesting') "
                f"RETURNING *",
                guild_id, user_id, reason, grace_h, display_name,
            )
        else:
            rows = await db.fetch_all(
                f"UPDATE cc_buddies SET "
                f"  status          = 'shelter', "
                f"  former_owner_id = owner_user_id, "
                f"  previous_owners = {_history_append('$6')}, "
                f"  owner_user_id   = NULL, "
                f"  is_active       = FALSE, "
                f"  abandoned_at    = NOW(), "
                f"  abandoned_reason = $3, "
                f"  adoptable_after = NOW() + ($4::int * INTERVAL '1 hour'), "
                f"  updated_at      = NOW() "
                f"WHERE guild_id = $1 AND owner_user_id = $2 AND id = $5 "
                f"  AND status = 'owned' "
                f"RETURNING *",
                guild_id, user_id, reason, grace_h, buddy_id, display_name,
            )
    except Exception:
        log.exception(
            "to_shelter failed gid=%s uid=%s reason=%s buddy_id=%s",
            guild_id, user_id, reason, buddy_id,
        )
        return []
    # NFT layer: clear owner_user_id on each surrendered buddy's token
    # (mirrors the cc_buddies UPDATE -- shelter buddies have no owner
    # until adoption flips it back). Best-effort.
    if rows:
        try:
            from services import items as _items
            for r in rows:
                tok = await _items.find_token(
                    db, source_table="cc_buddies",
                    source_id=int(r["id"]),
                )
                if tok:
                    await _items.set_owner(
                        db, str(tok["token_id"]), None,
                    )
        except Exception:
            log.debug(
                "nft to_shelter sync failed gid=%s uid=%s",
                guild_id, user_id, exc_info=True,
            )
    return rows or []


# =============================================================================
# Shelter listing
# =============================================================================

async def list_shelter(
    db: Any, guild_id: int, *, limit: int = 25, offset: int = 0,
) -> list[dict]:
    """Return adoptable shelter buddies, newest abandonment first."""
    return await db.fetch_all(
        "SELECT id, species, name, level, xp, rarity_tier, gender, "
        "       former_owner_id, abandoned_at, abandoned_reason "
        "FROM cc_buddies "
        "WHERE guild_id = $1 AND status = 'shelter' AND adoptable_after <= NOW() "
        "ORDER BY abandoned_at DESC "
        "LIMIT $2 OFFSET $3",
        guild_id, limit, offset,
    )


async def count_shelter(db: Any, guild_id: int) -> int:
    """How many adoptable buddies are in this guild's shelter right now."""
    val = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND status = 'shelter' AND adoptable_after <= NOW()",
        guild_id,
    )
    return int(val or 0)


# =============================================================================
# Adopt
# =============================================================================

async def try_adopt(
    db: Any, guild_id: int, user_id: int, buddy_id: int,
) -> tuple[bool, str, dict | None]:
    """Attempt to adopt a specific shelter buddy.

    Returns (ok, error_message, row). On success, ``row`` is the newly
    owned buddy. Players may own up to ``MAX_OWNED_BUDDIES`` at once; any
    adopted buddy beyond the first is added as INACTIVE (the player must
    explicitly promote it via the panel to move chat XP / decay to it).
    The partial unique index on is_active still guards against promoting
    two buddies into the active slot simultaneously.
    """
    # Pre-flight: how many owned buddies does the user have?
    count = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
        guild_id, user_id,
    )
    # Effective cap = battle base + battle slot upgrades. Adoption goes
    # to the active battle pool only; if battle is full the player has
    # to free a slot or buy another one (storage is for self-managed
    # collection, not the auto-route surface).
    from services.buddy_economy import user_max_battle_slots as _max_battle
    cap = await _max_battle(db, guild_id, user_id)
    if int(count or 0) >= cap:
        return (
            False,
            f"You already have the max of **{cap}** battle-active buddies. "
            f"Store one or buy a battle slot upgrade in `,buddy shop` "
            f"before adopting another.",
            None,
        )

    # The new buddy is active only if the user has no other owned buddy.
    be_active = int(count or 0) == 0

    h, hp, e = ADOPT_MOOD
    try:
        row = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  status        = 'owned', "
            "  owner_user_id = $2, "
            "  is_active     = $7, "
            "  hunger        = $4, "
            "  happiness     = $5, "
            "  energy        = $6, "
            "  abandoned_at  = NULL, "
            "  abandoned_reason = NULL, "
            "  adoptable_after  = NULL, "
            "  last_interacted_at = NOW(), "
            "  last_decay_at = NOW(), "
            "  updated_at    = NOW() "
            "WHERE guild_id = $1 AND id = $3 AND status = 'shelter' "
            "  AND adoptable_after <= NOW() "
            "RETURNING *",
            guild_id, user_id, buddy_id, h, hp, e, be_active,
        )
    except Exception as exc:
        # Unique-index conflict means another adopter beat this one.
        msg = str(exc).lower()
        if "cc_buddies_one_active_per_user" in msg or "unique" in msg:
            return False, "Adoption failed -- a concurrent buddy promotion raced this one. Try again.", None
        log.exception("try_adopt failed gid=%s uid=%s buddy_id=%s", guild_id, user_id, buddy_id)
        return False, "Adoption failed. Please try again.", None

    if not row:
        return False, "That buddy is not available for adoption.", None
    # NFT layer: transfer the buddy token to the new owner. Best-effort.
    try:
        from services import items as _items
        tok = await _items.find_token(
            db, source_table="cc_buddies", source_id=int(row["id"]),
        )
        if tok:
            await _items.transfer(db, str(tok["token_id"]), int(user_id))
    except Exception:
        log.debug(
            "nft adopt sync failed gid=%s uid=%s buddy=%s",
            guild_id, user_id, row.get("id"), exc_info=True,
        )
    return True, "", row


# =============================================================================
# Reclaim
# =============================================================================

async def try_reclaim(
    db: Any, guild_id: int, user_id: int,
) -> tuple[bool, str, dict | None]:
    """Reclaim a buddy the caller used to own, while still within grace.

    Multi-pet aware: if the caller's owned slot is empty the reclaimed
    buddy comes back ACTIVE; if they already own others the reclaimed
    buddy slots in inactive (they can promote it from the panel). Still
    rejected if they're already at the owned cap.
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
            f"You already have the max of **{cap}** battle-active buddies. "
            f"Store one or buy a battle slot upgrade in `,buddy shop` "
            f"before reclaiming another.",
            None,
        )
    be_active = int(count or 0) == 0

    # Grace: adoptable_after is strictly in the future for leave/ban rows.
    h, hp, e = RECLAIM_MOOD
    try:
        row = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  status        = 'owned', "
            "  owner_user_id = $2, "
            "  is_active     = $6, "
            "  hunger        = $3, "
            "  happiness     = $4, "
            "  energy        = $5, "
            "  abandoned_at  = NULL, "
            "  abandoned_reason = NULL, "
            "  adoptable_after  = NULL, "
            "  last_interacted_at = NOW(), "
            "  last_decay_at = NOW(), "
            "  updated_at    = NOW() "
            "WHERE guild_id = $1 AND former_owner_id = $2 AND status = 'shelter' "
            "  AND adoptable_after > NOW() "
            "RETURNING *",
            guild_id, user_id, h, hp, e, be_active,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "cc_buddies_one_active_per_user" in msg or "unique" in msg:
            return False, "Reclaim failed -- another promotion raced this one. Try again.", None
        log.exception("try_reclaim failed gid=%s uid=%s", guild_id, user_id)
        return False, "Reclaim failed. Please try again.", None

    if not row:
        return False, "No buddy is available for you to reclaim. Reclaim is only possible within 24 hours of leaving or being banned.", None
    # NFT layer: transfer the buddy token back to the reclaiming user.
    # Best-effort.
    try:
        from services import items as _items
        tok = await _items.find_token(
            db, source_table="cc_buddies", source_id=int(row["id"]),
        )
        if tok:
            await _items.transfer(db, str(tok["token_id"]), int(user_id))
    except Exception:
        log.debug(
            "nft reclaim sync failed gid=%s uid=%s buddy=%s",
            guild_id, user_id, row.get("id"), exc_info=True,
        )
    return True, "", row


# =============================================================================
# Active-buddy selection
# =============================================================================

async def set_active_buddy(
    db: Any, guild_id: int, user_id: int, buddy_id: int,
) -> tuple[bool, str, dict | None]:
    """Promote ``buddy_id`` to be the user's active buddy.

    Demotes whatever else they had active in the same guild. Runs inside
    a transaction so the demote + promote happens atomically against the
    ``cc_buddies_one_active_per_user`` partial unique index.

    Returns (ok, error_message, new_active_row).
    """
    try:
        async with db.transaction() as conn:
            # Verify the target actually belongs to the caller before we flip anything.
            # for_sale buddies are soft-locked: they can't be active or fight
            # while listed on the marketplace. Caller must delist first.
            target = await conn.fetchrow(
                "SELECT id, for_sale FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 AND id = $3 "
                "  AND status = 'owned' "
                "FOR UPDATE",
                guild_id, user_id, buddy_id,
            )
            if not target:
                return False, "That buddy isn't yours.", None
            if bool(target.get("for_sale")):
                return False, (
                    "That buddy is listed on the market and can't be "
                    "active. Cancel the listing first with "
                    "`,buddy delist <listing_id>`."
                ), None
            # Demote all of this user's buddies first (there should be at
            # most one, but we clear across the set to guarantee the partial
            # unique index never trips).
            await conn.execute(
                "UPDATE cc_buddies SET is_active = FALSE, updated_at = NOW() "
                "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned' "
                "  AND is_active",
                guild_id, user_id,
            )
            row = await conn.fetchrow(
                "UPDATE cc_buddies SET "
                "  is_active = TRUE, last_decay_at = NOW(), "
                "  last_interacted_at = NOW(), updated_at = NOW() "
                "WHERE id = $1 "
                "RETURNING *",
                buddy_id,
            )
    except Exception as exc:
        msg = str(exc).lower()
        if "cc_buddies_one_active_per_user" in msg or "unique" in msg:
            return False, "Set-active raced another promotion. Try again.", None
        log.exception(
            "set_active_buddy failed gid=%s uid=%s buddy_id=%s",
            guild_id, user_id, buddy_id,
        )
        return False, "Set-active failed. Please try again.", None

    return True, "", dict(row) if row else None


# =============================================================================
# Decay sweep (background task)
# =============================================================================

# Rarity-scaled decay / regen. Rarer tiers decay slower and regenerate
# faster; the CASE expressions are baked from RARITY_TIERS at import time
# so the dict stays the single source of truth.
_DECAY_MULT_CASE = " ".join(
    f"WHEN {tier} THEN {meta['decay_mult']:.4f}"
    for tier, meta in RARITY_TIERS.items()
)
_REGEN_MULT_CASE = " ".join(
    f"WHEN {tier} THEN {meta['regen_mult']:.4f}"
    for tier, meta in RARITY_TIERS.items()
)


async def sweep_decay(db: Any) -> None:
    """Advance mood decay by whole hours for every ACTIVE owned buddy.

    Inactive buddies (multi-pet collection members not currently selected)
    are frozen -- no decay, no regen -- per the "only one buddy of your
    choosing moves stats at a time" rule.

    Fractional leftover hours stay on ``last_decay_at`` so the next sweep
    picks them up. The WHERE clause skips rows that haven't been waiting
    for at least an hour, so the sweep is cheap even for large collections.

    Rarity scales the per-hour step: a Common buddy decays at the nominal
    rate; a Legendary one decays at ~55% of that and regenerates ~1.75x.

    Energy regenerates (instead of decaying) when hunger and happiness are
    both above ENERGY_REGEN_THRESHOLD -- a well-cared-for active buddy
    naps back toward full. The CASE reads the pre-update hunger/happiness
    values (Postgres evaluates SET-clause column refs against the OLD row).
    """
    try:
        await db.execute(
            f"""
            UPDATE cc_buddies SET
                hunger = GREATEST(0, hunger - ROUND(
                    FLOOR(EXTRACT(EPOCH FROM (NOW() - last_decay_at)) / 3600.0)
                    * $1
                    * (CASE rarity_tier {_DECAY_MULT_CASE} ELSE 1.0 END)
                )::int),
                happiness = GREATEST(0, happiness - ROUND(
                    FLOOR(EXTRACT(EPOCH FROM (NOW() - last_decay_at)) / 3600.0)
                    * $2
                    * (CASE rarity_tier {_DECAY_MULT_CASE} ELSE 1.0 END)
                )::int),
                energy = GREATEST(0, LEAST(100,
                    CASE
                        WHEN hunger >= $5 AND happiness >= $5 THEN energy + ROUND(
                            FLOOR(EXTRACT(EPOCH FROM (NOW() - last_decay_at)) / 3600.0)
                            * $4
                            * (CASE rarity_tier {_REGEN_MULT_CASE} ELSE 1.0 END)
                        )::int
                        ELSE energy - ROUND(
                            FLOOR(EXTRACT(EPOCH FROM (NOW() - last_decay_at)) / 3600.0)
                            * $3
                            * (CASE rarity_tier {_DECAY_MULT_CASE} ELSE 1.0 END)
                        )::int
                    END
                )),
                last_decay_at = last_decay_at
                    + FLOOR(EXTRACT(EPOCH FROM (NOW() - last_decay_at)) / 3600.0) * INTERVAL '1 hour',
                updated_at = NOW()
            WHERE status = 'owned' AND is_active
              AND EXTRACT(EPOCH FROM (NOW() - last_decay_at)) >= 3600.0
            """,
            HUNGER_DECAY_PER_HOUR, HAPPINESS_DECAY_PER_HOUR,
            ENERGY_DECAY_PER_HOUR, ENERGY_REGEN_PER_HOUR, ENERGY_REGEN_THRESHOLD,
        )
    except Exception:
        log.exception("sweep_decay failed")


# =============================================================================
# Runaway sweep (background task)
# =============================================================================

# =============================================================================
# Storage ("buddy computer")
# =============================================================================
#
# Stored buddies sit in cc_buddies with status = 'stored'. They keep their
# owner_user_id (so the player can browse / withdraw them) but are excluded
# from:
#   - the MAX_OWNED_BUDDIES count (status = 'owned' filter)
#   - the partial unique index that enforces one active buddy per user
#   - the mood-decay + runaway sweeps (those filter on status = 'owned')
# Withdrawing a stored buddy promotes it back to 'owned' (subject to the
# MAX_OWNED cap) and -- if the user has no other owned buddy -- flips
# is_active = TRUE so the panel has something to render.

async def to_storage(
    db: Any, guild_id: int, user_id: int, buddy_id: int,
) -> tuple[bool, str, dict | None]:
    """Move ``buddy_id`` from owned -> stored for the calling user.

    Refuses if the buddy is currently for-sale (delist first), or if the
    storage move would drop the player to zero owned buddies (we keep at
    least one buddy active so the panel is never empty -- mirrors the
    surrender path which also blocks dropping to zero with shelter slots).

    Returns (ok, error_message, row).
    """
    try:
        async with db.transaction() as conn:
            target = await conn.fetchrow(
                "SELECT id, for_sale, is_active FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 AND id = $3 "
                "  AND status = 'owned' "
                "FOR UPDATE",
                guild_id, user_id, buddy_id,
            )
            if not target:
                return False, "That buddy isn't yours.", None
            if bool(target.get("for_sale")):
                return False, (
                    "That buddy is listed on the market. Cancel the listing "
                    "with `,buddy delist <listing_id>` first."
                ), None
            on_expedition = await conn.fetchval(
                "SELECT 1 FROM buddy_expeditions "
                "WHERE buddy_id = $1 AND status = 'running' LIMIT 1",
                buddy_id,
            )
            if on_expedition:
                return False, (
                    "That buddy is on an expedition -- wait for them to "
                    "return (`,expedition`) before storing."
                ), None
            owned = await conn.fetchval(
                "SELECT COUNT(*) FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned'",
                guild_id, user_id,
            )
            if int(owned or 0) <= 1:
                return False, (
                    "You can't store your only buddy -- hatch or adopt a "
                    "second one first, then store this one."
                ), None
            stored = await conn.fetchval(
                "SELECT COUNT(*) FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'stored'",
                guild_id, user_id,
            )
            from services.buddy_economy import user_max_storage_slots as _max_s
            storage_cap = await _max_s(db, guild_id, user_id)
            if int(stored or 0) >= int(storage_cap):
                return False, (
                    f"Your storage is full ({int(storage_cap)} slots). "
                    f"Upgrade storage in `,buddy shop` (`storage` item) "
                    f"or surrender / sell a stored buddy first."
                ), None
            row = await conn.fetchrow(
                "UPDATE cc_buddies SET "
                "  status      = 'stored', "
                "  is_active   = FALSE, "
                "  updated_at  = NOW() "
                "WHERE id = $1 "
                "RETURNING *",
                buddy_id,
            )
            # If the stored buddy was the active one, auto-promote any
            # other owned buddy so the user always has something active.
            if bool(target.get("is_active")):
                await conn.execute(
                    "UPDATE cc_buddies SET "
                    "  is_active = TRUE, last_decay_at = NOW(), "
                    "  updated_at = NOW() "
                    "WHERE id = ("
                    "    SELECT id FROM cc_buddies "
                    "    WHERE guild_id = $1 AND owner_user_id = $2 "
                    "      AND status = 'owned' "
                    "    ORDER BY level DESC, xp DESC, id ASC "
                    "    LIMIT 1"
                    ")",
                    guild_id, user_id,
                )
    except Exception:
        log.exception(
            "to_storage failed gid=%s uid=%s buddy_id=%s",
            guild_id, user_id, buddy_id,
        )
        return False, "Storage failed. Please try again.", None

    return True, "", dict(row) if row else None


async def from_storage(
    db: Any, guild_id: int, user_id: int, buddy_id: int,
) -> tuple[bool, str, dict | None]:
    """Withdraw ``buddy_id`` from storage back into the owned pool.

    Refuses if the user is already at the ``MAX_OWNED_BUDDIES`` cap. The
    withdrawn buddy lands inactive unless the user has zero owned buddies,
    in which case it is auto-promoted to active so the panel renders.

    Returns (ok, error_message, row).
    """
    try:
        async with db.transaction() as conn:
            target = await conn.fetchrow(
                "SELECT id FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 AND id = $3 "
                "  AND status = 'stored' "
                "FOR UPDATE",
                guild_id, user_id, buddy_id,
            )
            if not target:
                return False, (
                    "That buddy isn't in your storage. "
                    "Use `,buddy storage` to browse what you have stored."
                ), None
            owned = await conn.fetchval(
                "SELECT COUNT(*) FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned'",
                guild_id, user_id,
            )
            from services.buddy_economy import user_max_battle_slots as _max_battle
            cap = await _max_battle(db, guild_id, user_id)
            if int(owned or 0) >= cap:
                return False, (
                    f"You already have the max of **{cap}** "
                    f"battle-active buddies. Store another, surrender, "
                    f"list one, or buy a battle slot upgrade first."
                ), None
            be_active = int(owned or 0) == 0
            row = await conn.fetchrow(
                "UPDATE cc_buddies SET "
                "  status        = 'owned', "
                "  is_active     = $2, "
                "  last_decay_at = NOW(), "
                "  last_interacted_at = NOW(), "
                "  updated_at    = NOW() "
                "WHERE id = $1 "
                "RETURNING *",
                buddy_id, be_active,
            )
    except Exception as exc:
        msg = str(exc).lower()
        if "cc_buddies_one_active_per_user" in msg or "unique" in msg:
            return False, (
                "Withdraw raced another promotion. Try again."
            ), None
        log.exception(
            "from_storage failed gid=%s uid=%s buddy_id=%s",
            guild_id, user_id, buddy_id,
        )
        return False, "Withdraw failed. Please try again.", None

    return True, "", dict(row) if row else None


async def list_storage(
    db: Any, guild_id: int, user_id: int,
    *, limit: int = 25, offset: int = 0,
) -> list[dict]:
    """Page through the calling user's stored buddies. Newest first.

    Includes ``boss_zone_id`` so the storage panel can show the unique
    boss display name + the BOSS_ASCII_FRAMES portrait. Falls back to
    the column-less query if migration 0263 hasn't been applied yet so
    storage continues to work mid-deploy.
    """
    try:
        rows = await db.fetch_all(
            "SELECT id, species, name, level, xp, rarity_tier, gender, "
            "       hunger, happiness, energy, wins, losses, "
            "       boss_zone_id, updated_at "
            "FROM cc_buddies "
            "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'stored' "
            "ORDER BY level DESC, xp DESC, id DESC "
            "LIMIT $3 OFFSET $4",
            guild_id, user_id, int(limit), int(offset),
        )
    except Exception:
        rows = await db.fetch_all(
            "SELECT id, species, name, level, xp, rarity_tier, gender, "
            "       hunger, happiness, energy, wins, losses, "
            "       updated_at "
            "FROM cc_buddies "
            "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'stored' "
            "ORDER BY level DESC, xp DESC, id DESC "
            "LIMIT $3 OFFSET $4",
            guild_id, user_id, int(limit), int(offset),
        )
    return rows or []


async def count_storage(db: Any, guild_id: int, user_id: int) -> int:
    """Total stored-buddy count for a user (no pagination)."""
    n = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'stored'",
        guild_id, user_id,
    )
    return int(n or 0)


async def slot_pressure(
    db: Any, guild_id: int, user_id: int,
) -> dict:
    """Return current active + storage + held-egg slot usage with a hint.

    A capture is only ACTUALLY blocked when both the active cap and the
    storage cap are full -- captures auto-route to storage when active
    is full. So we only warn when the player has nowhere left for the
    buddy to land. Eggs are reported separately (held-egg cap is its
    own thing) so a full held bag still pings the player even if their
    active / storage slots have room.

    Returns a dict with:

        {
            "active_count":  int,    # status='owned'
            "active_max":    int,    # battle slot cap (inc. purchases)
            "storage_count": int,    # status='stored'
            "storage_max":   int,    # storage slot cap (inc. purchases)
            "buddy_full":    bool,   # active AND storage both at cap
            "egg_held":      int,
            "egg_max":       int,
            "egg_full":      bool,
            "warning":       str | None,
        }

    Best-effort: any DB lookup failure falls back to permissive values so
    the surface stays callable even if the probe is flaky.
    """
    try:
        from configs.buddies_config import MAX_OWNED_BUDDIES as _BASE_OWNED
        from services.buddy_economy import (
            user_max_battle_slots as _max_battle,
            user_max_storage_slots as _max_storage,
        )
        try:
            active_max = int(await _max_battle(db, guild_id, user_id))
        except Exception:
            active_max = int(_BASE_OWNED)
        try:
            storage_max = int(await _max_storage(db, guild_id, user_id))
        except Exception:
            storage_max = 10
        counts = await db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status = 'owned'  THEN 1 ELSE 0 END), 0)::int
                AS active_count,
              COALESCE(SUM(CASE WHEN status = 'stored' THEN 1 ELSE 0 END), 0)::int
                AS storage_count
              FROM cc_buddies
             WHERE guild_id = $1 AND owner_user_id = $2
            """,
            int(guild_id), int(user_id),
        )
        active_count = int((counts or {}).get("active_count") or 0)
        storage_count = int((counts or {}).get("storage_count") or 0)
    except Exception:
        log.debug("slot_pressure: buddy probe failed", exc_info=True)
        active_count, active_max = 0, 3
        storage_count, storage_max = 0, 10
    try:
        from configs.fishing_config import MAX_HELD_EGGS as _MAX_EGGS
        egg_max = int(_MAX_EGGS)
        held_eggs_json = await db.fetch_val(
            "SELECT held_eggs FROM user_fishing "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        if isinstance(held_eggs_json, str):
            import json as _json
            try:
                held_eggs_json = _json.loads(held_eggs_json)
            except Exception:
                held_eggs_json = []
        egg_held = len(held_eggs_json or [])
    except Exception:
        log.debug("slot_pressure: egg probe failed", exc_info=True)
        egg_held, egg_max = 0, 50

    # A capture is only actually refused when BOTH active and storage are
    # full -- captures auto-route to storage when active is at cap.
    active_full = active_count >= active_max
    storage_full = storage_count >= storage_max
    buddy_full = active_full and storage_full
    egg_full = egg_held >= egg_max

    bits: list[str] = []
    if buddy_full:
        bits.append(
            f"⚠️ **Active + storage both full** "
            f"(active {active_count}/{active_max}, "
            f"storage {storage_count}/{storage_max}) -- captures will be "
            f"refused until you free a slot via `,buddy store`, "
            f"`,buddy surrender`, or buy more from `,buddy shop`."
        )
    if egg_full:
        bits.append(
            f"⚠️ **Held-egg cap reached** ({egg_held}/{egg_max}) "
            f"-- new eggs auto-sell for LURE until you `,fish egg hatch` "
            f"or `,fish egg sell` some."
        )
    warning = "\n".join(bits) if bits else None

    return {
        "active_count":  active_count,
        "active_max":    active_max,
        "storage_count": storage_count,
        "storage_max":   storage_max,
        # Legacy keys preserved for any external callers.
        "buddy_owned":   active_count,
        "buddy_max":     active_max,
        "buddy_full":  buddy_full,
        "egg_held":    egg_held,
        "egg_max":     egg_max,
        "egg_full":    egg_full,
        "warning":     warning,
    }


async def sweep_runaway(db: Any) -> list[dict]:
    """Send neglected ACTIVE buddies to the shelter with reason = 'ran_away'.

    Runaway trigger: hunger == 0 AND happiness == 0 AND idle >= RUNAWAY_IDLE_HOURS.
    Only active buddies can run away -- inactive collection members are
    frozen and don't track neglect. Returns the rows that just fled so the
    cog can DM each former owner.
    """
    try:
        rows = await db.fetch_all(
            "UPDATE cc_buddies SET "
            "  status        = 'shelter', "
            "  former_owner_id = owner_user_id, "
            "  previous_owners = previous_owners || jsonb_build_array(jsonb_build_object("
            "    'user_id',      owner_user_id, "
            "    'display_name', NULL, "
            "    'from_ts',      COALESCE("
            "                       (SELECT (elem->>'to_ts')::bigint "
            "                          FROM jsonb_array_elements(previous_owners) elem "
            "                         ORDER BY (elem->>'to_ts')::bigint DESC NULLS LAST "
            "                         LIMIT 1),"
            "                       EXTRACT(EPOCH FROM hatched_at)::bigint,"
            "                       EXTRACT(EPOCH FROM NOW())::bigint"
            "                    ), "
            "    'to_ts',        EXTRACT(EPOCH FROM NOW())::bigint, "
            "    'reason',       'ran_away'"
            "  )), "
            "  owner_user_id = NULL, "
            "  is_active     = FALSE, "
            "  abandoned_at  = NOW(), "
            "  abandoned_reason = 'ran_away', "
            "  adoptable_after  = NOW(), "
            "  updated_at    = NOW() "
            "WHERE status = 'owned' AND is_active "
            "  AND hunger = 0 AND happiness = 0 "
            "  AND EXTRACT(EPOCH FROM (NOW() - last_interacted_at)) >= $1 "
            "RETURNING id, guild_id, former_owner_id, species, name, level",
            RUNAWAY_IDLE_HOURS * 3600,
        )
    except Exception:
        log.exception("sweep_runaway failed")
        return []
    return rows or []
