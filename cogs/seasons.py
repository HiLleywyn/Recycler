"""cogs/seasons.py - server season view + admin lifecycle commands.

Commands
--------
``,season``
    View the currently active season (or the latest finalized one if
    none is active). Shows the leaderboard preview by net worth.

``,season last``
    Show the results of the most recently finalized season.

``,season start <days> <pool_usd> <name...>``   (Manage Server)
    Start a new net-worth season for ``days`` days with ``pool_usd``
    prize pool. Fails if one is already active.

``,season end``                                 (Manage Server)
    Finalize the currently active season immediately.

Background task
---------------
A 5-minute loop checks for seasons past ``ends_at`` and finalizes them
automatically, so guilds do not need to manually end their seasons on
schedule.
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
    C_GOLD, C_NAVY, C_NEUTRAL, C_SUCCESS, FormatKit, fmt_ts,
)
from services import seasons as _svc
from services import auto_seasons as _auto_svc
import configs.seasonpass_config as _pass_cfg
import configs.seasons_pairs_config as _pairs_cfg

log = logging.getLogger(__name__)

_PREVIEW_ROWS = 10


def _require_admin(ctx: DiscoContext) -> bool:
    return bool(
        getattr(getattr(ctx.author, "guild_permissions", None), "manage_guild", False)
    )


async def _latest_finalized(db, guild_id: int) -> dict | None:
    return await db.fetch_one(
        """
        SELECT season_id, guild_id, name, metric, prize_pool_usd,
               started_at, ends_at, finalized_at, status
        FROM seasons
        WHERE guild_id = $1 AND status = 'finalized'
        ORDER BY finalized_at DESC NULLS LAST
        LIMIT 1
        """,
        guild_id,
    )


def _fmt_entry_line(
    guild: discord.Guild | None, row: dict, idx: int | None = None,
    metric: str = "net_worth",
) -> str:
    rank = int(row.get("final_rank") or idx or 0)
    uid = int(row["user_id"])
    value = float(row.get("metric_value") or row.get("value") or 0.0)
    reward = float(row.get("reward_usd") or 0.0)
    medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(rank, f"#{rank}")
    member = guild.get_member(uid) if guild else None
    name = member.display_name if member else f"User {uid}"
    reward_str = f"  -  {FormatKit.usd(reward)}" if reward > 0 else ""
    return f"{medal} **{name}**  -  {_svc.format_metric(metric, value)}{reward_str}"


async def _build_active_card(
    ctx: DiscoContext, season: dict,
) -> discord.Embed:
    """Render the current standings for an active season (no rewards yet)."""
    metric = season["metric"]
    ranked = await _svc.fetch_standings(ctx.db, season, limit=_PREVIEW_ROWS)
    lines = []
    for i, (uid, v) in enumerate(ranked, start=1):
        lines.append(
            _fmt_entry_line(
                ctx.guild,
                {"user_id": uid, "final_rank": i, "metric_value": v},
                metric=metric,
            )
        )
    preview = "\n".join(lines) if lines else "No ranked players yet."

    sid = int(season["season_id"])
    my_xp = await _svc.get_xp(ctx.db, sid, ctx.author.id)
    my_tier = _pass_cfg.tier_for_xp(my_xp)

    theme = season.get("theme") or "classic"
    builder = (
        card(
            f"\U0001F3C6 Season: {season['name']}",
            description=(
                f"Metric: **{_svc.metric_label(metric)}**  -  "
                f"Prize pool: **{FormatKit.usd(float(season['prize_pool_usd']))}**"
            ),
            color=C_GOLD,
        )
        .field("Ends", fmt_ts(season["ends_at"]), True)
        .field("Started", fmt_ts(season["started_at"]), True)
        .field(
            "Your Pass",
            f"Tier **{my_tier}/{_pass_cfg.MAX_TIER}**  -  {my_xp:,} XP",
            True,
        )
        .field_if(
            theme != "classic",
            f"\U0001F3A8 Theme: {theme}",
            _pass_cfg.theme_summary(theme),
            False,
        )
        .field(f"Top {_PREVIEW_ROWS} (live)", preview, False)
        .footer(
            "Rankings update live. See ,season pass for your season pass."
        )
    )
    return builder.build()


def _build_season_user_help() -> discord.Embed:
    """Player-facing overview of seasons and the season pass."""
    metric_lines = []
    for m in _svc.METRICS:
        metric_lines.append(
            f"**{_svc.metric_label(m)}** (`{m}`)  -  "
            f"{_svc.metric_description(m)}"
        )
    return (
        card(
            "\U0001F3C6 Seasons + Pass Help",
            description=(
                "A season is a time-boxed leaderboard race with a USD prize "
                "pool. While a season runs, a parallel season pass rewards "
                "anyone who plays actively -- even if you don't top the "
                "leaderboard, you can still earn tier rewards."
            ),
            color=C_GOLD,
        )
        .field(
            "Leaderboard",
            "`,season` - live top 10 + your pass tier\n"
            "`,season last` - results from the most recent season\n"
            "`,season top` - top 10 by season pass XP",
            inline=False,
        )
        .field(
            "Season Pass",
            f"`,season pass` - your XP, unlocked tier, ready-to-claim list\n"
            f"`,season claim <tier>` - claim one tier reward\n"
            f"`,season claim all` - sweep everything you've unlocked\n"
            f"{_pass_cfg.MAX_TIER} tiers total; earn XP from almost every "
            f"activity.",
            inline=False,
        )
        .field(
            "Possible metrics",
            "\n".join(metric_lines),
            inline=False,
        )
        .field(
            "End of season",
            "Top 10 split the prize pool (40/24/14/9/6/3/2/1/0.6/0.4% of "
            "the weights). A recap embed is posted to the events channel "
            "with full payouts. Pass rewards stay claimable after the "
            "season ends, but XP resets for the next season.",
            inline=False,
        )
        .footer("Admins: run ,season help admin for lifecycle commands.")
        .build()
    )


def _build_season_admin_help() -> discord.Embed:
    """Admin-facing lifecycle + operations reference."""
    metric_lines = []
    for m in _svc.METRICS:
        metric_lines.append(
            f"`{m}`  -  {_svc.metric_description(m)}"
        )
    return (
        card(
            "\U0001F6E0 Season Admin Help",
            description=(
                "Season lifecycle is guild-scoped. Only one season can be "
                "active per guild at a time. Manage Server permission is "
                "required for start/end."
            ),
            color=C_NAVY,
        )
        .field(
            "Start a season",
            "`,season start <metric> <days> <pool_usd> <name...>`\n"
            "Example: `,season start buddy_wins 14 10000 Spring Melee`\n"
            "Valid durations: 1-90 days. Prize pool is split across the "
            "top 10 at finalize.",
            inline=False,
        )
        .field(
            "Metrics",
            "\n".join(metric_lines),
            inline=False,
        )
        .field(
            "End a season",
            "`,season end`  -  immediately finalize the active season. "
            "Snapshots the leaderboard, pays top 10 from the prize pool, "
            "records final ranks in ``season_entries``, and posts a recap "
            "embed to the events channel.\n"
            "Seasons also auto-finalize 5 minutes after ``ends_at`` passes.",
            inline=False,
        )
        .field(
            "Announcement channel",
            "Recaps + big-achievement hype embeds use the guild's "
            "``events_channel`` (falls back to ``crypto_channel``). Without "
            "one configured, the feature silently no-ops; starting a "
            "season still works but players won't see a public recap.",
            inline=False,
        )
        .field(
            "Season Pass",
            f"Runs automatically alongside any season. XP accumulates from "
            f"the full bus event surface, tiers unlock at "
            f"{_pass_cfg.TIER_XP_COST:,} XP each up to {_pass_cfg.MAX_TIER}. "
            f"Edit seasonpass_config.py to tune rewards or add new XP "
            f"sources.",
            inline=False,
        )
        .field(
            "Themes (XP multipliers)",
            "`,season themes`  -  browse available themes + their boosts.\n"
            "`,season theme <name>`  -  apply a theme to the active season. "
            "Existing XP isn't changed; only future grants get the "
            "multiplier. Good for flavor events (Mining Madness, Buddy "
            "Brawls) without needing a brand-new season.",
            inline=False,
        )
        .footer(
            "Player commands: ,season help. Pass leaderboard: ,season top."
        )
        .build()
    )


def _build_pass_card(
    ctx: DiscoContext, season: dict,
    xp: int, unlocked: int, claimed: set[int],
) -> discord.Embed:
    """Render the caller's season pass state for a season.

    Shows overall progress (xp toward next tier), a compact list of
    claimed tiers, every ready-to-claim tier with its reward, and a
    small preview of upcoming locked tiers so the next goal is visible.
    """
    max_tier = _pass_cfg.MAX_TIER
    is_active = season["status"] == "active"

    # Bar progress toward the NEXT tier (not the whole pass).
    if unlocked >= max_tier:
        bar = FormatKit.bar(1, 1, width=14, show_pct=False)
        next_line = f"Max tier ({max_tier}) reached."
    else:
        base = _pass_cfg.xp_for_tier(unlocked)
        goal = _pass_cfg.xp_for_tier(unlocked + 1)
        cur = max(0, xp - base)
        span = max(1, goal - base)
        bar = FormatKit.bar(cur, span, width=14, show_pct=False)
        next_line = f"{cur:,}/{span:,} XP to tier {unlocked + 1}"

    ready = [t for t in range(1, unlocked + 1) if t not in claimed]

    builder = (
        card(
            f"\U0001F3AB {season['name']} - Season Pass",
            description=(
                f"Season: {'active' if is_active else 'finalized'}  -  "
                f"Total XP: **{xp:,}**\n"
                f"`{bar}` {next_line}"
            ),
            color=C_GOLD,
        )
        .field(
            "Tier",
            f"**{unlocked}/{max_tier}**",
            True,
        )
        .field(
            "Claimed",
            f"**{len(claimed)}** / {unlocked}" if unlocked else "0 / 0",
            True,
        )
        .field(
            "Pass Pool",
            FormatKit.usd(_pass_cfg.total_pool()),
            True,
        )
    )

    if ready:
        lines = []
        for t in ready[:10]:
            lines.append(f"Tier {t}  -  **{FormatKit.usd(_pass_cfg.tier_reward(t))}**")
        extra = "" if len(ready) <= 10 else f"\n...and {len(ready) - 10} more."
        builder.field(
            f"Ready to claim ({len(ready)})",
            "\n".join(lines) + extra,
            False,
        )

    # Upcoming locked preview: next 5 tiers with their reward peek.
    if unlocked < max_tier:
        peek = []
        for t in range(unlocked + 1, min(unlocked + 6, max_tier + 1)):
            peek.append(f"Tier {t}  -  {FormatKit.usd(_pass_cfg.tier_reward(t))}")
        builder.field("Upcoming", "\n".join(peek), False)

    builder.footer(
        "Earn XP from trades, work, daily, mining, gambling, and more. "
        "Claim with ,season claim <tier|all>."
    )
    return builder.build()


async def _build_recap_card(
    bot: Discoin, guild: discord.Guild, season: dict,
) -> discord.Embed:
    """Public recap posted to the guild events channel when a season ends.

    Mirrors ``_build_finalized_card`` but framed as an announcement (gold
    banner, winner shout-out) rather than a lookup. Reads entries from
    the service to stay consistent with the ``,season last`` card.
    """
    sid = int(season["season_id"])
    rows = await _svc.entries(bot.db, sid, limit=_PREVIEW_ROWS)
    lines = [_fmt_entry_line(guild, r) for r in rows] or ["No entries recorded."]
    leaderboard_paid = sum(float(r.get("reward_usd") or 0.0) for r in rows)
    pass_paid = await _svc.total_pass_payout(bot.db, sid)

    top_shout = ""
    if rows:
        top = rows[0]
        uid = int(top["user_id"])
        member = guild.get_member(uid)
        name = member.mention if member else f"User {uid}"
        top_shout = (
            f"\U0001F451 Congratulations {name}, the champion of "
            f"**{season['name']}**!\n"
        )

    builder = (
        card(
            f"\U0001F3C1 Season Ended: {season['name']}",
            description=(
                f"{top_shout}"
                f"Prize pool: **{FormatKit.usd(float(season['prize_pool_usd']))}**"
            ),
            color=C_GOLD,
        )
        .field(
            "Duration",
            f"{fmt_ts(season['started_at'])} -> "
            f"{fmt_ts(season.get('finalized_at') or season['ends_at'])}",
            False,
        )
        .field("Leaderboard paid", FormatKit.usd(leaderboard_paid), True)
        .field("Pass rewards paid", FormatKit.usd(pass_paid), True)
        .field("Total payout", FormatKit.usd(leaderboard_paid + pass_paid), True)
        .field(f"Final Top {_PREVIEW_ROWS}", "\n".join(lines), False)
        .footer(
            "Start the next season with ,season start -- "
            "season pass XP resets with the new season."
        )
    )
    return builder.build()


async def _build_finalized_card(
    ctx: DiscoContext, season: dict,
) -> discord.Embed:
    metric = season["metric"]
    rows = await _svc.entries(ctx.db, int(season["season_id"]), limit=_PREVIEW_ROWS)
    lines = (
        [_fmt_entry_line(ctx.guild, r, metric=metric) for r in rows]
        or ["No entries recorded."]
    )
    builder = (
        card(
            f"\U0001F4DC Season Results: {season['name']}",
            description=(
                f"Metric: **{_svc.metric_label(metric)}**  -  "
                f"Prize pool: **{FormatKit.usd(float(season['prize_pool_usd']))}**"
            ),
            color=C_NAVY,
        )
        .field("Finalized", fmt_ts(season.get("finalized_at") or season["ends_at"]), True)
        .field("Started", fmt_ts(season["started_at"]), True)
        .field(f"Top {_PREVIEW_ROWS}", "\n".join(lines), False)
    )
    return builder.build()


class Seasons(commands.Cog):
    """Time-bounded server leaderboard competitions with prize payouts."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._expiry_loop.start()
        try:
            _svc.attach_pass_listeners(self.bot)
        except Exception as exc:
            log.exception("pass listener attach failed: %s", exc)
        try:
            _auto_svc.attach_listeners(self.bot)
        except Exception as exc:
            log.exception("auto_seasons listener attach failed: %s", exc)
        self.bot.bus.subscribe("season_ended", self._on_season_ended)

    async def _on_season_ended(self, **kw) -> None:
        """Post a recap embed to the guild's events channel when a season ends.

        Subscribes to ``season_ended`` (published by services/seasons.end_season)
        and resolves the configured announcement channel the same way
        cogs/events.py does. Silently no-ops when no channel is configured
        or the bot can't post to it.
        """
        guild = kw.get("guild")
        season_id = kw.get("season_id")
        if guild is None or season_id is None:
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
            season = await _svc.get_season(self.bot.db, int(season_id))
            if season is None:
                return
            embed = await _build_recap_card(self.bot, guild, season)
            await ch.send(embed=embed)
        except Exception as exc:
            log.exception("season recap post failed: %s", exc)

    def cog_unload(self) -> None:
        self._expiry_loop.cancel()

    @tasks.loop(minutes=5)
    async def _expiry_loop(self) -> None:
        try:
            await _svc.check_expired(self.bot)
        except Exception as exc:
            log.exception("season expiry loop: %s", exc)
        # Bootstrap pass: any guild that flipped auto-rotation on but
        # has no active season (fresh install, season just ended on a
        # restart, listener wasn't attached when ``season_ended`` fired)
        # should land a pair on the next tick. ``ensure_running`` is a
        # no-op when a season is already active.
        try:
            for guild in list(self.bot.guilds or []):
                try:
                    await _auto_svc.ensure_running(self.bot, int(guild.id))
                except Exception as exc:
                    log.debug(
                        "auto_seasons ensure_running gid=%s failed: %s",
                        guild.id, exc,
                    )
        except Exception as exc:
            log.exception("auto_seasons bootstrap loop: %s", exc)
        pulse("season_expiry")

    @_expiry_loop.before_loop
    async def _before_expiry(self) -> None:
        await self.bot.wait_until_ready()

    @commands.group(name="season", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def season(self, ctx: DiscoContext) -> None:
        """View the current season (or the latest finalized one)."""
        active = await _svc.get_active(ctx.db, ctx.guild_id)
        if active is not None:
            await ctx.send_embed(await _build_active_card(ctx, active))
            return
        last = await _latest_finalized(ctx.db, ctx.guild_id)
        if last is None:
            embed = (
                card(
                    "\U0001F3C6 Seasons",
                    description=(
                        "No season is active. A server admin can start one "
                        "with `,season start <days> <pool> <name...>`."
                    ),
                    color=C_NEUTRAL,
                )
                .build()
            )
            await ctx.send_embed(embed)
            return
        await ctx.send_embed(await _build_finalized_card(ctx, last))

    @season.command(name="history", aliases=["past", "log"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_history(self, ctx: DiscoContext) -> None:
        """List the last 10 finalized seasons with their winners + pool."""
        rows = await ctx.db.fetch_all(
            """
            SELECT s.season_id, s.name, s.metric, s.prize_pool_usd,
                   s.started_at, s.ends_at, s.finalized_at,
                   (SELECT user_id FROM season_entries
                      WHERE season_id = s.season_id AND final_rank = 1
                      LIMIT 1) AS winner_id,
                   (SELECT metric_value FROM season_entries
                      WHERE season_id = s.season_id AND final_rank = 1
                      LIMIT 1) AS winner_value
            FROM seasons s
            WHERE s.guild_id = $1 AND s.status = 'finalized'
            ORDER BY s.finalized_at DESC NULLS LAST
            LIMIT 10
            """,
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("No finalized seasons yet.")
            return
        lines = []
        for r in rows:
            win_id = r.get("winner_id")
            if win_id is not None:
                member = ctx.guild.get_member(int(win_id)) if ctx.guild else None
                win_name = member.display_name if member else f"User {int(win_id)}"
                win_val = float(r.get("winner_value") or 0.0)
                win_str = f"{win_name} ({_svc.format_metric(r['metric'], win_val)})"
            else:
                win_str = "no entries"
            lines.append(
                f"**{r['name']}**  -  {_svc.metric_label(r['metric'])}\n"
                f"   {fmt_ts(r['started_at'])} -> "
                f"{fmt_ts(r.get('finalized_at') or r['ends_at'])}  -  "
                f"Pool: {FormatKit.usd(float(r['prize_pool_usd']))}\n"
                f"   \U0001F947 {win_str}"
            )
        embed = (
            card(
                "\U0001F4DC Season History",
                description="\n\n".join(lines),
                color=C_NAVY,
            )
            .footer(f"Last {len(rows)} finalized seasons on this server")
            .build()
        )
        await ctx.send_embed(embed)

    @season.command(name="last")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_last(self, ctx: DiscoContext) -> None:
        """Show the results of the most recently finalized season."""
        last = await _latest_finalized(ctx.db, ctx.guild_id)
        if last is None:
            await ctx.reply_error("No finalized season to display yet.")
            return
        await ctx.send_embed(await _build_finalized_card(ctx, last))

    @season.command(name="start")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_start(
        self, ctx: DiscoContext,
        metric: str, duration_days: int, prize_pool_usd: float, *, name: str,
    ) -> None:
        """Start a new season. Requires Manage Server.

        Usage: ,season start <metric> <days> <pool_usd> <name...>
        Metrics: net_worth, volume, trades, pass_xp, buddy_wins
        Example: ,season start buddy_wins 14 10000 Spring Melee

        Starts on the 'classic' theme (no XP boosts). Apply a theme after
        with `,season theme <name>`, or see `,season themes` for the list.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to start a season.")
            return
        metric = metric.lower()
        if metric not in _svc.METRICS:
            await ctx.reply_error(
                f"Unknown metric **{metric}**. Valid: "
                f"{', '.join(_svc.METRICS)}.\n"
                f"See `,season help admin` for details."
            )
            return
        if duration_days < 1 or duration_days > 90:
            await ctx.reply_error("Duration must be between 1 and 90 days.")
            return
        if prize_pool_usd <= 0:
            await ctx.reply_error("Prize pool must be positive.")
            return
        if not name.strip():
            await ctx.reply_error("Provide a season name.")
            return
        row = await _svc.start(
            ctx.db, ctx.guild_id, name.strip(), metric,
            prize_pool_usd, duration_days,
        )
        if row is None:
            await ctx.reply_error(
                "A season is already active. End it with `,season end` first."
            )
            return
        embed = (
            card(
                f"\U0001F3C6 Season Started: {row['name']}",
                description=(
                    f"Metric: **{_svc.metric_label(metric)}**\n"
                    f"{_svc.metric_description(metric)}\n"
                    f"Prize pool: **{FormatKit.usd(float(row['prize_pool_usd']))}**\n"
                    f"Ends: **{fmt_ts(row['ends_at'])}**"
                ),
                color=C_SUCCESS,
            )
            .footer(
                "Theme: classic. Use ,season theme <name> to apply XP "
                "boosts; see ,season themes for options."
            )
            .build()
        )
        await ctx.send_embed(embed)

    @season.command(name="themes")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_themes(self, ctx: DiscoContext) -> None:
        """List available season pass themes with their XP boosts."""
        active = await _svc.get_active(ctx.db, ctx.guild_id)
        current_theme = (active.get("theme") if active else None) or "none"
        lines = []
        for name in _pass_cfg.theme_names():
            marker = " \U00002B50 active" if name == current_theme else ""
            lines.append(f"**{name}**{marker}\n  {_pass_cfg.theme_summary(name)}")
        embed = (
            card(
                "\U0001F3A8 Season Pass Themes",
                description="\n\n".join(lines),
                color=C_GOLD,
            )
            .footer(
                "Admins: ,season theme <name> applies a theme to the "
                "currently active season."
            )
            .build()
        )
        await ctx.send_embed(embed)

    @season.command(name="theme")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_theme(self, ctx: DiscoContext, name: str) -> None:
        """Set the XP-multiplier theme on the active season. Requires
        Manage Server. Future XP grants use the new multipliers; already-
        earned XP is unchanged.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to set a theme.")
            return
        name = name.lower().strip()
        if name not in _pass_cfg.THEMES:
            await ctx.reply_error(
                f"Unknown theme **{name}**. Available: "
                f"{', '.join(_pass_cfg.theme_names())}.\n"
                f"See `,season themes` for details."
            )
            return
        row = await _svc.set_theme(ctx.db, ctx.guild_id, name)
        if row is None:
            await ctx.reply_error("No active season to theme.")
            return
        await ctx.reply_success(
            f"Theme set to **{name}**.\n{_pass_cfg.theme_summary(name)}",
            title="Theme Applied",
        )

    @season.command(name="help")
    @guild_only
    @no_bots
    async def season_help(self, ctx: DiscoContext, target: str = "user") -> None:
        """Explain seasons + the season pass. ``,season help admin`` shows
        admin lifecycle commands; default shows the player surface.
        """
        if target.lower() in ("admin", "mod", "gm"):
            await ctx.send_embed(_build_season_admin_help())
            return
        await ctx.send_embed(_build_season_user_help())

    @season.command(name="top", aliases=["passtop"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_top(self, ctx: DiscoContext) -> None:
        """Top 10 users by season pass XP in the current or latest season."""
        active = await _svc.get_active(ctx.db, ctx.guild_id)
        season = active or await _latest_finalized(ctx.db, ctx.guild_id)
        if season is None:
            await ctx.reply_error("No season has run on this server yet.")
            return
        sid = int(season["season_id"])
        rows = await _svc.top_xp(ctx.db, sid, limit=10)
        if not rows:
            await ctx.reply_error("No pass XP has been earned yet this season.")
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            xp = int(r["xp"])
            tier = _pass_cfg.tier_for_xp(xp)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"User {uid}"
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"#{i}")
            lines.append(
                f"{medal} **{name}**  -  Tier **{tier}/{_pass_cfg.MAX_TIER}** "
                f"({xp:,} XP)"
            )
        state = "active" if active else "finalized"
        embed = (
            card(
                f"\U0001F3AB Pass Leaderboard: {season['name']}",
                description="\n".join(lines),
                color=C_GOLD,
            )
            .footer(f"Season {state}  -  top 10 by season pass XP")
            .build()
        )
        await ctx.send_embed(embed)

    @season.command(name="pass", aliases=["sp", "seasonpass"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_pass(self, ctx: DiscoContext) -> None:
        """View your season pass progress and claimable tier rewards."""
        db = ctx.db
        active = await _svc.get_active(db, ctx.guild_id)
        season = active or await _latest_finalized(db, ctx.guild_id)
        if season is None:
            await ctx.reply_error(
                "No season has started on this server yet. "
                "Ask an admin to start one with `,season start`."
            )
            return

        uid = ctx.author.id
        sid = int(season["season_id"])
        xp = await _svc.get_xp(db, sid, uid)
        claimed = await _svc.claimed_tiers(db, sid, uid)
        unlocked = _pass_cfg.tier_for_xp(xp)
        await ctx.send_embed(_build_pass_card(ctx, season, xp, unlocked, claimed))

    @season.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_claim(self, ctx: DiscoContext, target: str) -> None:
        """Claim a pass tier reward. ``target`` is a tier number or 'all'.

        Tiers past the one you've unlocked cannot be claimed. Already-
        claimed tiers are silently skipped when using ``all``.
        """
        db = ctx.db
        uid = ctx.author.id
        gid = ctx.guild_id
        active = await _svc.get_active(db, gid)
        season = active or await _latest_finalized(db, gid)
        if season is None:
            await ctx.reply_error("No season to claim from.")
            return

        sid = int(season["season_id"])
        xp = await _svc.get_xp(db, sid, uid)
        unlocked = _pass_cfg.tier_for_xp(xp)
        claimed = await _svc.claimed_tiers(db, sid, uid)

        tiers: list[int]
        if target.lower() == "all":
            tiers = [t for t in range(1, unlocked + 1) if t not in claimed]
            if not tiers:
                await ctx.reply_error("No unclaimed tiers ready yet.")
                return
        else:
            try:
                t = int(target)
            except ValueError:
                await ctx.reply_error("Tier must be a number or 'all'.")
                return
            tiers = [t]

        total = 0.0
        names: list[str] = []
        failures: list[str] = []
        for t in tiers:
            ok, msg, reward = await _svc.claim_tier(self.bot, sid, uid, gid, t)
            if ok:
                total += reward
                names.append(f"Tier {t}  -  {FormatKit.usd(reward)}")
            else:
                failures.append(msg)

        if not names:
            await ctx.reply_error(failures[0] if failures else "Nothing to claim.")
            return

        body = "\n".join(f"\U00002705 {n}" for n in names)
        body += f"\n\n**Total: {FormatKit.usd(total)}**"
        await ctx.send_embed(
            card("Pass Rewards Claimed", description=body, color=C_SUCCESS).build()
        )

    @season.command(name="end")
    @guild_only
    @no_bots
    @ensure_registered
    async def season_end(self, ctx: DiscoContext) -> None:
        """Finalize the active season immediately. Requires Manage Server."""
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to end a season.")
            return
        active = await _svc.get_active(ctx.db, ctx.guild_id)
        if active is None:
            await ctx.reply_error("No active season to end.")
            return
        ok = await ctx.confirm(
            f"End the **{active['name']}** season now and pay out "
            f"{FormatKit.usd(float(active['prize_pool_usd']))} in rewards?"
        )
        if not ok:
            return
        await _svc.end_season(self.bot, int(active["season_id"]))
        final = await _svc.get_season(ctx.db, int(active["season_id"]))
        await ctx.send_embed(await _build_finalized_card(ctx, final or active))

    # ── ,season auto ... ─ admin: auto-rotation toggle + config ──────────
    @season.group(name="auto", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto(self, ctx: DiscoContext) -> None:
        """Auto-rotation status: themed (season + 5 challenges) pairs.

        Subcommands (Manage Server):
            ``,season auto on``          -- enable rotation
            ``,season auto off``         -- disable rotation
            ``,season auto pool <usd>``  -- set per-season pool
            ``,season auto cpool <usd>`` -- set total challenge pool
            ``,season auto days <n>``    -- set duration (1-30 days)
            ``,season auto next``        -- force-start the next pair
        """
        s = await ctx.db.get_guild_settings(ctx.guild_id) or {}
        enabled = bool(s.get("auto_seasons_enabled"))
        days, season_pool, challenge_pool, idx = await _auto_svc._read_config(
            ctx.db, ctx.guild_id,
        )
        cur_pair = _pairs_cfg.get_pair(idx)
        rows: list[str] = []
        rows.append(f"**Status:** {'ON' if enabled else 'OFF'}")
        rows.append(f"**Duration:** {days} day(s)")
        rows.append(f"**Season pool:** {FormatKit.usd(season_pool)}")
        rows.append(
            f"**Challenge pool:** {FormatKit.usd(challenge_pool)} "
            f"(split across {_pairs_cfg.CHALLENGES_PER_PAIR} challenges by weight)"
        )
        rows.append(
            f"**Next pair:** `{cur_pair.key}` -- {cur_pair.season.name} "
            f"({cur_pair.season.theme}, metric `{cur_pair.season.metric}`)"
        )
        rows.append("")
        rows.append("**Pairs in rotation:**")
        for i, p in enumerate(_pairs_cfg.PAIRS):
            marker = "->" if i == idx else "  "
            rows.append(f"`{marker}` {i + 1}. `{p.key}` ({p.season.theme})")
        embed = card(
            "🔁 Season Auto-Rotation",
            description="\n".join(rows),
            color=C_NAVY,
        ).footer(
            "Toggle: ,season auto on/off  ·  ,season auto pool <usd>  ·  "
            ",season auto days <1-30>"
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @season_auto.command(name="on", aliases=["enable", "start"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_on(self, ctx: DiscoContext) -> None:
        """Enable auto-rotation. Starts a pair immediately if none active."""
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to toggle auto-rotation.")
            return
        await ctx.db.execute(
            "INSERT INTO guild_settings (guild_id, auto_seasons_enabled) "
            "VALUES ($1, TRUE) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_enabled=TRUE",
            ctx.guild_id,
        )
        result = await _auto_svc.ensure_running(self.bot, ctx.guild_id)
        if result is None:
            active = await _svc.get_active(ctx.db, ctx.guild_id)
            tail = (
                f"Active season `{active['name']}` will rotate on its `ends_at`."
                if active else
                "No pair could be started yet (check that `seasons_pairs_config.PAIRS` "
                "isn't empty)."
            )
            await ctx.reply_success(
                f"Auto-rotation **enabled**. {tail}",
                title="🔁 Season Auto-Rotation",
            )
            return
        pair = result["pair"]
        n = len(result["challenges"])
        await ctx.reply_success(
            f"Auto-rotation **enabled** and pair `{pair.key}` "
            f"(`{pair.season.name}`) started with **{n}** challenges.",
            title="🔁 Season Auto-Rotation",
        )

    @season_auto.command(name="off", aliases=["disable", "stop"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_off(self, ctx: DiscoContext) -> None:
        """Disable auto-rotation. The current season + challenges keep
        running to their existing deadlines.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to toggle auto-rotation.")
            return
        await ctx.db.execute(
            "INSERT INTO guild_settings (guild_id, auto_seasons_enabled) "
            "VALUES ($1, FALSE) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_enabled=FALSE",
            ctx.guild_id,
        )
        await ctx.reply_success(
            "Auto-rotation **disabled**. Active season + challenges keep "
            "running to their deadlines but no new pair will start when "
            "they end.",
            title="🔁 Season Auto-Rotation",
        )

    @season_auto.command(name="pool", aliases=["seasonpool"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_pool(self, ctx: DiscoContext, usd: float) -> None:
        """Set the per-season prize pool (USD). Applied to the next
        pair started, not the currently-running season.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to configure auto-rotation.")
            return
        if usd < _pairs_cfg.MIN_POOL_USD or usd > _pairs_cfg.MAX_POOL_USD:
            await ctx.reply_error(
                f"Pool must be between {FormatKit.usd(_pairs_cfg.MIN_POOL_USD)} "
                f"and {FormatKit.usd(_pairs_cfg.MAX_POOL_USD)}."
            )
            return
        await ctx.db.execute(
            "INSERT INTO guild_settings (guild_id, auto_seasons_pool_usd) "
            "VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_pool_usd=$2",
            ctx.guild_id, _auto_svc._usd_to_scaled(usd),
        )
        await ctx.reply_success(
            f"Per-season pool set to **{FormatKit.usd(usd)}**. "
            f"Applies to the next pair started.",
            title="🔁 Season Auto-Rotation",
        )

    @season_auto.command(name="cpool", aliases=["challengepool", "chalpool"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_cpool(self, ctx: DiscoContext, usd: float) -> None:
        """Set the total challenge pool (USD). Split across the pair's
        5 challenges by ``pool_weight``. Applied to the next pair.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to configure auto-rotation.")
            return
        if usd < _pairs_cfg.MIN_POOL_USD or usd > _pairs_cfg.MAX_POOL_USD:
            await ctx.reply_error(
                f"Pool must be between {FormatKit.usd(_pairs_cfg.MIN_POOL_USD)} "
                f"and {FormatKit.usd(_pairs_cfg.MAX_POOL_USD)}."
            )
            return
        await ctx.db.execute(
            "INSERT INTO guild_settings (guild_id, auto_seasons_challenge_pool_usd) "
            "VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_challenge_pool_usd=$2",
            ctx.guild_id, _auto_svc._usd_to_scaled(usd),
        )
        await ctx.reply_success(
            f"Total challenge pool set to **{FormatKit.usd(usd)}** "
            f"(split across {_pairs_cfg.CHALLENGES_PER_PAIR} challenges).",
            title="🔁 Season Auto-Rotation",
        )

    @season_auto.command(name="days", aliases=["duration"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_days(self, ctx: DiscoContext, days: int) -> None:
        """Set the per-pair duration in days (1-30). Applied to the
        next pair started.
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to configure auto-rotation.")
            return
        if days < _pairs_cfg.MIN_DURATION_DAYS or days > _pairs_cfg.MAX_DURATION_DAYS:
            await ctx.reply_error(
                f"Duration must be between {_pairs_cfg.MIN_DURATION_DAYS} "
                f"and {_pairs_cfg.MAX_DURATION_DAYS} days."
            )
            return
        await ctx.db.execute(
            "INSERT INTO guild_settings (guild_id, auto_seasons_days) "
            "VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_days=$2",
            ctx.guild_id, int(days),
        )
        await ctx.reply_success(
            f"Per-pair duration set to **{days} day(s)**. Applies to the next pair.",
            title="🔁 Season Auto-Rotation",
        )

    @season_auto.command(name="next", aliases=["force", "rotate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def season_auto_next(self, ctx: DiscoContext) -> None:
        """Force-start the next pair immediately. No-op if a season is
        already active (end the current one first with ``,season end``).
        """
        if not _require_admin(ctx):
            await ctx.reply_error("You need Manage Server to force-rotate.")
            return
        active = await _svc.get_active(ctx.db, ctx.guild_id)
        if active is not None:
            await ctx.reply_error_action(
                f"`{active['name']}` is already running. End it first with "
                f"`,season end` before forcing the next pair.",
                "End Current Season",
                "season end",
            )
            return
        result = await _auto_svc.start_next_pair(self.bot, ctx.guild_id)
        if result is None:
            await ctx.reply_error(
                "Could not start a pair (race with another caller, or no pairs "
                "configured)."
            )
            return
        pair = result["pair"]
        n = len(result["challenges"])
        await ctx.reply_success(
            f"Started pair `{pair.key}` -- season `{pair.season.name}` "
            f"({pair.season.theme}) + **{n}** challenges.",
            title="🔁 Season Auto-Rotation",
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Seasons(bot))
