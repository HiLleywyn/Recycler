"""
core/framework/ui.py  -  Canonical UI primitives for Discoin.

Provides:
  ── Colors ────────────────────────────────────────────────────────────────────
  C_SUCCESS / C_ERROR / C_WARNING / C_INFO / C_GOLD / C_PURPLE
  C_TEAL / C_NAVY / C_PINK / C_NEUTRAL / C_BUY / C_SELL / C_AMBER

  ── Formatting ────────────────────────────────────────────────────────────────
  FormatKit            -  static helpers: usd(), token(), pct(), delta(), bar(),
                        stat_row(), gas(), mkt_cap(), short_hash(), time_ago()
  fmt_token / fmt_usd / fmt_pct / fmt_gas   -  module-level C3 convenience aliases

  ── Views ─────────────────────────────────────────────────────────────────────
  ConfirmView          -  yes / no confirmation dialog (author-locked)
  Paginator            -  ⏮ ◀ [X/N] ▶ ⏭ multi-page embed navigator
  CategoryPaginator    -  Select menu category switcher + per-category Prev/Next
  ValidatorSelectView  -  drill-down dropdown for validator/delegation inspection

  ── Modals ────────────────────────────────────────────────────────────────────
  InputModal           -  single-field text input

  ── Helpers ───────────────────────────────────────────────────────────────────
  send_paginated       -  send one embed or launch a full Paginator
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import math
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from core.framework.context import DiscoContext


# ══════════════════════════════════════════════════════════════════════════════
# Color palette
# ══════════════════════════════════════════════════════════════════════════════

from constants.ui import (  # noqa: F401  -  re-exported for existing imports
    C_SUCCESS, C_ERROR, C_WARNING, C_INFO, C_GOLD, C_PURPLE,
    C_TEAL, C_NAVY, C_PINK, C_NEUTRAL, C_AMBER, C_BUY, C_SELL,
    C_BLURPLE, C_GRAY, C_DARK_BLUE, C_STEEL, C_SUBTLE, C_CHART_BG,
    C_CRIMSON, C_BLACK, C_BULL, C_BEAR, C_VOLATILE, C_CATASTROPHE,
    C_RARITY_COMMON, C_RARITY_UNCOMMON, C_RARITY_RARE,
    C_RARITY_EPIC, C_RARITY_LEGENDARY, RARITY_COLORS,
    RARITY_DOT, RARITY_SQUARE, RARITY_ABBR,
    C_FARMING, C_DUNGEON, C_CRAFTING, C_FISHING, C_BUDDY,
    C_TIER_BRONZE, C_TIER_SILVER, C_TIER_GOLD,
    C_TIER_PLATINUM, C_TIER_DIAMOND, ARENA_TIER_COLORS,
)

# PoS constants  -  imported from constants/ (pure Python, no circular import)
from constants.validators import MAX_SLASH_COUNT
from constants.trading import PRICE_IMPACT_DIVISOR as _PRICE_IMPACT_DIVISOR, SLIPPAGE_WARN as _SLIPPAGE_WARN
from core.framework.scale import to_human as _to_human


# ══════════════════════════════════════════════════════════════════════════════
# Slippage preview + warning tiers  -  shared by cogs/trade.py and cogs/crypto.py
# so buy / sell / swap confirmations all surface the same numbers.
# ══════════════════════════════════════════════════════════════════════════════

# Escalates a confirm card from amber to orange when impact is this high.
_SLIPPAGE_TIER_NOTABLE: float = 0.02


def estimate_cefi_impact(
    notional_usd: float,
    cur_price: float,
    circ_supply: float,
    *,
    is_sell: bool,
) -> float:
    """Preview the price impact a CeFi buy/sell will incur.

    Mirrors the impact formula in the execute path so confirmation embeds
    show the same slippage the trade will actually realise.  Returns the
    fractional impact (``0.05`` == ``5%``).
    """
    if not math.isfinite(notional_usd) or notional_usd <= 0 or cur_price <= 0:
        return 0.0
    impact = notional_usd / _PRICE_IMPACT_DIVISOR
    mkt_cap = cur_price * max(0.0, circ_supply)
    if mkt_cap > 0 and notional_usd > 0.001 * mkt_cap:
        mc_ratio = notional_usd / mkt_cap
        mc_multiplier = min(1.0 + mc_ratio * 2.0, 5.0)
        impact *= mc_multiplier
    if is_sell:
        impact = min(impact, 0.95)
    return impact


def slippage_banner(impact: float) -> tuple[str, int | None]:
    """Return ``(banner_markdown, color_override)`` for a given impact fraction.

    The banner is prepended to a confirmation description so the user sees
    the slippage BEFORE clicking confirm.  ``color_override`` replaces the
    default ``C_AMBER`` on the confirm card; ``None`` means keep the default.
    """
    if impact >= _SLIPPAGE_WARN:
        return (
            f"🚨 **HIGH SLIPPAGE: `-{impact*100:.2f}%`**\n"
            f"This trade size will move the price significantly. You'll "
            f"receive noticeably less than the spot quote. Consider splitting "
            f"into smaller trades.\n\n",
            C_ERROR,
        )
    if impact >= _SLIPPAGE_TIER_NOTABLE:
        return (
            f"⚠️ **Notable price impact: `-{impact*100:.2f}%`** - your fill "
            f"will be worse than the spot price shown.\n\n",
            C_WARNING,
        )
    return ("", None)


# ══════════════════════════════════════════════════════════════════════════════
# FormatKit
# ══════════════════════════════════════════════════════════════════════════════

class FormatKit:
    """
    Static formatting helpers for uniform display across all embeds.

    Usage::

        FormatKit.usd(1234.5)          → "$1,234.50"
        FormatKit.token(1.5, "ARC")    → "1.5000 ARC"
        FormatKit.delta(-50, "USD")    → "-$50.00"
        FormatKit.pct(0.045)           → "4.50%"
        FormatKit.bar(3, 10)           → "███░░░░░░░ 30%"
        FormatKit.sparkline([1,3,5,2,8])  → "▁▃▅▂█"
    """

    @staticmethod
    def usd(amount: float, sign: bool = False) -> str:
        """Format a USD amount: "$1,234.56" or "+$1,234.56" with sign=True."""
        if sign and amount > 0:
            return f"+${amount:,.2f}"
        if amount < 0:
            return f"-${abs(amount):,.2f}"
        return f"${amount:,.2f}"

    @staticmethod
    def token(amount: float, symbol: str, sign: bool = False, decimals: int | None = None) -> str:
        """Format a token amount: "1.2345 ARC"."""
        d = decimals if decimals is not None else (2 if symbol == "USD" else 4)
        prefix = "+" if sign and amount > 0 else ""
        return f"{prefix}{amount:,.{d}f} {symbol}"

    @staticmethod
    def pct(rate: float, decimals: int = 2) -> str:
        """Format a rate as a percentage: "4.50%"."""
        return f"{rate * 100:.{decimals}f}%"

    @staticmethod
    def delta(amount: float, symbol: str = "USD") -> str:
        """Format a signed change: "+$50.00" or "-0.5000 ARC"."""
        if symbol == "USD":
            sign = "+" if amount >= 0 else "-"
            return f"{sign}${abs(amount):,.2f}"
        sign = "+" if amount >= 0 else ""
        return f"{sign}{amount:,.4f} {symbol}"

    @staticmethod
    def bar(value: float, max_val: float, width: int = 10,
            fill: str = "█", empty: str = "░", show_pct: bool = True) -> str:
        """Render a text progress bar: "████░░░░░░ 40%"."""
        if max_val <= 0:
            bar = empty * width
            return f"{bar}  - " if show_pct else bar
        ratio = min(max(value / max_val, 0.0), 1.0)
        filled = round(ratio * width)
        bar = fill * filled + empty * (width - filled)
        return f"{bar} {ratio * 100:.0f}%" if show_pct else bar

    @staticmethod
    def stat_row(value: float, max_val: float, label: str, *,
                 width: int = 8, suffix: str = "",
                 count_width: int = 5) -> str:
        """Render a stat row in the polished panel style:
        '`9 / 15` ▓▓▓▓░░░░ Fishing Rod (+13)`.

        Stacks neatly when several rows are placed in the same field
        because the leading count is wrapped in a fixed-width code span
        and the bar is the same length on every row.
        """
        cnt = f"{int(value):>{count_width}} / {int(max_val):<{count_width}}"
        bar = FormatKit.bar(value, max_val, width=width, show_pct=False)
        tail = f"  {suffix}" if suffix else ""
        return f"`{cnt}` {bar}  {label}{tail}"

    @staticmethod
    def time_ago(seconds: int) -> str:
        """Humanize an elapsed time: "5m ago", "2h ago"."""
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    @staticmethod
    def short_hash(tx_hash: str, prefix_len: int = 8, suffix_len: int = 6) -> str:
        """Shorten a tx hash for display: "abc12345…xyz789"."""
        if len(tx_hash) <= prefix_len + suffix_len + 1:
            return tx_hash
        return f"{tx_hash[:prefix_len]}…{tx_hash[-suffix_len:]}"

    @staticmethod
    def gas(fee: float, coin: str, emoji: str = "") -> str:
        """Format a gas fee: "0.00000150 ⛽ARC" (8 decimal places)."""
        if fee <= 0:
            return " - "
        label = f"{emoji}{coin}".strip()
        return f"{fee:.8f} {label}".strip()

    @staticmethod
    def mkt_cap(price: float, circulating: float) -> str:
        """Format a market cap value: "$1.23M" or "$45.67K"."""
        cap = price * circulating
        if cap <= 0:
            return " - "
        if cap >= 1_000_000_000:
            return f"${cap / 1_000_000_000:.2f}B"
        if cap >= 1_000_000:
            return f"${cap / 1_000_000:.2f}M"
        if cap >= 1_000:
            return f"${cap / 1_000:.2f}K"
        return f"${cap:,.2f}"

    @staticmethod
    def sparkline(values: list[float], *, lo: float | None = None,
                  hi: float | None = None) -> str:
        """Render a Unicode-block sparkline for a series of numeric samples.

        Each sample maps to one of eight increasing block heights so a glance
        at the string communicates the trajectory of the series. The bounds
        default to the data's own min/max but can be pinned by callers (e.g.
        latency = 0..max(samples) so a flat-good run reads as flat-low).
        """
        if not values:
            return ""
        blocks = "▁▂▃▄▅▆▇█"
        vmin = lo if lo is not None else min(values)
        vmax = hi if hi is not None else max(values)
        if vmax <= vmin:
            return blocks[0] * len(values)
        span = vmax - vmin
        out = []
        for v in values:
            ratio = (v - vmin) / span
            ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
            idx = min(len(blocks) - 1, int(ratio * (len(blocks) - 1) + 0.5))
            out.append(blocks[idx])
        return "".join(out)


# ══════════════════════════════════════════════════════════════════════════════
# Module-level convenience aliases (C3 standard interface)
# ══════════════════════════════════════════════════════════════════════════════
# Use these in all cog embed builders for uniformity:
#   fmt_token(1.5, "ARC")            → "1.5000 ARC"
#   fmt_usd(1234.5)                  → "$1,234.50"
#   fmt_pct(0.045)                   → "+4.50%"  (sign always shown)
#   fmt_gas(0.00000150, "ARC", "⛽") → "0.00000150 ⛽ARC"

def fmt_token(amount: float, symbol: str, emoji: str = "") -> str:
    prefix = f"{emoji}" if emoji else ""
    if symbol == "USD":
        d = 2
    else:
        # Use 6 decimals normally; bump to 8 when the 6-decimal representation
        # would show all zeros (e.g. MTA during warmup or very small rewards).
        d = 6
        if amount != 0 and abs(amount) < 0.5e-6:
            d = 8
    return f"{prefix}{amount:,.{d}f} {symbol}".strip()

def fmt_usd(amount: float) -> str:
    return FormatKit.usd(amount)

def fmt_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"

def fmt_gas(fee: float, coin: str, emoji: str = "") -> str:
    return FormatKit.gas(fee, coin, emoji)

def fmt_ts(ts, fmt: str = "%m/%d %H:%M") -> str:
    """Format a DB timestamp (epoch float or datetime) to a human string."""
    if ts is None:
        return "N/A"
    if isinstance(ts, (int, float)):
        try:
            return _dt.datetime.utcfromtimestamp(ts).strftime(fmt)
        except (OSError, ValueError, OverflowError):
            return str(ts)[:16]
    if hasattr(ts, "strftime"):
        return ts.strftime(fmt)
    return str(ts)[:16]


def _ts_to_epoch(ts) -> int | None:
    """Coerce a DB timestamp (epoch float, int, or datetime) to a unix epoch int."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return int(ts)
        except (OSError, ValueError, OverflowError):
            return None
    if hasattr(ts, "timestamp"):
        try:
            return int(ts.timestamp())
        except (OSError, ValueError, OverflowError):
            return None
    return None


