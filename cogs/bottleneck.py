"""Wealth Bottleneck cog -- player-facing curve, pool, and history surface.

Replaces the legacy ``,wealth`` (Wealth Equalizer) cog. The bottleneck
itself runs inline at every credit point (work / beg / ape / daily /
faucet / drops / trade gains / gamba yield / stake yield / LP yield /
PoS rewards / delegation rewards / moon-pool yield / savings interest)
via :func:`services.bottleneck.apply_bottleneck`, so this cog ships
*only* the read-side surfaces players need to see what the system did
to their gains.

Subcommands::

    ,bottleneck            see your current rank, multiplier, and recent flow
    ,bottleneck curve      pretty-print the full multiplier curve
    ,bottleneck pool       guild-wide community-pool snapshot
    ,bottleneck me         your last 14 days of drag/boost
    ,bottleneck recent     the last 25 bottleneck events in this guild

Aliases ``,bn`` and ``,wealth`` are wired up so the legacy command word
still resolves; the equalizer-style subcommands (``flow``, ``cycles``,
``runnow``, ``ctrl``, ``tx``) no longer exist  -  the rank-based system
has no cycles or controller.
"""
from __future__ import annotations

import logging

from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human
from core.framework.ui import (
    C_AMBER,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_SUCCESS,
    fmt_ts,
    fmt_usd,
    mention,
)
from services import bottleneck as bn_svc

log = logging.getLogger(__name__)


def _curve_lines(highlight_pctile: float | None = None) -> list[str]:
    """Render the active curve as a list of lines for embed fields."""
    raw = list(getattr(
        Config, "BOTTLENECK_CURVE", bn_svc.BOTTLENECK_DEFAULT_CURVE,
    )) or bn_svc.BOTTLENECK_DEFAULT_CURVE
    out: list[str] = []
    for pctile, mult in raw:
        marker = ""
        if (
            highlight_pctile is not None
            and abs(float(pctile) - highlight_pctile) < 0.05
        ):
            marker = "  <- you"
        tag = bn_svc.percentile_label(float(pctile))
        out.append(
            f"`{float(pctile)*100:>5.1f}%` ({tag:<11}) -> "
            f"x**{float(mult):.2f}**{marker}"
        )
    return out


