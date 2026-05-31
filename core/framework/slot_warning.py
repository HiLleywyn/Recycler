"""core/framework/slot_warning.py -- shared "your slots are full" notice.

Combat surfaces (fishing wild battle, delve wild-buddy battle, farm wild
battle) all want to tell the player when their owned-buddy shelter or
held-egg cap is full so a winning capture roll won't drop into the
shelter. The check + the embed it ships are identical across the three
cogs, so the helper lives here.

Usage from any cog:

    from core.framework.slot_warning import maybe_warn_full_slots
    await maybe_warn_full_slots(ctx, surface="fishing", phase="game_start")

``surface`` distinguishes the dedupe key (so a player who fishes and
then delves doesn't get the same warning twice in 30 seconds), and
``phase`` is one of:
    "game_start"  -- bare ,fish / ,delve / ,farm panel
    "fight_start" -- entering a wild-buddy battle
    "fight_end"   -- the post-battle result embed

Per-(uid, gid, surface, phase) dedupe runs on a 10-minute idle window so
a player who clears a slot mid-session and then fills it again still
gets the next warning.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_AMBER

log = logging.getLogger(__name__)


_SLOT_WARN_TS: dict[tuple[int, int, str, str], float] = {}
_SLOT_WARN_DEDUPE_S: float = 600.0


async def maybe_warn_full_slots(
    ctx: DiscoContext, *, surface: str, phase: str,
) -> None:
    """Post an inline warning when the user's owned-buddy or held-egg
    cap is full. Best-effort; swallows DB / Discord errors so the
    surface still works if the slot probe is flaky.
    """
    try:
        from services.buddy_lifecycle import slot_pressure as _sp
        info = await _sp(ctx.db, ctx.guild_id, ctx.author.id)
    except Exception:
        log.debug("slot_pressure probe failed", exc_info=True)
        return
    if not info.get("warning"):
        return
    key = (int(ctx.author.id), int(ctx.guild_id), str(surface), str(phase))
    now = time.time()
    last = _SLOT_WARN_TS.get(key, 0.0)
    if now - last < _SLOT_WARN_DEDUPE_S:
        return
    _SLOT_WARN_TS[key] = now
    try:
        await ctx.reply(
            embed=card(
                "⚠️ Slots Full",
                color=C_AMBER,
                description=str(info["warning"]),
            ).build(),
            mention_author=False,
        )
    except Exception:
        log.debug("slot warning send failed", exc_info=True)


async def slot_warning_text(
    db: Any, guild_id: int, user_id: int,
) -> str:
    """Return the same warning string as :func:`maybe_warn_full_slots`
    without sending a message.

    Used by post-fight result embeds that want to splice the warning
    into their existing description / footer rather than firing a
    second message. Returns ``""`` when nothing is full.
    """
    try:
        from services.buddy_lifecycle import slot_pressure as _sp
        info = await _sp(db, guild_id, user_id)
    except Exception:
        log.debug("slot_pressure probe failed", exc_info=True)
        return ""
    return str(info.get("warning") or "")
