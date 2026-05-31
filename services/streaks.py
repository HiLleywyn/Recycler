"""services/streaks.py - daily login streaks.

Increments a per-user counter each day the user claims ``,daily``. The
gap-to-yesterday check uses ``DATE`` arithmetic on the DB clock so
timezone skew and container drift can never miscount a streak.

Flow
----
1. Bus event ``daily_claimed`` fires (from cogs/earn.py).
2. ``update_on_claim(db, uid, gid)`` is called.
3. If the user claimed today already (same date), return current state
   unchanged. If yesterday was the last claim, streak += 1. Otherwise
   reset to 1.
4. ``longest_streak`` is updated only when the new current is greater.
5. Returns a summary dict so callers (achievements service) can check
   threshold achievements without a second DB read.

Public API
----------
``update_on_claim(db, user_id, guild_id)``  -> dict
``get(db, user_id, guild_id)``              -> dict | None
``top(db, guild_id, limit=10)``             -> list[dict]
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def update_on_claim(db, user_id: int, guild_id: int) -> dict:
    """Update the streak for ``user_id`` after a daily claim.

    Assumes ``daily_claimed`` fires at most once per day per user (the
    daily-command cooldown gates this upstream). Consecutive = current+1;
    a gap of more than one day = reset to 1. The longest bar ratchets
    only upward.

    Returns ``{current, longest, total_claims}``.
    """
    row = await db.fetch_one(
        """
        INSERT INTO user_streaks (
            user_id, guild_id, current_streak, longest_streak,
            last_claim_date, total_claims, updated_at
        )
        VALUES ($1, $2, 1, 1, CURRENT_DATE, 1, NOW())
        ON CONFLICT (user_id, guild_id) DO UPDATE SET
            current_streak = CASE
                WHEN user_streaks.last_claim_date = CURRENT_DATE - INTERVAL '1 day'
                    THEN user_streaks.current_streak + 1
                WHEN user_streaks.last_claim_date = CURRENT_DATE
                    THEN user_streaks.current_streak
                ELSE 1
            END,
            longest_streak = GREATEST(
                user_streaks.longest_streak,
                CASE
                    WHEN user_streaks.last_claim_date = CURRENT_DATE - INTERVAL '1 day'
                        THEN user_streaks.current_streak + 1
                    WHEN user_streaks.last_claim_date = CURRENT_DATE
                        THEN user_streaks.current_streak
                    ELSE 1
                END
            ),
            last_claim_date = CURRENT_DATE,
            total_claims = user_streaks.total_claims
                + (CASE WHEN user_streaks.last_claim_date = CURRENT_DATE THEN 0 ELSE 1 END),
            updated_at = NOW()
        RETURNING current_streak, longest_streak, total_claims
        """,
        user_id, guild_id,
    )
    if row is None:
        return {"current": 0, "longest": 0, "total_claims": 0}
    return {
        "current": int(row["current_streak"]),
        "longest": int(row["longest_streak"]),
        "total_claims": int(row["total_claims"]),
    }


async def get(db, user_id: int, guild_id: int) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT current_streak, longest_streak, last_claim_date, total_claims
        FROM user_streaks
        WHERE user_id = $1 AND guild_id = $2
        """,
        user_id, guild_id,
    )
    if row is None:
        return None
    return {
        "current": int(row["current_streak"]),
        "longest": int(row["longest_streak"]),
        "last_claim_date": row["last_claim_date"],
        "total_claims": int(row["total_claims"]),
    }


async def top(db, guild_id: int, limit: int = 10) -> list[dict]:
    """Return the top ``limit`` active streaks for a guild."""
    rows = await db.fetch_all(
        """
        SELECT user_id, current_streak, longest_streak, total_claims
        FROM user_streaks
        WHERE guild_id = $1
          AND last_claim_date >= CURRENT_DATE - INTERVAL '1 day'
        ORDER BY current_streak DESC
        LIMIT $2
        """,
        guild_id, int(limit),
    )
    return rows or []
