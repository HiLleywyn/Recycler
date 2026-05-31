"""services/ai_context_render.py -- single source for AI-context inspector embeds.

The four-page "what does Disco know about this user" view used to be
copy-pasted between ,aictx (cogs/bank.py) and ,dev aictx (cogs/dev.py). It now
lives here so the player-facing ,disco ctx command and the developer tool
render the exact same pages from the exact same data.

These builders are read-only -- nothing here feeds an AI call or mutates
state.
"""
from __future__ import annotations

import logging

import discord

from core.framework.embed import card
from core.framework.ui import (
    C_AMBER,
    C_INFO,
    C_NEUTRAL,
    C_PURPLE,
    fmt_ts,
)

log = logging.getLogger(__name__)


def _fmt_traits(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    return "\n".join(
        f"`{t['trait_key']}` conf:{float(t.get('confidence', 0)):.2f} "
        f"wt:{float(t.get('weight', 0)):.2f} n={int(t.get('sample_size', 0))}"
        for t in rows
    )


async def build_aictx_pages(
    db,
    user_id: int,
    guild_id: int,
    name: str,
    *,
    avatar_url: str | None = None,
    footer: str = "",
) -> list[discord.Embed]:
    """Return the four-page AI-context inspector view for one member.

    Pages: 1) the exact context string Disco would inject, 2) layered traits,
    3) text memory + reaction ratios + tool use, 4) the recent signal log.
    """
    from services.ai_memory import build_user_context
    from services.ai_traits import build_reaction_ratios

    memory, all_traits, reaction_rows, tool_rows, events = await _gather(db, user_id, guild_id)

    try:
        ctx_str = await build_user_context(db, user_id, guild_id, name)
    except Exception as exc:  # noqa: BLE001
        log.debug("build_user_context failed for %s/%s: %s", user_id, guild_id, exc)
        ctx_str = f"(unavailable: {exc})"

    def _author(builder):
        return builder.author(name, icon_url=avatar_url) if avatar_url else builder.author(name)

    p1 = _author(card("AI Context Preview", color=C_INFO)).description(
        f"```\n{ctx_str[:1800] or '(empty - no traits or memory yet)'}\n```"
    )
    if footer:
        p1 = p1.footer(footer)

    stable = [t for t in all_traits if t["layer"] == "stable"]
    volatile = [t for t in all_traits if t["layer"] == "volatile"]
    interaction = [t for t in all_traits if t["layer"] == "interaction"]
    b2 = _author(card(f"AI Traits ({len(all_traits)} tracked)", color=C_PURPLE))
    b2 = (
        b2.field("Stable", _fmt_traits(stable)[:1020], False)
        .field("Volatile", _fmt_traits(volatile)[:1020], False)
        .field("Interaction", _fmt_traits(interaction)[:1020], False)
    )
    if footer:
        b2 = b2.footer(footer)

    rx_ratios = build_reaction_ratios(reaction_rows) or "(none)"
    tool_lines = "\n".join(
        f"`{r['tool_key']}` x{r['use_count']}  last: {fmt_ts(r['last_used'])}"
        for r in tool_rows
    ) or "(none)"
    b3 = _author(card("AI Memory", color=C_AMBER))
    b3 = (
        b3.field("Text Memory", f"```\n{(memory or '(empty)')[:900]}\n```", False)
        .field("Reaction Ratios", rx_ratios[:500], True)
        .field("Top Tools", tool_lines[:500], True)
    )
    if footer:
        b3 = b3.footer(footer)

    event_lines = "\n".join(
        f"`{r['event_type']}.{r['event_subtype']}`  {fmt_ts(r['created_at'])}"
        for r in events
    ) or "(no events recorded yet)"
    try:
        total_ev = int(await db.fetch_val(
            "SELECT COUNT(*) FROM ai_user_events WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        ) or 0)
    except Exception:  # noqa: BLE001
        total_ev = 0
    b4 = _author(card(f"AI Events ({total_ev} logged)", color=C_NEUTRAL))
    b4 = b4.field("Last 10 Signals", event_lines[:1020], False)
    if footer:
        b4 = b4.footer(footer)

    return [p1.build(), b2.build(), b3.build(), b4.build()]


async def _gather(db, user_id: int, guild_id: int):
    """Fetch every inspector data source in parallel."""
    import asyncio

    try:
        return await asyncio.gather(
            db.get_ai_user_memory(user_id, guild_id),
            db.get_ai_traits(user_id, guild_id, min_confidence=0.0, limit=50),
            db.get_ai_reaction_memory(user_id, guild_id, limit=10),
            db.get_ai_tool_memory(user_id, guild_id, limit=10),
            db.fetch_all(
                "SELECT event_type, event_subtype, created_at "
                "FROM ai_user_events WHERE user_id=$1 AND guild_id=$2 "
                "ORDER BY created_at DESC LIMIT 10",
                user_id, guild_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("aictx gather failed for %s/%s: %s", user_id, guild_id, exc)
        return None, [], [], [], []