def fmt_rel(ts, *, style: str = "R", fallback: str = "N/A") -> str:
    """Render a DB timestamp as Discord's auto-updating timestamp markup.

    ``style`` matches Discord's ``<t:EPOCH:STYLE>`` suffixes:
      * ``R`` -- relative ("in 3 hours" / "5 minutes ago"). Default.
      * ``F`` -- long date+time ("Wednesday, December 25, 2024 14:30").
      * ``f`` -- short date+time ("December 25, 2024 14:30").
      * ``D`` / ``d`` -- long / short date.
      * ``T`` / ``t`` -- long / short time.

    Use this instead of inline ``f"<t:{int(ts)}:R>"`` so all relative-time
    rendering shares a single code path (and gracefully degrades to ``fmt_ts``
    when no epoch can be coerced).
    """
    epoch = _ts_to_epoch(ts)
    if epoch is None:
        if ts is None:
            return fallback
        # Last-ditch: fall back to absolute formatting via fmt_ts so the
        # caller never gets a broken `<t:None:R>` token in user-facing text.
        return fmt_ts(ts)
    return f"<t:{epoch}:{style}>"


def fmt_bottleneck(result) -> str:
    """One-line footer for any embed that ran a credit through the bottleneck.

    ``result`` is a :class:`services.bottleneck.BottleneckResult`. The output
    is a plain ASCII string (no em / en dashes) like::

        Bottleneck: 0.55x (top 10%) - $12.34 to community pool
        Bottleneck: 1.20x (bottom 25%) - +$2.40 from community pool
        Bottleneck: 1.00x (median) - no effect

    The skipped case (small guild, system-disabled, gross<=0, etc.) returns
    an empty string so the caller can drop it through ``set_tx``'s
    ``footer_extra`` without an awkward "Bottleneck: ..." stub.
    """
    if result is None or getattr(result, "skipped", True):
        return ""
    try:
        from services.bottleneck import percentile_label
        label = percentile_label(float(result.percentile))
    except Exception:
        label = "median"
    mult = float(result.multiplier)
    drag_usd = float(result.drag_usd_raw) / 1e18
    boost_usd = float(result.boost_wallet_raw) / 1e18
    if drag_usd > 0:
        return f"Bottleneck: x{mult:.2f} ({label}) - ${drag_usd:,.2f} to community pool"
    if boost_usd > 0:
        return f"Bottleneck: x{mult:.2f} ({label}) - +${boost_usd:,.2f} from community pool"
    return f"Bottleneck: x{mult:.2f} ({label}) - no effect"


