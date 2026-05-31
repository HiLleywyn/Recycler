"""
services/fight_lock.py  -  One-fight-at-a-time per-(guild, user) lock.

Discoin lets a player engage in PvP buddy battles, delve wild battles,
fish wild battles, farm wild battles, and escaped-buddy world events
on entirely independent surfaces. Without coordination, a player can
start two or three at the same time and the resolution paths step on
each other (HP from one battle leaks into another, captured buddies
go to the wrong run, two ,buddy battle invocations both think they're
the only fight running).

This module is the single semaphore. Entry-point commands call
``acquire`` before starting the fight; resolution paths call
``release``. Stale locks past their TTL are stolen automatically so
a crashed battle never traps a player.

API:
    acquire(db, gid, uid, kind, *, ref=None, ttl_s=480) -> LockResult
        Returns acquired=True on success, acquired=False with the
        existing lock attached on contention.
    release(db, gid, uid, *, kind=None) -> None
        Drops the user's lock. Pass kind to be defensive (ignore the
        release if a different kind grabbed the slot in the meantime).
    refresh(db, gid, uid, kind, *, ttl_s=480) -> bool
        Bump expires_at on the existing lock. Used by long battles
        between rounds. Returns True if the row was the caller's, False
        if someone stole it.
    peek(db, gid, uid) -> dict | None
        Read the current lock without mutation.
    clear_user(db, gid, uid) -> int
        Admin force-clear. Returns rows deleted.
    fight_lock_guard(ctx, kind, *, ref=None, ttl_s=480) -> async ctx mgr
        Convenience wrapper: acquires on enter, releases on exit, raises
        FightLockBusy when contended. Most cogs should use this rather
        than calling acquire/release directly.

Conventions:
    kind values used by the cogs:
        buddy_pvp     -- ,buddy battle <member>
        fish_wild     -- fishing wild-buddy battle (Challenge button)
        delve_wild    -- delve wild-buddy battle (Challenge button)
        farm_wild     -- ,farm battle
        escape_event  -- escaped-buddy world event Challenge
    The kind is opaque to this module; it's just a label for diagnostics
    and the friendly error message.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


KIND_LABELS: dict[str, str] = {
    "buddy_pvp":    "buddy PvP battle",
    "fish_wild":    "fishing wild-buddy battle",
    "delve_wild":   "delve wild-buddy battle",
    "farm_wild":    "farm wild-buddy battle",
    "escape_event": "escaped-buddy event",
}


@dataclass
class LockResult:
    """Return value from ``acquire``. ``acquired`` is the only field
    callers must check; the rest is for friendly error rendering when
    a fight is contended."""
    acquired: bool
    kind: str
    existing_kind: Optional[str] = None
    existing_ref: Optional[str] = None
    existing_seconds_remaining: Optional[int] = None


class FightLockBusy(Exception):
    """Raised by ``fight_lock_guard`` when another fight already holds
    the slot. The cog should catch this and route to a friendly
    "you're already in a fight" reply."""

    def __init__(self, lock: LockResult) -> None:
        self.lock = lock
        label = KIND_LABELS.get(lock.existing_kind or "", lock.existing_kind or "fight")
        rem = lock.existing_seconds_remaining or 0
        if rem > 0:
            super().__init__(
                f"You're already in a **{label}**. Finish or wait "
                f"~{rem}s for the lock to clear."
            )
        else:
            super().__init__(f"You're already in a **{label}**.")


# ── core API ──────────────────────────────────────────────────────────


async def acquire(
    db: Any,
    guild_id: int,
    user_id: int,
    kind: str,
    *,
    ref: Optional[str] = None,
    ttl_s: int = 480,
) -> LockResult:
    """Try to take the per-user fight lock. Atomic INSERT...ON CONFLICT
    that lets a stale-by-TTL row be stolen. Returns ``acquired=True``
    on success; otherwise ``acquired=False`` plus the existing kind /
    ref / remaining seconds for the friendly error message.

    Stale takeover is the safety valve for crashed battles -- if a fight
    view never gets to call release(), the lock self-frees ttl_s
    seconds after the last refresh."""
    if ttl_s <= 0:
        ttl_s = 480
    # First, opportunistically clear stale rows for this user. Cheap
    # single-row delete, runs once per fight start.
    await db.execute(
        """
        DELETE FROM active_fight_locks
         WHERE guild_id  = $1
           AND user_id   = $2
           AND expires_at < NOW()
        """,
        int(guild_id), int(user_id),
    )
    # Try the insert. ON CONFLICT DO NOTHING means the row already
    # exists with a still-fresh expiry; we read that row and report it.
    row = await db.fetch_one(
        """
        INSERT INTO active_fight_locks (guild_id, user_id, lock_kind, lock_ref, expires_at)
        VALUES ($1, $2, $3, $4, NOW() + ($5 * INTERVAL '1 second'))
        ON CONFLICT (guild_id, user_id) DO NOTHING
        RETURNING lock_kind, lock_ref, expires_at
        """,
        int(guild_id), int(user_id), str(kind), ref, int(ttl_s),
    )
    if row:
        return LockResult(acquired=True, kind=str(row.get("lock_kind") or kind))
    # Contended: read the existing row to report what's blocking.
    existing = await db.fetch_one(
        """
        SELECT lock_kind, lock_ref,
               EXTRACT(EPOCH FROM (expires_at - NOW()))::INTEGER AS seconds_remaining
          FROM active_fight_locks
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id),
    )
    return LockResult(
        acquired=False, kind=kind,
        existing_kind=str((existing or {}).get("lock_kind") or "unknown"),
        existing_ref=str((existing or {}).get("lock_ref") or "") or None,
        existing_seconds_remaining=int((existing or {}).get("seconds_remaining") or 0),
    )


