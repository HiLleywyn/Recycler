"""``$chart`` -- live OHLC chart for the CoinGecko-backed path.

The non-crypto path (equities / ETFs / forex / commodities / indices)
is in :mod:`cogs._dollar.chart_handler` -- this module only handles
the crypto leg via ``cog.client`` (CoinGecko :class:`RealMarketClient`).
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from core.config import Config
from core.framework.chart import _aggregate, build_chart_png, build_footer_chips, parse_chart_args
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD
from services.pattern_scout import detect_best
from services.real_market import SUPPORTED_TIMEFRAMES, RealMarketError

from ._shared import (
    _FOOTER_BRAND,
    _LIVE_PREFIX,
    _MAX_COMPARES,
    _TF_SECONDS,
    _TF_SHAPE,
    _pattern_emoji,
)

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


async def handle(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    tokens = raw_args.split()
    if not tokens:
        await ctx.reply_error_hint(
            "You must give a symbol.",
            hint="Try `$chart MTA` or `$chart ARC 4h rsi macd bb wide`.",
            command_name="$chart",
        )
        return

    symbol = tokens[0]
    # tf is the optional second token. Three cases:
    #   1. A supported timeframe -> use it, drop it from the tail.
    #   2. A token that LOOKS like a timeframe (`12h`, `2d`, ...) but isn't
    #      supported -> reject with a clear error so it doesn't silently
    #      leak into the indicator chip list and mislabel the chart.
    #   3. Anything else -> treat as an indicator/flag so
    #      `$chart MTA rsi macd` works without retyping the default tf.
    timeframe = "1h"
    tail = tokens[1:]
    router_timeframe: str | None = None
    if len(tokens) > 1:
        candidate = tokens[1].lower()
        if candidate in SUPPORTED_TIMEFRAMES:
            # Native CoinGecko timeframe (5m/15m/30m/1h/4h/1d) -- the
            # legacy crypto path handles it directly.
            timeframe = candidate
            tail = tokens[2:]
        else:
            # Anything else: hand off to the cross-asset market router
            # which knows about Pyth (sub-minute), Yahoo (daily-equity),
            # TradingView UDF (anything mounted), and CoinGecko for the
            # native subset. The router gives us 24 timeframes from
            # 1s..all instead of the 6-tier CoinGecko free-tier list.
            from services.market.timeframes import canonical_tf
            tf_norm = canonical_tf(candidate)
            if tf_norm is not None:
                router_timeframe = tf_norm
                tail = tokens[2:]
            elif _TF_SHAPE.match(candidate):
                from services.market.timeframes import SUPPORTED_TIMEFRAMES as _ALL_TFS
                await ctx.reply_error(
                    f"Unknown timeframe `{candidate}`. Supported: "
                    f"`{', '.join(_ALL_TFS)}`."
                )
                return

    # Sub-5m and other extended timeframes route through the new
    # market-data router (it will pick Pyth / TradingView / CoinGecko
    # per the canonical timeframe table).
    if router_timeframe is not None:
        scan_flags = {"scan", "pattern", "patterns"}
        flags = [t for t in tail if t.lower() not in scan_flags]
        try:
            from services.market.router import get_router
            router_obj = get_router(cog.bot)
            resolved = await router_obj.resolve(symbol)
        except Exception:
            log.exception("[$chart] router.resolve failed")
            resolved = None
        if resolved is None:
            await ctx.reply_error(
                f"Couldn't resolve `{symbol.upper()}` for "
                f"`{router_timeframe}` chart."
            )
            return
        try:
            from cogs._dollar.chart_handler import handle_chart_router
            await handle_chart_router(
                ctx, resolved, timeframe_raw=router_timeframe, flags=flags,
            )
        except Exception:
            log.exception("[$chart router] router-tf handler crashed")
            await ctx.reply_error(
                f"Chart renderer for `{symbol.upper()}` at `{router_timeframe}` "
                "hit an error."
            )
        return

    # Pattern auto-tag is opt-in: only fires if `scan` / `pattern` is
    # in the flag list. Pull out of tail before the generic parser
    # sees it.
    scan_flags = {"scan", "pattern", "patterns"}
    want_scan = any(t.lower() in scan_flags for t in tail)
    if want_scan:
        tail = [t for t in tail if t.lower() not in scan_flags]

    record = await cog._resolve_or_error(ctx, symbol, command_name="$chart")
    if not record:
        return

    # Non-crypto assets route to the new market router.
    if record.get("_asset_class") and record["_asset_class"] != "crypto":
        try:
            from cogs._dollar.chart_handler import handle_chart_router
            await handle_chart_router(
                ctx, record["_resolved"], timeframe_raw=timeframe, flags=tail,
            )
        except Exception:
            log.exception("[$chart router] non-crypto handler crashed")
            await ctx.reply_error(
                f"Chart renderer for {record['symbol']} hit an error.",
            )
        return

    try:
        primary_candles = await cog.client.get_ohlc(record["id"], timeframe)
    except RealMarketError as exc:
        log.warning("$chart ohlc failed for %s: %s", record["id"], exc)
        await ctx.reply_error(
            f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
            "Try again in a moment."
        )
        return

    if len(primary_candles) < 2:
        await ctx.reply_error(
            f"Not enough live history for **{record['symbol']}** at `{timeframe}` yet."
        )
        return

    tf_sec = _TF_SECONDS[timeframe]
    agg = _aggregate(primary_candles, tf_sec) or primary_candles

    layout, compare_syms, quote_in, clean_inds = parse_chart_args(
        tail, valid_symbols=None, primary=record["symbol"],
    )

    # ── in:SYM -- re-quote primary in terms of the quote token ────────
    token_b_display = "USD"
    if quote_in and quote_in.upper() != "USD":
        quote_record = await cog.client.resolve_symbol(quote_in)
        if quote_record and quote_record.get("id"):
            try:
                quote_candles = await cog.client.get_ohlc(
                    quote_record["id"], timeframe,
                )
                quote_agg = _aggregate(quote_candles, tf_sec) or quote_candles
                qmap = {c["ts"]: float(c["close"] or 0.0) for c in quote_agg}
                converted: list[dict] = []
                for c in agg:
                    q = qmap.get(c["ts"])
                    if not q or q <= 0:
                        continue
                    converted.append({
                        "ts": c["ts"],
                        "open":  c["open"]  / q,
                        "high":  c["high"]  / q,
                        "low":   c["low"]   / q,
                        "close": c["close"] / q,
                        "volume": c.get("volume", 0.0),
                    })
                if len(converted) >= 2:
                    agg = converted
                    token_b_display = quote_record["symbol"]
            except RealMarketError as exc:
                log.warning(
                    "$chart quote conversion failed for %s: %s",
                    quote_record.get("id"), exc,
                )

    # ── compare:SYM -- normalised-to-100 overlay series ───────────────
    times = [c["ts"] for c in agg]
    comparisons: list[dict] = []
    for sym in compare_syms[:_MAX_COMPARES]:
        cmp_record = await cog.client.resolve_symbol(sym)
        if not cmp_record or not cmp_record.get("id"):
            continue
        try:
            cmp_raw = await cog.client.get_ohlc(cmp_record["id"], timeframe)
        except RealMarketError as exc:
            log.warning(
                "$chart compare failed for %s: %s", cmp_record.get("id"), exc,
            )
            continue
        cmp_agg = _aggregate(cmp_raw, tf_sec) or cmp_raw
        if len(cmp_agg) < 2:
            continue
        cmp_map = {c["ts"]: float(c["close"] or 0.0) for c in cmp_agg}
        anchor = None
        pts: list[dict] = []
        for t in times:
            v = cmp_map.get(t)
            if v is None or v <= 0:
                continue
            if anchor is None:
                anchor = v
            pts.append({"time": t, "value": 100.0 * v / anchor})
        if pts:
            comparisons.append({"symbol": cmp_record["symbol"], "points": pts})

    base_norm: list[dict] = []
    if comparisons:
        closes = [c["close"] for c in agg]
        anchor = next((x for x in closes if x), closes[0] if closes else 0.0)
        if anchor:
            base_norm = [
                {"time": t, "value": 100.0 * c / anchor}
                for t, c in zip(times, closes) if c
            ]

    async with ctx.typing():
        try:
            png_bytes, stats = await build_chart_png(
                agg,
                layout=layout,
                clean_inds=clean_inds,
                tf_seconds=tf_sec,
                pair=f"{record['symbol']}/{token_b_display}",
                timeframe=timeframe,
                comparisons=comparisons,
                base_norm=base_norm,
                quoted_in=token_b_display,
                live=True,
            )
        except Exception as exc:
            log.exception("$chart render failed for %s", record["id"])
            await ctx.reply_error(f"Chart render failed: {type(exc).__name__}")
            return

    footer_chips = build_footer_chips(
        compare_syms=[c["symbol"] for c in comparisons],
        quote_in=token_b_display if token_b_display != "USD" else None,
        layout=layout,
        clean_inds=clean_inds,
    )
    footer = (
        f"{_FOOTER_BRAND} · cached {Config.REAL_MARKET_CACHE_TTL_OHLC}s"
        + (f" · {footer_chips}" if footer_chips else "")
    )

    pct = stats["pct_change"]
    delta_arrow = "▲" if pct >= 0 else "▼"
    desc_lines = [
        f"💵 Close `{stats['close']:,.6f}`  "
        f"{delta_arrow} **{pct:+.2f}%**  ·  "
        f"H `{stats['high']:,.4f}`  L `{stats['low']:,.4f}`"
    ]

    if want_scan:
        try:
            match = detect_best(agg)
        except Exception:
            log.exception("$chart auto-tag failed for %s", record["id"])
            match = None
        if match is not None:
            desc_lines.append(
                f"{_pattern_emoji(match)} **{match.name}** spotted "
                f"({match.status.lower()}, {match.confidence:.0f}% conf)  ·  "
                f"`$scan {record['symbol']} {timeframe}` for details"
            )
        else:
            desc_lines.append(
                "🔍 No high-confidence pattern detected on this window."
            )

    embed = (
        card(
            f"{_LIVE_PREFIX} {record['symbol']}/{token_b_display} · {timeframe.upper()}",
            description="\n".join(desc_lines), color=C_GOLD,
        )
        .image("attachment://chart.png")
        .footer(footer)
        .build()
    )
    file = discord.File(io.BytesIO(png_bytes), filename="chart.png")
    await ctx.reply(embed=embed, file=file, mention_author=False)
