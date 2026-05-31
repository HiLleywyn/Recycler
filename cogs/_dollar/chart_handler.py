"""Non-crypto chart handler used by the dispatcher when ``_resolve_or_error``
returns an asset the legacy CoinGecko path can't render (equities,
ETFs, indices, forex, commodities).

Renders a Pillow candle PNG via the existing :func:`core.framework.chart.build_chart_png`
so the visual style matches ``,chart`` and the crypto ``$chart`` path
exactly. Uses the new :class:`services.market.router.MarketRouter` to
fetch OHLCV from Yahoo / Finnhub / TradingView as appropriate.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import discord

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD, C_INFO, fmt_pct, fmt_usd

from services.market.base import ResolvedSymbol
from services.market.router import get_router
from services.market.timeframes import canonical_tf, tf_label

log = logging.getLogger(__name__)


def _emoji(ac: str) -> str:
    return {
        "equity": "📈", "etf": "📦", "index": "🧮",
        "forex": "💱", "commodity": "🛢️", "perp": "🎚️",
        "dex": "🌀", "crypto": "🪙",
    }.get(ac, "•")


async def handle_chart_router(
    ctx: DiscoContext,
    resolved: ResolvedSymbol,
    timeframe_raw: str | None,
    flags: list[str],
) -> None:
    """Render a chart for any non-crypto asset that the router resolved."""
    tf = canonical_tf(timeframe_raw or "") or "1d"
    router = get_router(ctx.bot)

    try:
        candles, provider = await router.ohlc(resolved, tf)
    except Exception as exc:
        log.debug("[$chart router] ohlc failed for %s: %s", resolved.symbol, exc)
        try:
            from services.market.timeframes import providers_for_timeframe
            attempted = providers_for_timeframe(resolved.asset_class.value, tf)
        except Exception:
            attempted = ()
        # For each provider in the fan-out, show its CURRENT health
        # status + the last failure reason the registry recorded.
        # First-time failures don't flip the chip color (we need 2+
        # consecutive misses to go 🟡 and 5+ to go 🔴), so the reason
        # field is what tells the user what actually broke.
        lines = []
        for name in attempted:
            entry = router.registry.health.get(name)
            chip = {
                "healthy": "🟢", "degraded": "🟡",
                "down": "🔴", "disabled": "⚪",
            }.get(entry.status.value, "•")
            reason = (entry.reason or "")[:80]
            if reason:
                lines.append(f"{chip} `{name}` -- {reason}")
            else:
                lines.append(f"{chip} `{name}`")
        chain = "\n".join(lines) if lines else "(no provider supports this combination)"
        if tf in {"1s", "5s", "15s", "30s"}:
            hint = (
                "**1s/5s/15s/30s candles only come from Binance.** "
                "binance.com is geo-blocked from most US datacentres "
                "(including this Railway region). Try `$chart "
                f"{resolved.symbol.lower()} 1m` -- Coinbase Exchange "
                "(US-friendly, public) is the new primary 1m+ source."
            )
        else:
            hint = (
                "Run `$status` to see live provider health. The fan-out "
                "tried each provider below in order; each line shows "
                "its current chip + the last failure reason recorded. "
                "Common causes: Binance/Bybit geo-block from US "
                "datacentres (Coinbase is the US-friendly fallback), "
                "or CoinGecko free-tier 429."
            )
        await ctx.reply_error_hint(
            f"Couldn't fetch `{tf}` candles for **{resolved.symbol}** "
            f"({resolved.asset_class.value}).",
            hint=f"{hint}\n\nFan-out tried:\n{chain}",
            command_name="$chart",
        )
        return
    if not candles:
        await ctx.reply_error(
            f"No `{tf}` candles available for **{resolved.symbol}** yet "
            f"(provider `{provider}` returned empty)."
        )
        return

    # Best-effort spot quote for the description line.
    quote = None
    try:
        quote = await router.quote(resolved)
    except Exception:
        pass

    png_bytes = await _render_chart_png(
        symbol=resolved.symbol,
        timeframe=tf,
        candles=[c.to_chart_dict() for c in candles],
        flags=flags,
    )
    if not png_bytes:
        await ctx.reply_error(
            f"Chart renderer didn't produce a PNG for **{resolved.symbol}**.",
        )
        return

    file = discord.File(io.BytesIO(png_bytes), filename="chart.png")
    embed_color = C_GOLD if resolved.asset_class.value == "equity" else C_INFO
    embed = (
        card(
            f"{_emoji(resolved.asset_class.value)} ${resolved.symbol.upper()} ({tf_label(tf)})",
            color=embed_color,
        )
        .field(
            "Asset class",
            resolved.asset_class.value.upper(),
            True,
        )
        .field("Provider", provider, True)
    )
    if quote is not None:
        embed.field("Last", fmt_usd(quote.price_usd), True)
        if quote.pct_24h is not None:
            embed.field("24h", fmt_pct(quote.pct_24h), True)
        if quote.day_high is not None and quote.day_low is not None:
            embed.field(
                "Day H/L",
                f"{fmt_usd(quote.day_high)} / {fmt_usd(quote.day_low)}",
                True,
            )
        if quote.market_cap_usd:
            embed.field("Market cap", fmt_usd(quote.market_cap_usd), True)
    embed.image("attachment://chart.png").footer(
        f"$chart · {resolved.name} · {provider} · {int(time.time())}",
    )
    await ctx.reply(file=file, embed=embed.build(), mention_author=False)


async def _render_chart_png(
    *,
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    flags: list[str],
) -> bytes | None:
    """Bridge to the existing chart engine. Calls the real
    ``core.framework.chart.build_chart_png`` signature -- candles
    positional, the rest kw-only. Returns ``None`` if the renderer
    crashes so the caller can surface a clear error.
    """
    try:
        from core.framework.chart import build_chart_png, default_layout
        from services.market.timeframes import tf_seconds
    except Exception:
        log.debug("[$chart router] core.framework.chart not importable", exc_info=True)
        return None
    try:
        tf_sec = tf_seconds(timeframe)
    except Exception:
        tf_sec = 3600
    # Honour the same `wide` / `tall` / `minimal` flags the legacy
    # chart command parses out of the tail. Anything else gets dropped
    # silently rather than mis-coloured as an indicator chip.
    layout = default_layout()
    for tok in (flags or []):
        low = (tok or "").lower()
        if low in ("wide", "tall", "minimal"):
            layout["view"] = low
        elif low in ("light", "dark"):
            layout["theme"] = low
        elif low in ("log", "linear"):
            layout["scale"] = low
        elif low in ("candles", "line", "area", "bars", "heikinashi", "ha"):
            layout["style"] = "heikinashi" if low == "ha" else low
    indicator_set = {"ema20", "ema50", "ema200", "sma20", "sma50",
                     "rsi", "macd", "bb", "vol", "vwap", "stoch",
                     "adx", "atr", "supertrend", "st", "trend", "all"}
    clean_inds = [t.lower() for t in (flags or []) if t.lower() in indicator_set]
    try:
        png, _stats = await build_chart_png(
            candles,
            layout=layout,
            clean_inds=clean_inds,
            tf_seconds=tf_sec,
            pair=f"{symbol}/USD",
            timeframe=timeframe,
            comparisons=[],
            base_norm=[],
            quoted_in="USD",
            live=True,
        )
    except Exception:
        log.exception("[$chart router] build_chart_png crashed for %s %s", symbol, timeframe)
        return None
    if isinstance(png, (bytes, bytearray)):
        return bytes(png)
    return None
