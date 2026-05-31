"""Resolve recently-active players for use in AI flavor text.

The helper fetches players who have been active within the last 90 days,
resolves their Discord display names, and caches the result for 5 minutes.
"""
from __future__ import annotations

import random
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

# ── In-memory fallback cache ──────────────────────────────────────────────────
_cache: dict[int, tuple[float, list[tuple[int, str]]]] = {}  # guild_id -> (expires, [(uid, name)])
_CACHE_TTL = 300  # 5 minutes

_NUMERIC_RE = re.compile(r"^\d+$")


async def _fetch_active_display_names(
    guild: "discord.Guild",
    db,
    *,
    days: int = 90,
    limit: int = 50,
) -> list[tuple[int, str]]:
    """Query DB for active players and resolve their display names.

    Skips members who left the server or whose display name is purely numeric
    (to guarantee we never expose a raw user-ID as a name).
    """
    rows = await db.get_active_players(guild.id, days=days, limit=limit)
    results: list[tuple[int, str]] = []
    for row in rows:
        uid = row["user_id"]
        member = guild.get_member(uid)
        if member is None:
            continue
        name = member.display_name
        # Skip numeric-only names  -  could be mistaken for a user ID
        if _NUMERIC_RE.match(name):
            continue
        results.append((uid, name))
    return results


async def get_random_active_players(
    guild: "discord.Guild",
    db,
    exclude_user_id: int,
    count: int = 2,
    *,
    days: int = 90,
) -> list[str]:
    """Return up to *count* random display names of recently active players.

    * Never returns the excluded user (the command invoker).
    * Never returns numeric-only names.
    * Cached per guild for 5 minutes (in-memory).

    Returns a plain ``list[str]`` of display names ready for AI prompts.
    """
    now = time.time()
    cached = _cache.get(guild.id)
    if cached and now < cached[0]:
        pool = cached[1]
    else:
        pool = await _fetch_active_display_names(guild, db, days=days)
        _cache[guild.id] = (now + _CACHE_TTL, pool)

    # Filter out the invoking user
    eligible = [name for uid, name in pool if uid != exclude_user_id]
    if not eligible:
        return []
    chosen = random.sample(eligible, min(count, len(eligible)))
    return chosen
