"""
cogs/chat_income.py  -  Silent chat income.

Players earn small wallet credits by chatting in the configured income channel
(or any thread beneath it).  Replying to the bot or reacting to a bot message
grants an extra bonus on top of the base chat tick.

Design:
  - Per-user cooldown prevents spam farming (one tick every _TICK_COOLDOWN s)
  - Income channel is stored in ``guild_settings.income_channel`` and managed
    via ``.admin setchannel income #channel``
  - Rewards are small and randomised within a narrow band so the feed stays
    organic.  Bot-reply / bot-reaction bonus is a flat multiplier on top.
  - All credits are silent: no messages sent, only wallet updates and a
    ``chat_income`` ledger entry so players can audit it via tx history.
  - Redis-backed cooldowns with in-memory fallback. The Redis path survives
    restarts; the in-memory fallback only persists for the current process.
"""
from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.scale import to_raw

log = logging.getLogger(__name__)

# ── Reward band (human USD) ───────────────────────────────────────────────────
_BASE_MIN = 0.02           # floor per tick
_BASE_MAX = 0.08           # ceiling per tick
_BOT_REPLY_MULT = 3.0      # reply-to-bot bonus multiplier
_BOT_REACT_MULT = 2.0      # react-to-bot bonus multiplier
_MIN_CHARS = 4             # messages shorter than this don't count

# ── Cooldowns ────────────────────────────────────────────────────────────────
_TICK_COOLDOWN = 45        # seconds between chat ticks per user
_REACT_COOLDOWN = 120      # seconds between react ticks per user

_REDIS_TICK_PREFIX = "discoin:chat_income:tick"
_REDIS_REACT_PREFIX = "discoin:chat_income:react"

# In-memory fallback when Redis is unavailable.  Keyed by (guild_id, user_id).
_last_tick: dict[tuple[int, int], float] = {}
_last_react: dict[tuple[int, int], float] = {}


class ChatIncome(commands.Cog):
    """Silent wallet income for chatting in the configured income channel."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── Redis helpers (graceful fallback to in-memory) ────────────────────────

    def _redis(self):
        return getattr(self.bot.bus, "_redis", None)

    async def _cooldown_ok(
        self, prefix: str, window: int, guild_id: int, user_id: int,
        fallback: dict[tuple[int, int], float],
    ) -> bool:
        """Return True and mark the cooldown if the user is ready for another tick."""
        r = self._redis()
        if r is not None:
            try:
                key = f"{prefix}:{guild_id}:{user_id}"
                placed = await r.set(key, "1", ex=window, nx=True)
                if placed:
                    fallback[(guild_id, user_id)] = time.time()
                    return True
                return False
            except Exception:
                pass  # Redis down - fall through to in-memory
        now = time.time()
        key = (guild_id, user_id)
        if now - fallback.get(key, 0.0) < window:
            return False
        fallback[key] = now
        return True

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _income_channel_id(self, guild_id: int) -> int | None:
        try:
            settings = await self.bot.db.get_guild_settings(guild_id)
        except Exception:
            return None
        ch_id = settings.get("income_channel")
        return int(ch_id) if ch_id else None

    @staticmethod
    def _in_income_scope(channel: discord.abc.GuildChannel | discord.Thread, target_id: int) -> bool:
        """True if *channel* is the income channel or a thread under it."""
        if channel.id == target_id:
            return True
        if isinstance(channel, discord.Thread) and channel.parent_id == target_id:
            return True
        return False

    async def _credit(
        self, guild_id: int, user_id: int, amount_human: float, reason: str,
    ) -> None:
        """Credit *amount_human* USD to the user's wallet and log a ledger entry."""
        if amount_human <= 0:
            return
        raw = to_raw(amount_human)
        if raw <= 0:
            return
        try:
            await self.bot.db.ensure_user(user_id, guild_id)
            await self.bot.db.update_wallet(user_id, guild_id, raw)
        except Exception as exc:
            log.debug("chat_income credit failed uid=%s gid=%s: %s", user_id, guild_id, exc)
            return
        try:
            await self.bot.db.log_tx(
                guild_id, user_id, "chat_income",
                symbol_in="USD", amount_in=raw,
            )
        except Exception:
            log.debug("chat_income log_tx failed uid=%s gid=%s", user_id, guild_id, exc_info=True)
        log.debug(
            "[chat_income] %s credited $%.4f to uid=%s gid=%s",
            reason, amount_human, user_id, guild_id,
        )

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        if not message.content or len(message.content.strip()) < _MIN_CHARS:
            return
        if message.webhook_id:
            return

        guild_id = message.guild.id
        clanktank = self.bot.get_cog("Clanktank")
        if clanktank and await clanktank.is_clanker(message.author.id, guild_id):
            return

        target_id = await self._income_channel_id(guild_id)
        if not target_id:
            return
        if not self._in_income_scope(message.channel, target_id):
            return

        # Ignore command invocations so the feed stays conversational.
        content = message.content.lstrip()
        if content.startswith((",", ".", "/", "$", "!", "?")):
            return

        if not await self._cooldown_ok(
            _REDIS_TICK_PREFIX, _TICK_COOLDOWN, guild_id, message.author.id, _last_tick,
        ):
            return

        amount = random.uniform(_BASE_MIN, _BASE_MAX)
        reason = "chat"

        # Reply-to-bot bonus.
        bot_user = self.bot.user
        if bot_user and message.reference is not None:
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message) and resolved.author.id == bot_user.id:
                amount *= _BOT_REPLY_MULT
                reason = "chat+botreply"

        await self._credit(guild_id, message.author.id, amount, reason)

    # ── Reaction listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or payload.member is None or payload.member.bot:
            return
        bot_user = self.bot.user
        if bot_user is None:
            return

        target_id = await self._income_channel_id(payload.guild_id)
        if not target_id:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or not self._in_income_scope(channel, target_id):
            return

        # Only credit reactions to bot messages.
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception:
            return
        if msg.author.id != bot_user.id:
            return

        if not await self._cooldown_ok(
            _REDIS_REACT_PREFIX, _REACT_COOLDOWN,
            payload.guild_id, payload.member.id, _last_react,
        ):
            return

        amount = random.uniform(_BASE_MIN, _BASE_MAX) * _BOT_REACT_MULT
        await self._credit(payload.guild_id, payload.member.id, amount, "react")


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChatIncome(bot))
