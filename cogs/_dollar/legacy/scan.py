"""``$scan`` -- pattern-scout alert embed.

Crypto-only path (via CoinGecko OHLC). The optional AI overlay is
fired separately by the dispatcher (see :mod:`cogs._dollar.scan_ai`).
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from core.framework.chart import _aggregate, build_chart_png
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO
from services.pattern_scout import detect_best, market_context
from services.real_market import SUPPORTED_TIMEFRAMES, RealMarketError

from ._shared import (
    _FOOTER_BRAND,
    _TF_SECONDS,
    _TF_SHAPE,
    _build_scan_embed,
    _default_scan_layout,
)

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


async def handle(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    """Heuristic pattern scout over live OHLC.

    Usage: ``$scan SYMBOL [timeframe]`` (alias ``$pattern``). Default
    timeframe is 30m -- sweet spot for flag/wedge formations on most
    majors.
    """
    tokens = raw_args.split()
    if not tokens:
        await ctx.reply_error_hint(
            "You must give a symbol.",
            hint="Try `$scan MTA` or `$scan ARC 1h`. "
                 "Default timeframe is 30m.",
            command_name="$scan",
        )
        return

    symbol = tokens[0]
    timeframe = "30m"
    if len(tokens) > 1:
        candidate = tokens[1].lower()
        if candidate in SUPPORTED_TIMEFRAMES:
            timeframe = candidate
        else:
            # Map any canonical timeframe outside CoinGecko's native
            # 6-tier set onto the closest native one so the pattern
            # scout has enough candles to work. Anything finer than 5m
            # bucket-aggregates from the 5m stream below; anything
            # coarser than 1d uses 1d daily candles.
            from services.market.timeframes import canonical_tf
            tf_norm = canonical_tf(candidate)
            if tf_norm is None and _TF_SHAPE.match(candidate):
                from services.market.timeframes import SUPPORTED_TIMEFRAMES as _ALL_TFS
                await ctx.reply_error(
                    f"Unknown timeframe `{candidate}`. Supported: "
                    f"`{', '.join(_ALL_TFS)}`."
                )
                return
            if tf_norm is not None and tf_norm not in SUPPORTED_TIMEFRAMES:
                # Sub-5m -> use the 5m CoinGecko stream as the data
                # source for the scout (patterns at sub-5m are usually
                # noise anyway -- the detector wants structure). 3d/1w
                # and above collapse to 1d.
                seconds_map = {
                    "1s": 5*60, "5s": 5*60, "15s": 5*60, "30s": 5*60,
                    "1m": 5*60, "3m": 5*60,
                    "45m": 30*60, "2h": 60*60, "6h": 4*3600,
                    "8h": 4*3600, "12h": 4*3600,
                    "3d": 86400, "1w": 86400, "1mo": 86400,
                    "3mo": 86400, "6mo": 86400, "1y": 86400, "all": 86400,
                }
                fallback = seconds_map.get(tf_norm)
                if fallback == 300:
                    timeframe = "5m"
                elif fallback == 1800:
                    timeframe = "30m"
                elif fallback == 3600:
                    timeframe = "1h"
                elif fallback in (4 * 3600,):
                    timeframe = "4h"
                else:
                    timeframe = "1d"
            elif tf_norm in SUPPORTED_TIMEFRAMES:
                timeframe = tf_norm

    record = await cog._resolve_or_error(ctx, symbol, command_name="$scan")
    if not record:
        return

    try:
        primary = await cog.client.get_ohlc(record["id"], timeframe)
    except RealMarketError as exc:
        log.warning("$scan ohlc failed for %s: %s", record["id"], exc)
        await ctx.reply_error(
            f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
            "Try again in a moment."
        )
        return

    if len(primary) < 30:
        await ctx.reply_error(
            f"Not enough live history for **{record['symbol']}** at "
            f"`{timeframe}` to scan for patterns yet."
        )
        return

    tf_sec = _TF_SECONDS[timeframe]
    agg = _aggregate(primary, tf_sec) or primary

    try:
        match = detect_best(agg)
    except Exception:
        log.exception("$scan detect_best crashed for %s", record["id"])
        match = None

    pair = f"{record['symbol']}/USDT"

    if match is None:
        embed = (
            card(
                f"🔍 No pattern on {pair} ({timeframe.upper()})",
                description=(
                    f"Scanned the last {len(agg)} `{timeframe}` candles -- "
                    "no high-confidence pattern detected. The market is "
                    "either ranging without structure or in a phase the "
                    "scout doesn't recognise.\n\n"
                    "Try a different timeframe (e.g. `$scan "
                    f"{record['symbol']} 1h`) or run `$chart "
                    f"{record['symbol']} {timeframe}` to inspect manually."
                ),
                color=C_INFO,
            )
            .footer(_FOOTER_BRAND)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    png_bytes: bytes | None = None
    try:
        async with ctx.typing():
            png_bytes, _ = await build_chart_png(
                agg,
                layout=_default_scan_layout(),
                clean_inds=["ema20", "ema50"],
                tf_seconds=tf_sec,
                pair=pair,
                timeframe=timeframe,
                comparisons=[],
                base_norm=[],
                quoted_in="USD",
                pattern_overlay=match.overlay or None,
                live=True,
            )
    except Exception:
        log.exception("$scan chart render failed for %s", record["id"])

    try:
        ctx_data = market_context(agg)
    except Exception:
        log.exception("$scan market_context failed for %s", record["id"])
        ctx_data = None
    embed = _build_scan_embed(
        match, pair=pair, timeframe=timeframe, context=ctx_data,
    )
    if png_bytes is not None:
        file = discord.File(io.BytesIO(png_bytes), filename="scan.png")
        await ctx.reply(embed=embed, file=file, mention_author=False)
    else:
        await ctx.reply(embed=embed, mention_author=False)
