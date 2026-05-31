"""``$oracle SYMBOL`` and the ``$info`` oracle panel.

Returns a medianised oracle quote across Pyth / RedStone / Switchboard
with confidence interval, publish age, divergence and stale-feed
warnings.
"""

from __future__ import annotations

import logging
import time

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD, C_WARNING, fmt_usd

from services.market.oracle import aggregate_oracle
from services.market.router import get_router

log = logging.getLogger(__name__)


async def handle_oracle(ctx: DiscoContext, raw_args: str) -> None:
    sym = (raw_args or "").strip().split(" ", 1)[0]
    if not sym:
        await ctx.reply_error_hint(
            "Need a symbol.",
            hint="`$oracle MTA`  ·  `$oracle ARC`  ·  `$oracle SOL`",
            command_name="$oracle",
        )
        return

    router = get_router(ctx.bot)
    try:
        resolved = await router.resolve(sym)
    except Exception:
        resolved = None
    if resolved is None:
        await ctx.reply_error(f"Couldn't resolve `{sym}` for oracle lookup.")
        return

    agg = await aggregate_oracle(router, resolved)
    if agg is None:
        await ctx.reply(
            embed=card(
                "🛰️ $oracle · no feed",
                description=(
                    f"No oracle provider returned a feed for "
                    f"**{resolved.symbol}**. Pyth, RedStone, and Switchboard "
                    "all said no -- this symbol may not be on the major "
                    "oracle networks."
                ),
                color=C_WARNING,
            ).build(),
            mention_author=False,
        )
        return

    color = C_GOLD
    if agg.has_stale or agg.has_divergence:
        color = C_WARNING

    embed = (
        card(
            f"🛰️ $oracle · {resolved.symbol}",
            description=f"Medianised across {len(agg.quotes)} feed"
                        f"{'s' if len(agg.quotes) != 1 else ''}.",
            color=color,
        )
        .field("Median (USD)", f"**{fmt_usd(agg.median_usd)}**", True)
        .field("Divergence", f"{agg.divergence_pct:.3f}%", True)
        .field("Max publish age", f"{agg.max_age:.1f}s", True)
    )

    for q in agg.quotes:
        chips = []
        if q.confidence and q.price_usd:
            chips.append(f"± {(q.confidence / q.price_usd * 100):.3f}%")
        if q.is_stale:
            chips.append("⚠️ stale")
        chip_str = (" · ".join(chips)) if chips else "fresh"
        embed.field(
            q.provider,
            f"{fmt_usd(q.price_usd)}\n"
            f"age {q.publish_age:.1f}s · {chip_str}",
            True,
        )

    notes = []
    if agg.has_stale:
        notes.append("⚠️ at least one feed exceeded the stale threshold")
    if agg.has_divergence:
        notes.append("⚠️ providers disagree by more than 0.5%")
    if agg.has_vol_anomaly:
        notes.append("⚠️ at least one feed reported >1% confidence band")
    if notes:
        embed.field("Flags", "\n".join(notes), False)

    embed.footer(f"Oracle aggregate · {int(time.time())}")
    await ctx.reply(embed=embed.build(), mention_author=False)