def fmt_bonus(base_value: str, bonus_pct: float, label: str = "") -> str:
    """Format a value with a stone bonus indicator if bonus > 0.

    Examples:
        fmt_bonus("1,200 MH/s", 0.13)           → "1,200 MH/s  💎+13%"
        fmt_bonus("$5.23/day", 0.05, "Lockstone") → "$5.23/day  🔒+5%"
        fmt_bonus("1,200 MH/s", 0.0)             → "1,200 MH/s"
    """
    if bonus_pct <= 0:
        return base_value
    pct = f"{bonus_pct * 100:.0f}"
    return f"{base_value}  💎+{pct}%"


def mention(user_id: int, guild: discord.Guild | None = None, bot: discord.Client | None = None) -> str:
    """Resolve a user ID to a **display name** for use in embeds.

    Centralised helper  -  use this instead of f"<@{uid}>" or raw IDs so the
    display stays human-readable and never regresses to numeric IDs.

    Lookup order: guild.get_member → bot.get_user → fallback "Unknown User".
    """
    if guild:
        m = guild.get_member(user_id)
        if m:
            return f"**@{m.display_name}**"
    if bot:
        u = bot.get_user(user_id)
        if u:
            return f"**@{u.display_name}**"
    return f"**@Unknown User**"


# ══════════════════════════════════════════════════════════════════════════════
# ConfirmView
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    """
    Author-locked yes/no confirmation dialog.

    Usage::

        view = ConfirmView(ctx.author.id)
        msg  = await ctx.reply("Are you sure?", view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if confirmed:
            ...
    """

    def __init__(self, author_id: int, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self.result: bool | None = None
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This confirmation isn't for you.", ephemeral=True
            )
            return False
        return True

    async def wait_result(self) -> bool | None:
        """Wait for a button press and return True/False/None (timeout)."""
        await self._event.wait()
        return self.result

    def _resolve(self, value: bool) -> None:
        self.result = value
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self._event.set()
        self.stop()

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._resolve(True)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._resolve(False)
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self._event.set()


