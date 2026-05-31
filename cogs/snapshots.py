"""cogs/snapshots.py - Periodic economy snapshots for rollback support."""
from __future__ import annotations

import logging

from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin

log = logging.getLogger(__name__)


class SnapshotCog(commands.Cog):
    """Takes economy snapshots every 30 minutes per guild."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.snapshot_loop.start()

    async def cog_unload(self) -> None:
        self.snapshot_loop.cancel()

    @tasks.loop(minutes=Config.SNAPSHOT_INTERVAL_MINUTES)
    async def snapshot_loop(self) -> None:
        for guild in self.bot.guilds:
            try:
                snap_id = await self.bot.db.snapshots.take_snapshot(guild.id)
                log.debug("Snapshot %s taken for guild %s", snap_id, guild.id)
            except Exception:
                log.exception("Failed to take snapshot for guild %s", guild.id)

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(SnapshotCog(bot))
