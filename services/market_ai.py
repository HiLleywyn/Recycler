"""Market-side AI: ``$scan ... ai`` summaries and ``$query`` Q&A.

Two prompt modes wired to the existing ``core.framework.ai.client.complete``
plumbing:

- :data:`MARKET_SCAN_AI` -- annotates a structured :class:`ScanSnapshot`
  with probabilistic, source-grounded commentary. Never predicts prices.
- :data:`MARKET_QUERY` -- professional Q&A. **Strictly excludes** the
  player profile and game-state blocks the chat AI normally injects.

Both modes return :class:`MarketAIResult` with summary text, confidence
chip, and a sanitised citation list ready for the Sources button.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.config import Config

from services.market.ta import ScanSnapshot

log = logging.getLogger(__name__)


MARKET_SCAN_AI = "market_scan_ai"
MARKET_QUERY = "market_query"


# ── Citation sanitiser ────────────────────────────────────────────────
#
# Hard allowlist of gold-standard sources. URLs from any other domain are
# dropped before the Sources button is built. Designed to be conservative
# -- we'd rather omit a citation than surface a sketchy one.

_ALLOWED_DOMAINS: frozenset[str] = frozenset({
    # Regulators / official primary sources
    "sec.gov", "federalreserve.gov", "treasury.gov", "europa.eu",
    "ecb.europa.eu", "bankofengland.co.uk", "boj.or.jp",
    "imf.org", "worldbank.org", "bis.org",
    "bls.gov", "bea.gov", "census.gov",
    "investor.gov",
    # Premier financial press
    "bloomberg.com", "reuters.com", "ft.com", "wsj.com",
    "barrons.com", "marketwatch.com", "cnbc.com", "axios.com",
    "economist.com",
    # Market data providers (the ones we actually consume)
    "finance.yahoo.com", "yahoo.com",
    "coingecko.com", "coinmarketcap.com",
    "pyth.network",
    "redstone.finance",
    "switchboard.xyz",
    "dexscreener.com",
    "coinglass.com",
    "coinalyze.net",
    "finnhub.io",
    "tradingview.com",
    "chartscout.io",
    # Crypto / on-chain primary sources
    "etherscan.io", "solscan.io", "polygonscan.com",
    "arbiscan.io", "basescan.org", "snowtrace.io",
    "arcadia.org", "moneta.org",
    # Exchange announcements (often the source of record)
    "binance.com", "coinbase.com", "kraken.com", "bybit.com",
    "okx.com", "bitfinex.com", "gemini.com",
    "nasdaq.com", "nyse.com", "cmegroup.com",
    # Project foundations / docs
    "arcadia.foundation", "solana.com", "polkadot.network",
    "chainlink.gov",
})


def _domain(url: str) -> str:
    m = re.match(r"^https?://([^/]+)/?", url.strip(), re.IGNORECASE)
    if not m:
        return ""
    host = m.group(1).lower()
    # Strip ``www.`` and any leading subdomain that isn't part of the
    # trust comparison.
    if host.startswith("www."):
        host = host[4:]
    # Match on the registrable suffix.
    parts = host.split(".")
    if len(parts) >= 2:
        suffix = ".".join(parts[-2:])
        if suffix in _ALLOWED_DOMAINS:
            return suffix
        if len(parts) >= 3:
            tri = ".".join(parts[-3:])
            if tri in _ALLOWED_DOMAINS:
                return tri
    return host if host in _ALLOWED_DOMAINS else ""


def sanitize_citations(
    raw: list[dict[str, Any]] | None,
    *,
    max_items: int = 8,
) -> list[dict[str, str]]:
    """Filter ``raw`` (list of ``{title, url, provider?}``) to the trusted
    allowlist. Strips obvious junk (empty fields, shorteners, discord
    invites). Returns a deduplicated list ready for ``_SourcesView``.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        title = (entry.get("title") or "").strip()
        if not (url and title):
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        # Obvious bad patterns.
        low = url.lower()
        if "discord.gg" in low or "discord.com/invite" in low:
            continue
        if any(s in low for s in ("bit.ly", "tinyurl.com", "t.co/", "goo.gl")):
            continue
        if _domain(url) == "":
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "title": title[:100],
            "url": url,
            "provider": (entry.get("provider") or "").strip()[:32],
        })
        if len(out) >= max_items:
            break
    return out


