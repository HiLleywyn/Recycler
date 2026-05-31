"""V3 Pillar 5: persistent in-bot inbox.

Every system writes notification-worthy events here via ``post()`` so
players can revisit them at leisure instead of relying on flying
channel messages or one-shot DMs. The inbox is keyed by user; the
guild_id is recorded for filtering on multi-server users but isn't a
hard requirement (cross-guild systems like cosmetics legitimately
post with gid=None).

Public surface:

    await post(db, uid, category, title, body, severity="info",
               payload=None, gid=None)
    await unread(db, uid, *, limit=20)
    await read(db, uid, msg_id)
    await mark_all_read(db, uid)
    await purge(db, uid, *, before=None)

Categories used today (free-form strings; new categories don't need a
schema migration):
    market_event, raid, season, achievement, mastery, clan_war,
    governance, auction, cosmetic, system

Severity drives the PNG accent in services/inbox_render.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


_SEVERITIES = {"info", "success", "warning", "error", "critical"}


async def post(
    db, user_id: int, category: str, title: str, body: str,
    *,
    severity: str = "info",
    payload: dict | None = None,
    gid: int | None = None,
) -> int | None:
    """Append one inbox message for a single user.

    Returns the new row id on success, ``None`` on failure. Failures
    are logged but never raised -- a producer must not crash because
    the inbox layer hiccupped.
    """
    if severity not in _SEVERITIES:
        severity = "info"
    try:
        row = await db.fetch_one(
            "INSERT INTO user_inbox "
            "(user_id, guild_id, category, title, body, severity, payload_json) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id",
            user_id, gid, category[:64], title[:256], body[:4000],
            severity,
            json.dumps(payload) if payload is not None else None,
        )
        return int(row["id"]) if row else None
    except Exception:
        log.exception(
            "inbox: post failed uid=%s gid=%s category=%s",
            user_id, gid, category,
        )
        return None


async def post_many(
    db, user_ids: list[int], category: str, title: str, body: str,
    *,
    severity: str = "info",
    payload: dict | None = None,
    gid: int | None = None,
) -> int:
    """Bulk-post the same message to multiple users.

    Useful for events (market crashes, season starts, clan war
    announcements) that want to land in every active player's inbox.
    Returns the count of successfully written rows.
    """
    if not user_ids:
        return 0
    ok = 0
    for uid in user_ids:
        if await post(
            db, uid, category, title, body,
            severity=severity, payload=payload, gid=gid,
        ):
            ok += 1
    return ok


async def unread(db, user_id: int, *, limit: int = 20) -> list[dict]:
    """Return the newest unread messages, newest first."""
    try:
        rows = await db.fetch_all(
            "SELECT * FROM user_inbox "
            "WHERE user_id = $1 AND read_at IS NULL "
            "ORDER BY posted_at DESC LIMIT $2",
            user_id, max(1, int(limit)),
        )
        return [dict(r) for r in rows]
    except Exception:
        log.exception("inbox: unread query failed uid=%s", user_id)
        return []


async def recent(db, user_id: int, *, limit: int = 20) -> list[dict]:
    """Return the newest messages (read + unread), newest first."""
    try:
        rows = await db.fetch_all(
            "SELECT * FROM user_inbox "
            "WHERE user_id = $1 "
            "ORDER BY posted_at DESC LIMIT $2",
            user_id, max(1, int(limit)),
        )
        return [dict(r) for r in rows]
    except Exception:
        log.exception("inbox: recent query failed uid=%s", user_id)
        return []


async def get(db, user_id: int, msg_id: int) -> dict | None:
    try:
        row = await db.fetch_one(
            "SELECT * FROM user_inbox WHERE id = $1 AND user_id = $2",
            msg_id, user_id,
        )
        return dict(row) if row else None
    except Exception:
        log.exception(
            "inbox: get failed uid=%s msg=%s", user_id, msg_id,
        )
        return None


async def read(db, user_id: int, msg_id: int) -> bool:
    """Mark a single message read. Idempotent."""
    try:
        await db.execute(
            "UPDATE user_inbox SET read_at = $3 "
            "WHERE id = $1 AND user_id = $2 AND read_at IS NULL",
            msg_id, user_id, datetime.now(timezone.utc),
        )
        return True
    except Exception:
        log.exception(
            "inbox: read failed uid=%s msg=%s", user_id, msg_id,
        )
        return False


async def mark_all_read(db, user_id: int) -> int:
    """Mark every unread message read. Returns the count affected.

    Best-effort count: the DB driver's status string is not portable
    across all our backends, so we issue an UPDATE + a count query.
    """
    try:
        count_row = await db.fetch_one(
            "SELECT COUNT(*) AS c FROM user_inbox "
            "WHERE user_id = $1 AND read_at IS NULL",
            user_id,
        )
        count = int(count_row.get("c") or 0) if count_row else 0
        await db.execute(
            "UPDATE user_inbox SET read_at = $2 "
            "WHERE user_id = $1 AND read_at IS NULL",
            user_id, datetime.now(timezone.utc),
        )
        return count
    except Exception:
        log.exception("inbox: mark_all_read failed uid=%s", user_id)
        return 0


async def purge(
    db, user_id: int, *, before: datetime | None = None,
) -> int:
    """Delete messages older than ``before`` (or all read messages if None).

    Returns the count affected.
    """
    try:
        count_row = await db.fetch_one(
            "SELECT COUNT(*) AS c FROM user_inbox "
            "WHERE user_id = $1 AND ("
            "  ($2::TIMESTAMPTZ IS NOT NULL AND posted_at < $2) "
            "  OR ($2::TIMESTAMPTZ IS NULL AND read_at IS NOT NULL)"
            ")",
            user_id, before,
        )
        count = int(count_row.get("c") or 0) if count_row else 0
        await db.execute(
            "DELETE FROM user_inbox WHERE user_id = $1 AND ("
            "  ($2::TIMESTAMPTZ IS NOT NULL AND posted_at < $2) "
            "  OR ($2::TIMESTAMPTZ IS NULL AND read_at IS NOT NULL)"
            ")",
            user_id, before,
        )
        return count
    except Exception:
        log.exception("inbox: purge failed uid=%s", user_id)
        return 0


async def unread_count(db, user_id: int) -> int:
    try:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS c FROM user_inbox "
            "WHERE user_id = $1 AND read_at IS NULL",
            user_id,
        )
        return int(row.get("c") or 0) if row else 0
    except Exception:
        return 0


# ── DM preferences ─────────────────────────────────────────────────────
async def get_prefs(db, user_id: int) -> dict[str, bool]:
    """Return ``{category: dm_enabled}`` for the user (defaults False)."""
    try:
        rows = await db.fetch_all(
            "SELECT category, dm_enabled FROM user_inbox_prefs "
            "WHERE user_id = $1",
            user_id,
        )
        return {str(r["category"]): bool(r["dm_enabled"]) for r in rows}
    except Exception:
        return {}


async def set_pref(
    db, user_id: int, category: str, dm_enabled: bool,
) -> None:
    """Upsert the DM-mirroring preference for one category."""
    try:
        await db.execute(
            "INSERT INTO user_inbox_prefs (user_id, category, dm_enabled) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, category) DO UPDATE SET dm_enabled = EXCLUDED.dm_enabled",
            user_id, category[:64], bool(dm_enabled),
        )
    except Exception:
        log.exception(
            "inbox: set_pref failed uid=%s category=%s", user_id, category,
        )
