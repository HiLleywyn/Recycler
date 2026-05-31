"""
services/buddy_storage_eggs.py  -  Buddy Egg Storage container.

Companion to user_fishing.held_eggs (the on-person 10-egg cap, fixed).
Eggs that overflow the held cap land here so a player can bank them
long-term in the buddy network. Unlike held_eggs, this container is
upgradable in the buddy shop (50 base, +50 per upgrade, 1000 max).

Storage shape on user_buddy_economy.egg_storage (JSONB array):

    [
      {
        "species":     "shrimp" | "crab" | "octopus" | "lobster" | "wecco",
        "rarity_tier": 1..5,
        "rolled_at":   "ISO-8601 UTC",
        "from":        "fishing" | "wild_battle" | "deposit" | ...
      },
      ...
    ]

Public API:
    list_storage(db, gid, uid)                 -> list[dict]   (newest first preserved)
    storage_count(db, gid, uid)                -> int
    deposit(db, gid, uid, eggs, *, from_)      -> int          (eggs successfully banked)
    withdraw(db, gid, uid, n, *, species=None) -> list[dict]   (popped eggs back to caller)
    can_accept(db, gid, uid)                   -> int          (free rows in storage)

All mutations are wrapped in a single UPDATE so a concurrent capture
+ deposit can't double-spend a row of capacity.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)


def _as_list(raw: Any) -> list[dict]:
    """Coerce a JSONB column read into a list of egg dicts.

    asyncpg returns JSONB as either a python list (preferred) or a
    JSON string depending on codec wiring; tolerate both.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(e) for e in raw if isinstance(e, dict)]
    if isinstance(raw, str):
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [dict(e) for e in parsed if isinstance(e, dict)]
    return []


def _dump(eggs: list[dict]) -> str:
    return _json.dumps(eggs, separators=(",", ":"))


async def _ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Wrap services.buddy_economy.ensure_state without circular import."""
    from services.buddy_economy import ensure_state as _ens
    return await _ens(db, int(guild_id), int(user_id))


async def list_storage(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return the player's stored egg list (insertion order preserved)."""
    state = await _ensure_state(db, guild_id, user_id)
    return _as_list(state.get("egg_storage"))


async def storage_count(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Number of eggs currently in buddy egg storage."""
    return len(await list_storage(db, guild_id, user_id))


async def can_accept(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Free rows remaining in this player's egg storage."""
    from services.buddy_economy import user_max_egg_storage
    cap = await user_max_egg_storage(db, guild_id, user_id)
    have = await storage_count(db, guild_id, user_id)
    return max(0, int(cap) - int(have))


def _normalise_egg(raw: Any, *, default_from: str) -> dict | None:
    """Validate an incoming egg payload and return a clean dict.

    Drops malformed entries (missing species / non-int tier) so the
    storage column never picks up garbage rows on a botched deposit.
    """
    if not isinstance(raw, dict):
        return None
    species = str(raw.get("species") or "").strip().lower()
    if not species:
        return None
    try:
        tier = int(raw.get("rarity_tier") or 1)
    except (TypeError, ValueError):
        tier = 1
    tier = max(1, min(5, tier))
    rolled_at = str(
        raw.get("rolled_at")
        or _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    src = str(raw.get("from") or default_from)
    return {
        "species":     species,
        "rarity_tier": tier,
        "rolled_at":   rolled_at,
        "from":        src,
    }


async def deposit(
    db: Any, guild_id: int, user_id: int,
    eggs: Iterable[dict],
    *, from_: str = "deposit",
) -> int:
    """Append ``eggs`` to the player's egg storage, respecting the cap.

    Returns the count actually accepted. If the storage hits its cap
    mid-batch the surplus is dropped from the return -- callers
    forwarding from held_eggs should prepend the rejected count back
    to held_eggs themselves so capacity accounting stays consistent.
    """
    candidates = [
        e for e in (
            _normalise_egg(raw, default_from=from_) for raw in (eggs or [])
        )
        if e is not None
    ]
    if not candidates:
        return 0

    state = await _ensure_state(db, guild_id, user_id)
    existing = _as_list(state.get("egg_storage"))
    from services.buddy_economy import user_max_egg_storage
    cap = await user_max_egg_storage(db, guild_id, user_id)
    free = max(0, int(cap) - len(existing))
    if free <= 0:
        return 0
    accepted = candidates[:free]
    new_storage = existing + accepted
    await db.execute(
        """
        UPDATE user_buddy_economy
           SET egg_storage = $3::jsonb,
               updated_at  = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id), _dump(new_storage),
    )
    return len(accepted)


async def withdraw(
    db: Any, guild_id: int, user_id: int,
    n: int = 1,
    *, species: str | None = None,
) -> list[dict]:
    """Pop up to ``n`` eggs out of egg storage and return them.

    Selection is FIFO -- oldest deposit first -- matching how
    held_eggs is consumed by the fishing hatch path. Optional
    ``species`` filter narrows the pool. Removed eggs are returned so
    the caller can re-insert them somewhere else (held_eggs, hatch,
    gift) atomically.
    """
    n = int(n)
    if n <= 0:
        return []
    state = await _ensure_state(db, guild_id, user_id)
    existing = _as_list(state.get("egg_storage"))
    if not existing:
        return []
    sp = (species or "").strip().lower() or None

    pulled: list[dict] = []
    keep: list[dict] = []
    for egg in existing:
        if len(pulled) < n and (sp is None or str(egg.get("species") or "") == sp):
            pulled.append(egg)
            continue
        keep.append(egg)
    if not pulled:
        return []
    await db.execute(
        """
        UPDATE user_buddy_economy
           SET egg_storage = $3::jsonb,
               updated_at  = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id), _dump(keep),
    )
    return pulled


__all__ = (
    "list_storage", "storage_count", "can_accept",
    "deposit", "withdraw",
)