# ── Prompt templates ──────────────────────────────────────────────────

_SCAN_SYSTEM = """You are a real-market technical analyst voice for a Discord bot.

You receive a STRUCTURED SCAN SNAPSHOT with indicator readings, pattern
matches, derivatives state, and an oracle quote. Your job:

1. Summarise WHAT THE DATA SHOWS in 2-4 short paragraphs.
2. Explain the SIGNIFICANCE of any detected chart pattern (what it
   typically implies, what would confirm or invalidate it).
3. Call out CONFLICTING signals plainly (e.g. "RSI is overbought but
   funding is negative -- mixed picture").
4. Provide a CONFIDENCE estimate as a single 0..1 float, where:
   - 0.8+ = data strongly aligns
   - 0.4-0.7 = mixed
   - 0.0-0.3 = thin sample / weak setup
5. Cite every numeric claim back to a provider name in brackets, e.g.
   ``RSI 71 [coingecko]`` or ``funding +0.012% [coinglass]``.

NEVER:
- Predict a target price or timeframe.
- Claim certainty about direction.
- Mention the Discoin economy, net worth, leaderboards, in-game tokens,
  staking, levelup, or any Discord-game-state. This is a real-market
  analysis surface.
- Tell users to buy or sell.

ALWAYS:
- Distinguish facts (snapshot fields) from interpretation (your read).
- Hedge with probabilistic language ("suggests", "consistent with",
  "would expect").
- Be concise. Discord embeds are read on mobile.

Output format:
```
SUMMARY: <2-3 sentences>
PATTERN: <if any, plain English>
CONFLICTS: <bullets, or 'none'>
CONFIDENCE: <float 0..1>
```
"""

_QUERY_SYSTEM = """You are a market-research assistant for Discoin's
``$query`` command. Tone: professional, precise, neutral.

ABSOLUTE RULES:
- You are answering REAL-WORLD market and finance questions only.
- You MUST NOT mention the Discoin game economy, the user's net worth,
  any in-game balance / wallet / leaderboard / quest / item, or any
  Discord-internal state. The game does not exist for the purposes of
  this command.
- You MUST NOT speculate about future prices or give buy/sell advice.
- For any specific numeric claim (price, market cap, percentage move,
  earnings figure, supply), you may ONLY cite values that appear
  verbatim in the ``LIVE MARKET CONTEXT`` block or in the
  ``WEB SEARCH RESULTS`` block of the user message. If neither block
  contains the value, you MUST NOT state a specific number -- say you
  don't have current data instead.
- You MUST NOT name-drop a source (e.g. "according to CoinMarketCap")
  unless that source's URL appears in the ``WEB SEARCH RESULTS`` block.
  Inventing a citation that wasn't in the input is a hard violation.

RESPONSE FORMAT:
- 2-5 short paragraphs.
- Plain text, no markdown headings.
- If you used a URL from the ``WEB SEARCH RESULTS``, end with a
  ``Sources:`` line listing those URLs (the bot also surfaces them
  ephemerally via the Sources button).

If you are uncertain, say so directly. If the question implies a
recommendation, decline the recommendation and provide neutral context.

If the user's question is unrelated to markets or finance, you may
politely answer in your normal professional voice -- but never reference
the game.
"""


# ── Result type ───────────────────────────────────────────────────────

@dataclass(slots=True)
class MarketAIResult:
    summary: str
    confidence: float = 0.0
    citations: list[dict[str, str]] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)
    mode: str = ""


