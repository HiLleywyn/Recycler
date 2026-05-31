"""``$convert <amount> <from> <to>`` -- coin <-> coin or coin <-> USD."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD
from services.real_market import RealMarketError

from ._shared import _FOOTER_BRAND, _LIVE_PREFIX, _fmt_price_usd

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


async def handle(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    tokens = raw_args.split()
    if len(tokens) < 3:
        await ctx.reply_error_hint(
            "Usage: `$convert <amount> <from> <to>`.",
            hint="Try `$convert 1 MTA ARC`  ·  `$convert 100 USD SOL`  "
                 "·  `$convert 0.5 ARC USD`.",
            command_name="$convert",
        )
        return
    try:
        amount = float(tokens[0].replace(",", "").lstrip("$"))
    except ValueError:
        await ctx.reply_error("Amount must be a number, e.g. `$convert 1 MTA ARC`.")
        return
    if amount <= 0:
        await ctx.reply_error("Amount must be greater than zero.")
        return
    sym_from = tokens[1].upper()
    sym_to   = tokens[2].upper()
    if sym_from == sym_to:
        await ctx.reply_error("From and to symbols can't be the same.")
        return

    async with ctx.typing():
        ids: list[str] = []
        from_record = None
        to_record   = None
        if sym_from != "USD":
            from_record = await cog._resolve_or_error(
                ctx, sym_from, command_name="$convert",
            )
            if not from_record:
                return
            ids.append(from_record["id"])
        if sym_to != "USD":
            to_record = await cog._resolve_or_error(
                ctx, sym_to, command_name="$convert",
            )
            if not to_record:
                return
            ids.append(to_record["id"])

        try:
            prices = await cog.client.get_simple_price(ids)
        except RealMarketError as exc:
            log.warning("$convert prices failed: %s", exc)
            await ctx.reply_error(
                f"CoinGecko is temporarily unavailable (status {exc.status or '?'}). "
                "Try again in a moment."
            )
            return

    from_usd = 1.0 if sym_from == "USD" else prices.get(from_record["id"], 0.0)
    to_usd   = 1.0 if sym_to   == "USD" else prices.get(to_record["id"],   0.0)
    if from_usd <= 0 or to_usd <= 0:
        await ctx.reply_error(
            "Couldn't fetch a current USD price for one of the coins. "
            "Try again in a moment."
        )
        return

    usd_value = amount * from_usd
    out_amount = usd_value / to_usd
    rate = from_usd / to_usd

    from_label = "USD" if sym_from == "USD" else from_record["symbol"]
    to_label   = "USD" if sym_to   == "USD" else to_record["symbol"]

    def _amt(v: float, sym: str) -> str:
        return _fmt_price_usd(v) if sym == "USD" else f"{v:,.8g} {sym}"

    embed = (
        card(
            f"{_LIVE_PREFIX} 💱 {from_label} → {to_label}",
            description=(
                f"## {_amt(amount, from_label)}  =  **{_amt(out_amount, to_label)}**\n"
                f"≈ {_fmt_price_usd(usd_value)} USD value\n\n"
                f"📈 **Rate:** `1 {from_label} = "
                f"{_amt(rate, to_label)}`"
            ),
            color=C_GOLD,
        )
        .footer(
            f"{_FOOTER_BRAND} · cached "
            f"{Config.REAL_MARKET_CACHE_TTL_OVIEW}s · live USD reference"
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)
