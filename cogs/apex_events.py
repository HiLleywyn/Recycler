"""``,apex`` cog -- world events for Discoin.

World events (codename "Apex Events" internally and in DB tables) are
server-wide buff/debuff windows that spawn on a random roll, run for a
fixed duration, and then expire. Each event stacks one or more
modifier keys (e.g. ``mining.hashrate x1.5``) that consumers read via
``services/apex_events.modifier``.

Commands:
    ,apex                   -- show what's currently live (poster + embed)
    ,apex catalog           -- browse every event in the rotation
    ,apex info <id>         -- inspect a single event's flavour + modifiers
    ,apex history           -- last 10 events that ran here
    ,apex trigger <id>      -- admin: force-start an event for testing
"""
from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands, tasks

from configs.apex_events_config import EVENTS
from core.config import Config
from constants.ui import C_AMBER, C_CATASTROPHE, C_VOLATILE
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_INFO, C_SUCCESS, fmt_rel, fmt_ts
from services import apex_events as _svc
from services.event_poster import render_event_poster

log = logging.getLogger(__name__)


_RARITY_COLOR = {
    "info":         C_INFO,
    "warning":      C_AMBER,
    "volatile":     C_VOLATILE,
    "catastrophe":  C_CATASTROPHE,
}

_RARITY_TAGLINE = {
    "info":         "Calm. Small skew to one or two systems.",
    "warning":      "Tense. Material modifiers; plan around them.",
    "volatile":     "Wild. Big swings, short window -- act fast.",
    "catastrophe": "Catastrophic. Rare, severe, and brief.",
}


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h" + (f" {m}m" if m else "")
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d" + (f" {h}h" if h else "")


def _fmt_modifier(key: str, val: float) -> str:
    """Render one modifier line. ``x1.50`` style with a +/- delta in plain English."""
    try:
        v = float(val)
    except Exception:
        return f"`{key}`  -  invalid"
    delta_pct = (v - 1.0) * 100.0
    if abs(delta_pct) < 0.05:
        return f"`{key}`  x{v:.2f}  (flat)"
    sign = "+" if delta_pct > 0 else ""
    return f"`{key}`  x{v:.2f}  ({sign}{delta_pct:.0f}%)"


def _event_card_embed(event_id: str, ev: dict, *, color: int):
    """Build the standard info card for one event (used by catalog + info)."""
    rarity = ev.get("rarity", "info")
    rarity_label = rarity.title()
    rarity_tagline = _RARITY_TAGLINE.get(rarity, "")
    modifiers = ev.get("modifiers") or {}
    mod_lines = "\n".join(
        _fmt_modifier(k, v) for k, v in sorted(modifiers.items())
    ) or "_None._"
    builder = (
        card(f"World Event  -  {ev.get('name', event_id)}", color=color)
        .description(ev.get("flavour", "_No flavour text._"))
        .field(
            "Rarity",
            f"**{rarity_label}**  -  {rarity_tagline}".strip(),
            False,
        )
        .field(
            "Duration",
            _fmt_duration(int(ev.get("duration_secs", 0))),
            True,
        )
        .field("Roll weight", str(ev.get("weight", 0)), True)
        .field(f"Modifiers ({len(modifiers)})", mod_lines[:1024], False)
        .footer(f"Event id: {event_id}")
    )
    return builder


