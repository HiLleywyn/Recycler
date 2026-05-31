"""V3 Pillar 5: ``,inbox`` cog -- per-user persistent notifications."""
from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_INFO
from services import inbox as _svc
from services.inbox_render import render_inbox_index, render_inbox_message

log = logging.getLogger(__name__)


class Inbox(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_group(name="inbox", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def inbox(self, ctx: DiscoContext, msg_id: int | None = None) -> None:
        """Show your inbox.

        Run with no arguments to see recent messages, or with a message
        id to expand one. Use `,inbox clear` to mark everything read,
        `,inbox prefs` to toggle DM mirroring per category.
        """
        if msg_id is not None:
            await self._render_one(ctx, msg_id)
            return
        msgs = await _svc.recent(ctx.db, ctx.author.id, limit=20)
        unread = await _svc.unread_count(ctx.db, ctx.author.id)
        png = render_inbox_index(
            msgs, display_name=ctx.author.display_name,
            unread_count=unread,
        )
        file = discord.File(io.BytesIO(png), filename="inbox.png")
        embed = (
            card("Inbox", color=C_GOLD)
            .description(
                f"**{unread}** unread of {len(msgs)} shown. "
                f"Run `{ctx.prefix or '.'}inbox <id>` to open one."
            )
            .image("attachment://inbox.png")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    async def _render_one(self, ctx: DiscoContext, msg_id: int) -> None:
        msg = await _svc.get(ctx.db, ctx.author.id, msg_id)
        if not msg:
            await ctx.reply_error(f"No inbox message `{msg_id}`.")
            return
        await _svc.read(ctx.db, ctx.author.id, msg_id)
        png = render_inbox_message(msg, display_name=ctx.author.display_name)
        file = discord.File(io.BytesIO(png), filename="inbox_msg.png")
        embed = (
            card(msg.get("title") or "Inbox", color=C_INFO)
            .description((msg.get("body") or "")[:1024])
            .image("attachment://inbox_msg.png")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @inbox.command(name="clear", aliases=["readall", "markread"])
    async def inbox_clear(self, ctx: DiscoContext) -> None:
        """Mark every unread message read."""
        n = await _svc.mark_all_read(ctx.db, ctx.author.id)
        await ctx.reply_success(
            f"Marked {n} message{'s' if n != 1 else ''} read.",
            title="Inbox cleared",
        )

    @inbox.command(name="purge")
    async def inbox_purge(self, ctx: DiscoContext) -> None:
        """Delete every already-read message in your inbox."""
        n = await _svc.purge(ctx.db, ctx.author.id)
        await ctx.reply_success(
            f"Purged {n} read message{'s' if n != 1 else ''}.",
            title="Inbox purged",
        )

    @inbox.command(name="prefs")
    async def inbox_prefs(
        self, ctx: DiscoContext, category: str | None = None,
        enable: bool | None = None,
    ) -> None:
        """View or toggle per-category DM mirroring.

        Usage: `,inbox prefs` shows current settings.
        `,inbox prefs raid on` turns on DM mirroring for raid alerts.
        Categories: market_event, raid, season, achievement, mastery,
        clan_war, governance, auction, cosmetic.
        """
        if category is None:
            prefs = await _svc.get_prefs(ctx.db, ctx.author.id)
            if not prefs:
                desc = (
                    "No per-category overrides set. Every category is "
                    "inbox-only by default. "
                    f"Use `{ctx.prefix or '.'}inbox prefs <category> on` to add DM mirroring."
                )
            else:
                lines = [
                    f"**{cat}**: {'DM on' if v else 'inbox only'}"
                    for cat, v in sorted(prefs.items())
                ]
                desc = "\n".join(lines)
            embed = card("Inbox preferences", description=desc, color=C_INFO).build()
            await ctx.reply(embed=embed, mention_author=False)
            return
        await _svc.set_pref(ctx.db, ctx.author.id, category, bool(enable))
        await ctx.reply_success(
            f"DM mirroring for `{category}` set to "
            f"{'ON' if enable else 'OFF'}.",
            title="Pref updated",
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Inbox(bot))
