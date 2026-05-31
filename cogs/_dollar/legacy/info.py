"""``$info SYMBOL`` -- crypto snapshot embed.

Non-crypto assets are handled by :mod:`cogs._dollar.info_handler`; this
module only renders the CoinGecko-backed crypto leg.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.config import Config
from core.framework.chart import _aggregate
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD, fmt_ts
from services.real_market import RealMarketError

from ._shared import (
    _FOOTER_BRAND,
    _LIVE_PREFIX,
    _NEWS_FIELD_CHAR_CAP,
    _TF_SECONDS,
    _fmt_big_usd,
    _fmt_price_usd,
    _fmt_supply,
    _parse_iso_to_epoch,
    _pct_chip,
    _summarize_indicators,
)

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


def _format_whale_field(
    tickers: list[dict],
    *,
    total_vol_usd: float | None = None,
    market_cap_usd: float | None = None,
) -> str:
    """Top exchange venues moving USD in/out of this asset over 24h.

    CoinGecko's free tier doesn't expose per-coin holder distributions,
    so the largest CEX/DEX venues are the cleanest free-tier proxy for
    "where the whales are trading". Adds a turnover chip at the bottom.
    """
    if not tickers:
        return "Exchange-venue data unavailable."
    try:
        tot = float(total_vol_usd or 0.0)
    except (TypeError, ValueError):
        tot = 0.0
    lines: list[str] = []
    for t in tickers:
        vol = float(t.get("volume_usd") or 0.0)
        share = (vol / tot * 100.0) if tot > 0 else None
        pair = f"{t.get('base', '')}/{t.get('target', '')}".strip("/")
        chip = f"💱 **{t['exchange']}**"
        if pair:
            chip += f" `{pair}`"
        chip += f"  ·  {_fmt_big_usd(vol)}"
        if share is not None:
            chip += f"  ·  {share:.1f}% of 24h vol"
        lines.append(chip)
    try:
        mcap = float(market_cap_usd or 0.0)
    except (TypeError, ValueError):
        mcap = 0.0
    if tot > 0 and mcap > 0:
        turnover = tot / mcap * 100.0
        descriptor = (
            "🌶️ high turnover" if turnover >= 25 else
            "🔥 active" if turnover >= 10 else
            "🟢 healthy" if turnover >= 3 else
            "🟡 quiet"
        )
        lines.append(
            f"\n📊 **24h turnover:** `{turnover:.2f}%` of market cap "
            f"({_fmt_big_usd(tot)} traded vs {_fmt_big_usd(mcap)} cap)  ·  "
            f"{descriptor}"
        )
    return "\n".join(lines)


async def handle(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    tokens = raw_args.split()
    if not tokens:
        await ctx.reply_error_hint(
            "You must give a symbol.",
            hint="Try `$info MTA` or `$info ARC`.",
            command_name="$info",
        )
        return
    symbol = tokens[0]

    record = await cog._resolve_or_error(ctx, symbol, command_name="$info")
    if not record:
        return

    # Non-crypto assets get the router-aware info panel.
    if record.get("_asset_class") and record["_asset_class"] != "crypto":
        try:
            from cogs._dollar.info_handler import handle_info_router
            await handle_info_router(ctx, record["_resolved"])
        except Exception:
            log.exception("[$info router] non-crypto handler crashed")
            await ctx.reply_error(
                f"Couldn't load info for {record['symbol']}.",
            )
        return

    async with ctx.typing():
        try:
            overview = await cog.client.get_overview(record["id"])
        except RealMarketError as exc:
            log.warning("$info overview failed for %s: %s", record["id"], exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return

        try:
            indi_candles = await cog.client.get_ohlc(record["id"], "4h")
            agg = _aggregate(indi_candles, _TF_SECONDS["4h"]) or indi_candles
            indicators_block, indicators_short = _summarize_indicators(agg)
        except RealMarketError:
            indicators_block = "Indicator data temporarily unavailable."
            indicators_short = "n/a"

        try:
            news = await cog.client.get_news(
                overview.get("name", record["symbol"]),
                record["symbol"],
                limit=3,
            )
        except RealMarketError:
            news = []

        top_tickers = await cog.client.get_top_tickers(record["id"], limit=5)

    md = overview.get("market_data", {})
    price = (md.get("current_price") or {}).get("usd")
    h24   = (md.get("high_24h") or {}).get("usd")
    l24   = (md.get("low_24h")  or {}).get("usd")
    vol   = (md.get("total_volume") or {}).get("usd")
    mcap  = (md.get("market_cap") or {}).get("usd")
    fdv   = (md.get("fully_diluted_valuation") or {}).get("usd")
    circ  = md.get("circulating_supply")
    total = md.get("total_supply")
    max_s = md.get("max_supply")
    ath   = (md.get("ath") or {}).get("usd")
    athd  = _parse_iso_to_epoch((md.get("ath_date") or {}).get("usd"))
    atl   = (md.get("atl") or {}).get("usd")
    atld  = _parse_iso_to_epoch((md.get("atl_date") or {}).get("usd"))

    p1h = (md.get("price_change_percentage_1h_in_currency")  or {}).get("usd")
    p24 = (md.get("price_change_percentage_24h_in_currency") or {}).get("usd")
    p7d = (md.get("price_change_percentage_7d_in_currency")  or {}).get("usd")
    p30 = (md.get("price_change_percentage_30d_in_currency") or {}).get("usd")
    rank = overview.get("market_cap_rank") or "—"

    news_lines: list[str] = []
    if news:
        for item in news:
            src = item.get("source") or ""
            title = item.get("title") or ""
            url = item.get("url") or ""
            src_suffix = f" -- {src}" if src else ""
            line = f"• [{title}]({url}){src_suffix}"
            if len(line) > 340:
                line = line[:337] + "…"
            if sum(len(x) for x in news_lines) + len(line) + 2 > _NEWS_FIELD_CHAR_CAP:
                break
            news_lines.append(line)
    news_value = "\n".join(news_lines) if news_lines else "No recent headlines."

    whale_value = _format_whale_field(
        top_tickers, total_vol_usd=vol, market_cap_usd=mcap,
    )

    ath_chip = "—"
    if ath is not None:
        ath_chip = _fmt_price_usd(ath)
        if athd:
            ath_chip += f"  ·  {fmt_ts(athd)}"
    atl_chip = "—"
    if atl is not None:
        atl_chip = _fmt_price_usd(atl)
        if atld:
            atl_chip += f"  ·  {fmt_ts(atld)}"

    price_chip = "—" if price is None else f"**{_fmt_price_usd(price)}**"
    delta_line = (
        f"1h `{_pct_chip(p1h)}`  ·  "
        f"24h `{_pct_chip(p24)}`  ·  "
        f"7d `{_pct_chip(p7d)}`  ·  "
        f"30d `{_pct_chip(p30)}`"
    )

    builder = (
        card(
            f"{_LIVE_PREFIX} {overview.get('name', record['symbol'])} ({record['symbol']})",
            description=f"{price_chip}  ·  rank #{rank}  ·  {indicators_short}",
            color=C_GOLD,
        )
        .field("📈 Price changes", delta_line, False)
        .field(
            "📊 24h",
            f"H `{_fmt_price_usd(h24)}`  ·  L `{_fmt_price_usd(l24)}`  ·  "
            f"Vol `{_fmt_big_usd(vol)}`",
            False,
        )
        .field("💹 Indicators (4h)", indicators_block, False)
        .field(
            "💰 Market",
            f"Cap `{_fmt_big_usd(mcap)}`  ·  FDV `{_fmt_big_usd(fdv)}`",
            False,
        )
        .field(
            "🧮 Supply",
            f"Circ `{_fmt_supply(circ)}`  ·  Total `{_fmt_supply(total)}`  ·  "
            f"Max `{_fmt_supply(max_s)}`",
            False,
        )
        .field("🏔️ ATH", ath_chip, True)
        .field("🪨 ATL", atl_chip, True)
        .field("🐋 Whale flows (top 24h venues by USD volume)", whale_value, False)
        .field("📰 Recent news", news_value, False)
        .footer(f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_OVIEW}s")
    )
    img = overview.get("image")
    if img:
        builder = builder.thumbnail(img)
    await ctx.reply(embed=builder.build(), mention_author=False)
