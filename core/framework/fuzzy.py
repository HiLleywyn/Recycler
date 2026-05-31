"""Fuzzy subcommand matching for command groups.

When a user misspells a subcommand (e.g. ``$bank savngs`` instead of
``$bank savings``), the group handler runs but ``ctx.invoked_subcommand``
is ``None``.  This module provides :func:`suggest_subcommand` which
detects the mistyped word, fuzzy-matches it against valid subcommands,
and offers a one-click correction via the same "Did you mean?" UI used
for top-level command typos.
"""
from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from constants.ui import C_AMBER
from core.framework.embed import card

if TYPE_CHECKING:
    from core.framework.context import DiscoContext


async def suggest_subcommand(ctx: "DiscoContext", group: commands.Group) -> bool:
    """Offer a fuzzy suggestion if the user mistyped a subcommand.

    Call this at the top of every ``invoke_without_command=True`` group
    handler::

        async def bank(self, ctx):
            if await suggest_subcommand(ctx, self.bank):
                return
            # ... normal help text ...

    Returns ``True`` if a suggestion was sent (caller should ``return``).
    Returns ``False`` if no typo was detected (user typed just the group
    name, or there was no close match).
    """
    if ctx.invoked_subcommand is not None:
        return False  # A real subcommand was matched  -  nothing to do

    # Figure out what the user typed after the group command
    raw = ctx.message.content if ctx.message else ""
    prefix = ctx.prefix or "."
    after_prefix = raw[len(prefix):].strip() if raw.startswith(prefix) else raw.strip()
    parts = after_prefix.split()

    # How many words make up the group's full name (e.g. "chain contract" = 2)
    group_depth = len(group.qualified_name.split())
    if len(parts) <= group_depth:
        return False  # User typed just the group name, no subcommand attempted

    attempted = parts[group_depth].lower()
    rest = " ".join(parts[group_depth + 1:])

    # Collect all valid subcommand names + aliases
    all_names: list[str] = []
    for sub in group.commands:
        all_names.append(sub.name)
        all_names.extend(sub.aliases)

    if not all_names:
        return False

    matches = difflib.get_close_matches(attempted, all_names, n=1, cutoff=0.6)
    if not matches:
        return False

    suggestion = f"{group.qualified_name} {matches[0]}"
    full_label = f"{suggestion} {rest}".strip() if rest else suggestion
    invoked_label = f"{group.qualified_name} {attempted}"

    # Build the "Did you mean?" embed + buttons (same UX as top-level fuzzy)
    import copy

    class _FuzzyView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=30.0)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("Not your prompt.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
        async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(view=self)
            new_msg = copy.copy(ctx.message)
            new_msg.content = f"{prefix}{full_label}"  # type: ignore[attr-defined]
            await ctx.bot.process_commands(new_msg)
            self.stop()

        @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
        async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(view=self)
            self.stop()

        async def on_timeout(self) -> None:
            self.stop()

    desc = f"Command `.{invoked_label}` not found. Did you mean **`.{full_label}`**?"
    embed = card("", description=desc, color=C_AMBER).build()
    try:
        await ctx.reply(embed=embed, view=_FuzzyView(), mention_author=False)
    except Exception:
        pass
    return True
