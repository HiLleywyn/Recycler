"""cogs/_ui.py -- small Components V2 helpers shared by the feature cogs.

Keeps the cogs free of repeated view-wiring: a confirm dialog and a simple
paginator, both built on the framework's Components V2 surface so the bot's
UI is consistent and modern by default.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Sequence

import discord

from core.framework.components import Container, render, send_v2
from core.framework.ui import C_NEUTRAL, C_WARNING


async def confirm_v2(
    ctx,
    *,
    title: str,
    body: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    danger: bool = True,
    timeout: float = 30.0,
) -> bool:
    """Show a Components V2 confirm panel; return True only if the invoker
    presses confirm before the timeout."""
    result = {"value": False}
    done = asyncio.Event()
    author_id = ctx.author.id

    async def _guard(interaction: discord.Interaction) -> bool:
        if interaction.user.id != author_id:
            await interaction.response.send_message(
                "This prompt isn't yours.", ephemeral=True
            )
            return False
        return True

    async def _yes(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        result["value"] = True
        done.set()
        await interaction.response.defer()

    async def _no(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        result["value"] = False
        done.set()
        await interaction.response.defer()

    panel = (
        Container(accent_color=C_WARNING)
        .text(f"## {title}")
        .separator()
        .text(body)
        .add_row(
            Container.make_button(
                confirm_label, custom_id="cfm:yes",
                style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
                callback=_yes,
            ),
            Container.make_button(
                cancel_label, custom_id="cfm:no",
                style=discord.ButtonStyle.secondary, callback=_no,
            ),
        )
    )
    view = render(panel, timeout=timeout)
    msg = await send_v2(ctx, view)

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        result["value"] = False

    closing = (
        Container(accent_color=C_NEUTRAL)
        .text(f"## {title}")
        .separator()
        .text("Confirmed." if result["value"] else "Cancelled / timed out.")
    )
    try:
        if msg is not None:
            await msg.edit(view=render(closing))
    except discord.HTTPException:
        pass
    return result["value"]


async def paginate_v2(
    ctx,
    pages: Sequence[Container],
    *,
    timeout: float = 120.0,
) -> None:
    """Paginate a list of pre-built Containers with prev/next buttons."""
    pages = list(pages)
    if not pages:
        return
    if len(pages) == 1:
        await send_v2(ctx, pages[0])
        return

    state = {"i": 0}
    author_id = ctx.author.id
    # Remember each page's original child count so re-rendering doesn't stack
    # multiple navigation rows onto the same Container instance.
    base_lens = [len(p._children) for p in pages]

    def _build() -> Container:
        page = pages[state["i"]]
        del page._children[base_lens[state["i"]]:]
        return page.add_row(
            Container.make_button(
                "Prev", custom_id="pg:prev", style=discord.ButtonStyle.secondary,
                disabled=state["i"] == 0, callback=_prev,
            ),
            Container.make_button(
                f"{state['i'] + 1}/{len(pages)}", custom_id="pg:count",
                style=discord.ButtonStyle.secondary, disabled=True,
            ),
            Container.make_button(
                "Next", custom_id="pg:next", style=discord.ButtonStyle.secondary,
                disabled=state["i"] == len(pages) - 1, callback=_next,
            ),
        )

    async def _turn(interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != author_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        state["i"] = max(0, min(len(pages) - 1, state["i"] + delta))
        await interaction.response.edit_message(view=render(_build(), timeout=timeout))

    async def _prev(interaction: discord.Interaction) -> None:
        await _turn(interaction, -1)

    async def _next(interaction: discord.Interaction) -> None:
        await _turn(interaction, +1)

    await send_v2(ctx, render(_build(), timeout=timeout))
