"""
core/framework/leaderboard.py  -  shared LB-row filtering.

Every leaderboard surface in the bot needs the same filter: drop the
``user_id == 0`` placeholder rows that have leaked into the DB since
day 1, drop the bot's own rows (Disco arbs its own oracle pumps and
shows up on a few volume LBs), drop any other bots in the guild, and
drop members who left or were banned (Discord membership covers both
-- ``guild.get_member`` returns None for both states once cache is
warm). Centralised here so all 15+ LB call sites stay in sync.

Usage:
    from core.framework.leaderboard import filter_lb_user_ids

    keep = await filter_lb_user_ids(
        ctx,
        [r["user_id"] for r in rows],
    )
    rows = [r for r in rows if int(r["user_id"]) in keep]
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)


async def filter_lb_user_ids(
    ctx: Any, user_ids: Iterable[int],
) -> set[int]:
    """Return the subset of ``user_ids`` that should appear on a leaderboard.

    Excludes:
      * ``uid <= 0``                 -- placeholder / corrupted rows
      * the bot itself               -- ``ctx.bot.user.id``
      * any other bot in the guild   -- ``member.bot is True``
      * members no longer in guild   -- left or banned
    """
    ids = [int(u) for u in (user_ids or [])]
    if not ids:
        return set()
    guild = getattr(ctx, "guild", None)
    bot_user = getattr(getattr(ctx, "bot", None), "user", None)
    bot_id = int(bot_user.id) if bot_user is not None else 0

    # One round-trip to populate any uncached members so the get_member
    # check below resolves the same way for current members and
    # ex-members alike.
    if guild is not None:
        missing = [u for u in ids if guild.get_member(u) is None]
        if missing:
            try:
                await guild.query_members(user_ids=missing, cache=True)
            except Exception:
                log.debug("filter_lb_user_ids: query_members failed", exc_info=True)

    keep: set[int] = set()
    for uid in ids:
        if uid <= 0:
            continue
        if bot_id and uid == bot_id:
            continue
        if guild is None:
            keep.add(uid)
            continue
        member = guild.get_member(uid)
        if member is None:
            continue
        if getattr(member, "bot", False):
            continue
        keep.add(uid)
    return keep


__all__ = ("filter_lb_user_ids",)
