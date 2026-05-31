"""Graceful-shutdown support for bet-backed interactive games.

Two layers of protection against Railway redeploys (or any process exit)
losing user funds that were deducted upfront for an in-flight game:

1. **Active-view drain.** Every View that holds a live bet registers itself
   here. On SIGTERM the bot iterates the registry and awaits each view's
   ``handle_shutdown()`` coroutine, giving it a chance to refund or cash out
   at the current state before connections are torn down.

2. **DB-backed session recovery.** Each game also persists a
   ``game_sessions`` row at ``status='active'`` when the bet is deducted and
   flips it to ``'completed'`` on resolution. If layer 1 cannot run (SIGKILL,
   OOM, power loss), the next boot sweeps any leftover ``'active'`` rows and
   credits the bet back to the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class ShutdownAwareView(Protocol):
    async def handle_shutdown(self) -> None: ...


_active_views: set[ShutdownAwareView] = set()


def register_active_view(view: ShutdownAwareView) -> None:
    _active_views.add(view)


def unregister_active_view(view: ShutdownAwareView) -> None:
    _active_views.discard(view)


def active_view_count() -> int:
    return len(_active_views)


async def drain_active_views(timeout: float = 20.0) -> None:
    """Ask every registered view to resolve itself, bounded by ``timeout``.

    Each view's ``handle_shutdown()`` is expected to refund or cash out the
    player's bet and set its ``done_event`` so the command coroutine wakes up
    and commits the balance change through the normal resolution path.
    """
    if not _active_views:
        return
    views = list(_active_views)
    log.info("Draining %d active bet-backed game view(s)", len(views))

    async def _one(v: ShutdownAwareView) -> None:
        try:
            await v.handle_shutdown()
        except Exception as exc:
            log.warning("handle_shutdown raised for %s: %s", type(v).__name__, exc)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_one(v) for v in views), return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(
            "drain_active_views timed out after %.1fs; %d view(s) unresolved",
            timeout, len(_active_views),
        )


# ── DB-backed session persistence ────────────────────────────────────────────

async def start_game_session(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    game_type: str,
    bet_amount_raw: int,
    token: str,
) -> str:
    """Insert an 'active' game_sessions row and return its UUID as a string."""
    sid = await db.fetch_val(
        "INSERT INTO game_sessions (guild_id, user_id, game_type, bet_amount, state) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
        guild_id, user_id, game_type, bet_amount_raw,
        json.dumps({"token": token}),
    )
    return str(sid)


async def complete_game_session(db: Any, session_id: str | None) -> None:
    """Mark a session 'completed'. No-op on NULL id or status != 'active'."""
    if not session_id:
        return
    try:
        await db.execute(
            "UPDATE game_sessions SET status='completed' "
            "WHERE id=$1::uuid AND status='active'",
            session_id,
        )
    except Exception as exc:
        log.warning("complete_game_session(%s) raised: %s", session_id, exc)


async def recover_orphaned_sessions(db: Any) -> int:
    """Refund every 'active' game_sessions row left from a prior process.

    Called once at bot startup after the DB pool is connected but before any
    cogs accept new game input. Idempotent: rows are flipped to 'cancelled'
    after the refund, so a subsequent call is a no-op.
    """
    rows = await db.fetch_all(
        "SELECT id::text AS id, guild_id, user_id, game_type, "
        "       bet_amount, state::text AS state_json "
        "  FROM game_sessions "
        " WHERE status='active'"
    )
    if not rows:
        return 0

    count = 0
    for row in rows:
        try:
            state = json.loads(row.get("state_json") or "{}")
        except json.JSONDecodeError:
            state = {}
        token = (state.get("token") or "USD").upper()
        bet_raw = int(row["bet_amount"])
        uid = int(row["user_id"])
        gid = int(row["guild_id"])
        try:
            if bet_raw > 0:
                if token == "USD":
                    await db.update_wallet(uid, gid, bet_raw)
                else:
                    await db.update_holding(uid, gid, token, bet_raw)
            await db.execute(
                "UPDATE game_sessions "
                "   SET status='cancelled', "
                "       state = state || '{\"cancel_reason\":\"startup_recovery\"}'::jsonb "
                " WHERE id=$1::uuid AND status='active'",
                row["id"],
            )
            count += 1
            log.info(
                "Refunded orphaned %s session %s: user=%d guild=%d bet=%d %s",
                row["game_type"], row["id"], uid, gid, bet_raw, token,
            )
        except Exception as exc:
            log.warning(
                "Failed to recover session %s (user=%d): %s",
                row["id"], uid, exc,
            )
    return count
