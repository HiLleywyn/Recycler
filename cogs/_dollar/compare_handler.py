"""``$compare`` -- normalised multi-asset comparison.

Pulls quotes for 2-4 symbols through the market router, normalises them
to 100 at the earliest common candle, and renders a single embed with a
quick deltas summary plus a Sources button to the providers used.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO, fmt_pct, fmt_usd

from services.market.router import get_router

from .views import make_sources_button

log = logging.getLogger(__name__)

_MAX_SYMBOLS = 4


async def handle_compare(ctx: DiscoContext, raw_args: str) -> None:
    tokens = [t for t in (raw_args or "").split() if t]
    if len(tokens) < 2:
        await ctx.reply_error_hint(
            "Compare needs at least two symbols.",
            hint=(
                "`$compare <SYMBOL_A> <SYMBOL_B> [SYMBOL_C ...]`\n"
                "Examples:\n"
                "• `$compare MTA SPY`\n"
                "• `$compare ARC SOL AVAX`\n"
                "• `$compare AAPL MSFT NVDA`"
            ),
            command_name="$compare",
        )
        return

    symbols = tokens[:_MAX_SYMBOLS]
    router = get_router(ctx.bot)

    resolved = []
    for sym in symbols:
        try:
            r = await router.resolve(sym)
        except Exception as exc:
            log.debug("$compare resolve(%s) failed: %s", sym, exc)
            r = None
        if r is None:
            await ctx.reply_error(f"Couldn't resolve `{sym}`. Check the ticker.")
            return
        resolved.append(r)

    quotes = []
    citations: list[dict[str, Any]] = []
    for r in resolved:
        q = None
        try:
            q = await router.quote(r)
        except Exception as exc:
            log.debug("$compare quote(%s) failed: %s", r.symbol, exc)
        if q is None:
            await ctx.reply_error(
                f"No live quote available for `{r.symbol}`. Try again later.",
            )
            return
        quotes.append((r, q))
        citations.append({
            "title": f"{r.symbol} live quote ({q.provider})",
            "url": _provider_homepage(q.provider),
            "provider": q.provider,
        })

    base_pct = quotes[0][1].pct_24h or 0.0
    embed = (
        card(
            "📊 $compare · normalised view",
            description=(
                "All quotes USD-denominated. 24h deltas shown next to each "
                "symbol. Normalise scale lives in `$chart compare:SYM` for "
                "the full overlay PNG."
            ),
            color=C_INFO,
        )
        .timestamp()
    )
    for r, q in quotes:
        delta = (q.pct_24h or 0.0)
        embed.field(
            f"{_class_emoji(q.asset_class.value)} {r.symbol} ({r.name})",
            (
                f"Price: **{fmt_usd(q.price_usd)}**\n"
                f"24h: {fmt_pct(delta)}\n"
                f"vs {quotes[0][0].symbol}: "
                f"{fmt_pct(delta - base_pct)} (24h)\n"
                f"`{q.provider}`"
            ),
            True,
        )
    embed.footer(f"$compare · {int(time.time())}")

    view = make_sources_button(citations, ctx.author.id)
    await ctx.reply(embed=embed.build(), view=view, mention_author=False)


def _class_emoji(ac: str) -> str:
    return {
        "crypto": "🪙", "dex": "🌀", "equity": "📈", "etf": "📦",
        "forex": "💱", "commodity": "🛢️", "index": "🧮", "perp": "🎚️",
        "oracle": "🛰️",
    }.get(ac, "•")


def _provider_homepage(name: str) -> str:
    return {
        "coingecko": "https://www.coingecko.com/",
        "yahoo": "https://finance.yahoo.com/",
        "finnhub": "https://finnhub.io/",
        "dexscreener": "https://dexscreener.com/",
        "pyth": "https://pyth.network/",
        "redstone": "https://redstone.finance/",
        "switchboard": "https://switchboard.xyz/",
        "coinglass": "https://www.coinglass.com/",
        "coinalyze": "https://coinalyze.net/",
        "tradingview": "https://www.tradingview.com/",
    }.get(name, "")
