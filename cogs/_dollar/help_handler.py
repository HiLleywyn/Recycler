"""Tour-style ``$help``.

Compact multi-field embed; pairs with the ``,help realmarket`` category
inside :mod:`cogs.help` for the long-form reference card. The two
surfaces are kept in sync deliberately.
"""

from __future__ import annotations

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_GOLD


async def handle_help_v2(ctx: DiscoContext) -> None:
    embed = (
        card(
            "📡 $help · cross-asset market platform",
            description=(
                "Live cross-asset market data in a dedicated `$` namespace "
                "-- fully separate from the simulated in-game market under "
                "`,chart` / `,trade`. The `$` namespace is prefix-only and "
                "contributes zero slash commands by design."
            ),
            color=C_GOLD,
        )
        .field(
            "📊 Charts & technical analysis",
            "`$chart SYMBOL [tf] [flags...]` -- candle PNG with indicator "
            "overlays.\n"
            "`$scan SYMBOL [tf]` -- pattern + indicator scan.\n"
            "`$scan SYMBOL [tf] ai` -- append `ai` for a probabilistic "
            "AI commentary follow-up with a Sources button.",
            False,
        )
        .field(
            "💹 $info SYMBOL · auto-detects asset class",
            "Crypto -> price + 1h/24h/7d/30d + cap + ATH/ATL + news.\n"
            "Stocks/ETFs -> price + P/E + EPS + 52w range + next earnings.\n"
            "Perps (MTA/ARC/SOL...) -> adds oracle + funding + open "
            "interest panels when CoinGlass / Pyth are reachable.",
            False,
        )
        .field(
            "📈 Market-wide intel ($market <sub>)",
            "`fear` `heatmap [N]` `gainers [N]` `losers [N]` `trending` "
            "`top [N]` `dom` `global` `convert <amt> <from> <to>`\n"
            "Short aliases (`$fear`, `$heatmap`, `$gainers`, `$losers`, "
            "`$trending`, `$top`, `$dom`, `$global`, `$convert`) keep working.",
            False,
        )
        .field(
            "⚖️ Cross-asset + derivatives + oracle",
            "`$compare MTA SPY` -- normalised view across 2-4 symbols.\n"
            "`$oracle SOL` -- Pyth + RedStone + Switchboard medianised "
            "quote with confidence, divergence + stale flags.\n"
            "`$funding MTA` / `$oi MTA` -- perp funding rate + OI by "
            "exchange.",
            False,
        )
        .field(
            "👁️ $watch · personal alerts",
            "`$watch add MTA 75000 above` (works on equities too).\n"
            "`$watch list` / `$watch remove SYMBOL` / `$watch clear`.\n"
            "Background worker checks every ~60s; one-shot ping when the "
            "threshold trips. Re-add to re-arm.",
            False,
        )
        .field(
            "🧠 $query · professional AI Q&A",
            "`$query <natural-language question>` -- market research voice, "
            "never references the game. Surfaces a **Sources** button "
            "(ephemeral) carrying only trusted-domain citations "
            "(sec.gov, reuters.com, bloomberg.com, pyth.network, "
            "finance.yahoo.com, coingecko.com, ...). Sketchy / out-of-date "
            "domains are dropped before the button is built.",
            False,
        )
        .field(
            "🕐 Timeframes",
            "`1s` `5s` `15s` `30s` `1m` `3m` `5m` `15m` `30m` `45m` `1h` "
            "`2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1mo` `3mo` `6mo` "
            "`1y` `all`",
            False,
        )
        .field(
            "🔌 Provider stack",
            "CoinGecko · Yahoo Finance · Finnhub · DexScreener · Pyth "
            "Hermes · RedStone · Switchboard (Crossbar) · CoinGlass · "
            "Coinalyze · TradingView UDF (the bot hosts its own at "
            "`/api/v2/udf`). Every provider is optional -- the router "
            "skips disabled / unhealthy ones.",
            False,
        )
        .field(
            "🩺 $status -- live provider health",
            "`$status` probes every provider with a quote against a "
            "canary symbol and reports back: 🟢 healthy / 🟡 degraded / "
            "🔴 down / ⚪ disabled, plus Redis cache, AI gate, and the "
            "UDF bridge. Use it when something looks off in a chart "
            "embed -- a 🔴 here tells you exactly which upstream is "
            "the culprit.",
            False,
        )
        .footer("Real markets. Trusted providers. No game state.")
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)
