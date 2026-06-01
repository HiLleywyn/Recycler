"""cogs/meta.py — help, about, ping, invite and the setup audit."""
from __future__ import annotations

import time

import discord
from discord.ext import commands

from core.framework.cogs import BaseCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_INFO, C_NEUTRAL, C_SUCCESS
from clanklib.settings import setting
from clanklib.permissions import (
    FEATURES,
    audit_permissions,
    invite_url,
    pretty_perm,
)

_START = time.time()


class Meta(BaseCog):
    @commands.command(name="help", aliases=["commands", "h"])
    async def help_cmd(self, ctx: DiscoContext, *, topic: str = "") -> None:
        # The dynamic help hub: one surface, a section multi-select, command
        # lists generated live from the command tree.
        from cogs._help_view import send_help
        await send_help(ctx)

    @commands.command(name="about", aliases=["info"])
    async def about_cmd(self, ctx: DiscoContext) -> None:
        guilds = len(self.bot.guilds)
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        panel = (
            Container(accent_color=C_INFO)
            .text("## About Recycler")
            .text("A free, open moderation and server management bot. Backups, "
                  "templates, chatlogs, sync, and the clank containment system, "
                  "all in one place.")
            .separator()
            .section(f"**Servers** {guilds:,}\n**Members** {users:,}",
                     accessory=Container.accessory_button(
                         "Add to a server", url=self._invite_url()))
        )
        await send_v2(ctx, panel)

    @commands.command(name="ping", aliases=["latency"])
    async def ping_cmd(self, ctx: DiscoContext) -> None:
        latency = round(self.bot.latency * 1000)
        uptime = int(time.time() - _START)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        panel = (
            Container(accent_color=C_SUCCESS if latency < 200 else C_NEUTRAL)
            .text("## Pong")
            .text(f"**Gateway** {latency} ms\n**Uptime** {h}h {m}m {s}s")
        )
        await send_v2(ctx, panel)

    @commands.command(name="invite")
    async def invite_cmd(self, ctx: DiscoContext) -> None:
        panel = (
            Container(accent_color=C_INFO)
            .text("## Invite Recycler")
            .section("This link asks only for the permissions the bot actually "
                     "uses, not Administrator.",
                     accessory=Container.accessory_button("Invite", url=self._invite_url()))
        )
        await send_v2(ctx, panel)

    @commands.command(name="setup", aliases=["permissions", "perms", "diagnose"])
    @commands.has_guild_permissions(manage_guild=True)
    async def setup_cmd(self, ctx: DiscoContext) -> None:
        """Check the bot's permissions in this server and show exactly what to
        change to run it securely."""
        me = ctx.guild.me
        results = audit_permissions(me)
        all_ok = all(r.ok for r in results)

        panel = Container(accent_color=C_SUCCESS if all_ok else C_ERROR)
        panel.text("## Server setup check")

        if me.guild_permissions.administrator:
            panel.text("The bot currently has **Administrator**. That works, but "
                       "it is more access than the bot needs. For a tighter "
                       "setup, remove Administrator and grant the permissions "
                       "listed below instead.")
            panel.separator()

        for res in results:
            if res.ok:
                panel.text(f"**{res.feature.label}** — ready")
            else:
                need = ", ".join(pretty_perm(p) for p in res.missing)
                panel.text(f"**{res.feature.label}** — missing: {need}\n"
                           f"-# {res.feature.note}")

        panel.separator()
        if all_ok:
            panel.text("Everything the bot needs is in place. You are good to go.")
        else:
            role_name = me.top_role.name if me.top_role else "the bot's role"
            panel.text(
                "To fix this, open **Server Settings → Roles**, select "
                f"**{role_name}**, and enable the permissions listed above. "
                "For the clank containment system, also make sure the bot's role "
                "sits **above** the Clanker role and any roles it needs to manage."
            )
            panel.add_row(Container.make_button(
                "Re-invite with the right permissions", url=self._invite_url()))

        await send_v2(ctx, panel)

    def _invite_url(self) -> str:
        cid = getattr(self.bot.user, "id", None) or setting(self.bot, "DISCORD_CLIENT_ID", "")
        return invite_url(cid)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
