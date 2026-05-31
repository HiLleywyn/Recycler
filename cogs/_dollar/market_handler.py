"""``$market`` umbrella -- thin router into the legacy market-wide
handlers in :mod:`cogs.realmarket`.

Keeps the user-facing surface small. ``$market fear`` calls the existing
fear-greed handler, ``$market top stocks 15`` will reach the stocks-aware
top-list once that's wired, and so on. The existing aliases (``$fear``,
``$top``, ``$gainers``, ``$losers``, ``$heatmap``, ``$dom``, ``$trending``,
``$global``, ``$convert``) keep working unchanged.
"""

from __future__ import annotations

import logging

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO

log = logging.getLogger(__name__)


_HELP_TEXT = (
    "**`$market <sub> [args]`** -- market-wide intel umbrella.\n\n"
    "Subcommands:\n"
    "• `$market fear` -- Fear & Greed Index\n"
    "• `$market heatmap [N]` -- top N coins by 24h %\n"
    "• `$market gainers [N]` -- biggest 24h winners\n"
    "• `$market losers [N]` -- biggest 24h losers\n"
    "• `$market trending` -- most-searched coins\n"
    "• `$market top [N]` -- top coins by market cap\n"
    "• `$market dom` -- MTA/ARC dominance bars\n"
    "• `$market global` -- total cap, volume, 24h delta\n"
    "• `$market convert <amt> <from> <to>` -- quick conversion\n\n"
    "Every subcommand has a short alias: `$fear`, `$heatmap`, `$gainers`, "
    "`$losers`, `$trending`, `$top`, `$dom`, `$global`, `$convert`."
)


async def handle_market(ctx: DiscoContext, raw_args: str, cog) -> None:
    parts = (raw_args or "").split(maxsplit=1)
    sub = (parts[0] or "").lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if not sub or sub in ("help", "h", "?"):
        embed = card("📈 $market", description=_HELP_TEXT, color=C_INFO).build()
        await ctx.reply(embed=embed, mention_author=False)
        return

    # Delegate to the existing handlers on the cog so we don't duplicate
    # any of their formatting / cache logic.
    if sub in ("fear", "fg", "feargreed", "greed"):
        await cog._handle_fear_greed(ctx)
        return
    if sub in ("heatmap", "hm", "hmap"):
        await cog._handle_heatmap(ctx, rest)
        return
    if sub in ("gainers", "winners"):
        await cog._handle_movers(ctx, rest, direction="gainers")
        return
    if sub in ("losers", "dumpers"):
        await cog._handle_movers(ctx, rest, direction="losers")
        return
    if sub in ("trending", "tr"):
        await cog._handle_trending(ctx, rest)
        return
    if sub in ("top", "t", "markets"):
        await cog._handle_top(ctx, rest)
        return
    if sub in ("dom", "dominance"):
        await cog._handle_dominance(ctx)
        return
    if sub in ("global", "g", "total", "overview"):
        await cog._handle_global(ctx, rest)
        return
    if sub in ("convert", "conv"):
        await cog._handle_convert(ctx, rest)
        return

    await ctx.reply_error_hint(
        f"Unknown `$market` subcommand `{sub}`.",
        hint="Try `$market help` for the full list.",
        command_name="$market",
    )
