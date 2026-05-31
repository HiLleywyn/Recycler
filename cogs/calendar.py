"""
cogs/calendar.py  -  Server calendar embed + view.

Exposes:
* ``,calendar`` (alias ``,events``)  -  player-facing browser for
  upcoming + recurring server events / challenges.
* ``post_calendar_to_bot_channel(bot, guild)``  -  the auto-post
  helper the admin event / challenge commands call so a fresh
  calendar embed lands in the configured bot channel whenever
  something new is scheduled.

The view layout mirrors the ,mines game button grid: each calendar
item gets a tile (5 per row, up to 4 rows + a bottom Refresh / Close
row). Tile color encodes the kind:
  * ``challenge``  -> Blurple (primary)
  * ``market``     -> Success (green)
  * ``recurring``  -> Secondary (gray)
Click a tile to see the full description / countdown for that item
ephemerally so the public message stays clean.

The data layer lives in services/calendar.py so this cog is purely
presentation. Sorted live-first then upcoming-soonest.
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
    C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, fmt_rel,
)
from services.calendar import CalendarItem, list_calendar

log = logging.getLogger(__name__)


_KIND_STYLE: dict[str, discord.ButtonStyle] = {
    "challenge": discord.ButtonStyle.primary,    # blurple
    "market":    discord.ButtonStyle.success,    # green (live event)
    "recurring": discord.ButtonStyle.secondary,  # gray
}

_KIND_COLOR: dict[str, int] = {
    "challenge": C_INFO,
    "market":    C_GOLD,
    "recurring": C_NEUTRAL,
}

# Tile cap. Mines uses a 5x5 grid; we reserve row 4 for Refresh / Close
# so 4 rows of 5 tiles = 20 calendar slots. Extra items overflow to a
# secondary "more" embed field.
_MAX_TILES = 20


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tile_label(item: CalendarItem, idx: int) -> str:
    """Compact label for a tile button (max 80 chars per Discord)."""
    return f"{idx + 1}. {_short(item.title, 70)}"


def _tile_emoji(item: CalendarItem) -> str | None:
    """Single emoji for the tile glyph -- skipped if blurb already has one."""
    em = item.emoji or ""
    return em or None


def _build_calendar_embed(
    guild: discord.Guild | None,
    items: list[CalendarItem],
) -> discord.Embed:
    """Render the calendar overview embed.

    Items already sorted live-first by services.calendar. Each tile
    references back to its label index here so the click handler can
    map the button press to the correct item.
    """
    title = (
        f"\U0001F5D3️  {guild.name} -- Calendar"
        if guild else "\U0001F5D3️  Server Calendar"
    )
    if not items:
        return card(
            title, color=C_NAVY,
            description=(
                "No active challenges or events right now.\n"
                "Recurring resets show up here too -- come back at the "
                "next daily / weekly turnover."
            ),
        ).build()

    live_count = sum(1 for it in items if it.active_now)
    scheduled_count = sum(1 for it in items if not it.active_now)

    builder = card(
        title, color=C_NAVY,
        description=(
            f"**{live_count}** live  ·  **{scheduled_count}** scheduled\n"
            "Tap a tile for full details + countdown."
        ),
    )

    # Live now -- list with live relative countdowns inline.
    live_lines: list[str] = []
    sched_lines: list[str] = []
    for i, it in enumerate(items[:_MAX_TILES]):
        ts = it.ends_at if it.active_now else it.starts_at
        when = fmt_rel(ts, fallback="ongoing") if ts is not None else "ongoing"
        kind_tag = {
            "challenge": "challenge",
            "market":    "market event",
            "recurring": "recurring",
        }.get(it.kind, it.kind)
        line = (
            f"**{i + 1}. {it.emoji or ''} {it.title}** "
            f"({kind_tag})  ·  {'ends' if it.active_now else 'starts'} {when}\n"
            f"-# {_short(it.blurb, 200)}"
        )
        (live_lines if it.active_now else sched_lines).append(line)

    if live_lines:
        builder = builder.field(
            f"Live now ({len(live_lines)})",
            "\n".join(live_lines), False,
        )
    if sched_lines:
        builder = builder.field(
            f"Scheduled ({len(sched_lines)})",
            "\n".join(sched_lines), False,
        )

    overflow = max(0, len(items) - _MAX_TILES)
    if overflow > 0:
        builder = builder.field(
            "More",
            f"_+{overflow} more not shown -- finish or wait for the live ones._",
            False,
        )

    builder = builder.footer(
        "Buttons mirror the order above. Tile color: blurple = challenge, "
        "green = market event, gray = recurring."
    )
    return builder.build()


def _build_detail_embed(item: CalendarItem) -> discord.Embed:
    """Per-tile detail embed (ephemeral)."""
    color = _KIND_COLOR.get(item.kind, C_INFO)
    builder = card(
        f"{item.emoji or ''} {item.title}".strip(),
        color=color, description=item.blurb,
    )
    if item.starts_at:
        builder = builder.field(
            "Starts",
            f"{fmt_rel(item.starts_at, style='F')} ({fmt_rel(item.starts_at)})",
            False,
        )
    if item.ends_at:
        builder = builder.field(
            "Ends",
            f"{fmt_rel(item.ends_at, style='F')} ({fmt_rel(item.ends_at)})",
            False,
        )
    if item.kind == "challenge":
        ex = item.extra or {}
        prog = int(ex.get("progress") or 0)
        target = int(ex.get("target") or 0)
        pool = float(ex.get("reward_pool_usd") or 0.0)
        builder = builder.field(
            "Progress",
            f"**{prog:,} / {target:,}**  ·  "
            f"reward pool **${pool:,.2f}**",
            False,
        )
    if item.cmd_hint:
        builder = builder.field("Command", f"`{item.cmd_hint}`", True)
    builder = builder.field(
        "Type", item.kind.replace("_", " ").title(), True,
    )
    return builder.build()


# ---------------------------------------------------------------------------
# Calendar tile button + view
# ---------------------------------------------------------------------------


class _CalendarTileButton(discord.ui.Button):
    def __init__(self, item: CalendarItem, idx: int) -> None:
        super().__init__(
            label=_tile_label(item, idx),
            emoji=_tile_emoji(item),
            style=_KIND_STYLE.get(item.kind, discord.ButtonStyle.secondary),
            row=idx // 5,
        )
        self.item = item

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=_build_detail_embed(self.item), ephemeral=True,
        )


class _CalendarRefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Refresh", emoji="\U0001F504",
            style=discord.ButtonStyle.secondary, row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CalendarView" = self.view  # type: ignore[assignment]
        await view.refresh(interaction)


class _CalendarCloseButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Close", emoji="\U0000274C",
            style=discord.ButtonStyle.danger, row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            pass


class CalendarView(discord.ui.View):
    """Mines-style grid of calendar tiles.

    Owned-channel-public: anyone in the guild can browse the calendar
    without owning the message (calendars are shared-public surfaces).
    Refresh re-pulls the data; Close strips the view leaving the embed.
    """

    def __init__(self, bot: Discoin, guild: discord.Guild | None) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.message: discord.Message | None = None
        self.items: list[CalendarItem] = []

    @classmethod
    async def open(
        cls, bot: Discoin, channel: discord.abc.Messageable,
        guild: discord.Guild | None,
    ) -> "CalendarView":
        view = cls(bot, guild)
        items = await list_calendar(
            bot.db, int(guild.id) if guild else 0,
            redis=getattr(bot, "redis", None),
        )
        view.items = items
        view._rebuild_buttons()
        embed = _build_calendar_embed(guild, items)
        try:
            view.message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.debug("calendar: send failed", exc_info=True)
            view.message = None
        return view

    def _rebuild_buttons(self) -> None:
        for child in list(self.children):
            self.remove_item(child)
        for i, item in enumerate(self.items[:_MAX_TILES]):
            self.add_item(_CalendarTileButton(item, i))
        self.add_item(_CalendarRefreshButton())
        self.add_item(_CalendarCloseButton())

    async def refresh(self, interaction: discord.Interaction) -> None:
        items = await list_calendar(
            self.bot.db,
            int(self.guild.id) if self.guild else 0,
            redis=getattr(self.bot, "redis", None),
        )
        self.items = items
        self._rebuild_buttons()
        embed = _build_calendar_embed(self.guild, items)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            log.debug("calendar: refresh edit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Auto-post helper for admin commands
# ---------------------------------------------------------------------------


async def post_calendar_to_bot_channel(
    bot: Discoin, guild: discord.Guild,
) -> discord.Message | None:
    """Drop a fresh calendar embed in the guild's configured bot channel.

    Called by the admin challenge / event commands so a calendar update
    follows every new schedule item without the admin running
    ``,calendar`` themselves. Best-effort: silently no-ops on missing
    channel / send permission failures.
    """
    if guild is None:
        return None
    channel = None
    try:
        # Reuse the guild's configured "events" / "bot" channel resolver.
        s = await bot.db.get_guild_settings(guild.id)
        for col in ("events_channel_id", "bot_channel_id", "general_channel_id"):
            cid = int(s.get(col) or 0) if s else 0
            if cid:
                ch = guild.get_channel(cid)
                if ch is not None:
                    channel = ch
                    break
    except Exception:
        log.debug("calendar: settings lookup failed", exc_info=True)
    if channel is None:
        # Fall back to the guild's system channel; if that's also None,
        # bail. Players can still run ,calendar manually.
        channel = guild.system_channel
    if channel is None:
        return None
    try:
        view = await CalendarView.open(bot, channel, guild)
        return view.message
    except Exception:
        log.debug("calendar: auto-post failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Cog -- player commands
# ---------------------------------------------------------------------------


class Calendar(commands.Cog):
    """``,calendar`` -- upcoming + recurring server events / challenges."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="calendar", aliases=["agenda", "schedule"],
        with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def calendar_cmd(self, ctx: DiscoContext) -> None:
        """Open the server calendar -- live events, active challenges, recurring resets.

        Mines-style button grid: tap a tile for full details and countdown.
        Updates whenever an admin starts a challenge or triggers a
        market event; ``,calendar`` always shows the live state.
        """
        view = CalendarView(self.bot, ctx.guild)
        items = await list_calendar(
            ctx.db, ctx.guild_id,
            redis=getattr(self.bot, "redis", None),
        )
        view.items = items
        view._rebuild_buttons()
        embed = _build_calendar_embed(ctx.guild, items)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Calendar(bot))
