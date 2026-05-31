"""V3 anti-alt detection signals.

Soft-flag heuristics that write into ``user_security_signals``. No
auto-bans -- the staff dashboard surfaces flagged pairs and operators
decide.

Public surface:
    flag_pair(db, gid, uid_a, uid_b, kind, *, severity=2, payload=None)
    twin_join_check(db, gid, uid, window_secs=60)
    lockstep_trade_check(db, gid, uid_a, uid_b, window_secs=120)

This module is intentionally heuristic + best-effort: every check
returns gracefully when the DB shape isn't exactly what it expects,
so a guild with a non-standard schema doesn't crash the bot.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


async def flag_pair(
    db, gid: int, uid_a: int, uid_b: int, kind: str,
    *, severity: int = 2, payload: dict | None = None,
) -> bool:
    """Record a soft anti-alt signal. Idempotent per pair+kind+day."""
    severity = max(1, min(5, int(severity)))
    try:
        # Idempotency: don't double-flag the same pair+kind in the same
        # rolling 24h window.
        existing = await db.fetch_one(
            "SELECT 1 FROM user_security_signals "
            "WHERE guild_id = $1 AND user_id = $2 AND other_user_id = $3 "
            "  AND signal_kind = $4 "
            "  AND flagged_at > now() - INTERVAL '24 hours' "
            "LIMIT 1",
            gid, uid_a, uid_b, kind,
        )
        if existing:
            return False
        await db.execute(
            "INSERT INTO user_security_signals "
            "(user_id, guild_id, signal_kind, other_user_id, payload_json, severity) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uid_a, gid, kind, uid_b,
            json.dumps(payload) if payload else None,
            severity,
        )
        return True
    except Exception:
        log.exception(
            "anti_alt: flag_pair failed gid=%s a=%s b=%s kind=%s",
            gid, uid_a, uid_b, kind,
        )
        return False


async def twin_join_check(
    db, gid: int, uid: int, *, window_secs: int = 60,
) -> int | None:
    """Return another user id that registered within ``window_secs`` of this user.

    Caller chains the result into ``flag_pair(..., kind='twin_join')`` if
    non-None. Window default of 60s is intentionally tight -- two users
    accidentally registering within a minute is rare enough to be
    worth a soft flag.
    """
    try:
        row = await db.fetch_one(
            "SELECT user_id FROM users "
            "WHERE guild_id = $1 AND user_id != $2 "
            "  AND ABS(EXTRACT(EPOCH FROM (created_at - ("
            "    SELECT created_at FROM users WHERE guild_id=$1 AND user_id=$2"
            "  )))) < $3 "
            "ORDER BY created_at ASC LIMIT 1",
            gid, uid, float(window_secs),
        )
        return int(row["user_id"]) if row else None
    except Exception:
        return None


async def unresolved_pairs(db, gid: int, *, limit: int = 50) -> list[dict]:
    """Return unresolved signals for the staff dashboard."""
    try:
        rows = await db.fetch_all(
            "SELECT * FROM user_security_signals "
            "WHERE guild_id = $1 AND resolved_at IS NULL "
            "ORDER BY severity DESC, flagged_at DESC LIMIT $2",
            gid, max(1, int(limit)),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def resolve(db, signal_id: int, by_user_id: int, *, notes: str = "") -> bool:
    """Mark a signal resolved by ``by_user_id`` (typically the operator)."""
    try:
        await db.execute(
            "UPDATE user_security_signals "
            "SET resolved_at = $2, resolved_by = $3, notes = $4 "
            "WHERE id = $1",
            signal_id, datetime.now(timezone.utc), by_user_id, notes[:1024],
        )
        return True
    except Exception:
        return False
