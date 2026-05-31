"""
cogs/disco_ai.py -- DiscoAI memory sidecar.

This cog does NOT own a command surface. Generation stays on the existing
core.framework.ai (OpenRouter) pipeline in cogs/help.py, and the admin
controls for this memory store now live under ``,ai memory`` (see
cogs/ai.py). What this cog provides:

    - A MemoryService wired to the bot's Postgres + Redis so any cog
      can read/write disco_facts, disco_episodes, and short-term
      per-user conversation turns.
    - A TrainingLogger so any cog can append full (system, user,
      assistant) turns + tool calls to disco_training_turns for later
      curation / offline training.
    - A ToolRegistry with the standard Discoin-facing tools, ready to
      hand to any model backend that speaks OpenAI tool-call format.
    - Passive episode listener: when DISCOAI_PASSIVE_LEARNING is on
      and a channel is opted in, logs ambient messages as episodes
      for the memory recall tools to later surface.

Other cogs retrieve the services via `bot.get_cog('DiscoAI')` and
access `.memory`, `.training`, `.tools`, `.settings`, `.facts_for_prompt()`.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ai import (
    DiscoAISettings,
    MemoryService,
    ToolRegistry,
    TrainingLogger,
    build_default_registry,
    guild_scope,
    user_scope,
)
from ai.tools import _ApiClient
from core.config import Config
from core.framework.bot import Discoin

log = logging.getLogger(__name__)


class DiscoAI(commands.Cog):
    """Memory + learning sidecar for the existing chat AI."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._settings = DiscoAISettings.from_config()
        self._api: _ApiClient | None = None
        self._memory: MemoryService | None = None
        self._training: TrainingLogger | None = None
        self._tools: ToolRegistry | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        redis = getattr(self.bot.bus, "_redis", None)
        self._api = _ApiClient(self._settings.api_base_url, timeout_s=10)
        await self._api.start()
        self._memory = MemoryService(
            db=self.bot.db,
            redis=redis,
            short_term_turns=self._settings.short_term_turns,
            short_term_ttl_s=self._settings.short_term_ttl_s,
        )
        self._training = TrainingLogger(self.bot.db)
        self._tools = build_default_registry(self._api, self._memory)
        log.info("DiscoAI memory sidecar ready")

    async def cog_unload(self) -> None:
        if self._api is not None:
            await self._api.close()

    # ── Public accessors (for other cogs) ──────────────────────────────

    @property
    def memory(self) -> MemoryService | None:
        return self._memory

    @property
    def training(self) -> TrainingLogger | None:
        return self._training

    @property
    def tools(self) -> ToolRegistry | None:
        return self._tools

    @property
    def settings(self) -> DiscoAISettings:
        return self._settings

    async def facts_for_prompt(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        user_limit: int = 5,
        guild_limit: int = 5,
    ) -> str:
        """Render per-user + per-guild facts as a system-prompt snippet.

        Callers splice the returned string into the OpenRouter system prompt so
        anything `remember_fact` has captured resurfaces on later turns.
        Empty string if there's nothing to surface.
        """
        if self._memory is None:
            return ""
        lines: list[str] = []
        try:
            if user_id:
                u_facts = await self._memory.get_facts(user_scope(user_id, guild_id), limit=user_limit)
                for f in u_facts:
                    lines.append(f"- about you -- {f.key}: {f.value}")
            if guild_id is not None:
                g_facts = await self._memory.get_facts(guild_scope(int(guild_id)), limit=guild_limit)
                for f in g_facts:
                    lines.append(f"- about this server -- {f.key}: {f.value}")
        except Exception as exc:
            log.debug("DiscoAI facts_for_prompt failed: %s", exc)
            return ""
        if not lines:
            return ""
        return "Things you remember:\n" + "\n".join(lines)

    # ── Passive-learning listener ──────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._settings.passive_learning:
            return
        if message.author.bot or not message.guild or self._memory is None:
            return
        bot_user = self.bot.user
        if bot_user and bot_user.mentioned_in(message):
            return  # mentions are handled by help.py; don't double-log
        if message.content.startswith(Config.PREFIX):
            return
        try:
            opted_in = await self.bot.db.fetch_val(
                """
                SELECT 1 FROM disco_passive_channels
                WHERE guild_id = $1 AND channel_id = $2
                """,
                int(message.guild.id), int(message.channel.id),
            )
        except Exception:
            opted_in = None
        if not opted_in:
            return
        content = (message.content or "").strip()
        if not content:
            return
        summary = (
            f"{message.author.display_name}: "
            f"{content if len(content) <= 280 else content[:277] + '...'}"
        )
        try:
            await self._memory.record_episode(
                scope=guild_scope(message.guild.id),
                summary=summary,
                tags=["passive", f"channel:{message.channel.id}"],
            )
        except Exception as exc:
            log.debug("DiscoAI passive episode record failed: %s", exc)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(DiscoAI(bot))
