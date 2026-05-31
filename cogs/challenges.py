"""cogs/challenges.py - server-wide collective challenges.

Commands
--------
``,challenge`` (aliases: ``ch``, ``challenges``)
    List every active challenge on this server with progress bars.
``,challenge info <id>``
    Detail view with reward pool, deadline, and top 10 contributors.
``,challenge history``
    Last 10 finalized (succeeded or failed) challenges.
``,challenge help``
    How the system works + the full trigger list.

Admin commands live on the ``,admin challenge`` subgroup in cogs/admin.py.

Background
----------
A 2-minute task sweeps for expired challenges and flips them to 'failed'
(no payout). Succeeded challenges are handled synchronously at the
moment the progress counter crosses the target, inside services.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import (
    C_ERROR, C_GOLD, C_NAVY, C_NEUTRAL, C_SUCCESS, FormatKit, fmt_ts,
)
from services import challenges as _svc

log = logging.getLogger(__name__)


def _progress_block(progress: int, target: int) -> str:
    bar = FormatKit.bar(progress, target, width=14, show_pct=False)
    pct = min(100, int(100 * progress / max(1, target)))
    return f"`{bar}` **{progress:,}/{target:,}** ({pct}%)"


def _fmt_active_line(row: dict) -> str:
    label = _svc.trigger_label(row["trigger"])
    return (
        f"**{row['name']}** (#{int(row['challenge_id'])})  -  {label}\n"
        f"{_progress_block(int(row['progress']), int(row['target']))}\n"
        f"Pool: **{FormatKit.usd(float(row['reward_pool_usd']))}**  -  "
        f"Ends: {fmt_ts(row['ends_at'])}"
    )


def _fmt_history_line(guild: discord.Guild | None, row: dict) -> str:
    status_icon = {
        "succeeded": "\U00002705",
        "failed":    "\U0000274C",
    }.get(row["status"], "\U00002753")
    label = _svc.trigger_label(row["trigger"])
    return (
        f"{status_icon} **{row['name']}** (#{int(row['challenge_id'])})  -  {label}\n"
        f"   {int(row['progress']):,}/{int(row['target']):,}  -  "
        f"Pool: {FormatKit.usd(float(row['reward_pool_usd']))}  -  "
        f"{fmt_ts(row.get('completed_at') or row['ends_at'])}"
    )


class Challenges(commands.Cog):
    """Server-wide collective challenges with shared reward pools."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        try:
            _svc.attach_listeners(self.bot)
        except Exception as exc:
            log.exception("challenge listener attach failed: %s", exc)
        self.bot.bus.subscribe("challenge_succeeded", self._on_challenge_succeeded)
        self._expiry_loop.start()

    def cog_unload(self) -> None:
        self._expiry_loop.cancel()

    @tasks.loop(minutes=2)
    async def _expiry_loop(self) -> None:
        try:
            await _svc.check_expired(self.bot)
        except Exception as exc:
            log.exception("challenge expiry loop: %s", exc)
        pulse("challenge_expiry")

    @_expiry_loop.before_loop
    async def _before_expiry(self) -> None:
        await self.bot.wait_until_ready()

    async def _on_challenge_succeeded(self, **kw) -> None:
        """Post a public success embed to the guild events channel."""
        guild = kw.get("guild")
        if guild is None:
            return
        try:
            settings = await self.bot.db.get_guild_settings(guild.id)
            ch_id = (
                settings.get("events_channel")
                or settings.get("crypto_channel")
            ) if settings else None
            if not ch_id:
                return
            ch = guild.get_channel(int(ch_id))
            if ch is None:
                return
            paid = kw.get("paid") or []
            contributors = len(paid)
            top_lines: list[str] = []
            for p in sorted(paid, key=lambda x: x.get("reward_usd", 0.0), reverse=True)[:5]:
                member = guild.get_member(int(p.get("user_id") or 0))
                name = member.mention if member else f"User {p.get('user_id')}"
                top_lines.append(
                    f"{name}  -  {FormatKit.usd(float(p.get('reward_usd', 0.0)))}"
                )
            body = (
                f"The server crushed the **{kw.get('name')}** challenge!\n"
                f"{int(kw.get('target') or 0):,} "
                f"{_svc.trigger_label(kw.get('trigger') or '').lower()} reached. "
                f"Reward pool of **{FormatKit.usd(float(kw.get('reward_pool_usd') or 0.0))}** "
                f"split between **{contributors}** contributor"
                f"{'s' if contributors != 1 else ''}."
            )
            builder = card(
                "\U0001F389 Challenge Complete!",
                description=body, color=C_SUCCESS,
            )
            if top_lines:
                builder.field("Top earners", "\n".join(top_lines), False)
            await ch.send(embed=builder.build())
        except Exception as exc:
            log.exception("challenge success post failed: %s", exc)

    # ── User commands ────────────────────────────────────────────────────

    @commands.group(
        name="challenge", aliases=["ch", "challenges"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def challenge(self, ctx: DiscoContext) -> None:
        """List every active challenge on this server."""
        rows = await _svc.list_active(ctx.db, ctx.guild_id)
        if not rows:
            embed = (
                card(
                    "\U0001F3C1 No Active Challenges",
                    description=(
                        "No server-wide challenges are running right now. "
                        "Ask an admin to start one with "
                        "`,admin challenge start`."
                    ),
                    color=C_NEUTRAL,
                )
                .build()
            )
            await ctx.send_embed(embed)
            return
        blocks = [_fmt_active_line(r) for r in rows]
        embed = (
            card(
                f"\U0001F3AF Server Challenges ({len(rows)} active)",
                description="\n\n".join(blocks),
                color=C_GOLD,
            )
            .footer(
                "Every activity contributes. Hit the target as a server to "
                "split the reward pool. Use ,challenge info <id> for detail."
            )
            .build()
        )
        await ctx.send_embed(embed)

    @challenge.command(name="info")
    @guild_only
    @no_bots
    @ensure_registered
    async def challenge_info(self, ctx: DiscoContext, challenge_id: int) -> None:
        """Show a single challenge with its top 10 contributors."""
        row = await _svc.get(ctx.db, challenge_id)
        if row is None or int(row["guild_id"]) != ctx.guild_id:
            await ctx.reply_error(f"No challenge with id **{challenge_id}**.")
            return
        contribs = await _svc.top_contributors(ctx.db, challenge_id, limit=10)
        status = row["status"]
        icon = {
            "active":    "\U0001F3AF",
            "succeeded": "\U00002705",
            "failed":    "\U0000274C",
        }.get(status, "\U00002753")
        color = {
            "active":    C_GOLD,
            "succeeded": C_SUCCESS,
            "failed":    C_ERROR,
        }.get(status, C_NEUTRAL)
        desc = (
            f"**{_svc.trigger_label(row['trigger'])}**\n"
            f"{_progress_block(int(row['progress']), int(row['target']))}\n\n"
            f"Pool: **{FormatKit.usd(float(row['reward_pool_usd']))}**\n"
            f"Started: {fmt_ts(row['started_at'])}\n"
            f"Ends: {fmt_ts(row['ends_at'])}"
        )
        if row.get("description"):
            desc = f"{row['description']}\n\n" + desc
        if row.get("completed_at"):
            desc += f"\nCompleted: {fmt_ts(row['completed_at'])}"

        builder = card(
            f"{icon} #{challenge_id} {row['name']}  -  {status.title()}",
            description=desc, color=color,
        )
        if contribs:
            lines = []
            for i, c in enumerate(contribs, start=1):
                uid = int(c["user_id"])
                member = ctx.guild.get_member(uid) if ctx.guild else None
                name = member.display_name if member else f"User {uid}"
                medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(
                    i, f"#{i}",
                )
                paid = float(c.get("reward_paid") or 0.0)
                paid_str = f"  -  {FormatKit.usd(paid)} paid" if paid > 0 else ""
                lines.append(
                    f"{medal} **{name}**  -  {int(c['contribution']):,}{paid_str}"
                )
            builder.field("Top contributors", "\n".join(lines), False)
        await ctx.send_embed(builder.build())

    @challenge.command(name="history", aliases=["past", "log"])
    @guild_only
    @no_bots
    @ensure_registered
    async def challenge_history(self, ctx: DiscoContext) -> None:
        """List the last 10 finalized (succeeded or failed) challenges."""
        rows = await _svc.list_history(ctx.db, ctx.guild_id, limit=10)
        if not rows:
            await ctx.reply_error("No finalized challenges yet.")
            return
        lines = [_fmt_history_line(ctx.guild, r) for r in rows]
        embed = (
            card(
                "\U0001F4DC Challenge History",
                description="\n\n".join(lines),
                color=C_NAVY,
            )
            .footer(f"Last {len(rows)} finalized challenges")
            .build()
        )
        await ctx.send_embed(embed)

    @challenge.command(name="help")
    @guild_only
    @no_bots
    async def challenge_help(self, ctx: DiscoContext) -> None:
        """Explain how server-wide challenges work."""
        triggers_lines = []
        for t in _svc.TRIGGERS:
            triggers_lines.append(f"`{t}`  -  {_svc.trigger_label(t)}")
        embed = (
            card(
                "\U0001F3AF Challenges Help",
                description=(
                    "A challenge is a server-wide goal: hit a numeric target "
                    "across every player's activity before the deadline, and "
                    "the reward pool splits proportionally among everyone "
                    "who contributed. Fail the deadline, no payout."
                ),
                color=C_GOLD,
            )
            .field(
                "Viewing",
                "`,challenge` - every active challenge with progress bars\n"
                "`,challenge info <id>` - detail + top 10 contributors\n"
                "`,challenge history` - past outcomes",
                inline=False,
            )
            .field(
                "How contribution works",
                "Every qualifying activity (trade, mining payout, buddy "
                "win, etc.) adds +1 to the global counter AND +1 to your "
                "personal contribution row. The final payout is "
                "(your contribution / total contribution) x pool.",
                inline=False,
            )
            .field(
                "Valid triggers",
                "\n".join(triggers_lines),
                inline=False,
            )
            .footer(
                "Admins: ,admin challenge start / end. "
                "Auto-fails 2 minutes after deadline."
            )
            .build()
        )
        await ctx.send_embed(embed)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Challenges(bot))
