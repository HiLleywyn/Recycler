"""V3 Pillar 3: ``,war`` cog.

Surfaces the active match map, the queue, history, and a 60s heartbeat
that pairs queued groups + settles finished matches.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_INFO
from services import clan_wars as _svc
from services.war_render import render_war_map

log = logging.getLogger(__name__)


class ClanWars(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        if getattr(Config, "CLAN_WARS_ENABLED", True):
            self.tick.start()
            register_interval(
                "clan_wars_tick",
                int(getattr(Config, "CLAN_WARS_TICK", 60)),
            )

    def cog_unload(self) -> None:
        try:
            self.tick.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=int(getattr(Config, "CLAN_WARS_TICK", 60)))
    async def tick(self) -> None:
        if not getattr(Config, "CLAN_WARS_ENABLED", True):
            return
        for guild in list(self.bot.guilds):
            try:
                await _svc.settle_finished(self.bot.db, guild.id)
                pairs = await _svc.pair_queue(self.bot.db, guild.id)
                for a, b, pool in pairs:
                    await _svc.create_match(
                        self.bot.db, guild.id, a, b, entry_raw=pool,
                    )
            except Exception:
                log.exception("clan_wars tick failed gid=%s", guild.id)
        pulse("clan_wars_tick")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="war", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def war(self, ctx: DiscoContext) -> None:
        """Show your group's current Apex Conflict (or a stub if not queued)."""
        # The group lookup is best-effort: cogs/groups.py owns the
        # exact schema, so we just read the user's group_id from
        # whatever helper it exposes.
        group_id = await _resolve_user_group(ctx)
        if not group_id:
            await ctx.reply_error(
                "You're not in a group. Join or create one with `,group create`."
            )
            return
        match = await _svc.active_match(ctx.db, ctx.guild_id, group_id)
        if not match:
            await ctx.reply(
                embed=card(
                    "Apex Conflict",
                    description=(
                        f"Your group has no live match. "
                        f"`{ctx.prefix or '.'}war queue` to enter the next pairing."
                    ),
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return
        nodes = await _svc.node_scores(ctx.db, int(match["id"]))
        a_name, b_name = (
            await _resolve_group_name(ctx, int(match["group_a_id"])),
            await _resolve_group_name(ctx, int(match["group_b_id"])),
        )
        ends: datetime = match["ends_at"]
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        rem = int((ends - datetime.now(timezone.utc)).total_seconds())
        png = render_war_map(
            match, nodes,
            group_a_name=a_name, group_b_name=b_name,
            time_remaining_sec=max(0, rem),
        )
        file = discord.File(io.BytesIO(png), filename="war_map.png")
        sl = await _svc.scoreline(ctx.db, int(match["id"]))
        embed = (
            card(f"Apex Conflict  -  {a_name} vs {b_name}", color=C_GOLD)
            .description(
                f"{a_name}: **{sl['a_nodes']}** nodes / {sl['a_total']:,} pts  -  "
                f"{b_name}: **{sl['b_nodes']}** nodes / {sl['b_total']:,} pts"
            )
            .image("attachment://war_map.png")
            .footer(f"Ends <t:{int(ends.timestamp())}:R>")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @war.command(name="queue")
    async def war_queue(self, ctx: DiscoContext) -> None:
        """Queue your group for the next Apex Conflict pairing."""
        group_id = await _resolve_user_group(ctx)
        if not group_id:
            await ctx.reply_error("You're not in a group.")
            return
        ok, msg = await _svc.queue_group(ctx.db, ctx.guild_id, group_id, entry_raw=0)
        if not ok:
            await ctx.reply_error(msg)
            return
        await ctx.reply_success(
            "Your group is queued. The next pairing tick will match you.",
            title="Queued for war",
        )

    @war.command(name="history")
    async def war_history(self, ctx: DiscoContext) -> None:
        """Show the last 10 settled wars in this guild."""
        rows = await ctx.db.fetch_all(
            "SELECT id, group_a_id, group_b_id, winner_group, started_at, settled_at "
            "FROM clan_war_matches "
            "WHERE guild_id = $1 AND status = 'settled' "
            "ORDER BY settled_at DESC LIMIT 10",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("No wars have settled yet.")
            return
        lines = []
        for r in rows:
            winner = r["winner_group"]
            tag = "tie" if winner is None else f"<#{winner}> won"
            lines.append(
                f"Match `{r['id']}`: {r['group_a_id']} vs {r['group_b_id']}  -  {tag}"
            )
        embed = (
            card("War history", color=C_INFO)
            .description("\n".join(lines))
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def _resolve_user_group(ctx: DiscoContext) -> int | None:
    """Best-effort lookup for the user's current group id.

    Tries a couple of likely DB shapes used by cogs/groups.py. Falls
    back to None when the user isn't in a group, which is the safe
    default that lets the bot keep running even if the groups schema
    diverges in dev.
    """
    queries = (
        "SELECT group_id FROM group_members WHERE guild_id=$1 AND user_id=$2 LIMIT 1",
        "SELECT group_id FROM users WHERE guild_id=$1 AND user_id=$2",
    )
    for q in queries:
        try:
            row = await ctx.db.fetch_one(q, ctx.guild_id, ctx.author.id)
            if row and row.get("group_id"):
                return int(row["group_id"])
        except Exception:
            continue
    return None


async def _resolve_group_name(ctx: DiscoContext, group_id: int) -> str:
    """Best-effort lookup of a group's display name."""
    queries = (
        "SELECT name FROM groups WHERE guild_id=$1 AND group_id=$2",
        "SELECT name FROM groups WHERE id=$2",
    )
    for q in queries:
        try:
            row = await ctx.db.fetch_one(q, ctx.guild_id, group_id)
            if row and row.get("name"):
                return str(row["name"])[:24]
        except Exception:
            continue
    return f"Group {group_id}"


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ClanWars(bot))
