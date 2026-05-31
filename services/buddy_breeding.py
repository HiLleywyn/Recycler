"""
services/buddy_breeding.py  -  Daycare / breeding flow for CC Buddies.

Players start with one nest slot and can buy up to ``NEST_SLOTS_HARD_CAP``
total via the buddy shop. Each slot is one ``cc_buddy_daycare`` row,
keyed on a synthetic BIGSERIAL id (post-migration 0215). Player deposits
two of their owned buddies as parents; the egg's species + rarity tier
are pre-rolled at deposit time and persisted, so the player can plan
around the result while it incubates. Once egg_ready_at <= NOW() the egg
can be collected -- it lands in the standard fishing held-egg slot via
:func:`services.fishing.give_egg` so the player hatches it the same way
they hatch wild eggs (single egg pipeline).

Inventory model: deposited parents flip to ``status='nesting'`` so
they no longer occupy the user's battle (status='owned') or storage
(status='stored') slot caps. This avoids the old foot-gun where 10
nests x 2 parents = 20 buddies all classed as 'owned' even though
the active cap is 10. On collect/cancel the parents are routed back
to 'owned' if there is room under the battle cap, otherwise to
'stored' if there is room under the storage cap; otherwise the action
is refused with a clear "free a slot first" message so the egg / nest
stays put.

All time comparisons run on the DB clock (EXTRACT(EPOCH FROM NOW() - ts))
per the project's DB-side-clocks rule.

Public API:
    deposit(db, gid, uid, parent1, parent2)  -> tuple[bool, str, dict | None]
    list_nests(db, gid, uid)                 -> list[dict]
    status(db, gid, uid)                     -> dict | None  (back-compat: oldest)
    collect(db, gid, uid, slot_id=None)      -> tuple[bool, str, dict | None]
    cancel(db, gid, uid, slot_id=None)       -> tuple[bool, str, list[int], dict[int, str]]
    is_in_daycare(db, buddy_id)              -> bool
"""
from __future__ import annotations

import logging
import random
from typing import Any

from configs.buddies_config import (
    DAYCARE_FEE_BUD,
    DAYCARE_INCUBATION_S,
    DAYCARE_MIN_PARENT_LEVEL,
    DAYCARE_RARITY_INHERIT_W,
)
from core.framework.scale import to_raw

log = logging.getLogger(__name__)


def _roll_egg_rarity(parent_tiers: tuple[int, int]) -> int:
    """Roll the egg's rarity tier from the two parents' tiers.

    Index into ``DAYCARE_RARITY_INHERIT_W`` by the higher parent tier
    (clamped to 1..5). Picks (down, equal, up) and shifts the higher
    tier accordingly, then clamps to [1, 5].
    """
    hi = max(1, min(5, max(int(parent_tiers[0]), int(parent_tiers[1]))))
    weights = DAYCARE_RARITY_INHERIT_W[hi - 1]
    pick = random.choices(("down", "same", "up"), weights=weights, k=1)[0]
    if pick == "down":
        return max(1, hi - 1)
    if pick == "up":
        return min(5, hi + 1)
    return hi


def _roll_egg_species(parent_species: tuple[str, str]) -> str:
    """Pick the egg species from the two parents' species (50/50)."""
    a = (parent_species[0] or "").lower()
    b = (parent_species[1] or "").lower()
    if not a and not b:
        return "zenny"
    if not a:
        return b
    if not b:
        return a
    return random.choice((a, b))


async def _user_nest_cap(db: Any, guild_id: int, user_id: int) -> int:
    """Resolve the user's current nest cap via buddy_economy.

    Imported lazily to keep the breeding -> economy direction one-way:
    economy doesn't import breeding, so importing here at call time
    avoids any circular-import surprise.
    """
    from services.buddy_economy import user_max_nest_slots
    return int(await user_max_nest_slots(db, guild_id, user_id))


