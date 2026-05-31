"""``$global`` / ``$top`` / ``$trending`` / ``$gainers`` / ``$losers``
/ ``$heatmap`` / ``$fear`` / ``$dom`` -- market-wide views.

All read-only CoinGecko queries; one function per legacy ``_handle_*``
plus a thin :func:`handle` dispatcher so the cog can use a single
import.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_BEAR, C_BULL, C_GOLD, C_INFO
from services.real_market import RealMarketError

from ._shared import (
    _FOOTER_BRAND,
    _LIVE_PREFIX,
    _fmt_big_usd,
    _fmt_price_usd,
    _fng_color,
    _fng_emoji,
    _heat_emoji,
    _pct_chip,
)

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


async def handle_global(ctx: DiscoContext, _raw: str, *, cog: "RealMarket") -> None:
    """Total-crypto-market overview."""
    async with ctx.typing():
        try:
            data = await cog.client.get_global()
        except RealMarketError as exc:
            log.warning("$global failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return

    mcap = data.get("total_market_cap_usd")
    vol  = data.get("total_volume_usd")
    change = data.get("market_cap_change_pct_24h_usd")
    dom_map = data.get("market_cap_percentage") or {}
    coins = data.get("active_cryptocurrencies") or 0
    markets = data.get("markets") or 0

    change_chip = _pct_chip(change) if change is not None else "—"
    change_emoji = "▲" if (change or 0) >= 0 else "▼"
    color = C_BULL if (change or 0) >= 0 else C_BEAR

    dom_sorted = sorted(
        ((k.upper(), float(v)) for k, v in dom_map.items() if v is not None),
        key=lambda kv: kv[1], reverse=True,
    )[:6]
    dom_lines = "\n".join(
        f"• **{sym}**  `{pct:.2f}%`" for sym, pct in dom_sorted
    ) or "—"

    embed = (
        card(
            f"{_LIVE_PREFIX} 🌐 Crypto market overview",
            description=(
                f"💰 **Total market cap:** `{_fmt_big_usd(mcap)}`  "
                f"{change_emoji} **{change_chip}** 24h\n"
                f"📦 **24h volume:** `{_fmt_big_usd(vol)}`\n"
                f"🪙 **Active coins:** `{coins:,}`  ·  "
                f"💱 **Markets:** `{markets:,}`"
            ),
            color=color,
        )
        .field("🏛️ Dominance (top by market cap share)", dom_lines, False)
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_GLOBAL}s · "
            "use $top for the leaderboard"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_top(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    """``$top [N]`` -- top N coins by market cap (default 10, max 25)."""
    tokens = raw_args.split()
    n = 10
    if tokens and tokens[0].isdigit():
        n = max(3, min(25, int(tokens[0])))
    async with ctx.typing():
        try:
            rows = await cog.client.get_markets(
                order="market_cap_desc", per_page=max(n, 10), page=1,
            )
        except RealMarketError as exc:
            log.warning("$top failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return
    rows = rows[:n]
    if not rows:
        await ctx.reply_error("CoinGecko returned no market data right now.")
        return

    lines = []
    for r in rows:
        rank = r.get("market_cap_rank") or "—"
        sym  = r.get("symbol") or "?"
        pct  = r.get("pct_24h")
        lines.append(
            f"`#{rank:<3}` {_heat_emoji(pct)} **{sym:<5}**  "
            f"{_fmt_price_usd(r.get('price')):<12}  "
            f"24h `{_pct_chip(pct)}`  ·  cap `{_fmt_big_usd(r.get('market_cap'))}`"
        )
    embed = (
        card(
            f"{_LIVE_PREFIX} 🏆 Top {len(rows)} by market cap",
            description="\n".join(lines),
            color=C_GOLD,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_MARKETS}s · "
            "tap `$info SYM` for a deep-dive"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_trending(ctx: DiscoContext, _raw: str, *, cog: "RealMarket") -> None:
    """Top trending coins on CoinGecko (most-searched in 24h)."""
    async with ctx.typing():
        try:
            trends = await cog.client.get_trending()
        except RealMarketError as exc:
            log.warning("$trending failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return
    if not trends:
        await ctx.reply_error("CoinGecko returned no trending data right now.")
        return

    lines: list[str] = []
    for i, t in enumerate(trends[:15], start=1):
        rank = t.get("market_cap_rank") or "—"
        sym  = t.get("symbol") or "?"
        name = t.get("name") or sym
        pct  = t.get("pct_24h")
        price = t.get("price_usd")
        chip = f"`#{i:<2}` 🔥 **{sym}** ({name})  ·  cap-rank `#{rank}`"
        if price is not None:
            chip += f"  ·  {_fmt_price_usd(price)}"
        if pct is not None:
            chip += f"  ·  {_heat_emoji(pct)} `{_pct_chip(pct)}` 24h"
        lines.append(chip)
    embed = (
        card(
            f"{_LIVE_PREFIX} 🔥 Trending on CoinGecko",
            description="\n".join(lines),
            color=C_GOLD,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_TRENDING}s · "
            "most-searched coins in the last 24h"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_movers(
    ctx: DiscoContext, raw_args: str, *, direction: str, cog: "RealMarket",
) -> None:
    """Top 24h gainers/losers inside the top-250 by market cap."""
    tokens = raw_args.split()
    n = 10
    if tokens and tokens[0].isdigit():
        n = max(3, min(25, int(tokens[0])))
    async with ctx.typing():
        try:
            rows = await cog.client.get_markets(
                order="market_cap_desc", per_page=250, page=1,
            )
        except RealMarketError as exc:
            log.warning("$%s failed: %s", direction, exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return
    rows = [r for r in rows if r.get("pct_24h") is not None]
    reverse = (direction == "gainers")
    rows.sort(key=lambda r: float(r["pct_24h"] or 0.0), reverse=reverse)
    rows = rows[:n]
    if not rows:
        await ctx.reply_error("No 24h movers available right now.")
        return

    title_emoji = "🚀" if direction == "gainers" else "💀"
    title_word  = "gainers" if direction == "gainers" else "losers"
    color = C_BULL if direction == "gainers" else C_BEAR

    lines = []
    for r in rows:
        rank = r.get("market_cap_rank") or "—"
        sym  = r.get("symbol") or "?"
        pct  = r.get("pct_24h")
        lines.append(
            f"`#{rank:<3}` {_heat_emoji(pct)} **{sym:<5}**  "
            f"{_fmt_price_usd(r.get('price')):<12}  "
            f"24h `{_pct_chip(pct)}`  ·  vol `{_fmt_big_usd(r.get('total_volume'))}`"
        )
    embed = (
        card(
            f"{_LIVE_PREFIX} {title_emoji} Top {len(rows)} 24h {title_word}",
            description="\n".join(lines),
            color=color,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_MARKETS}s · "
            "sourced from the top-250 by market cap"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_heatmap(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    """Colour-coded grid of top N coins by 24h change."""
    tokens = raw_args.split()
    n = 25
    if tokens and tokens[0].isdigit():
        n = max(8, min(50, int(tokens[0])))
    async with ctx.typing():
        try:
            rows = await cog.client.get_markets(
                order="market_cap_desc", per_page=max(n, 25), page=1,
            )
        except RealMarketError as exc:
            log.warning("$heatmap failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return
    rows = rows[:n]
    if not rows:
        await ctx.reply_error("CoinGecko returned no market data right now.")
        return

    def cell(r: dict) -> str:
        sym = (r.get("symbol") or "?")[:5]
        pct = r.get("pct_24h")
        try:
            v = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        pct_str = f"{v:+6.2f}%"
        return f"{_heat_emoji(pct)} {sym:<5} {pct_str}"

    chunks: list[str] = []
    for i in range(0, len(rows), 2):
        left = cell(rows[i])
        right = cell(rows[i + 1]) if (i + 1) < len(rows) else ""
        chunks.append(f"{left:<24}{right}")
    grid = "```\n" + "\n".join(chunks) + "\n```"

    pcts = [float(r["pct_24h"]) for r in rows if r.get("pct_24h") is not None]
    ups = sum(1 for v in pcts if v > 0)
    downs = sum(1 for v in pcts if v < 0)
    avg  = sum(pcts) / len(pcts) if pcts else 0.0
    color = C_BULL if avg >= 0 else C_BEAR

    embed = (
        card(
            f"{_LIVE_PREFIX} 🗺️ Market heatmap · top {len(rows)} by cap",
            description=(
                f"🟢 **Up:** {ups}  ·  🔴 **Down:** {downs}  ·  "
                f"📊 **Avg 24h:** `{_pct_chip(avg)}`\n"
                f"{grid}"
            ),
            color=color,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_MARKETS}s · "
            "🟩 ≥+5%  🟢 >0%  ⚫ flat  🔴 <0%  🟥 ≤-5%"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_fear_greed(ctx: DiscoContext, *, cog: "RealMarket") -> None:
    """Crypto Fear & Greed Index (alternative.me)."""
    async with ctx.typing():
        data = await cog.client.get_fear_greed()
    if not data:
        await ctx.reply_error(
            "Fear & Greed Index is temporarily unavailable. Try again later."
        )
        return

    value = int(data.get("value") or 0)
    klass = data.get("classification") or "—"
    emoji = _fng_emoji(value)
    color = _fng_color(value)
    ticks = 20
    filled = max(0, min(ticks, round(value / 100 * ticks)))
    bar = "█" * filled + "░" * (ticks - filled)

    def _delta_line(label: str, prior: int | None) -> str | None:
        if prior is None:
            return None
        d = value - prior
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
        return f"• {label}: `{prior}` ({arrow} {d:+d})"

    compare_lines: list[str] = []
    for label, key in (("Yesterday", "yesterday_value"),
                       ("Week ago",  "week_ago_value"),
                       ("Month ago", "month_ago_value")):
        line = _delta_line(label, data.get(key))
        if line:
            compare_lines.append(line)

    embed = (
        card(
            f"{_LIVE_PREFIX} {emoji} Fear & Greed Index",
            description=(
                f"## `{value} / 100`  ·  **{klass}**\n"
                f"`{bar}`\n"
                "*0 = Extreme Fear · 50 = Neutral · 100 = Extreme Greed*"
            ),
            color=color,
        )
        .field_if(
            bool(compare_lines), "📅 Historical comparison",
            "\n".join(compare_lines), False,
        )
        .footer(
            f"📡 Live · alternative.me · cached "
            f"{Config.REAL_MARKET_CACHE_TTL_FNG}s"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle_dominance(ctx: DiscoContext, *, cog: "RealMarket") -> None:
    """Market-cap dominance breakdown -- MTA, ARC, stables, and the rest."""
    async with ctx.typing():
        try:
            data = await cog.client.get_global()
        except RealMarketError as exc:
            log.warning("$dom failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return
    dom_map = data.get("market_cap_percentage") or {}
    if not dom_map:
        await ctx.reply_error("Dominance data is temporarily unavailable.")
        return
    rows = sorted(
        ((k.upper(), float(v)) for k, v in dom_map.items() if v is not None),
        key=lambda kv: kv[1], reverse=True,
    )[:10]
    other = max(0.0, 100.0 - sum(v for _, v in rows))

    ticks = 24

    def _bar(pct: float) -> str:
        filled = max(0, min(ticks, round(pct / 100 * ticks)))
        return "█" * filled + "░" * (ticks - filled)

    lines = [
        f"**{sym:<5}** `{_bar(pct)}` `{pct:5.2f}%`"
        for sym, pct in rows
    ]
    if other > 0:
        lines.append(f"**OTHER** `{_bar(other)}` `{other:5.2f}%`")

    mcap = data.get("total_market_cap_usd")
    embed = (
        card(
            f"{_LIVE_PREFIX} 🏛️ Market-cap dominance",
            description=(
                f"💰 **Total market cap:** `{_fmt_big_usd(mcap)}`\n\n"
                + "\n".join(lines)
            ),
            color=C_INFO,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_GLOBAL}s · "
            "high MTA dom = risk-off, low MTA dom = alt season"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)