class ApexEvents(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        if getattr(Config, "APEX_EVENTS_ENABLED", True):
            self.tick.start()
            register_interval(
                "apex_events_tick",
                int(getattr(Config, "APEX_EVENT_TICK", 30)),
            )

    def cog_unload(self) -> None:
        try:
            self.tick.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=int(getattr(Config, "APEX_EVENT_TICK", 30)))
    async def tick(self) -> None:
        if not getattr(Config, "APEX_EVENTS_ENABLED", True):
            return
        for guild in list(self.bot.guilds):
            try:
                await _svc.expire_finished(self.bot.db, guild.id)
                await _svc.try_roll(self.bot.db, guild.id)
            except Exception:
                log.exception("apex_events tick failed gid=%s", guild.id)
        pulse("apex_events_tick")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="apex", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def apex(self, ctx: DiscoContext) -> None:
        """Show currently-live world events (codename: Apex Events)."""
        live = await _svc.active(ctx.db, ctx.guild_id)
        tick_secs = int(getattr(Config, "APEX_EVENT_TICK", 30))
        roll_pct = float(getattr(Config, "APEX_EVENT_ROLL_PCT", 0.05))

        if not live:
            embed = (
                card("World Events  -  none active", color=C_INFO)
                .description(
                    "World Events are server-wide buff/debuff windows. "
                    "They roll on a fixed heartbeat -- most rolls miss, "
                    "but when one lands every player in the server feels "
                    "the modifiers until the window closes."
                )
                .field(
                    "How rolls work",
                    f"One roll every **{tick_secs}s**, "
                    f"**{roll_pct * 100:.1f}%** chance each roll. "
                    f"The catalogue has **{len(EVENTS)}** events on a "
                    "weighted draw; rarer events weigh less.",
                    False,
                )
                .field(
                    "Examples in the rotation",
                    "\n".join(
                        f"- **{ev['name']}** ({ev.get('rarity', 'info').title()})"
                        for ev in list(EVENTS.values())[:5]
                    ),
                    False,
                )
                .footer(
                    "`,apex catalog` -- every event  -  "
                    "`,apex info <id>` -- single event  -  "
                    "`,apex history` -- last 10 here"
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Render the most recent live event as the headline poster.
        head = max(live, key=lambda r: r["started_at"])
        head_id = head["event_id"]
        head_ev = EVENTS.get(head_id, {})
        rarity = head_ev.get("rarity", "info")
        accent = _RARITY_COLOR.get(rarity, C_INFO)
        png = render_event_poster(head_id, ends_at=head["ends_at"])
        file = discord.File(io.BytesIO(png), filename="apex.png")

        modifiers = head_ev.get("modifiers") or {}
        mod_lines = "\n".join(
            _fmt_modifier(k, v) for k, v in sorted(modifiers.items())
        ) or "_None._"
        # ``ends_at`` arrives as an epoch float via _coerce; fmt_rel / fmt_ts
        # accept either floats or datetimes and produce the right Discord
        # timestamp markup without touching ``.tzinfo`` on a float.
        ends_at = head["ends_at"]
        ends_field = f"{fmt_rel(ends_at)}  -  {fmt_ts(ends_at, fmt='%H:%M UTC')}"

        embed = (
            card(
                f"World Event  -  {head_ev.get('name', head_id)}",
                color=accent,
            )
            .description(head_ev.get("flavour", ""))
            .field(
                "Rarity",
                f"**{rarity.title()}**  -  {_RARITY_TAGLINE.get(rarity, '')}".strip(),
                True,
            )
            .field("Ends", ends_field, True)
            .field(
                f"Active modifiers ({len(modifiers)})",
                mod_lines[:1024],
                False,
            )
        )
        if len(live) > 1:
            others = [
                EVENTS.get(r["event_id"], {}).get("name", r["event_id"])
                for r in live
                if r["event_id"] != head_id
            ]
            embed.field("Also active", ", ".join(others), False)
        embed.image("attachment://apex.png").footer(
            f"Event id: {head_id}  -  ,apex catalog for every event"
        )
        await ctx.reply(embed=embed.build(), file=file, mention_author=False)

    @apex.command(name="catalog", aliases=["list", "events"])
    async def apex_catalog(self, ctx: DiscoContext) -> None:
        """Browse every world event in the rotation."""
        total_weight = sum(int(ev.get("weight", 0)) for ev in EVENTS.values()) or 1
        # Sort: rarest first (lowest weight), so big events lead.
        ordered = sorted(
            EVENTS.items(),
            key=lambda kv: (int(kv[1].get("weight", 0)), kv[0]),
        )
        embed = card(
            "World Events catalogue",
            color=C_GOLD,
            description=(
                "Every event in the rotation. The roller picks one on a "
                "weighted draw -- lower weight = rarer. Stacked weights "
                f"total **{total_weight}**, and you only see one event "
                "land at a time per server (though multiple can run "
                "simultaneously if rolls land back-to-back)."
            ),
        )
        for event_id, ev in ordered:
            rarity = ev.get("rarity", "info")
            mods = ev.get("modifiers") or {}
            mod_brief = ", ".join(
                f"`{k}` x{float(v):.2f}" for k, v in list(mods.items())[:3]
            )
            if len(mods) > 3:
                mod_brief += f", +{len(mods) - 3} more"
            weight = int(ev.get("weight", 0))
            chance = 100.0 * weight / total_weight
            body = (
                f"_{ev.get('flavour', '')}_\n"
                f"**Rarity:** {rarity.title()}  -  "
                f"**Duration:** {_fmt_duration(int(ev.get('duration_secs', 0)))}  -  "
                f"**Pick rate:** {chance:.1f}% (weight {weight})\n"
                f"**Modifiers:** {mod_brief or '_none_'}\n"
                f"`,apex info {event_id}` for the full sheet."
            )
            embed.field(
                f"{ev.get('name', event_id)}",
                body[:1024],
                inline=False,
            )
        embed.footer(
            f"{len(EVENTS)} events  -  `,apex info <id>` -- single event"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @apex.command(name="info")
    async def apex_info(self, ctx: DiscoContext, *, event_id: str) -> None:
        """Inspect a single event's flavour + every modifier on one card."""
        eid = event_id.strip().lower()
        ev = EVENTS.get(eid)
        if not ev:
            await ctx.reply_error(
                f"Unknown event `{eid}`. Run `,apex catalog` for the list."
            )
            return
        rarity = ev.get("rarity", "info")
        accent = _RARITY_COLOR.get(rarity, C_INFO)
        png = render_event_poster(eid, ends_at=None)
        file = discord.File(io.BytesIO(png), filename="apex.png")
        embed = _event_card_embed(eid, ev, color=accent).image("attachment://apex.png").build()
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @apex.command(name="history")
    async def apex_history(self, ctx: DiscoContext) -> None:
        """Show the last 10 world events that have run in this server."""
        rows = await ctx.db.fetch_all(
            "SELECT event_id, started_at, ended_at FROM apex_events_history "
            "WHERE guild_id = $1 ORDER BY started_at DESC LIMIT 10",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error(
                "No World Events have run in this server yet. "
                "Run `,apex catalog` to see what's in the rotation."
            )
            return
        lines = []
        for r in rows:
            name = EVENTS.get(r["event_id"], {}).get("name", r["event_id"])
            # started_at / ended_at come back as epoch floats via _coerce.
            started = float(r["started_at"]) if r.get("started_at") is not None else None
            ended = float(r["ended_at"]) if r.get("ended_at") is not None else None
            duration = int(ended - started) if started is not None and ended is not None else 0
            when = fmt_rel(started) if started is not None else "unknown"
            lines.append(
                f"**{name}**  -  {when} (ran {_fmt_duration(duration)})"
            )
        embed = (
            card("World Events  -  recent history", color=C_INFO)
            .description("\n".join(lines))
            .footer(
                f"{len(rows)} event{'s' if len(rows) != 1 else ''}  -  "
                "`,apex info <id>` for details"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @apex.command(name="trigger", hidden=True)
    @commands.has_permissions(manage_guild=True)
    async def apex_trigger(self, ctx: DiscoContext, event_id: str) -> None:
        """Admin: force-start a World Event by id."""
        result = await _svc.trigger(ctx.db, ctx.guild_id, event_id)
        if not result:
            await ctx.reply_error(
                f"Unknown event id `{event_id}`. Run `,apex catalog`."
            )
            return
        png = render_event_poster(event_id, ends_at=result["ends_at"])
        file = discord.File(io.BytesIO(png), filename="apex.png")
        embed = (
            card(f"Triggered: {result['name']}", color=C_SUCCESS)
            .description(result["flavour"])
            .image("attachment://apex.png")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ApexEvents(bot))
