"""cogs/hub.py -- ``,today`` daily front-door command.

Thin shim that opens the unified Discoin panel exposed by
``cogs/overview.py``. The standalone hub UI was retired in May 2026
when the today / start panels were merged so streak, quests, claim,
and game-launching all share one tabbed view.

Aliases ``routine`` and ``login`` are kept for muscle memory.
"""
from __future__ import annotations

import logging

from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, no_bots

log = logging.getLogger(__name__)


class Hub(commands.Cog):
    """Daily front-door command -- delegates to the unified panel."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_command(name="today", aliases=["routine", "login"])
    @guild_only
    @no_bots
    @ensure_registered
    async def hub(self, ctx: DiscoContext) -> None:
        """Open the unified Discoin panel."""
        from cogs.overview import open_unified_panel
        await open_unified_panel(ctx)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Hub(bot))
