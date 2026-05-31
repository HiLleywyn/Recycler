"""Non-crypto ``$info`` handler.

Renders a single-embed snapshot for any asset class the router resolves
(equities, ETFs, indices, forex, commodities). For equities/ETFs the
panel includes fundamentals (P/E, market cap, 52w range) and the next
earnings entry when Finnhub has a key configured.

Crypto paths still flow through the legacy :meth:`cogs.realmarket.RealMarket._handle_info`
unchanged -- this handler is only invoked when the legacy CoinGecko
resolver returns ``None``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD, C_INFO, fmt_pct, fmt_usd

from services.market.base import ResolvedSymbol
from services.market.router import get_router

log = logging.getLogger(__name__)


def _emoji(ac: str) -> str:
    return {
        "equity": "📈", "etf": "📦", "index": "🧮",
        "forex": "💱", "commodity": "🛢️", "perp": "🎚️",
        "dex": "🌀", "crypto": "🪙",
    }.get(ac, "•")


async def handle_info_router(
    ctx: DiscoContext,
    resolved: ResolvedSymbol,
) -> None:
    router = get_router(ctx.bot)
    quote = None
    try:
        quote = await router.quote(resolved)
    except Exception as exc:
        log.debug("[$info router] quote failed for %s: %s", resolved.symbol, exc)
    if quote is None:
        await ctx.reply_error(
            f"No live quote available for **{resolved.symbol}** "
            f"({resolved.asset_class.value}).",
        )
        return

    color = C_GOLD if resolved.asset_class.value == "equity" else C_INFO
    embed = (
        card(
            f"{_emoji(resolved.asset_class.value)} ${resolved.symbol.upper()} · {resolved.name}",
            color=color,
        )
        .field("Price", fmt_usd(quote.price_usd), True)
        .field("Asset class", resolved.asset_class.value.upper(), True)
        .field("Provider", quote.provider, True)
    )
    if quote.pct_24h is not None:
        embed.field("24h", fmt_pct(quote.pct_24h), True)
    if quote.day_high is not None:
        embed.field("Day high", fmt_usd(quote.day_high), True)
    if quote.day_low is not None:
        embed.field("Day low", fmt_usd(quote.day_low), True)
    if quote.day_volume_usd:
        embed.field("Day volume (USD)", fmt_usd(quote.day_volume_usd), True)
    if quote.market_cap_usd:
        embed.field("Market cap", fmt_usd(quote.market_cap_usd), True)

    # ── Fundamentals (equities / ETFs) ──
    if resolved.asset_class.value in ("equity", "etf"):
        await _add_fundamentals_panel(embed, router, resolved, quote)

    # ── Forex / commodity / index extras ──
    extras = quote.extras or {}
    if extras.get("fiftyTwoWeekHigh") and extras.get("fiftyTwoWeekLow"):
        embed.field(
            "52w range",
            f"{fmt_usd(extras['fiftyTwoWeekLow'])} – {fmt_usd(extras['fiftyTwoWeekHigh'])}",
            True,
        )
    if extras.get("exchange"):
        embed.field("Exchange", str(extras["exchange"]), True)
    if extras.get("currency") and extras["currency"] != "USD":
        embed.field("Currency", str(extras["currency"]), True)

    embed.footer(f"$info · {quote.provider} · {int(time.time())}")
    await ctx.reply(embed=embed.build(), mention_author=False)


async def _add_fundamentals_panel(
    embed: Any,
    router: Any,
    resolved: ResolvedSymbol,
    quote: Any,
) -> None:
    extras = quote.extras or {}
    pe = extras.get("pe")
    if pe is not None:
        try:
            embed.field("P/E (TTM)", f"{float(pe):.2f}", True)
        except (TypeError, ValueError):
            pass
    eps = extras.get("eps")
    if eps is not None:
        try:
            embed.field("EPS (TTM)", f"{float(eps):.2f}", True)
        except (TypeError, ValueError):
            pass

    # Earnings calendar -- only available when FINNHUB_API_KEY is set.
    earnings = None
    try:
        earnings = await router.earnings(resolved)
    except Exception:
        earnings = None
    upcoming = (earnings or {}).get("upcoming") if earnings else None
    if upcoming:
        nxt = upcoming[0]
        date = nxt.get("date") or "?"
        eps_est = nxt.get("epsEstimate")
        rev_est = nxt.get("revenueEstimate")
        bits = [f"📅 {date}"]
        if eps_est is not None:
            bits.append(f"EPS est {eps_est}")
        if rev_est is not None:
            try:
                bits.append(f"rev est {fmt_usd(float(rev_est))}")
            except (TypeError, ValueError):
                pass
        embed.field("Next earnings", " · ".join(bits), False)