class WealthBottleneck(commands.Cog):
    """Read-only player surface for the Wealth Bottleneck."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── ,bottleneck (root) ────────────────────────────────────────────────

    @commands.group(
        name="bottleneck",
        aliases=["bn", "wealth", "throttle"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def bottleneck(self, ctx: DiscoContext) -> None:
        """Show your wealth bottleneck multiplier and recent activity.

        Every USD-equivalent gain you earn (work, beg, ape, daily,
        faucet, drops, trade profit, gamba yield, stake / LP / PoS /
        delegation / mining / network / savings yield) is scaled by your
        rank on the leaderboard. Top of the leaderboard keeps less of
        each gain (drag, fed into a per-guild community pool); bottom of
        the leaderboard gets a USD top-up on each gain (boost, drawn from
        the same pool). Existing holdings (stones, bags, rigs, NFTs,
        savings deposits, validator stakes, delegations, LP, mining,
        moon stakes, gamba stakes) are NEVER touched.
        """
        gid = ctx.guild_id
        uid = ctx.author.id
        pctile, nw_usd, n = await bn_svc.lookup_percentile(
            ctx.db, uid=uid, gid=gid,
        )
        min_holders = int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5))
        active = n >= max(2, min_holders)
        mult = bn_svc.bottleneck_multiplier(pctile) if active else 1.0
        pool = await bn_svc.get_pool_state(ctx.db, gid)
        history = await bn_svc.get_user_history(
            ctx.db, uid=uid, gid=gid, days=7,
        )

        if mult > 1.0:
            color = C_SUCCESS
            arrow = "+"
            verdict = "Boost tier - your gains get a community-pool top-up."
        elif mult < 1.0:
            color = C_AMBER
            arrow = "-"
            verdict = "Drag tier - a slice of your gains feeds the community pool."
        else:
            color = C_INFO
            arrow = "="
            verdict = (
                "Neutral. The bottleneck is sleeping for you right now."
                if active
                else "Bottleneck is dormant - guild needs more ranked players."
            )

        b = (
            card("Wealth Bottleneck", color=color)
            .author(
                ctx.author.display_name,
                icon_url=ctx.author.display_avatar.url,
            )
            .description(verdict)
            .field(
                "Your Rank",
                (
                    f"**{bn_svc.percentile_label(pctile)}** "
                    f"({pctile*100:.1f}%-ile)\n"
                    f"Net worth: **{fmt_usd(nw_usd)}**\n"
                    f"Holders: **{n:,}**"
                ),
                True,
            )
            .field(
                "Multiplier",
                (
                    f"**x{mult:.2f}** {arrow}\n"
                    f"Applied to every USD-equivalent gain "
                    f"you earn (work / beg / ape / daily / "
                    f"trade gain / gamba yield / stake / LP / "
                    f"PoS / mining / network / savings)."
                ),
                True,
            )
            .field(
                "Community Pool",
                (
                    f"**{fmt_usd(pool['pool_usd'])}**\n"
                    f"Drag from rich players collects here. "
                    f"Boost to poor players is paid out of it - when "
                    f"the pool runs dry, boosts pause until refilled."
                ),
                True,
            )
            .field(
                "Last 7 days for you",
                (
                    f"Credits: **{history['credits']:,}**\n"
                    f"Drag: **{fmt_usd(history['drag_usd'])}**\n"
                    f"Boost: **{fmt_usd(history['boost_usd'])}**\n"
                    f"Net swing: **{fmt_usd(history['net_swing_usd'])}**"
                ),
                False,
            )
            .field(
                "Curve",
                "\n".join(_curve_lines(highlight_pctile=pctile)),
                False,
            )
            .footer(
                f",{ctx.prefix or ','}bottleneck pool / curve / me / recent  "
                f"|  ,{ctx.prefix or ','}help bottleneck"
            )
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── ,bottleneck curve ─────────────────────────────────────────────────

    @bottleneck.command(name="curve", aliases=["chart", "schedule"])
    @guild_only
    @no_bots
    @ensure_registered
    async def bottleneck_curve(self, ctx: DiscoContext) -> None:
        """Show the multiplier curve in full."""
        pctile, _, n = await bn_svc.lookup_percentile(
            ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
        )
        active = n >= max(
            2, int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5)),
        )
        b = (
            card("Wealth Bottleneck Curve", color=C_NAVY)
            .description(
                "Multiplier applied to every USD-equivalent gain you "
                "earn, by your rank on the wealth leaderboard. The "
                "curve interpolates linearly between anchors."
            )
            .field(
                "Anchors",
                "\n".join(_curve_lines(highlight_pctile=pctile if active else None)),
                False,
            )
            .field(
                "Small-Server Gate",
                (
                    f"Bottleneck only activates with at least "
                    f"**{int(getattr(Config, 'BOTTLENECK_MIN_HOLDERS', 5))}** "
                    f"ranked holders.\n"
                    f"Currently: **{n:,}** ranked holders."
                ),
                True,
            )
            .field(
                "Boost Cap",
                (
                    f"A single credit can at most have its USD value "
                    f"multiplied by **"
                    f"x{1.0 + float(getattr(Config, 'BOTTLENECK_MAX_BOOST_MULTIPLE_OF_GROSS', 1.0)):.2f}**.\n"
                    f"Even a giant pool cannot turn one ,beg into a fortune."
                ),
                True,
            )
            .footer("Every credit shows the multiplier on its own embed footer.")
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── ,bottleneck pool ──────────────────────────────────────────────────

    @bottleneck.command(name="pool", aliases=["community", "treasury"])
    @guild_only
    @no_bots
    @ensure_registered
    async def bottleneck_pool(self, ctx: DiscoContext) -> None:
        """Snapshot the per-guild USD community pool."""
        gid = ctx.guild_id
        pool = await bn_svc.get_pool_state(ctx.db, gid)
        # 24h flow summary aggregated from the audit log.
        row = await ctx.db.fetch_one(
            "SELECT COALESCE(SUM(drag_usd_raw),0)     AS drag_in, "
            "       COALESCE(SUM(boost_wallet_raw),0) AS boost_out, "
            "       COUNT(*)                          AS n_credits "
            "FROM bottleneck_log "
            "WHERE guild_id=$1 AND at >= NOW() - INTERVAL '24 hours'",
            gid,
        )
        drag_24h = to_human(int(row.get("drag_in") or 0))
        boost_24h = to_human(int(row.get("boost_out") or 0))
        n_24h = int(row.get("n_credits") or 0)
        b = (
            card("Wealth Bottleneck - Community Pool", color=C_GOLD)
            .description(
                "Closed-loop per-guild pool. Drag taken off rich players' "
                "gains feeds it; boost paid to poor players' gains drains "
                "it. When the pool is empty, boosts pause until it refills "
                "(no value is ever printed)."
            )
            .field(
                "Pool Balance",
                f"**{fmt_usd(pool['pool_usd'])}**",
                True,
            )
            .field(
                "Last Updated",
                fmt_ts(pool.get("updated_at")) if pool.get("updated_at") else "-",
                True,
            )
            .field(
                "Last 24 hours",
                (
                    f"Credits: **{n_24h:,}**\n"
                    f"Drag in: **{fmt_usd(drag_24h)}**\n"
                    f"Boost out: **{fmt_usd(boost_24h)}**\n"
                    f"Net pool delta: **{fmt_usd(drag_24h - boost_24h)}**"
                ),
                False,
            )
            .footer(",bottleneck recent for the per-credit log.")
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── ,bottleneck me ────────────────────────────────────────────────────

    @bottleneck.command(name="me", aliases=["mine", "history"])
    @guild_only
    @no_bots
    @ensure_registered
    async def bottleneck_me(self, ctx: DiscoContext) -> None:
        """Your last 14 days of bottleneck activity."""
        gid, uid = ctx.guild_id, ctx.author.id
        hist7 = await bn_svc.get_user_history(
            ctx.db, uid=uid, gid=gid, days=7,
        )
        hist14 = await bn_svc.get_user_history(
            ctx.db, uid=uid, gid=gid, days=14,
        )
        recent = await bn_svc.get_recent_log(
            ctx.db, gid=gid, uid=uid, limit=10,
        )
        body_lines: list[str] = []
        for r in recent:
            kind = str(r.get("kind") or "?")
            sym = str(r.get("symbol") or "USD")
            mult = float(r.get("multiplier") or 1.0)
            drag = r["drag_usd"]
            boost = r["boost_usd"]
            net = r["net_usd"]
            ts = fmt_ts(r.get("at"))
            tag = (
                f"-{fmt_usd(drag)} drag" if drag > 0
                else (f"+{fmt_usd(boost)} boost" if boost > 0 else "no effect")
            )
            body_lines.append(
                f"`{ts}` {kind:<16} x{mult:.2f}  net {fmt_usd(net)} {sym}  ({tag})"
            )
        b = (
            card("Your Bottleneck History", color=C_INFO)
            .author(
                ctx.author.display_name,
                icon_url=ctx.author.display_avatar.url,
            )
            .field(
                "Last 7 days",
                (
                    f"Credits: **{hist7['credits']:,}**\n"
                    f"Drag: **{fmt_usd(hist7['drag_usd'])}**\n"
                    f"Boost: **{fmt_usd(hist7['boost_usd'])}**\n"
                    f"Net swing: **{fmt_usd(hist7['net_swing_usd'])}**"
                ),
                True,
            )
            .field(
                "Last 14 days",
                (
                    f"Credits: **{hist14['credits']:,}**\n"
                    f"Drag: **{fmt_usd(hist14['drag_usd'])}**\n"
                    f"Boost: **{fmt_usd(hist14['boost_usd'])}**\n"
                    f"Net swing: **{fmt_usd(hist14['net_swing_usd'])}**"
                ),
                True,
            )
            .field(
                "Recent Credits",
                "\n".join(body_lines) if body_lines else "*no credits yet*",
                False,
            )
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── ,bottleneck recent ────────────────────────────────────────────────

    @bottleneck.command(name="recent", aliases=["log", "feed"])
    @guild_only
    @no_bots
    @ensure_registered
    async def bottleneck_recent(self, ctx: DiscoContext) -> None:
        """Last 25 bottleneck events across the guild."""
        gid = ctx.guild_id
        rows = await bn_svc.get_recent_log(ctx.db, gid=gid, limit=25)
        if not rows:
            await ctx.reply_error(
                "No bottleneck events yet. The system kicks in once "
                "players start earning."
            )
            return
        lines = []
        for r in rows:
            who = mention(int(r["user_id"]), ctx.guild, ctx.bot)
            kind = str(r.get("kind") or "?")
            mult = float(r.get("multiplier") or 1.0)
            drag = r["drag_usd"]
            boost = r["boost_usd"]
            tag = (
                f"-{fmt_usd(drag)}" if drag > 0
                else (f"+{fmt_usd(boost)}" if boost > 0 else "no effect")
            )
            lines.append(
                f"`{fmt_ts(r.get('at'))}` {who} {kind:<14} x{mult:.2f}  {tag}"
            )
        b = (
            card("Bottleneck Feed (last 25)", color=C_NAVY)
            .description("\n".join(lines))
            .footer(",bottleneck for your own multiplier  |  ,bottleneck pool for the community pool")
        )
        await ctx.reply(embed=b.build(), mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(WealthBottleneck(bot))