async def release(
    db: Any,
    guild_id: int,
    user_id: int,
    *,
    kind: Optional[str] = None,
) -> None:
    """Drop the user's lock. Pass ``kind`` to be defensive: if a
    different kind already took over (because the original fight
    crashed and was stolen via TTL), the release becomes a no-op
    instead of clobbering the new fight's lock."""
    if kind is None:
        await db.execute(
            "DELETE FROM active_fight_locks WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(user_id),
        )
    else:
        await db.execute(
            "DELETE FROM active_fight_locks "
            "WHERE guild_id = $1 AND user_id = $2 AND lock_kind = $3",
            int(guild_id), int(user_id), str(kind),
        )


async def refresh(
    db: Any,
    guild_id: int,
    user_id: int,
    kind: str,
    *,
    ttl_s: int = 480,
) -> bool:
    """Bump expires_at on the caller's existing lock. Returns False if
    the row was stolen (different kind) so the caller can abort."""
    if ttl_s <= 0:
        ttl_s = 480
    status = await db.execute(
        """
        UPDATE active_fight_locks
           SET expires_at = NOW() + ($4 * INTERVAL '1 second')
         WHERE guild_id = $1
           AND user_id  = $2
           AND lock_kind = $3
        """,
        int(guild_id), int(user_id), str(kind), int(ttl_s),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1]) > 0
    except (TypeError, ValueError):
        return False


async def peek(
    db: Any, guild_id: int, user_id: int,
) -> Optional[dict]:
    """Read the active lock row, or None if free. Stale rows are
    surfaced too -- callers that care should check seconds_remaining."""
    return await db.fetch_one(
        """
        SELECT lock_kind, lock_ref,
               EXTRACT(EPOCH FROM locked_at)        AS locked_epoch,
               EXTRACT(EPOCH FROM expires_at)       AS expires_epoch,
               EXTRACT(EPOCH FROM (expires_at - NOW()))::INTEGER AS seconds_remaining
          FROM active_fight_locks
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id),
    )


async def clear_user(db: Any, guild_id: int, user_id: int) -> int:
    """Admin force-clear. Used by ``,admin fightlock clear <user>`` for
    the rare case where neither acquire-side stale-takeover nor TTL
    expiry is enough (e.g. an in-progress battle the player wants to
    abandon and the bot can't see is stuck). Returns rows deleted."""
    status = await db.execute(
        "DELETE FROM active_fight_locks WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


# ── ergonomic guard ───────────────────────────────────────────────────


@asynccontextmanager
async def fight_lock_guard(
    ctx: Any,
    kind: str,
    *,
    ref: Optional[str] = None,
    ttl_s: int = 480,
):
    """Async context manager: acquire the lock on enter, release on
    exit (even on exception), raise ``FightLockBusy`` if contended.

    Use from any cog command that starts a fight:

        from services.fight_lock import fight_lock_guard, FightLockBusy
        try:
            async with fight_lock_guard(ctx, kind="buddy_pvp"):
                ...  # run the fight
        except FightLockBusy as exc:
            await ctx.reply_error(str(exc))
            return
    """
    db = ctx.db
    gid = int(ctx.guild_id)
    uid = int(ctx.author.id)
    result = await acquire(db, gid, uid, kind, ref=ref, ttl_s=ttl_s)
    if not result.acquired:
        raise FightLockBusy(result)
    try:
        yield result
    finally:
        try:
            await release(db, gid, uid, kind=kind)
        except Exception:
            log.debug("fight_lock release failed gid=%s uid=%s kind=%s",
                      gid, uid, kind, exc_info=True)