# ── Public API ────────────────────────────────────────────────────────

async def run_scan_ai(
    snapshot: ScanSnapshot,
    *,
    user_id: int | None = None,
) -> MarketAIResult:
    """Run the ``ai`` modifier on a ``$scan`` snapshot."""
    if not getattr(Config, "OPENROUTER_API_KEY", ""):
        return MarketAIResult(
            summary=("AI mode is unavailable -- the bot host hasn't "
                     "configured an OPENROUTER_API_KEY."),
            confidence=0.0,
            disclaimers=["ai-unavailable"],
            mode=MARKET_SCAN_AI,
        )
    snap_json = snapshot.to_dict()
    user_msg = (
        "SCAN SNAPSHOT (JSON):\n"
        f"{_compact_json(snap_json)}\n\n"
        "Respond in the format described in the system message."
    )
    text = await _complete(
        system=_SCAN_SYSTEM,
        user=user_msg,
        user_id=user_id,
        max_tokens=480,
        temperature=0.4,
    )
    if not text:
        return MarketAIResult(
            summary="AI commentary failed -- upstream model unreachable.",
            confidence=0.0,
            disclaimers=["ai-failed"],
            mode=MARKET_SCAN_AI,
        )
    summary, conf = _split_summary(text)
    citations = sanitize_citations([
        {"title": f"{snapshot.symbol} snapshot ({p})",
         "url": _provider_homepage(p), "provider": p}
        for p in {snapshot.provider, *(_providers_in_snapshot(snapshot))}
        if p and _provider_homepage(p)
    ])
    return MarketAIResult(
        summary=summary,
        confidence=conf,
        citations=citations,
        disclaimers=["Not financial advice. Probabilistic reading only."],
        mode=MARKET_SCAN_AI,
    )


