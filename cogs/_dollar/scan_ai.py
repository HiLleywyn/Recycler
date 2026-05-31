"""AI mode extension for ``$scan``.

The existing :meth:`cogs.realmarket.RealMarket._handle_scan` produces a
PNG + embed via the legacy CoinGecko path. When the user appends ``ai``
to the args, this module:

1. Re-runs the technical scan via the new router (which broadens
   provider coverage).
2. Wraps the result in a :class:`ScanSnapshot`.
3. Calls :func:`services.market_ai.run_scan_ai`.
4. Sends a follow-up message with the AI commentary + Sources button.

We keep this as a SECOND message rather than rewriting the legacy scan
handler so existing UX is preserved and the AI overlay is purely
additive.
"""

from __future__ import annotations

import logging

import discord

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO

from services.market.router import get_router
from services.market.ta import build_scan_snapshot
from services.market.timeframes import canonical_tf
from services.market_ai import run_scan_ai

from .views import make_sources_button

log = logging.getLogger(__name__)


async def maybe_run_scan_ai(
    ctx: DiscoContext,
    *,
    symbol: str,
    timeframe: str | None,
    parent_message: discord.Message | None = None,
) -> None:
    """Best-effort AI overlay on top of an already-sent ``$scan`` embed.
    All failures are swallowed -- the regular scan reply is the
    user-visible baseline. The AI reply is the bonus."""
    tf = canonical_tf(timeframe or "") or "30m"

    router = get_router(ctx.bot)
    try:
        resolved = await router.resolve(symbol)
    except Exception:
        log.debug("[$scan ai] resolve failed", exc_info=True)
        return
    if resolved is None:
        return

    try:
        candles, provider = await router.ohlc(resolved, tf)
    except Exception:
        log.debug("[$scan ai] ohlc failed", exc_info=True)
        return
    if not candles:
        return

    candle_dicts = [c.to_chart_dict() for c in candles]

    snapshot = await build_scan_snapshot(
        symbol=resolved.symbol,
        asset_class=resolved.asset_class.value,
        timeframe=tf,
        provider=provider,
        candles=candle_dicts,
    )

    result = await run_scan_ai(snapshot, user_id=ctx.author.id)

    embed = (
        card(
            f"🧠 $scan · {resolved.symbol} ({tf}) · AI commentary",
            description=result.summary[:3800],
            color=C_INFO,
        )
        .footer(
            "Probabilistic reading · not financial advice · "
            f"confidence {result.confidence * 100:.0f}% · "
            f"data via {provider}",
        )
        .build()
    )
    view = make_sources_button(result.citations, ctx.author.id)
    try:
        await ctx.reply(embed=embed, view=view, mention_author=False)
    except Exception:
        log.debug("[$scan ai] follow-up send failed", exc_info=True)
