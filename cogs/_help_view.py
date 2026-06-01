"""cogs/_help_view.py -- the modern, dynamic help hub (Components V2).

Presents the whole bot as ONE surface: a single header plus a multi-select of
feature sections. Picking one or more sections combines their commands into a
single seamless list, generated live from the bot's command tree. The same hub
backs ``.help`` and a bare ``.clank``.
"""
from __future__ import annotations

from typing import Any

import discord

from core.framework.components import Container, render, send_v2
from core.framework.ui import C_INFO
from clanklib.help import SECTIONS, command_lines, selected_sections


def _invite_url(bot: Any) -> str:
    from clanklib.settings import setting
    cid = getattr(bot.user, "id", None) or setting(bot, "DISCORD_CLIENT_ID", "")
    return (
        f"https://discord.com/oauth2/authorize?client_id={cid}"
        "&permissions=8&scope=bot%20applications.commands"
    )


def _build_panel(bot: Any, prefix: str, chosen_keys: list[str], author_id: int):
    """Build the help Container for the currently-selected sections.

    With nothing selected, show every section's summary (the overview). With one
    or more selected, show the combined, generated command list for just those.
    """
    chosen = selected_sections(chosen_keys)
    container = (
        Container(accent_color=C_INFO)
        .text("## Recycler")
        .text("One bot for server backups, templates, chatlogs, sync and "
              "account containment -- all free, all Components V2.")
        .separator()
    )

    if not chosen:
        # Overview: every section's one-liner.
        for sec in SECTIONS:
            container.text(f"{sec.emoji} **{sec.label}** -- {sec.blurb}")
        container.separator()
        container.text(f"-# Pick one or more sections below to list their commands. "
                       f"Prefix `{prefix}`.")
    else:
        # Combined command list for the selected sections.
        for sec in chosen:
            lines = command_lines(bot, sec, prefix)
            body = "\n".join(lines) if lines else "_No commands available._"
            container.text(f"{sec.emoji} **{sec.label}**\n{body}")
        container.separator()
        container.text(f"-# Showing {len(chosen)} of {len(SECTIONS)} sections. "
                       f"Most management commands need the Manage Server permission.")

    # The multi-select: choose any combination of sections.
    options = [
        discord.SelectOption(
            label=sec.label, value=sec.key, emoji=sec.emoji,
            description=sec.blurb[:100], default=sec.key in chosen_keys,
        )
        for sec in SECTIONS
    ]

    async def _on_select(interaction: discord.Interaction) -> None:
        if interaction.user.id != author_id:
            await interaction.response.send_message("This menu isn't yours -- "
                                                     f"run `{prefix}help` yourself.", ephemeral=True)
            return
        picked = list(interaction.data.get("values", []))  # type: ignore[union-attr]
        new_panel = _build_panel(bot, prefix, picked, author_id)
        await interaction.response.edit_message(view=render(new_panel, timeout=300.0))

    container.select(
        custom_id="help:sections",
        placeholder="Choose sections to view their commands...",
        options=options, min_values=0, max_values=len(SECTIONS),
        callback=_on_select,
    )
    # Invite as a quick action.
    container.add_row(Container.make_button("Add to server", url=_invite_url(bot)))
    return container


async def send_help(ctx: Any) -> None:
    from clanklib.settings import prefix as _prefix
    p = _prefix(ctx.bot)
    panel = _build_panel(ctx.bot, p, [], ctx.author.id)
    await send_v2(ctx, render(panel, timeout=300.0))
