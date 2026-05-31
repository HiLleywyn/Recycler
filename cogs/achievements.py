"""cogs/achievements.py - user-facing achievement browser + showcase.

Commands
--------
``,achievements``
    Browse every achievement, category-paginated, with earned/locked state
    and progress on counter-based requirements. Shows a small header with
    the caller's total earned / total in catalog.

``,achievements show [@user]``
    Display the user's earned achievements (grouped by category). Defaults
    to the caller.

Cog load syncs the achievements catalog to the ``badges`` table and
attaches bus listeners via services/achievements.py.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import (
    C_GOLD, C_NEUTRAL, FormatKit, fmt_ts, send_paginated,
)

import configs.achievements_config as _catalog
from services import achievements as _svc

log = logging.getLogger(__name__)

# Display label for each category. Keys match achievements_config.CATEGORIES.
_CAT_LABELS: dict[str, str] = {
    "getting_started": "\U0001F331 Getting Started",
    "trading":         "\U0001F4C8 Trading",
    "mining":          "\U000026CF Mining",
    "staking":         "\U0001F512 Staking",
    "defi":            "\U0001F9EA DeFi",
    "chat":            "\U0001F4AC Chat",
    "buddy":           "\U0001F436 Buddy",
    "gambling":        "\U0001F3B0 Gambling",
    "eat":             "\U0001F37D Eat the Rich",
    "milestone":       "\U0001F4B0 Milestones",
}


def _fmt_line(entry: dict, earned: bool, progress: int) -> str:
    """Render one achievement as a single display line."""
    icon = entry.get("icon", "") or "\U0001F3F7"
    name = entry["name"]
    desc = entry.get("description", "")
    reward = float(entry.get("reward_usd", 0.0) or 0.0)
    reward_str = f" (+{FormatKit.usd(reward)})" if reward > 0 else ""
    if earned:
        return f"\U00002705 {icon} **{name}**{reward_str}\n   {desc}"
    req = entry.get("requirement", {})
    if "count" in req:
        target = int(req["count"])
        bar = FormatKit.bar(min(progress, target), target, width=8, show_pct=False)
        counter = f"{min(progress, target)}/{target}"
        return (
            f"\U00002B1C {icon} **{name}**{reward_str}\n"
            f"   {desc}\n"
            f"   `{bar}` {counter}"
        )
    return f"\U00002B1C {icon} **{name}**{reward_str}\n   {desc}"


class Achievements(commands.Cog):
    """Achievements: badges awarded for hitting milestones in the economy."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Sync the catalog + wire bus listeners once the DB is up."""
        try:
            await _svc.sync_catalog(self.bot.db)
        except Exception as exc:
            log.exception("achievement catalog sync failed: %s", exc)
        try:
            _svc.attach_listeners(self.bot)
        except Exception as exc:
            log.exception("achievement listener attach failed: %s", exc)
        self.bot.bus.subscribe("badge_earned", self._on_badge_earned)

    async def _on_badge_earned(self, **kw) -> None:
        """Post a public hype embed to events_channel for rare achievements.

        Fires only on milestone-category or high-reward (>= $1000) badges
        so everyday grants (first_trade, first_paycheck) stay as a private
        DM and only the "oh wow" moments break into public chat. Silent
        no-op when the guild has no events/crypto channel configured.
        """
        guild = kw.get("guild")
        badge_id = kw.get("badge_id")
        user_id = kw.get("user_id")
        reward = float(kw.get("reward_usd") or 0.0)
        if guild is None or not badge_id or not user_id:
            return
        entry = _catalog.get(badge_id)
        if entry is None:
            return
        is_big = (
            entry.get("category") == "milestone"
            or float(entry.get("reward_usd") or 0.0) >= 1000.0
        )
        if not is_big:
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
            member = guild.get_member(int(user_id))
            mention = member.mention if member else f"<@{int(user_id)}>"
            icon = entry.get("icon", "") or "\U0001F3C6"
            desc = (
                f"{mention} unlocked **{entry['name']}**\n"
                f"> {entry.get('description', '')}"
            )
            if reward > 0:
                desc += f"\n\nReward paid: **{FormatKit.usd(reward)}**"
            embed = (
                card(
                    f"{icon} Achievement Unlocked",
                    description=desc,
                    color=C_GOLD,
                )
                .build()
            )
            await ch.send(embed=embed)
        except Exception as exc:
            log.exception("big achievement announce failed: %s", exc)

    @commands.group(name="achievements", aliases=["ach", "badges"], invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def achievements(self, ctx: DiscoContext) -> None:
        """Browse every achievement with your progress on each."""
        uid = ctx.author.id
        gid = ctx.guild_id
        earned = await _svc.earned_ids(ctx.db, uid, gid)
        total = len(_catalog.ACHIEVEMENTS)

        pages: list[discord.Embed] = []
        for cat in _catalog.CATEGORIES:
            entries = _catalog.by_category(cat)
            if not entries:
                continue
            lines: list[str] = []
            for e in entries:
                trigger = e["requirement"].get("trigger", "")
                progress = await _svc.progress_for(ctx.db, uid, gid, trigger) if trigger else 0
                lines.append(_fmt_line(e, e["badge_id"] in earned, progress))
            body = "\n\n".join(lines)
            # Embed description cap is 4096 chars; fields cap at 1024 each.
            # We use description for prose-like flow; split if too long.
            chunks = _chunk_description(body, limit=3800)
            for idx, chunk in enumerate(chunks):
                title = _CAT_LABELS.get(cat, cat.title())
                if len(chunks) > 1:
                    title = f"{title} ({idx + 1}/{len(chunks)})"
                embed = (
                    card(title, description=chunk, color=C_GOLD)
                    .footer(
                        f"Earned {len(earned)}/{total} across all categories  -  "
                        f"{ctx.author.display_name}"
                    )
                    .build()
                )
                pages.append(embed)
        if not pages:
            await ctx.reply_error("No achievements are configured yet.")
            return
        await send_paginated(ctx, pages)

    @commands.command(name="streak")
    @guild_only
    @no_bots
    @ensure_registered
    async def streak(
        self, ctx: DiscoContext, member: discord.Member | None = None,
    ) -> None:
        """Show a user's daily-claim streak (defaults to you)."""
        from services import streaks as _streaks
        target = member or ctx.author
        row = await _streaks.get(ctx.db, target.id, ctx.guild_id)
        if row is None or row["total_claims"] == 0:
            await ctx.reply_error(
                f"{target.display_name} hasn't claimed daily yet."
            )
            return
        current = int(row["current"])
        longest = int(row["longest"])
        total = int(row["total_claims"])
        # Find the next milestone threshold from the catalog.
        streak_tiers = sorted(
            int(e["requirement"]["threshold"])
            for e in _catalog.by_trigger("daily_streak")
        )
        next_tier = next((t for t in streak_tiers if t > current), None)
        next_line = (
            f"Next milestone: **{next_tier}d** ({next_tier - current} to go)"
            if next_tier is not None
            else "All streak milestones earned. Keep going for glory."
        )
        fire = "\U0001F525"
        embed = (
            card(
                f"{fire} {target.display_name}'s Streak",
                description=(
                    f"Current: **{current}** day{'s' if current != 1 else ''}\n"
                    f"Longest: **{longest}** day{'s' if longest != 1 else ''}\n"
                    f"Total claims: **{total}**\n\n"
                    f"{next_line}"
                ),
                color=C_GOLD,
            )
            .footer("Streak ticks up when you claim ,daily on consecutive days.")
            .build()
        )
        await ctx.send_embed(embed)

    @commands.command(name="streaks", aliases=["streaktop"])
    @guild_only
    @no_bots
    @ensure_registered
    async def streaks_top(self, ctx: DiscoContext) -> None:
        """Top 10 active daily streaks on this server."""
        from services import streaks as _streaks
        rows = await _streaks.top(ctx.db, ctx.guild_id, limit=10)
        if not rows:
            await ctx.reply_error("No active streaks on this server.")
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"User {uid}"
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"#{i}")
            lines.append(
                f"{medal} **{name}**  -  {int(r['current_streak'])}d "
                f"(best {int(r['longest_streak'])}d)"
            )
        embed = (
            card(
                "\U0001F525 Top Daily Streaks",
                description="\n".join(lines),
                color=C_GOLD,
            )
            .footer("Streaks showing are active (claimed within 24h).")
            .build()
        )
        await ctx.send_embed(embed)

    @achievements.command(name="help")
    @guild_only
    @no_bots
    async def achievements_help(self, ctx: DiscoContext) -> None:
        """Explain how achievements work and list every command."""
        total = len(_catalog.ACHIEVEMENTS)
        categories = ", ".join(
            _CAT_LABELS.get(c, c.title()).split(" ", 1)[-1]
            for c in _catalog.CATEGORIES
        )
        embed = (
            card(
                "\U0001F3C6 Achievements Help",
                description=(
                    f"Earn badges for hitting milestones across the economy. "
                    f"**{total}** achievements are available, grouped into "
                    f"**{len(_catalog.CATEGORIES)}** categories. Rewards pay "
                    f"to your wallet automatically and rare unlocks are "
                    f"announced publicly in the events channel."
                ),
                color=C_GOLD,
            )
            .field(
                "Browse + track",
                "`,achievements` - full catalog with progress bars\n"
                "`,ach show [@user]` - see a user's earned trophies\n"
                "`,ach leaderboard` - top 10 players by badge count",
                inline=False,
            )
            .field(
                "How progress works",
                "Most achievements tick up +1 per activity event (trades, "
                "work shifts, mining payouts). Milestone achievements fire "
                "once a threshold is reached (net worth, chat level). "
                "Progress persists even if you earn a badge later.",
                inline=False,
            )
            .field("Categories", categories, inline=False)
            .field(
                "Aliases",
                "`,ach` `,badges` (top-level); "
                "`,ach lb` `,ach top` for leaderboard.",
                inline=False,
            )
            .footer(
                "Big-badge unlocks (category=milestone or reward >= $1000) "
                "post to the server events channel."
            )
            .build()
        )
        await ctx.send_embed(embed)

    @achievements.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def achievements_leaderboard(self, ctx: DiscoContext) -> None:
        """Show the top 10 users by number of earned achievements."""
        rows = await ctx.db.fetch_all(
            """
            SELECT user_id, COUNT(*) AS earned
            FROM user_badges
            WHERE guild_id = $1
            GROUP BY user_id
            ORDER BY earned DESC, MAX(earned_at) ASC
            LIMIT 50
            """,
            ctx.guild_id,
        )
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r["user_id"]) for r in rows],
            )
            rows = [r for r in rows if int(r["user_id"]) in keep][:10]
        if not rows:
            await ctx.reply_error(
                "No achievements have been earned on this server yet."
            )
            return
        total = len(_catalog.ACHIEVEMENTS)
        lines = []
        for i, r in enumerate(rows, start=1):
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"#{i}")
            uid = int(r["user_id"])
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"User {uid}"
            earned = int(r["earned"])
            lines.append(f"{medal} **{name}**  -  {earned}/{total}")
        embed = (
            card(
                "\U0001F3C6 Achievement Leaderboard",
                description="\n".join(lines),
                color=C_GOLD,
            )
            .footer(f"Top 10 by achievements earned  -  {total} total in catalog")
            .build()
        )
        await ctx.send_embed(embed)

    @achievements.command(name="show")
    @guild_only
    @no_bots
    @ensure_registered
    async def achievements_show(
        self, ctx: DiscoContext, member: discord.Member | None = None,
    ) -> None:
        """Show the earned achievements for a user (defaults to you)."""
        target = member or ctx.author
        rows = await _svc.user_badges(ctx.db, target.id, ctx.guild_id)
        total = len(_catalog.ACHIEVEMENTS)

        if not rows:
            embed = (
                card(
                    f"\U0001F3F7 {target.display_name}'s Achievements",
                    description=f"No achievements earned yet (0/{total}).",
                    color=C_NEUTRAL,
                )
                .build()
            )
            await ctx.send_embed(embed)
            return

        # Group by category preserving catalog order.
        by_cat: dict[str, list[dict]] = {}
        for r in rows:
            by_cat.setdefault(r["category"], []).append(r)

        embed_builder = (
            card(
                f"\U0001F3F7 {target.display_name}'s Achievements",
                description=f"Earned **{len(rows)}/{total}**",
                color=C_GOLD,
            )
            .author(target.display_name, icon_url=target.display_avatar.url)
        )
        for cat in _catalog.CATEGORIES:
            items = by_cat.get(cat)
            if not items:
                continue
            items.sort(key=lambda r: r["earned_at"])
            lines: list[str] = []
            truncated = 0
            for r in items:
                icon = r.get("icon", "") or "\U0001F3F7"
                line = f"{icon} **{r['name']}**  -  earned {fmt_ts(r['earned_at'])}"
                # Keep room for a "+N more" tail under Discord's 1024-char field cap.
                if sum(len(s) + 1 for s in lines) + len(line) + 32 > 1024:
                    truncated = len(items) - len(lines)
                    break
                lines.append(line)
            if truncated:
                lines.append(f"_+{truncated} more..._")
            label = _CAT_LABELS.get(cat, cat.title())
            embed_builder.field(label, "\n".join(lines), inline=False)
        await ctx.send_embed(embed_builder.build())


def _chunk_description(body: str, limit: int = 3800) -> list[str]:
    """Split a multi-line description into embed-description-sized chunks.

    Keeps whole achievement blocks together (separator is the blank line).
    """
    blocks = body.split("\n\n")
    chunks: list[str] = []
    cur = ""
    for block in blocks:
        candidate = block if not cur else f"{cur}\n\n{block}"
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [body]


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Achievements(bot))
