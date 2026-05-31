"""
cogs/chat.py  -  AI memory refresh background task.

Responsibilities
----------------
  - Background loop: every REFRESH_AFTER_HOURS, walk every guild and refresh
    stale user memories via :func:`services.ai_memory.batch_refresh_guild`,
    then prune `ai_user_events` older than 7 days.

Why this cog is not an on_message listener
-------------------------------------------
Previously this file owned an on_message listener that handled replies to
bot messages with its own (non-streaming) AI pipeline. That produced a
second, inferior reply path that bypassed the rich help-cog pipeline
(no buttons, no sources, no tool markers, no image support, no progressive
UI) and in some cases double-replied against the image-reply path in
``cogs/social_context.py``. Both listeners have been removed: every AI
reply to a bot message now flows through :meth:`cogs.help.Help.handle_ai_reply`
(wired from :func:`core.framework.bot.Discoin.on_message`) which already owns
the full streaming rich pipeline.

Post-message housekeeping (tone ingest, count-based refresh, behavior-shift
refresh, trait pruning) was lifted into
:func:`services.ai_memory.run_post_message_tasks` so both the ``,ask``
command path and the reply/mention path can share one implementation.
"""
from __future__ import annotations

import asyncio
import logging
import random

from discord.ext import commands, tasks

from core.config import Config
from core.framework.ai import complete_default as ai_complete
from core.framework.bot import Discoin
from services.ai_memory import (
    REFRESH_AFTER_HOURS,
    batch_refresh_guild,
    prune_shift_cooldown,
)

log = logging.getLogger(__name__)


class Chat(commands.Cog):
    """Background memory refresh loop for the AI chat stack."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._refresh_loop.start()

    def cog_unload(self) -> None:
        self._refresh_loop.cancel()

    # ── Memory refresh background task ────────────────────────────────────────

    @tasks.loop(hours=REFRESH_AFTER_HOURS)
    async def _refresh_loop(self) -> None:
        """Refresh stale user memories and prune AI events across all guilds."""
        # Evict stale shift-refresh cooldown entries so the module dict stays bounded.
        prune_shift_cooldown()

        if not Config.OPENROUTER_API_KEY:
            return

        # Premium gate: AI memory refresh costs real token spend per guild.
        # Skip non-premium guilds entirely so we never burn budget on a
        # server that hasn't paid. The host guild bypasses the gate via
        # entitlements.is_premium so home-server memory still refreshes.
        from services import entitlements

        total = 0
        for guild in self.bot.guilds:
            try:
                if not await entitlements.is_premium(guild.id, self.bot.db):
                    continue
                count = await batch_refresh_guild(
                    self.bot.db, guild.id, ai_complete, stale_hours=REFRESH_AFTER_HOURS
                )
                total += count
            except Exception:
                log.debug("_refresh_loop: failed for guild %s", guild.id, exc_info=True)

            # Prune ai_user_events older than 7 days guild-wide so the table
            # stays bounded even for users who only react/use tools but never chat.
            # The prune still runs for non-premium guilds (no AI cost) so we
            # don't accumulate orphan rows for guilds that downgraded.
            try:
                await self.bot.db.execute(
                    "DELETE FROM ai_user_events WHERE guild_id=$1 "
                    "AND created_at < NOW() - INTERVAL '7 days'",
                    guild.id,
                )
            except Exception:
                log.debug("_refresh_loop: event prune failed gid=%s", guild.id)

        if total:
            log.info("Memory refresh loop: refreshed %d user memories", total)

    @_refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()
        # Stagger start by 10-30 min to avoid thundering herd on boot
        await asyncio.sleep(random.uniform(600, 1800))


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Chat(bot))
