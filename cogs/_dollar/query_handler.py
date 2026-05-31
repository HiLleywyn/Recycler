"""``$query`` -- professional AI market Q&A.

Strict rules (enforced by the system prompt + the gate below):

- Never references the Discoin game economy, net worth, leaderboards,
  in-game tokens, items, or any Discord-internal state.
- Replies in a professional voice. Hedges any directional statement.
- Surfaces citations through the existing Sources button (ephemeral).
- Calls ``data.web_search`` for current facts and seeds the prompt
  with live quotes from the market router for any tickers detected
  in the question so the model isn't relying on stale training data.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO, C_NEUTRAL

from services.market_ai import run_query

from .views import make_sources_button

log = logging.getLogger(__name__)

_MAX_QUESTION_LEN = 600

# Quick-pattern ticker extractor. Catches obvious uppercase tickers in
# the question (MTA, ARC, MSFT, AAPL, SPY, GOLD, XAU, ...) so we can
# pre-fetch live quotes via the market router. Conservative: 2-5 caps
# with a word boundary, ignores common English words via a stoplist.
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_TICKER_STOPLIST: frozenset[str] = frozenset({
    "I", "A", "AI", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN",
    "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "AM", "PM", "OK", "OS", "UK", "EU", "UN", "PHD", "CEO", "CFO",
    "CTO", "API", "CSV", "PDF", "URL", "HTTP", "HTTPS", "JSON", "FAQ",
    "TBD", "TLDR", "ETF", "IPO", "ICO", "DCA", "ATH", "ATL", "RSI", "MACD",
    "OHLC", "USD", "EUR", "GBP", "JPY", "AND", "BUT", "FOR", "NOT", "THE",
    "YOU", "ARE", "WAS", "HAVE", "HAS", "HAD", "WHO", "WHAT", "WHEN",
    "WHERE", "WHY", "HOW", "ALL", "ANY", "WILL", "WITH", "FROM", "INTO",
    "OVER", "THAN", "THEN", "THIS", "THAT", "ONLY", "JUST", "ALSO",
    "VERY", "EACH", "BOTH", "SOME", "MOST", "MUCH", "MORE", "MANY",
    "TLDR", "LIST", "GIVE", "WRITE", "TELL", "SHOW", "FIND", "LOOK",
    "PLEASE", "THANKS",
})


async def handle_query(ctx: DiscoContext, raw_args: str) -> None:
    question = (raw_args or "").strip()
    if not question:
        await ctx.reply_error_hint(
            "Tell me what to look up.",
            hint=(
                "`$query <question>`\n"
                "Examples:\n"
                "• `$query recent earnings results for Firefly Aerospace`\n"
                "• `$query upcoming IPOs and ICOs in the next 30 days`\n"
                "• `$query how did ARC move vs MTA in the last 72 hours`\n"
                "• `$query what is heikin ashi and when is it useful`"
            ),
            command_name="$query",
        )
        return

    if len(question) > _MAX_QUESTION_LEN:
        await ctx.reply_error(
            f"Question too long ({len(question)} chars; max {_MAX_QUESTION_LEN}).",
        )
        return

    if not getattr(Config, "MARKET_AI_ENABLED", False):
        await ctx.reply_error_hint(
            "`$query` is disabled on this deployment.",
            hint="The bot host hasn't enabled MARKET_AI_ENABLED / OPENROUTER_API_KEY.",
            command_name="$query",
        )
        return

    placeholder = await ctx.reply(
        embed=card(
            "🧠 $query · thinking…",
            description=f"> {question[:300]}",
            color=C_NEUTRAL,
        ).footer("Searching, fetching live quotes, asking the model.").build(),
        mention_author=False,
    )

    try:
        web_results = await _run_web_search(ctx, question)
    except Exception:
        log.exception("[$query] web-search step crashed (caught)")
        web_results = []
    try:
        extra_context = await _fetch_live_market_context(ctx, question)
    except Exception:
        log.exception("[$query] live-context step crashed (caught)")
        extra_context = ""
    have_live_data = bool(web_results) or bool(extra_context)

    result = await run_query(
        question,
        user_id=ctx.author.id,
        web_search_results=web_results,
        extra_context=extra_context,
        have_live_data=have_live_data,
    )

    desc = result.summary[:3800] if result.summary else "(no response)"

    embed = (
        card(
            "🧠 $query",
            description=desc,
            color=C_INFO,
        )
        .field(
            "Question",
            question if len(question) <= 1020 else question[:1017] + "…",
            False,
        )
    )
    chips: list[str] = []
    if web_results:
        chips.append(f"🌐 {len(web_results)} web result{'s' if len(web_results) != 1 else ''}")
    if extra_context:
        chips.append("💹 live quotes")
    if not have_live_data:
        chips.append("⚠️ no live data — answer is from model training only")
    chips.append(f"confidence {result.confidence * 100:.0f}%")
    embed.footer("AI Q&A · not financial advice · " + " · ".join(chips))

    view = make_sources_button(result.citations, ctx.author.id)

    try:
        if placeholder is not None:
            await placeholder.edit(
                embed=embed.build(),
                view=view if view is not None else discord.utils.MISSING,
            )
            return
    except Exception:
        log.debug("[$query] failed to edit placeholder, sending fresh reply", exc_info=True)
    await ctx.reply(embed=embed.build(), view=view, mention_author=False)


# ── Web search ───────────────────────────────────────────────────────────

async def _run_web_search(ctx: DiscoContext, question: str) -> list[dict[str, Any]]:
    """Invoke the real ``data.web_search`` agent tool and unpack its
    ``{title, url, snippet}`` rows. Returns ``[]`` on any failure so
    the caller can fall through cleanly."""
    try:
        from core.framework.agent_tools.core import ToolContext
        from core.framework.agent_tools.tools.data import web_search as web_search_tool
    except Exception:
        log.debug("[$query] web_search tool not importable", exc_info=True)
        return []

    bus = getattr(ctx.bot, "bus", None)
    # ToolContext.db is the bot's DB wrapper -- DiscoContext doesn't
    # carry one directly, only the bot does. Same for the bus.
    db = getattr(ctx.bot, "db", None)
    tool_ctx = ToolContext(
        user_id=ctx.author.id,
        guild_id=ctx.guild_id,
        db=db,
        bus=bus,
        actor="user",
    )
    try:
        result = await web_search_tool(tool_ctx, {"query": question, "max_results": 6})
    except Exception:
        log.exception("[$query] web_search crashed")
        return []
    if not getattr(result, "ok", False):
        log.debug(
            "[$query] web_search failed: %s",
            getattr(result, "error", "unknown"),
        )
        return []
    rows = ((result.data or {}).get("results") or []) if isinstance(result.data, dict) else []
    if not isinstance(rows, list):
        return []
    return [
        {
            "title": (r.get("title") or "").strip()[:140],
            "url":   (r.get("url") or "").strip(),
            "snippet": (r.get("snippet") or "").strip()[:240],
        }
        for r in rows
        if isinstance(r, dict) and (r.get("url") or "").strip()
    ]


# ── Live market data seeding ─────────────────────────────────────────────

async def _fetch_live_market_context(ctx: DiscoContext, question: str) -> str:
    """Detect tickers in the question and fetch fresh quotes via the
    market router. Returns a plain-text block that ``run_query`` prepends
    to the user message so the model has real numbers to work with
    instead of leaning on stale training data."""
    tickers = _extract_tickers(question)
    if not tickers:
        return ""
    try:
        from services.market.router import get_router
        router = get_router(ctx.bot)
    except Exception:
        return ""

    lines: list[str] = []
    for sym in tickers[:6]:
        try:
            resolved = await router.resolve(sym)
            if resolved is None:
                continue
            quote = await router.quote(resolved)
        except Exception:
            log.debug("[$query] live fetch failed for %s", sym, exc_info=True)
            continue
        if quote is None or not quote.price_usd:
            continue
        chip = f"- {resolved.symbol} ({resolved.asset_class.value}): ${quote.price_usd:,.2f}"
        if quote.pct_24h is not None:
            chip += f" ({quote.pct_24h:+.2f}% 24h)"
        if quote.market_cap_usd:
            chip += f" · market cap ${quote.market_cap_usd / 1e9:.2f}B"
        chip += f" · via {quote.provider}"
        lines.append(chip)
    if not lines:
        return ""
    import time
    return (
        "Fresh quotes pulled at " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()) + ":\n"
        + "\n".join(lines)
    )


def _extract_tickers(question: str) -> list[str]:
    """Pull plausible ticker symbols (2-5 uppercase letters) out of the
    question, dropping the English-stoplist false positives. Order
    preserved; deduped."""
    if not question:
        return []
    # Also catch lowercase-typed tickers like "mta" / "msft" by checking
    # both the original and an uppercased copy.
    candidates: list[str] = []
    seen: set[str] = set()
    # Upper-case matches (MTA, ARC, MSFT) -- highest signal.
    for token in _TICKER_RE.findall(question or ""):
        if token in _TICKER_STOPLIST:
            continue
        if token in seen:
            continue
        seen.add(token)
        candidates.append(token)
    # Catch lowercase tickers too, but only if they look like a
    # standalone word and aren't in the stoplist.
    for word in re.findall(r"\b([a-z]{3,5})\b", question or ""):
        up = word.upper()
        if up in _TICKER_STOPLIST or up in seen:
            continue
        # Only accept lowercase candidates that look like known crypto /
        # equity tickers -- conservative so we don't flag random words.
        if up in _COMMON_TICKERS:
            seen.add(up)
            candidates.append(up)
    return candidates


# Conservative list of common tickers we'll auto-detect even when the
# user typed them in lowercase. Not exhaustive -- just enough to catch
# the obvious "$query how is mta doing".
_COMMON_TICKERS: frozenset[str] = frozenset({
    # Crypto majors
    "MTA", "ARC", "SOL", "DOGE", "XRP", "ADA", "BNB", "MATIC", "AVAX",
    "LINK", "DOT", "ATOM", "LTC", "BCH", "SHIB", "STR", "TRX", "TON",
    "NEAR", "ARB", "OP", "SUI", "APT", "INJ", "TIA", "SEI", "KAS",
    # Equities + ETFs
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "GOOGL", "GOOG",
    "AMZN", "SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "HOOD", "COIN",
    "PLTR", "MARA", "RIOT",
    # Forex pairs (single-side mentions)
    "EUR", "GBP", "JPY", "CAD", "AUD", "CHF",
    # Commodities
    "XAU", "XAG", "WTI", "BRENT", "GOLD", "SILVER",
})
