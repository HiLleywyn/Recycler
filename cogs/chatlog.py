"""cogs/chatlog.py -- archive and replay a channel's messages."""
from __future__ import annotations

import re
import secrets

import discord
from discord.ext import commands

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_GOLD, C_INFO, C_SUCCESS, fmt_ts
from clanklib import serializer
from clanklib.settings import prefix as _prefix

_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _new_id() -> str:
    return secrets.token_hex(4)


class Chatlog(ModCog):
    @commands.group(name="chatlog", aliases=["log", "cl"], invoke_without_command=True)
    async def chatlog(self, ctx: DiscoContext) -> None:
        await self.chatlog_list(ctx)

    @chatlog.command(name="create", aliases=["save", "archive"])
    @commands.has_guild_permissions(manage_messages=True)
    @commands.bot_has_guild_permissions(read_message_history=True)
    async def chatlog_create(
        self, ctx: DiscoContext, channel: discord.TextChannel = None, limit: int = 100
    ) -> None:
        """Archive the last ``limit`` messages of a channel (default: here, 100)."""
        channel = channel or ctx.channel
        limit = max(1, min(int(limit), serializer.MAX_MESSAGE_LIMIT))
        async with ctx.typing():
            messages = await serializer._serialize_messages(channel, limit)
            cid = _new_id()
            await self.db.chatlog.create(
                chatlog_id=cid, owner_id=ctx.author.id, guild_id=ctx.guild.id,
                channel_id=channel.id, channel_name=channel.name, messages=messages,
            )
        await send_v2(ctx, Container(accent_color=C_SUCCESS)
                      .text("## Chatlog saved")
                      .text(f"**ID** `{cid}` · {len(messages)} messages from {channel.mention}")
                      .separator()
                      .text(f"-# Replay it with `{self._p()}chatlog load {cid} #channel`."))

    @chatlog.command(name="load", aliases=["restore", "replay"])
    @commands.has_guild_permissions(manage_webhooks=True)
    @commands.bot_has_guild_permissions(manage_webhooks=True)
    async def chatlog_load(
        self, ctx: DiscoContext, chatlog_id: str, channel: discord.TextChannel = None
    ) -> None:
        if not _ID_RE.match(chatlog_id.lower()):
            await send_v2(ctx, Container(accent_color=C_ERROR).text("Invalid chatlog id."))
            return
        row = await self.db.chatlog.get(chatlog_id.lower())
        if row is None or int(row["owner_id"]) != ctx.author.id:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No chatlog `{chatlog_id}` owned by you."))
            return
        target = channel or ctx.channel
        async with ctx.typing():
            sent = await serializer._replay_messages(target, row["data"])
        await send_v2(ctx, Container(accent_color=C_SUCCESS if sent else C_GOLD)
                      .text("## Chatlog replayed")
                      .text(f"Sent {sent} messages to {target.mention} via webhook."))

    @chatlog.command(name="list", aliases=["ls"])
    async def chatlog_list(self, ctx: DiscoContext) -> None:
        rows = await self.db.chatlog.list_for_owner(ctx.author.id)
        if not rows:
            await send_v2(ctx, Container(accent_color=C_INFO).text("## No chatlogs yet")
                          .text(f"Save one with `{self._p()}chatlog create`."))
            return
        lines = [
            f"`{r['id']}` · #{r.get('channel_name') or '?'} · "
            f"{r.get('message_count', 0)} msgs · {fmt_ts(r['created_at'])}"
            for r in rows
        ]
        await send_v2(ctx, Container(accent_color=C_INFO)
                      .text(f"## Your chatlogs ({len(rows)})")
                      .text("\n".join(lines)[:3900]))

    @chatlog.command(name="delete", aliases=["del", "rm"])
    async def chatlog_delete(self, ctx: DiscoContext, chatlog_id: str) -> None:
        deleted = await self.db.chatlog.delete(chatlog_id.lower(), ctx.author.id)
        color, text = (C_SUCCESS, f"Deleted chatlog `{chatlog_id}`.") if deleted else (
            C_ERROR, f"No chatlog `{chatlog_id}` owned by you.")
        await send_v2(ctx, Container(accent_color=color).text(text))

    def _p(self) -> str:
        return _prefix(self.bot)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chatlog(bot))
