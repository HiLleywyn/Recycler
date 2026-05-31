"""services/progression.py - one-call summary for every progression system.

Returns a compact dict suitable for sprinkling into existing embeds
(,bals, ,profile, ,buddy, ,status) and into AI system prompts. Keeps
the query shape in ONE place so the balance embed and the buddy panel
and the AI context always see identical numbers.

Usage
-----
    from services.progression import user_snapshot
    snap = await user_snapshot(ctx.db, uid, gid)
    # {'achievements_earned': 7, 'achievements_total': 43,
    #  'streak_current': 3, 'streak_longest': 12,
    #  'pass_tier': 4, 'pass_max_tier': 30, 'pass_xp': 4500,
    #  'season_name': 'Spring Open', 'season_metric': 'net_worth',
    #  'active_challenges': 2}

Every field is always present; absent data renders as 0 or None so
callers can use ``snap.get(...)`` without ``.get(..., default)`` boilerplate.
"""
from __future__ import annotations

import logging
from typing import Any

import configs.achievements_config as _ach_cfg
import configs.seasonpass_config as _pass_cfg

log = logging.getLogger(__name__)


async def user_snapshot(db, user_id: int, guild_id: int) -> dict[str, Any]:
    """Return a compact per-user progression summary. Safe on any guild
    state (fresh install, no active season, etc.) -- every field has a
    sensible default."""
    total_catalog = len(_ach_cfg.ACHIEVEMENTS)

    # Achievements earned.
    earned = await db.fetch_val(
        "SELECT COUNT(*) FROM user_badges "
        "WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    earned = int(earned or 0)

    # Streak.
    streak_row = await db.fetch_one(
        "SELECT current_streak, longest_streak FROM user_streaks "
        "WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    streak_current = int(streak_row["current_streak"]) if streak_row else 0
    streak_longest = int(streak_row["longest_streak"]) if streak_row else 0

    # Active season + pass state for this user.
    season = await db.fetch_one(
        """
        SELECT season_id, name, metric, theme
        FROM seasons
        WHERE guild_id = $1 AND status = 'active'
        """,
        guild_id,
    )
    pass_xp = 0
    if season is not None:
        xp_val = await db.fetch_val(
            "SELECT xp FROM season_xp "
            "WHERE season_id = $1 AND user_id = $2",
            int(season["season_id"]), user_id,
        )
        pass_xp = int(xp_val or 0)
    pass_tier = _pass_cfg.tier_for_xp(pass_xp) if pass_xp else 0

    # Count of active challenges on this guild. Not per-user, but shown
    # on the balance card as a nudge: "2 server challenges running".
    challenges = await db.fetch_val(
        "SELECT COUNT(*) FROM guild_challenges "
        "WHERE guild_id = $1 AND status = 'active'",
        guild_id,
    )
    challenges = int(challenges or 0)

    return {
        "achievements_earned": earned,
        "achievements_total":  total_catalog,
        "streak_current":      streak_current,
        "streak_longest":      streak_longest,
        "pass_tier":           pass_tier,
        "pass_max_tier":       _pass_cfg.MAX_TIER,
        "pass_xp":             pass_xp,
        "season_id":           int(season["season_id"]) if season else None,
        "season_name":         season["name"] if season else None,
        "season_metric":       season["metric"] if season else None,
        "season_theme":        (season.get("theme") if season else None) or "classic",
        "active_challenges":   challenges,
    }


def format_inline(snap: dict[str, Any]) -> str:
    """One-line progression summary for compact embeds (profile/balance).

    Example: ``Ach 7/43 | Streak 3d | Pass T4/30 | 2 challenges``
    """
    parts = [
        f"Ach **{snap['achievements_earned']}/{snap['achievements_total']}**",
        f"Streak **{snap['streak_current']}d**",
    ]
    if snap.get("season_name"):
        parts.append(
            f"Pass **T{snap['pass_tier']}/{snap['pass_max_tier']}**"
        )
    if snap.get("active_challenges"):
        parts.append(f"**{snap['active_challenges']}** challenge" +
                     ("s" if snap["active_challenges"] != 1 else ""))
    return "  |  ".join(parts)


def ai_context_line(snap: dict[str, Any]) -> str:
    """One-sentence summary to inject into AI system prompts.

    Suitable for the DiscoAI and buddy_ai context. Keeps it short so it
    doesn't blow the prompt budget.
    """
    bits = []
    bits.append(
        f"achievements {snap['achievements_earned']}/{snap['achievements_total']}"
    )
    bits.append(f"{snap['streak_current']}d streak")
    if snap.get("season_name"):
        bits.append(f"pass tier {snap['pass_tier']}/{snap['pass_max_tier']}")
        bits.append(f"active season '{snap['season_name']}'")
    if snap.get("active_challenges"):
        bits.append(f"{snap['active_challenges']} active server challenge(s)")
    return "Progression: " + ", ".join(bits) + "."


async def guild_totals(db, guild_id: int) -> dict[str, Any]:
    """Guild-wide progression snapshot for the status page.

    Returns:
        active_seasons: 0 or 1
        active_challenges: count
        active_streaks: count of streaks refreshed in the last day
        total_badges_earned: count of user_badges rows in guild
    """
    rows = await db.fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM seasons
               WHERE guild_id = $1 AND status = 'active')            AS active_seasons,
            (SELECT COUNT(*) FROM guild_challenges
               WHERE guild_id = $1 AND status = 'active')            AS active_challenges,
            (SELECT COUNT(*) FROM user_streaks
               WHERE guild_id = $1
                 AND last_claim_date >= CURRENT_DATE - INTERVAL '1 day') AS active_streaks,
            (SELECT COUNT(*) FROM user_badges
               WHERE guild_id = $1)                                  AS total_badges_earned
        """,
        guild_id,
    )
    if rows is None:
        return {
            "active_seasons": 0, "active_challenges": 0,
            "active_streaks": 0, "total_badges_earned": 0,
        }
    return {
        "active_seasons":      int(rows["active_seasons"] or 0),
        "active_challenges":   int(rows["active_challenges"] or 0),
        "active_streaks":      int(rows["active_streaks"] or 0),
        "total_badges_earned": int(rows["total_badges_earned"] or 0),
    }