async def run_query(
    question: str,
    *,
    user_id: int | None = None,
    extra_context: str = "",
    web_search_results: list[dict[str, Any]] | None = None,
    have_live_data: bool | None = None,
) -> MarketAIResult:
    """Run a ``$query`` AI Q&A.

    ``extra_context`` is a system-fetched live-quote block prepended to
    the user message so the model has real numbers to work with.
    ``web_search_results`` are sanitised through the trusted-domain
    allowlist before being shown via the Sources button.

    ``have_live_data`` is the caller's signal that BOTH the web search
    AND the live-quote fetch came back empty -- when ``False`` we switch
    the system prompt into a strict mode that refuses to invent any
    numeric figures, so the model won't bluff with stale training data.
    """
    if not getattr(Config, "OPENROUTER_API_KEY", ""):
        return MarketAIResult(
            summary=("AI Q&A is unavailable -- the bot host hasn't "
                     "configured an OPENROUTER_API_KEY."),
            confidence=0.0,
            disclaimers=["ai-unavailable"],
            mode=MARKET_QUERY,
        )

    # Default: if the caller didn't tell us, infer from what was passed.
    if have_live_data is None:
        have_live_data = bool(web_search_results) or bool(extra_context)

    user_msg = question.strip()
    if extra_context:
        user_msg += (
            "\n\nLIVE MARKET CONTEXT (system-fetched via the router, "
            "treat as authoritative for the listed tickers):\n" + extra_context
        )
    if web_search_results:
        bullets = []
        for r in (web_search_results or [])[:6]:
            title = (r.get("title") or "").strip()[:140]
            url = (r.get("url") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            if snippet:
                bullets.append(f"- {title}: {snippet} [{url}]")
            else:
                bullets.append(f"- {title} [{url}]")
        user_msg += (
            "\n\nWEB SEARCH RESULTS (current; cite the URL when you use "
            "a fact from one of these):\n" + "\n".join(bullets)
        )

    if not have_live_data:
        # Strict mode: the caller couldn't fetch live data. Make the
        # model honest about its knowledge cutoff so it doesn't bluff
        # with stale numbers like "MTA market cap is $1.3T".
        user_msg += (
            "\n\nIMPORTANT: NO LIVE DATA WAS RETRIEVED for this question. "
            "Your training data has a cutoff and prices / market caps / "
            "earnings results / IPO dates from your training are likely "
            "STALE OR WRONG. You MUST NOT state specific prices, market "
            "caps, percentage moves, supply numbers, or any other numeric "
            "figure as if it were current. If a numeric answer is "
            "required, say plainly that you can't access live data right "
            "now and suggest the user retry the command. You may still "
            "answer non-numeric / conceptual questions normally."
        )

    system_prompt = _QUERY_SYSTEM
    if not have_live_data:
        system_prompt += (
            "\n\nADDITIONAL RULE (no live data): Treat your training "
            "data as untrustworthy for any specific price, market cap, "
            "percentage move, earnings figure, or other dated numeric "
            "claim. Do not state these as facts. Either decline to "
            "answer numerically or hedge explicitly ('I don't have "
            "current data, but historically...')."
        )

    text = await _complete(
        system=system_prompt,
        user=user_msg,
        user_id=user_id,
        max_tokens=640,
        temperature=0.4 if have_live_data else 0.2,
    )
    if not text:
        return MarketAIResult(
            summary="The model didn't respond. Please try again.",
            confidence=0.0,
            disclaimers=["ai-failed"],
            mode=MARKET_QUERY,
        )

    citations = sanitize_citations(web_search_results or [])
    disclaimers = ["Not financial advice. Information is for research only."]
    confidence = 0.7 if have_live_data else 0.2
    if not have_live_data:
        disclaimers.insert(
            0,
            "Live web search and quote fetch both returned no results -- "
            "this answer is from model training only and may be out of date.",
        )

    return MarketAIResult(
        summary=text.strip(),
        confidence=confidence,
        citations=citations,
        disclaimers=disclaimers,
        mode=MARKET_QUERY,
    )


# ── Helpers ───────────────────────────────────────────────────────────

async def _complete(
    *,
    system: str,
    user: str,
    user_id: int | None = None,
    max_tokens: int = 512,
    temperature: float = 0.5,
) -> str | None:
    try:
        from core.framework.ai.client import complete
    except Exception as exc:
        log.warning("market_ai: cannot import ai client: %s", exc)
        return None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        return await complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            user_id=user_id,
            kind="system",
        )
    except Exception:
        log.exception("market_ai: completion crashed")
        return None


def _compact_json(payload: Any) -> str:
    import json
    try:
        return json.dumps(payload, separators=(",", ":"), default=str)[:6000]
    except Exception:
        return str(payload)[:6000]


_CONF_RE = re.compile(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def _split_summary(text: str) -> tuple[str, float]:
    conf = 0.5
    m = _CONF_RE.search(text)
    if m:
        try:
            conf = max(0.0, min(1.0, float(m.group(1))))
        except ValueError:
            pass
    return text.strip(), conf


def _providers_in_snapshot(snap: ScanSnapshot) -> list[str]:
    out: list[str] = []
    if snap.derivatives is not None:
        out.append("coinglass")
    if snap.oracle is not None and snap.oracle.provider_count:
        out.append("pyth")
    return out


_PROVIDER_HOMEPAGE: dict[str, str] = {
    "coingecko": "https://www.coingecko.com/",
    "yahoo": "https://finance.yahoo.com/",
    "finnhub": "https://finnhub.io/",
    "dexscreener": "https://dexscreener.com/",
    "pyth": "https://pyth.network/",
    "redstone": "https://redstone.finance/",
    "switchboard": "https://switchboard.xyz/",
    "coinglass": "https://www.coinglass.com/",
    "coinalyze": "https://coinalyze.net/",
    "tradingview": "https://www.tradingview.com/",
}


def _provider_homepage(name: str) -> str:
    return _PROVIDER_HOMEPAGE.get(name, "")
