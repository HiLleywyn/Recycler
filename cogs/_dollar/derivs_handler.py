"""``$funding`` and ``$oi`` -- perp derivatives quick-look.

Uses CoinGlass as the primary, Coinalyze as the fallback. When neither
provider has a key configured, returns a clean "n/a" embed rather than
crashing.
"""

from __future__ import annotations

import logging
import time

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO, C_NEUTRAL, fmt_pct, fmt_usd

from services.market.base import AssetClass
from services.market.router import get_router

log = logging.getLogger(__name__)


async def handle_funding(ctx: DiscoContext, raw_args: str) -> None:
    sym = (raw_args or "").strip().split(" ", 1)[0]
    if not sym:
        await ctx.reply_error_hint(
            "Need a symbol.",
            hint="`$funding MTA`  ·  `$funding ARC`",
            command_name="$funding",
        )
        return
    router = get_router(ctx.bot)
    resolved = await router.resolve(sym)
    if resolved is None:
        await ctx.reply_error(f"Couldn't resolve `{sym}`.")
        return
    # Re-flag as perp so the router picks coinglass / coinalyze.
    resolved.asset_class = AssetClass.PERP
    data = await router.funding(resolved)
    if not data:
        await ctx.reply(
            embed=card(
                "🎚️ $funding · n/a",
                description=(
                    f"No derivatives provider returned funding rate data "
                    f"for **{resolved.symbol}**. "
                    "CoinGlass or Coinalyze API key required."
                ),
                color=C_NEUTRAL,
            ).build(),
            mention_author=False,
        )
        return

    avg = data.get("weighted_rate") or 0.0
    embed = (
        card(
            f"🎚️ $funding · {resolved.symbol}",
            description=(
                f"OI-weighted current funding across exchanges: "
                f"**{avg * 100:+.4f}%** (per 8h interval)."
            ),
            color=C_INFO,
        )
    )
    for ex in (data.get("per_exchange") or [])[:8]:
        rate = float(ex.get("rate") or 0.0)
        embed.field(
            str(ex.get("exchange") or "?"),
            f"{rate * 100:+.4f}%",
            True,
        )
    embed.footer(f"$funding · {int(time.time())}")
    await ctx.reply(embed=embed.build(), mention_author=False)


async def handle_oi(ctx: DiscoContext, raw_args: str) -> None:
    sym = (raw_args or "").strip().split(" ", 1)[0]
    if not sym:
        await ctx.reply_error_hint(
            "Need a symbol.",
            hint="`$oi MTA`  ·  `$oi ARC`",
            command_name="$oi",
        )
        return
    router = get_router(ctx.bot)
    resolved = await router.resolve(sym)
    if resolved is None:
        await ctx.reply_error(f"Couldn't resolve `{sym}`.")
        return
    resolved.asset_class = AssetClass.PERP
    data = await router.open_interest(resolved)
    if not data:
        await ctx.reply(
            embed=card(
                "🎚️ $oi · n/a",
                description=(
                    f"No derivatives provider returned open interest data "
                    f"for **{resolved.symbol}**. "
                    "CoinGlass or Coinalyze API key required."
                ),
                color=C_NEUTRAL,
            ).build(),
            mention_author=False,
        )
        return

    total = float(data.get("total_usd") or 0.0)
    embed = (
        card(
            f"🎚️ $oi · {resolved.symbol}",
            description=f"Aggregate open interest: **{fmt_usd(total)}**.",
            color=C_INFO,
        )
    )
    for ex in (data.get("per_exchange") or [])[:8]:
        oi = float(ex.get("oi_usd") or 0.0)
        pct = ex.get("pct_24h")
        line = fmt_usd(oi)
        if pct is not None:
            try:
                line += f" · {fmt_pct(float(pct))}"
            except (TypeError, ValueError):
                pass
        embed.field(str(ex.get("exchange") or "?"), line, True)
    embed.footer(f"$oi · {int(time.time())}")
    await ctx.reply(embed=embed.build(), mention_author=False)