async def deposit(
    db: Any, guild_id: int, user_id: int,
    parent1_id: int, parent2_id: int,
) -> tuple[bool, str, dict | None]:
    """Deposit two of the user's owned buddies into a fresh nest slot.

    Charges ``DAYCARE_FEE_BUD`` at oracle rate (burns BUD). Both parents
    must be owned by the caller, status='owned', not currently for-sale,
    at least ``DAYCARE_MIN_PARENT_LEVEL``, and **opposite genders** (one
    male and one female). The egg's species + rarity are pre-rolled and
    stored on the daycare row. Gender is NOT pre-rolled -- eggs are
    genderless until they hatch. The egg is collectable once
    egg_ready_at <= NOW().

    Refused if the caller has already filled every nest slot they own
    (base + purchased upgrades).
    """
    if int(parent1_id) == int(parent2_id):
        return False, "Pick two different buddies as parents.", None

    cap = await _user_nest_cap(db, guild_id, user_id)
    try:
        async with db.transaction() as conn:
            # Refuse early when every nest slot the user owns is full.
            in_use = await conn.fetchval(
                "SELECT COUNT(*) FROM cc_buddy_daycare "
                "WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
            if int(in_use or 0) >= cap:
                return False, (
                    f"Every nest slot is busy ({int(in_use or 0)}/{cap}). "
                    f"Collect / cancel one first or buy a `,buddy slot "
                    f"nest buy` upgrade."
                ), None

            # Validate both parents.
            parents = await conn.fetch(
                "SELECT id, species, level, rarity_tier, name, "
                "       for_sale, gender "
                "FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned' AND id = ANY($3::bigint[]) "
                "FOR UPDATE",
                guild_id, user_id, [int(parent1_id), int(parent2_id)],
            )
            if len(parents) != 2:
                return False, (
                    "Both buddies must be owned by you (status='owned'). "
                    "Stored / shelter buddies can't breed."
                ), None
            for p in parents:
                if bool(p.get("for_sale")):
                    return False, (
                        f"Buddy `#{int(p['id'])}` is on the market. "
                        f"Delist it before depositing it in the nest."
                    ), None
                if int(p.get("level") or 1) < DAYCARE_MIN_PARENT_LEVEL:
                    return False, (
                        f"Both parents must be at least Lv. "
                        f"{DAYCARE_MIN_PARENT_LEVEL}. "
                        f"Buddy `#{int(p['id'])}` is too young."
                    ), None
            # Reject if either parent is already a parent in another
            # active nest. The unique partial indexes on parent1_id /
            # parent2_id will catch this too, but a friendly precheck
            # surfaces a clean error instead of an asyncpg constraint
            # exception.
            busy = await conn.fetch(
                "SELECT id, parent1_id, parent2_id "
                "FROM cc_buddy_daycare "
                "WHERE guild_id = $1 AND user_id = $2 "
                "  AND ($3::bigint IN (parent1_id, parent2_id) "
                "       OR $4::bigint IN (parent1_id, parent2_id))",
                guild_id, user_id,
                int(parent1_id), int(parent2_id),
            )
            if busy:
                return False, (
                    "One of those buddies is already incubating in "
                    "another nest. `,buddy nest` to see your slots."
                ), None
            # Opposite-gender requirement -- you can't breed two of the
            # same. The egg itself is genderless; gender rolls at hatch.
            genders = {str(p.get("gender") or "").upper() for p in parents}
            if genders != {"M", "F"}:
                from configs.buddies_config import gender_glyph as _glyph
                pa, pb = parents[0], parents[1]
                ga = _glyph(pa.get("gender")) or "?"
                gb = _glyph(pb.get("gender")) or "?"
                return False, (
                    f"Daycare needs **one male and one female**. "
                    f"You picked `#{int(pa['id'])}` ({ga}) and "
                    f"`#{int(pb['id'])}` ({gb}). Pair an opposite-gender "
                    f"buddy from `,buddy stats` and try again."
                ), None

            # Charge the daycare fee in BUD (burn from wallet_holdings).
            fee_raw = to_raw(DAYCARE_FEE_BUD)
            held = await conn.fetchval(
                "SELECT amount FROM wallet_holdings "
                "WHERE guild_id = $1 AND user_id = $2 AND symbol = 'BUD'",
                guild_id, user_id,
            )
            if int(held or 0) < fee_raw:
                return False, (
                    f"Daycare fee is **{DAYCARE_FEE_BUD:,.0f} BUD**. "
                    f"You don't have enough -- earn more from arena, "
                    f"FREN/BBT staking, or the buddy convert flow."
                ), None
            await conn.execute(
                "UPDATE wallet_holdings SET amount = amount - $3::numeric "
                "WHERE guild_id = $1 AND user_id = $2 AND symbol = 'BUD'",
                guild_id, user_id, str(fee_raw),
            )

            # Pre-roll the egg.
            p_by_id = {int(r["id"]): r for r in parents}
            p1 = p_by_id[int(parent1_id)]
            p2 = p_by_id[int(parent2_id)]
            species = _roll_egg_species(
                (str(p1.get("species") or ""), str(p2.get("species") or "")),
            )
            rarity = _roll_egg_rarity(
                (
                    int(p1.get("rarity_tier") or 1),
                    int(p2.get("rarity_tier") or 1),
                ),
            )

            # Move both parents off the active 'owned' roster: 'nesting'
            # excludes them from battle / storage caps, mood-decay, and
            # market listings while they incubate. is_active is cleared
            # so the partial unique index doesn't trip when the user
            # promotes a different buddy below.
            await conn.execute(
                "UPDATE cc_buddies SET "
                "  status     = 'nesting', "
                "  is_active  = FALSE, "
                "  updated_at = NOW() "
                "WHERE id = ANY($1::bigint[])",
                [int(parent1_id), int(parent2_id)],
            )
            # If either parent was the user's active buddy, auto-promote
            # the next-best owned buddy so the chat-XP / decay surface
            # never lands on an empty active slot. Mirrors to_storage().
            if any(bool(p.get("is_active")) for p in parents):
                still_active = await conn.fetchval(
                    "SELECT 1 FROM cc_buddies "
                    "WHERE guild_id = $1 AND owner_user_id = $2 "
                    "  AND status = 'owned' AND is_active LIMIT 1",
                    guild_id, user_id,
                )
                if not still_active:
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

            # Eggs are genderless -- gender is rolled at hatch time. The
            # egg_gender column is kept for backwards-compat (migration
            # 0175 drops it once all running daycares clear) but we no
            # longer write into it.
            #
            # Every parameter gets an explicit type cast. Without them
            # asyncpg's auto type-inference was tripping
            # IndeterminateDatatypeError on $3 (parent1_id) for some
            # Postgres + driver combinations. Forcing the casts here
            # is cheap and removes the ambiguity entirely.
            row = await conn.fetchrow(
                "INSERT INTO cc_buddy_daycare ("
                "  guild_id, user_id, parent1_id, parent2_id, "
                "  egg_ready_at, egg_species, egg_rarity_tier, "
                "  fee_paid_raw, fee_currency"
                ") VALUES ("
                "  $1::bigint, $2::bigint, $3::bigint, $4::bigint, "
                "  NOW() + ($5::int * INTERVAL '1 second'), "
                "  $6::text, $7::int, "
                "  $8::numeric, 'BUD'"
                ") RETURNING *",
                guild_id, user_id,
                int(parent1_id), int(parent2_id),
                int(DAYCARE_INCUBATION_S), species, int(rarity),
                str(fee_raw),
            )
    except Exception as exc:
        log.exception(
            "nest deposit failed gid=%s uid=%s p1=%s p2=%s -- %s: %s",
            guild_id, user_id, parent1_id, parent2_id,
            type(exc).__name__, exc,
        )
        # Surface the actual exception type / message so the player
        # can report something concrete instead of a generic
        # "please try again" that swallows every constraint /
        # missing-row / type-mismatch failure mode.
        return False, (
            f"Nest deposit failed: `{type(exc).__name__}: {exc}`. "
            f"Both parents must be opposite genders, both at least "
            f"the minimum level, and not currently listed for sale."
        ), None
    return True, "", dict(row) if row else None


async def list_nests(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return every active nest row for the user, ready ones first.

    The ``ready`` derived flag and ``seconds_remaining`` field mirror
    the legacy single-row ``status()`` shape, so cogs can iterate the
    list and render each row with the same code path.
    """
    rows = await db.fetch_all(
        "SELECT *, "
        "       GREATEST("
        "         0, "
        "         EXTRACT(EPOCH FROM (egg_ready_at - NOW()))::bigint"
        "       ) AS seconds_remaining "
        "FROM cc_buddy_daycare "
        "WHERE guild_id = $1 AND user_id = $2 "
        "ORDER BY egg_ready_at ASC, id ASC",
        guild_id, user_id,
    )
    out: list[dict] = []
    for row in rows or ():
        d = dict(row)
        d["ready"] = int(d.get("seconds_remaining") or 0) <= 0
        out.append(d)
    return out


async def status(
    db: Any, guild_id: int, user_id: int,
) -> dict | None:
    """Return the user's *next-ready* nest row, or None if empty.

    Kept for back-compat with call sites (e.g. the hub) that still ask
    for a single egg. Newer code should call ``list_nests`` directly.
    """
    rows = await list_nests(db, guild_id, user_id)
    return rows[0] if rows else None


async def _return_parents(
    db: Any, guild_id: int, user_id: int,
    parent_ids: tuple[int, int],
) -> tuple[bool, str, dict[int, str]]:
    """Route both nesting parents back into the user's inventory.

    Each parent is placed into 'owned' if there is still room under the
    battle slot cap, otherwise into 'stored' if there is room under the
    storage cap. If neither has room the call returns ok=False so the
    caller (collect / cancel) can refuse the action with a clear error
    message that asks the player to free a slot first -- the nest /
    egg stays put until they do.

    Takes the ``db`` wrapper (not the raw asyncpg ``conn``) so the
    economy helpers below see the same fetch_one / execute API surface
    they expect. The project's contextvar routes every wrapper call
    through the surrounding ``db.transaction()`` block automatically,
    so this still runs as a single atomic unit even though we use the
    high-level API throughout. (Earlier draft passed the raw asyncpg
    Connection here and crashed with ``'Connection' object has no
    attribute 'fetch_one'`` once the helpers tried to ensure_state.)

    Returns (ok, error_message, {buddy_id: 'owned'|'stored'}).
    """
    from services.buddy_economy import (
        user_max_battle_slots as _max_battle,
        user_max_storage_slots as _max_storage,
    )
    battle_cap = int(await _max_battle(db, guild_id, user_id))
    storage_cap = int(await _max_storage(db, guild_id, user_id))
    counts = await db.fetch_one(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN status = 'owned'  THEN 1 ELSE 0 END), 0)::int "
        "    AS owned_n, "
        "  COALESCE(SUM(CASE WHEN status = 'stored' THEN 1 ELSE 0 END), 0)::int "
        "    AS stored_n "
        "FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2",
        guild_id, user_id,
    )
    owned_n = int((counts or {}).get("owned_n") or 0)
    stored_n = int((counts or {}).get("stored_n") or 0)
    placement: dict[int, str] = {}
    for pid in parent_ids:
        if owned_n < battle_cap:
            placement[int(pid)] = "owned"
            owned_n += 1
        elif stored_n < storage_cap:
            placement[int(pid)] = "stored"
            stored_n += 1
        else:
            return False, (
                f"No room to bring the parents home -- battle "
                f"({owned_n}/{battle_cap}) and storage "
                f"({stored_n}/{storage_cap}) are both full. "
                f"Free a slot via `,buddy store`, `,buddy surrender`, "
                f"or buy more via `,buddy shop` and try again."
            ), {}

    owned_ids = [pid for pid, s in placement.items() if s == "owned"]
    stored_ids = [pid for pid, s in placement.items() if s == "stored"]
    if owned_ids:
        await db.execute(
            "UPDATE cc_buddies SET "
            "  status      = 'owned', "
            "  is_active   = FALSE, "
            "  last_decay_at = NOW(), "
            "  last_interacted_at = NOW(), "
            "  updated_at  = NOW() "
            "WHERE id = ANY($1::bigint[])",
            owned_ids,
        )
    if stored_ids:
        await db.execute(
            "UPDATE cc_buddies SET "
            "  status      = 'stored', "
            "  is_active   = FALSE, "
            "  updated_at  = NOW() "
            "WHERE id = ANY($1::bigint[])",
            stored_ids,
        )
    # If the user has no active buddy after returning the parents (e.g.
    # the deposit had emptied the active slot and the owned set is now
    # the returned parents), promote the highest-level owned buddy so
    # the panel always has something to render. Mirrors to_storage().
    has_active = await db.fetch_val(
        "SELECT 1 FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 "
        "  AND status = 'owned' AND is_active LIMIT 1",
        guild_id, user_id,
    )
    if not has_active:
        await db.execute(
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
    return True, "", placement


async def collect(
    db: Any, guild_id: int, user_id: int,
    slot_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    """Collect one nest egg (must be ready) and clear that slot.

    When ``slot_id`` is ``None`` the next-ready slot is taken (oldest
    ready first), so the legacy "one egg" call sites keep working
    without modification. Pass an explicit id from ``list_nests`` to
    target a specific slot.

    Returns (ok, msg, egg_dict). On success ``egg_dict`` has keys
    ``species``, ``rarity_tier``, ``parents``, ``slot_id``,
    ``parent_placement`` ({buddy_id: 'owned'|'stored'}).
    """
    placement: dict[int, str] = {}
    try:
        async with db.transaction() as conn:
            if slot_id is None:
                row = await conn.fetchrow(
                    "SELECT * FROM cc_buddy_daycare "
                    "WHERE guild_id = $1 AND user_id = $2 "
                    "  AND egg_ready_at <= NOW() "
                    "ORDER BY egg_ready_at ASC, id ASC "
                    "LIMIT 1 FOR UPDATE",
                    guild_id, user_id,
                )
                if not row:
                    # Differentiate "no nests at all" vs "still cooking".
                    any_row = await conn.fetchrow(
                        "SELECT GREATEST(0, "
                        "  EXTRACT(EPOCH FROM (egg_ready_at - NOW()))::bigint"
                        ") AS s FROM cc_buddy_daycare "
                        "WHERE guild_id = $1 AND user_id = $2 "
                        "ORDER BY egg_ready_at ASC LIMIT 1",
                        guild_id, user_id,
                    )
                    if not any_row:
                        return False, "You don't have any buddies in the nest.", None
                    secs = int(any_row.get("s") or 0)
                    hours = secs // 3600
                    mins = (secs % 3600) // 60
                    return False, (
                        f"No egg ready yet -- next one in about "
                        f"**{hours}h {mins}m**."
                    ), None
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM cc_buddy_daycare "
                    "WHERE id = $1 AND guild_id = $2 AND user_id = $3 "
                    "FOR UPDATE",
                    int(slot_id), guild_id, user_id,
                )
                if not row:
                    return False, (
                        f"Nest slot `#{int(slot_id)}` not found "
                        f"(or not yours). `,buddy nest` to see your slots."
                    ), None
                ready_in = await conn.fetchval(
                    "SELECT GREATEST(0, "
                    "  EXTRACT(EPOCH FROM (egg_ready_at - NOW()))::bigint"
                    ") FROM cc_buddy_daycare WHERE id = $1",
                    int(slot_id),
                )
                if int(ready_in or 0) > 0:
                    hours = int(ready_in or 0) // 3600
                    mins = (int(ready_in or 0) % 3600) // 60
                    return False, (
                        f"That egg is still incubating -- about "
                        f"**{hours}h {mins}m** to go."
                    ), None
            ok_p, err_p, placement = await _return_parents(
                db, guild_id, user_id,
                (int(row["parent1_id"]), int(row["parent2_id"])),
            )
            if not ok_p:
                # Don't delete the daycare row -- the egg stays ready
                # and the player can collect once they free a slot.
                return False, err_p, None
            await conn.execute(
                "DELETE FROM cc_buddy_daycare WHERE id = $1",
                int(row["id"]),
            )
    except Exception:
        log.exception(
            "nest collect failed gid=%s uid=%s slot=%s",
            guild_id, user_id, slot_id,
        )
        return False, "Egg collection failed. Please try again.", None
    return True, "", {
        "species":          str(row.get("egg_species") or ""),
        "rarity_tier":      int(row.get("egg_rarity_tier") or 1),
        "parents":          (int(row["parent1_id"]), int(row["parent2_id"])),
        "slot_id":          int(row["id"]),
        "parent_placement": placement,
    }


async def cancel(
    db: Any, guild_id: int, user_id: int,
    slot_id: int | None = None,
) -> tuple[bool, str, list[int], dict[int, str]]:
    """Abandon one active nest slot. The fee is NOT refunded.

    When ``slot_id`` is ``None`` the oldest pending slot is cancelled,
    matching the legacy single-slot call sites.

    Returns (ok, msg, [parent ids], {buddy_id: 'owned'|'stored'}) so the
    cog can echo which buddies were freed up and where they landed. The
    cancel itself is refused if the parents have nowhere to land (active
    + storage both at cap) -- the player has to free a slot first.
    """
    placement: dict[int, str] = {}
    try:
        async with db.transaction() as conn:
            if slot_id is None:
                row = await conn.fetchrow(
                    "SELECT id, parent1_id, parent2_id FROM cc_buddy_daycare "
                    "WHERE guild_id = $1 AND user_id = $2 "
                    "ORDER BY egg_ready_at ASC, id ASC LIMIT 1 FOR UPDATE",
                    guild_id, user_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT id, parent1_id, parent2_id FROM cc_buddy_daycare "
                    "WHERE id = $1 AND guild_id = $2 AND user_id = $3 "
                    "FOR UPDATE",
                    int(slot_id), guild_id, user_id,
                )
            if not row:
                if slot_id is None:
                    return False, "You don't have any buddies in the nest.", [], {}
                return False, (
                    f"Nest slot `#{int(slot_id)}` not found (or not yours)."
                ), [], {}
            ok_p, err_p, placement = await _return_parents(
                db, guild_id, user_id,
                (int(row["parent1_id"]), int(row["parent2_id"])),
            )
            if not ok_p:
                return False, err_p, [], {}
            await conn.execute(
                "DELETE FROM cc_buddy_daycare WHERE id = $1",
                int(row["id"]),
            )
    except Exception:
        log.exception(
            "nest cancel failed gid=%s uid=%s slot=%s",
            guild_id, user_id, slot_id,
        )
        return False, "Nest cancel failed. Please try again.", [], {}
    return True, "", [int(row["parent1_id"]), int(row["parent2_id"])], placement


async def is_in_daycare(db: Any, buddy_id: int) -> bool:
    """Quick predicate: is ``buddy_id`` currently a daycare parent?"""
    n = await db.fetch_val(
        "SELECT 1 FROM cc_buddy_daycare "
        "WHERE parent1_id = $1 OR parent2_id = $1 LIMIT 1",
        int(buddy_id),
    )
    return bool(n)