# ══════════════════════════════════════════════════════════════════════════════
# Paginator   ⏮ ◀ [X / N] ▶ ⏭
# ══════════════════════════════════════════════════════════════════════════════

class Paginator(discord.ui.View):
    """
    Multi-page embed navigator with full navigation controls.

    Controls (single row):  ⏮  ◀  [Page X / N]  ▶  ⏭

    - ⏮ / ⏭ jump to first / last page
    - ◀ / ▶ step one page
    - [Page X / N] disabled label showing current position

    Usage::

        pages = [embed1, embed2, embed3]
        await Paginator.send(ctx, pages)
    """

    def __init__(
        self,
        pages: list[discord.Embed],
        author_id: int,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._pages = pages
        self._author_id = author_id
        self._index = 0
        self._build()

    # ── Class-level send helper ────────────────────────────────────────────

    @classmethod
    async def send(
        cls,
        ctx: "DiscoContext",
        pages: list[discord.Embed],
        timeout: float = 120.0,
    ) -> None:
        if not pages:
            await ctx.reply_error("No data to display.")
            return
        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
            return
        view = cls(pages, ctx.author.id, timeout=timeout)
        view._stamp_footer()
        await ctx.reply(embed=pages[0], view=view, mention_author=False)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _stamp_footer(self) -> None:
        total = len(self._pages)
        for i, embed in enumerate(self._pages):
            existing = embed.footer.text or ""
            page_str = f"Page {i + 1} / {total}"
            if existing:
                embed.set_footer(text=f"{existing}  ·  {page_str}")
            else:
                embed.set_footer(text=page_str)

    def _build(self) -> None:
        """Rebuild nav buttons to reflect current index."""
        self.clear_items()
        total = len(self._pages)
        idx   = self._index

        first = discord.ui.Button(
            label="⏮", style=discord.ButtonStyle.secondary,
            disabled=(idx == 0), row=0,
        )
        first.callback = self._on_first
        self.add_item(first)

        prev = discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary,
            disabled=(idx == 0), row=0,
        )
        prev.callback = self._on_prev
        self.add_item(prev)

        counter = discord.ui.Button(
            label=f"{idx + 1} / {total}",
            style=discord.ButtonStyle.secondary,
            disabled=True, row=0,
        )
        self.add_item(counter)

        nxt = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary,
            disabled=(idx >= total - 1), row=0,
        )
        nxt.callback = self._on_next
        self.add_item(nxt)

        last = discord.ui.Button(
            label="⏭", style=discord.ButtonStyle.secondary,
            disabled=(idx >= total - 1), row=0,
        )
        last.callback = self._on_last
        self.add_item(last)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This paginator isn't for you.", ephemeral=True
            )
            return False
        return True

    async def _go(self, interaction: discord.Interaction, new_index: int) -> None:
        self._index = new_index
        self._build()
        await interaction.response.edit_message(
            embed=self._pages[self._index], view=self
        )

    async def _on_first(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, 0)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, max(0, self._index - 1))

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, min(len(self._pages) - 1, self._index + 1))

    async def _on_last(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, len(self._pages) - 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════════
# CategoryPaginator
# ══════════════════════════════════════════════════════════════════════════════

class CategoryPaginator(discord.ui.View):
    """
    Category-based paginator with a Select menu and per-category page nav.

    Layout:
      Row 0 ── [📂 Select a category ▼]  (all category labels)
      Row 1 ── [◀]  [Page X / N]  [▶]   (hidden if category has 1 page)

    The embed footer shows: "📂 Category Name  ·  Page X / N  ·  <tx info if any>"

    Usage::

        cats = {
            "💰 Economy": [embed1, embed2],
            "⛏️ Mining":  [embed3],
            "📊 Stats":   [embed4, embed5, embed6],
        }
        await CategoryPaginator.send(ctx, cats)
    """

    def __init__(
        self,
        categories: dict[str, list[discord.Embed]],
        author_id: int,
        timeout: float = 180.0,
        *,
        action_hints: dict[str, list[tuple]] | None = None,
        ctx: "DiscoContext | None" = None,
    ) -> None:
        """
        action_hints maps category label → list of tuples:
          (button_label, emoji, hint_text)             -  shows hint_text as ephemeral message
          (button_label, emoji, hint_text, cmd_name)   -  invokes cmd_name as a command
        """
        super().__init__(timeout=timeout)
        self._categories = categories
        self._author_id  = author_id
        self._cat_keys   = list(categories.keys())
        self._current    = self._cat_keys[0]
        self._page       = 0
        self._cat_offset = 0  # which 25-category window is shown in the select
        self._action_hints: dict[str, list[tuple]] = action_hints or {}
        self._ctx = ctx
        # Snapshot each embed's original footer so we can append page info cleanly
        self._orig_footer: dict[int, str] = {
            id(p): (p.footer.text or "")
            for pages in categories.values()
            for p in pages
        }
        self._build()

    # ── Class-level send helper ────────────────────────────────────────────

    @classmethod
    async def send(
        cls,
        ctx: "DiscoContext",
        categories: dict[str, list[discord.Embed]],
        timeout: float = 180.0,
        *,
        action_hints: dict[str, list[tuple]] | None = None,
    ) -> None:
        """Send the CategoryPaginator, collapsing to a bare embed when trivial."""
        if not categories:
            await ctx.reply_error("No data to display.")
            return
        all_pages = [p for pages in categories.values() for p in pages]
        if len(all_pages) == 1:
            await ctx.reply(embed=all_pages[0], mention_author=False)
            return
        view = cls(categories, ctx.author.id, timeout=timeout, action_hints=action_hints, ctx=ctx)
        embed = view._current_embed()
        view._stamp_footer(embed)
        await ctx.reply(embed=embed, view=view, mention_author=False)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _current_embed(self) -> discord.Embed:
        pages = self._categories[self._current]
        return pages[min(self._page, len(pages) - 1)]

    def _total_pages(self) -> int:
        return len(self._categories[self._current])

    def _stamp_footer(self, embed: discord.Embed) -> None:
        """Append category + page info to the embed's original footer."""
        total    = self._total_pages()
        base     = self._orig_footer.get(id(embed), "")
        parts: list[str] = [f"📂 {self._current}"]
        if total > 1:
            parts.append(f"Page {self._page + 1} / {total}")
        if base:
            parts.append(base)
        embed.set_footer(text="  ·  ".join(parts))

    def _build(self) -> None:
        """Rebuild all child items to reflect current state."""
        self.clear_items()

        # ── Row 0: Category Select (max 25 options; windowed if more) ─────
        _win_end = min(self._cat_offset + 25, len(self._cat_keys))
        _visible = self._cat_keys[self._cat_offset:_win_end]
        options = [
            discord.SelectOption(
                label=key,
                value=key,
                default=(key == self._current),
            )
            for key in _visible
        ]
        sel = discord.ui.Select(
            placeholder=f"📂 {self._current}",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        sel.callback = self._on_select
        self._sel = sel
        self.add_item(sel)

        # ── Row 1: Page navigation (only if >1 page in current category) ──
        total = self._total_pages()
        if total > 1:
            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page == 0),
                row=1,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)

            counter = discord.ui.Button(
                label=f"{self._page + 1} / {total}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            )
            self.add_item(counter)

            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page >= total - 1),
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        # ── Row 2: Category-specific action hint buttons ──────────────────
        hints = self._action_hints.get(self._current, [])
        # ── Row 3: Category-set navigation (only when >25 categories) ─────
        if len(self._cat_keys) > 25:
            _n_windows = math.ceil(len(self._cat_keys) / 25)
            _cur_win   = self._cat_offset // 25 + 1
            cat_prev = discord.ui.Button(
                label="< Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self._cat_offset == 0),
                row=3,
            )
            cat_prev.callback = self._on_cat_prev
            self.add_item(cat_prev)
            cat_counter = discord.ui.Button(
                label=f"Pg {_cur_win}/{_n_windows}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=3,
            )
            self.add_item(cat_counter)
            cat_next = discord.ui.Button(
                label="Next >",
                style=discord.ButtonStyle.secondary,
                disabled=(_win_end >= len(self._cat_keys)),
                row=3,
            )
            cat_next.callback = self._on_cat_next
            self.add_item(cat_next)
        for hint in hints[:5]:  # max 5 per Discord row
            label, emoji, hint_text = hint[0], hint[1], hint[2]
            cmd_name = hint[3] if len(hint) > 3 else None
            btn = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary if cmd_name else discord.ButtonStyle.primary,
                row=2,
            )

            if cmd_name and self._ctx:
                async def _cmd_callback(
                    interaction: discord.Interaction,
                    _cmd=cmd_name,
                    _ctx=self._ctx,
                ) -> None:
                    await interaction.response.defer()
                    cmd = interaction.client.get_command(_cmd)
                    if cmd:
                        await _ctx.invoke(cmd)
                    else:
                        await interaction.followup.send(f"Command `{_cmd}` not found.", ephemeral=True)

                btn.callback = _cmd_callback
            else:
                async def _hint_callback(interaction: discord.Interaction, _hint=hint_text) -> None:
                    await interaction.response.send_message(_hint, ephemeral=True)

                btn.callback = _hint_callback
            self.add_item(btn)

    # ── Interaction check ──────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    # ── Callbacks ──────────────────────────────────────────────────────────

    async def _render(self, interaction: discord.Interaction) -> None:
        self._build()
        embed = self._current_embed()
        self._stamp_footer(embed)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._current = self._sel.values[0]
        self._page    = 0
        await self._render(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        await self._render(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._total_pages() - 1, self._page + 1)
        await self._render(interaction)

    async def _on_cat_prev(self, interaction: discord.Interaction) -> None:
        self._cat_offset = max(0, self._cat_offset - 25)
        self._current = self._cat_keys[self._cat_offset]
        self._page = 0
        await self._render(interaction)

    async def _on_cat_next(self, interaction: discord.Interaction) -> None:
        self._cat_offset = min(
            (math.ceil(len(self._cat_keys) / 25) - 1) * 25,
            self._cat_offset + 25,
        )
        self._current = self._cat_keys[self._cat_offset]
        self._page = 0
        await self._render(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# InputModal / AmountModal
# ══════════════════════════════════════════════════════════════════════════════

class InputModal(discord.ui.Modal):
    """
    Single-field text input modal.

    Usage::

        modal = InputModal(title="Enter Name", label="Name", placeholder="e.g. Satoshi")
        await interaction.response.send_modal(modal)
        await modal.wait()
        name = modal.value
    """

    def __init__(
        self,
        *,
        title: str,
        label: str = "Input",
        placeholder: str = "",
        required: bool = True,
        max_length: int = 100,
        default: str = "",
    ) -> None:
        super().__init__(title=title)
        self.value: str | None = None
        self._input = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            required=required,
            max_length=max_length,
            default=default,
        )
        self.add_item(self._input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.value = self._input.value
        await interaction.response.defer()
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


# Convenience helpers
# ══════════════════════════════════════════════════════════════════════════════

async def send_paginated(ctx: "DiscoContext", pages: list[discord.Embed]) -> None:
    """Send a single embed or launch a full Paginator."""
    await Paginator.send(ctx, pages)



# ══════════════════════════════════════════════════════════════════════════════
# ValidatorSelectView
# ══════════════════════════════════════════════════════════════════════════════

class ValidatorSelectView(discord.ui.View):
    """
    Drill-down dropdown for .bals  -  lets users inspect a specific node,
    delegation, or their own PoS validator registration.

    Usage (in balance command)::

        view = ValidatorSelectView(
            ctx.author.id,
            stakes=stakes,
            delegations=delegations,
            pos_validators=pos_validators,
            db=ctx.db,
            guild_id=gid,
        )
        if view.has_options:
            await ctx.send(embed=overview_embed, view=view)
    """

    def __init__(
        self,
        author_id: int,
        stakes: list[dict],
        delegations: list[dict],
        pos_validators: list[dict],
        db,
        guild_id: int,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._stakes = {s["validator_id"]: s for s in stakes}
        self._delegations = {str(d["validator_user_id"]): d for d in delegations}
        self._pos = {str(p["user_id"]): p for p in pos_validators}
        self._db = db
        self._guild_id = guild_id

        options: list[discord.SelectOption] = []
        for s in stakes:
            _s_amt = _to_human(int(s["amount"] or 0))
            daily = _s_amt * s.get("reward_rate", 0.0)
            desc = f"{_s_amt:.4f} {s['symbol']} · +{daily:.6f}/day"[:100]
            options.append(discord.SelectOption(
                label=f"{s['emoji']} {s['name']}"[:100],
                value=f"node:{s['validator_id']}",
                description=desc,
            ))
        for d in delegations:
            _d_amt = _to_human(int(d["amount"] or 0))
            earned = _to_human(int(d.get("total_earned") or 0))
            desc = f"{_d_amt:.4f} {d['token']} delegated · {earned:.4f} earned"[:100]
            options.append(discord.SelectOption(
                label=f"🤝 Delegation → <@{d['validator_user_id']}>"[:100],
                value=f"del:{d['validator_user_id']}",
                description=desc,
            ))
        for p in pos_validators:
            if not p.get("is_active", False):
                continue
            net_short = p["network"].split()[0]
            desc = f"Stake: {_to_human(int(p['stake_amount'] or 0)):.4f} {p['stake_token']} · {p['network']}"[:100]
            options.append(discord.SelectOption(
                label=f"⛓️ My Validator [{net_short}]"[:100],
                value=f"pos:{p['user_id']}",
                description=desc,
            ))

        self.has_options = bool(options)
        if options:
            sel = discord.ui.Select(
                placeholder="🔍 View position details…",
                options=options[:25],
                min_values=1,
                max_values=1,
                row=0,
            )
            sel.callback = self._on_select
            self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        from core.framework.embed import card  # avoid potential circular at module level
        import time as _t
        value: str = interaction.data["values"][0]  # type: ignore[index]

        if value.startswith("node:"):
            vid = value[5:]
            s = self._stakes.get(vid)
            if not s:
                await interaction.response.send_message("Node data not found.", ephemeral=True)
                return
            price_row = await self._db.get_price(s["symbol"], self._guild_id)
            price = float(price_row["price"]) if price_row else 0.0
            _s_amt_h = _to_human(int(s["amount"] or 0))
            usd_val = _s_amt_h * price
            daily = _s_amt_h * s.get("reward_rate", 0.0)
            _sa = s.get("staked_at")
            staked_at = _sa.timestamp() if hasattr(_sa, 'timestamp') else (_sa or 0.0)
            unlock_at = staked_at + 86_400
            now = _t.time()
            if now < unlock_at:
                remaining = int(unlock_at - now)
                lock_str = f"🔒 {remaining // 3600}h {(remaining % 3600) // 60}m left"
            else:
                lock_str = "✅ Unlocked"
            slash_pct = s.get("slash_rate", 0.0) * 100
            embed = (
                card(f"{s['emoji']} {s['name']}  -  Node Detail", color=C_PURPLE)
                .field("🌐 Network",     s.get("network", " - "),                                True)
                .field("🪙 Token",       s["symbol"],                                           True)
                .field("💎 Staked",      f"**{_s_amt_h:,.6f} {s['symbol']}**",                True)
                .field("⚡ Yield / hr",  f"**+{daily/24:,.6f} {s['symbol']}**",               True)
                .field("📊 Yield / day", f"**+{daily:,.6f} {s['symbol']}**",                  True)
                .field("💵 USD Value",   f"**≈ ${usd_val:,.2f}**" if price > 0 else " - ",     True)
                .field("🔒 Lock",         lock_str,                                             True)
                .field("🛡 Slash Risk",   f"{slash_pct:.0f}% per tick",                       True)
                .build()
            )

        elif value.startswith("del:"):
            uid_str = value[4:]
            d = self._delegations.get(uid_str)
            if not d:
                await interaction.response.send_message("Delegation data not found.", ephemeral=True)
                return
            price_row = await self._db.get_price(d["token"], self._guild_id)
            price = float(price_row["price"]) if price_row else 0.0
            _d_amt_h = _to_human(int(d["amount"] or 0))
            _d_earned_h = _to_human(int(d.get("total_earned") or 0))
            usd_val = _d_amt_h * price
            earned_usd = _d_earned_h * price
            now = _t.time()
            lock_remaining = max(0, int(d.get("locked_until", 0) - now))
            lock_str = (
                f"🔒 {lock_remaining // 3600}h {(lock_remaining % 3600) // 60}m left"
                if lock_remaining > 0 else "✅ Unlocked"
            )
            embed = (
                card(f"🤝 Delegation to <@{uid_str}>", color=C_PURPLE)
                .field("🌐 Network",    d.get("network", " - "),                                         True)
                .field("🪙 Token",      d["token"],                                                     True)
                .field("💎 Delegated",  f"**{_d_amt_h:,.6f} {d['token']}**",                         True)
                .field("🏆 Earned",     f"**{_d_earned_h:,.6f} {d['token']}**",                      True)
                .field("💵 USD Value",  f"**≈ ${usd_val:,.2f}**" if price > 0 else " - ",              True)
                .field("💰 Earned USD", f"**≈ ${earned_usd:,.2f}**" if price > 0 else " - ",           True)
                .field("🔒 Lock",        lock_str,                                                      True)
                .build()
            )

        elif value.startswith("pos:"):
            uid_str = value[4:]
            p = self._pos.get(uid_str)
            if not p:
                await interaction.response.send_message("Validator data not found.", ephemeral=True)
                return
            price_row = await self._db.get_price(p["stake_token"], self._guild_id)
            price = float(price_row["price"]) if price_row else 0.0
            _p_stake_h = _to_human(int(p["stake_amount"] or 0))
            usd_val = _p_stake_h * price
            slashes = p.get("slash_count", 0)
            slash_str = f"⚠️ {slashes}/{MAX_SLASH_COUNT} slashes" if slashes > 0 else "✅ Clean record"
            status = "🟢 Active" if p.get("is_active") else "🔴 Inactive"
            dels = await self._db.get_delegations_for_validator(int(uid_str), self._guild_id, p["network"])
            del_count = len(dels)
            del_total_h = sum(_to_human(int(dd["amount"] or 0)) for dd in dels)
            embed = (
                card(f"⛓️ My Validator  -  {p['network']}", color=C_PURPLE)
                .field("🌐 Network",     p["network"],                                                              True)
                .field("🪙 Stake Token", p["stake_token"],                                                          True)
                .field("💎 Staked",      f"**{_p_stake_h:,.6f} {p['stake_token']}**",                            True)
                .field("💵 USD Value",   f"**≈ ${usd_val:,.2f}**" if price > 0 else " - ",                         True)
                .field("🏆 Blocks",      f"**{p.get('total_blocks_validated', 0):,}** confirmed",                 True)
                .field("🛡 Slashes",     slash_str,                                                                 True)
                .field("📊 Status",      status,                                                                    True)
                .field("👥 Delegators",  f"**{del_count}** · {del_total_h:,.4f} {p['stake_token']} total",       True)
                .build()
            )

        else:
            await interaction.response.send_message("Unknown selection.", ephemeral=True)
            return

        await interaction.response.edit_message(embed=embed)
