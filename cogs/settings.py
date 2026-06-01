"""cogs/settings.py -- per-guild configuration, shown and edited in Components V2.

This build (server tools: backups/templates/chatlog/sync/import-export) exposes
just the prefix and the log channel. The same per-guild keys are editable from
the Sojourns web UI via clanklib.guild_schema, so the two surfaces agree.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_INFO, C_SUCCESS
from clanklib.settings import prefix as _prefix


def _chan(guild: discord.Guild, cid) -> str:
    if not cid:
        return "_not set_"
    ch = guild.get_channel(int(cid))
    return ch.mention if ch else f"`{cid}` (missing)"


class Settings(ModCog):
    @commands.command(name="settings", aliases=["config", "cfg"])
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_cmd(self, ctx: DiscoContext) -> None:
        s = await self.db.get_guild_settings(ctx.guild.id)
        p = _prefix(self.bot)
        panel = (
            Container(accent_color=C_INFO)
            .text(f"## Settings for {ctx.guild.name}")
            .text(
                f"**Prefix**  `{s.get('prefix') or p}`\n"
                f"**Log channel**  {_chan(ctx.guild, s.get('log_channel'))}"
            )
            .separator()
            .text(
                f"-# Change with `{p}set prefix !` or `{p}set log #channel`. "
                f"Use `none` to clear. Both are also editable in the web UI."
            )
        )
        await send_v2(ctx, panel)

    @commands.group(name="set", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def set_grp(self, ctx: DiscoContext) -> None:
        await self.settings_cmd(ctx)

    @set_grp.command(name="prefix")
    async def set_prefix(self, ctx: DiscoContext, prefix: str) -> None:
        if len(prefix) > 5:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "Prefix must be 5 characters or fewer."))
            return
        await self.db.update_guild_setting(ctx.guild.id, "prefix", prefix)
        await send_v2(ctx, Container(accent_color=C_SUCCESS).text(
            f"Prefix set to `{prefix}`."))

    @set_grp.command(name="log", aliases=["logchannel"])
    async def set_log(self, ctx: DiscoContext, channel: str) -> None:
        await self._set_channel(ctx, "log_channel", channel, "Log channel")

    async def _set_channel(self, ctx: DiscoContext, key: str, value: str, label: str) -> None:
        if value.lower() in ("none", "off", "clear", "unset"):
            await self.db.update_guild_setting(ctx.guild.id, key, None)
            await send_v2(ctx, Container(accent_color=C_SUCCESS).text(f"{label} cleared."))
            return
        try:
            channel = await commands.TextChannelConverter().convert(ctx, value)
        except commands.BadArgument:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "Couldn't find that channel. Mention it or paste its id."))
            return
        await self.db.update_guild_setting(ctx.guild.id, key, channel.id)
        await send_v2(ctx, Container(accent_color=C_SUCCESS).text(
            f"{label} set to {channel.mention}."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
