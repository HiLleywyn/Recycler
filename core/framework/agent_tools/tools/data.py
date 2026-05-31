"""
core/framework/agent_tools/tools/data.py -- data access tools.

Three powerful tools for external / structured data:

    data.web_fetch      fetch a URL, strip HTML, return clean text + title.
    data.api_call       generic REST caller with an allowlist of hosts.
    data.db_query       allowlisted read-only SQL via named query templates.

Design notes:
  - Every tool returns structured JSON, never a raw text blob.
  - Web fetching is length-capped and HTML-stripped before return.
  - The API caller uses a strict allowlist; there is no "arbitrary host" path.
  - The DB query tool does NOT accept raw SQL. It only accepts a known query
    key whose template is defined here (QUERY_TEMPLATES). This eliminates
    prompt injection paths like DROP TABLE or cross-guild leakage.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp

from core.config import Config
from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.data")


# ── Allowlists ───────────────────────────────────────────────────────────────

# Hosts the api_call tool may hit. Never a wildcard.
_API_HOST_ALLOWLIST = {
    "api.coingecko.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "api.coincap.io",
    "min-api.cryptocompare.com",
    "api.alternative.me",
    "wttr.in",
}

# Hosts the web_fetch tool may retrieve. Broader than the API list because
# text scraping is lower stakes, but still no "anywhere" wildcard.
_WEB_HOST_ALLOWLIST = {
    "en.wikipedia.org",
    "news.ycombinator.com",
    "github.com",
    "raw.githubusercontent.com",
    "api.coingecko.com",
    "wttr.in",
    "html.duckduckgo.com",
    "lite.duckduckgo.com",
    "duckduckgo.com",
}

# Max body size pulled by web_fetch (bytes), clean text truncated to this char count.
_WEB_MAX_BYTES = 150_000
_WEB_MAX_CHARS = 4_000

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=4, sock_read=8)
# OpenRouter online models (e.g. perplexity/sonar) do live web retrieval
# before generating a response. 10 s is consistently too short -- raise to
# match the main AI client's non-streaming budget.
_SEARCH_OPENROUTER_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=6, sock_read=40)
# DDG HTML scraping can be sluggish when the endpoint rate-limits or the
# upstream is slow. 15 s gives enough headroom without blocking the loop.
_SEARCH_DDG_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=12)

# Regex to extract bare URLs from AI-generated text when no structured
# citations are returned by the search backend.
_URL_RE = re.compile(r'https?://[^\s\)\]\>"\'<,]+')


def _extract_urls_from_text(text: str, limit: int) -> list[str]:
    """Return up to *limit* URLs found in *text*, deduped, order-preserved."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".")
        if u not in seen:
            seen.add(u)
            urls.append(u)
            if len(urls) >= limit:
                break
    return urls


# ── data.web_fetch ───────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str = ""
        self._in_title = False
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)


def _strip_html(body: str) -> tuple[str, str]:
    parser = _HTMLStripper()
    try:
        parser.feed(body)
    except Exception:
        return "", ""
    text = "".join(parser.parts)
    text = re.sub(r"\s+", " ", text).strip()
    return parser.title.strip(), text


@tool(
    name="data.web_fetch",
    summary=(
        "Fetch a URL and return a clean extraction: title + stripped text "
        "(no HTML, no script blocks). Host must be in the allowlist."
    ),
    risk=RiskLevel.READ,
    category="data",
    cooldown_s=2,
    params=[
        ParamSpec("url", "str", description="HTTPS URL to fetch."),
        ParamSpec("max_chars", "int", required=False, default=_WEB_MAX_CHARS,
                  min=200, max=_WEB_MAX_CHARS,
                  description="Truncate extracted text to this many characters."),
    ],
)
async def web_fetch(ctx: ToolContext, args: dict) -> ToolResult:
    url = args["url"]
    if not url.startswith("https://"):
        return ToolResult.fail("url must use https://")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.hostname not in _WEB_HOST_ALLOWLIST:
        return ToolResult.fail(
            f"host_not_allowed: {parsed.hostname} not in web allowlist"
        )
    max_chars = int(args.get("max_chars") or _WEB_MAX_CHARS)

    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as sess:
            async with sess.get(url, headers={"User-Agent": "Discoin-Agent/1.0"}) as r:
                if r.status != 200:
                    return ToolResult.fail(f"http_{r.status}")
                body = await r.content.read(_WEB_MAX_BYTES)
                text = body.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_fetch] %s", exc)
        return ToolResult.fail(f"fetch_error: {type(exc).__name__}")

    title, clean = _strip_html(text)
    return ToolResult.success({
        "url": url,
        "host": parsed.hostname,
        "title": title[:200],
        "text": clean[:max_chars],
        "truncated": len(clean) > max_chars,
    })


# ── data.web_search ──────────────────────────────────────────────────────────
#
# Real keyword search via DuckDuckGo's HTML endpoint. The AI could technically
# compose a duckduckgo URL and call data.web_fetch, but giving it a first-class
# search verb (1) makes the intent explicit in audits and (2) lets us parse the
# results page into structured rows instead of a wall of stripped text.

# Cap on parsed results returned to the model. The page has ~30 links; more
# than ~10 starts being useless for an LLM answer.
_SEARCH_MAX_RESULTS = 10
_SEARCH_BODY_LIMIT = 300_000

# DDG wraps its result URLs in a redirect of the form
# ``//duckduckgo.com/l/?uddg=<url-encoded target>&...``. We unwrap it so the
# model sees real destinations instead of DDG bounce URLs.
_DDG_REDIRECT_RE = re.compile(r"/l/\?.*?uddg=([^&]+)")


def _unwrap_ddg_redirect(href: str) -> str:
    """Turn a DDG ``/l/?uddg=...`` bounce URL into the real destination."""
    if not href:
        return href
    # Normalise protocol-relative URLs so urlparse can read the query string.
    raw = href
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return href
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.endswith("/l/"):
        qs = parse_qs(parsed.query)
        target = (qs.get("uddg") or [""])[0]
        if target:
            return unquote(target)
    # Some older DDG responses embed it directly in the href without a query
    # parser-friendly form; fall back to the regex.
    m = _DDG_REDIRECT_RE.search(href)
    if m:
        return unquote(m.group(1))
    return href


class _DDGResultParser(HTMLParser):
    """Extract ``(title, url, snippet)`` tuples from a DDG HTML results page.

    The DDG HTML endpoint marks each result with
    ``<a class="result__a" href="...">Title</a>`` followed later by
    ``<a class="result__snippet">snippet text</a>``. We accumulate until we
    have ``_SEARCH_MAX_RESULTS`` complete rows.
    """

    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max = max_results
        self.results: list[dict] = []
        self._mode: str | None = None  # "title" | "snippet" | None
        self._cur_title_parts: list[str] = []
        self._cur_url: str = ""
        self._cur_snippet_parts: list[str] = []

    def _has_class(self, attrs: list[tuple[str, str | None]], target: str) -> bool:
        for k, v in attrs:
            if k == "class" and v and target in v.split():
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.results) >= self.max:
            return
        if tag != "a":
            return
        if self._has_class(attrs, "result__a"):
            href = ""
            for k, v in attrs:
                if k == "href" and v:
                    href = v
                    break
            self._cur_url = _unwrap_ddg_redirect(href)
            self._cur_title_parts = []
            self._mode = "title"
        elif self._has_class(attrs, "result__snippet"):
            self._cur_snippet_parts = []
            self._mode = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._mode is None:
            return
        if self._mode == "title":
            # A title on its own is not yet a complete row; wait for the
            # snippet that should follow, but keep the url/title cached.
            pass
        elif self._mode == "snippet":
            title = re.sub(r"\s+", " ", "".join(self._cur_title_parts)).strip()
            snippet = re.sub(r"\s+", " ", "".join(self._cur_snippet_parts)).strip()
            if title and self._cur_url:
                self.results.append({
                    "title": title[:200],
                    "url": self._cur_url[:500],
                    "snippet": snippet[:400],
                })
            self._cur_title_parts = []
            self._cur_snippet_parts = []
            self._cur_url = ""
        self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode == "title":
            self._cur_title_parts.append(data)
        elif self._mode == "snippet":
            self._cur_snippet_parts.append(data)


# ── OpenRouter search backend ─────────────────────────────────────────────────
#
# When SEARCH_BACKEND=openrouter, route queries through OpenRouter using
# SEARCH_MODEL (default: perplexity/sonar).  Perplexity's online models have
# live web access built-in; the response includes a summary and a citations
# list.  Other OpenRouter models that support retrieval also work here.
#
# Response shape we normalise to matches the DDG output:
#   {title, url, snippet}  per result row
# The Perplexity summary becomes the first row; each citation becomes a
# follow-up row the AI can pass to data.web_fetch for the full page.

async def _web_search_openrouter(
    query: str, max_results: int, model: str | None = None
) -> ToolResult:
    key = Config.OPENROUTER_API_KEY
    if not key:
        log.warning("[data.web_search/openrouter] OPENROUTER_API_KEY not set, falling back to DDG")
        return ToolResult.fail("openrouter_key_missing")

    model = model or Config.SEARCH_MODEL or "perplexity/sonar"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 1024,
        "temperature": 0.1,
    }
    try:
        async with aiohttp.ClientSession(timeout=_SEARCH_OPENROUTER_TIMEOUT) as sess:
            async with sess.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://econbot",
                    "X-Title": "Discoin",
                },
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.info("[data.web_search/openrouter] HTTP %s: %.200s", r.status, body)
                    return ToolResult.fail(f"search_http_{r.status}")
                data = await r.json()
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_search/openrouter] %s", exc)
        return ToolResult.fail(f"search_error: {type(exc).__name__}")

    choices = data.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    answer = (message.get("content") or "").strip()
    # Perplexity passes citations as a list of URL strings on the message object.
    # Other providers may omit this field; default to empty list.
    citations: list[str] = message.get("citations") or []

    if not answer:
        return ToolResult.fail("empty_response")

    # Fall back to URLs extracted from the answer text when the model
    # returns no structured citations.
    if not citations:
        citations = _extract_urls_from_text(answer, max_results - 1)

    results: list[dict] = []
    # First row: the model's synthesised answer as a readable snippet.
    # Use the first citation as the canonical URL for the summary row so the
    # Sources button always has at least one clickable link when citations exist.
    results.append({
        "title": f"{query[:60]} (AI summary)",
        "url": citations[0] if citations else "",
        "snippet": answer[:600],
    })
    # Subsequent rows: each citation URL the caller can web_fetch for detail.
    for i, cite_url in enumerate(citations[1:max_results - 1]):
        results.append({
            "title": f"Source {i + 2}",
            "url": cite_url,
            "snippet": "",
        })

    return ToolResult.success({
        "query": query,
        "backend": model,
        "result_count": len(results),
        "results": results[:max_results],
    })


# ── Brave Search backend ──────────────────────────────────────────────────────
#
# Calls api.search.brave.com for raw engine results (no AI summary).  Brave
# returns structured rows under data.web.results so we map directly to our
# {title, url, snippet} shape without any LLM in the loop -- this makes it the
# preferred backend when callers want sources rather than a synthesised answer.

_SEARCH_BRAVE_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=12)


async def _web_search_brave(query: str, max_results: int) -> ToolResult:
    key = Config.BRAVE_SEARCH_API_KEY
    if not key:
        log.warning("[data.web_search/brave] BRAVE_SEARCH_API_KEY not set")
        return ToolResult.fail("brave_key_missing")

    # Brave's `count` accepts 1-20.  Clamp to our own _SEARCH_MAX_RESULTS so
    # the engine doesn't return more rows than we'll surface to the agent.
    count = max(1, min(int(max_results), _SEARCH_MAX_RESULTS))
    params = {"q": query, "count": str(count)}
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }
    try:
        async with aiohttp.ClientSession(timeout=_SEARCH_BRAVE_TIMEOUT) as sess:
            async with sess.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers=headers,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.info("[data.web_search/brave] HTTP %s: %.200s", r.status, body)
                    return ToolResult.fail(f"search_http_{r.status}")
                data = await r.json()
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_search/brave] %s", exc)
        return ToolResult.fail(f"search_error: {type(exc).__name__}")

    web = (data.get("web") or {}) if isinstance(data, dict) else {}
    rows = web.get("results") or []
    if not rows:
        return ToolResult.fail("empty_response")

    results: list[dict] = []
    for row in rows[:max_results]:
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        snippet = (row.get("description") or row.get("snippet") or "").strip()
        if not url or not title:
            continue
        results.append({
            "title": title[:200],
            "url": url[:500],
            "snippet": snippet[:400],
        })

    if not results:
        return ToolResult.fail("empty_response")

    return ToolResult.success({
        "query": query,
        "backend": "brave",
        "result_count": len(results),
        "results": results,
    })


# ── Perplexity direct backend ──────────────────────────────────────────────────
#
# Calls api.perplexity.ai directly instead of routing through OpenRouter.
# Response shape is identical to the OpenRouter path (Perplexity is also
# OpenAI-compat) so we normalise to the same {title, url, snippet} rows.

async def _web_search_perplexity(
    query: str, max_results: int, model: str | None = None
) -> ToolResult:
    key = Config.PERPLEXITY_API_KEY
    if not key:
        log.warning("[data.web_search/perplexity] PERPLEXITY_API_KEY not set")
        return ToolResult.fail("perplexity_key_missing")

    # Perplexity model IDs drop the "perplexity/" org prefix used on OpenRouter.
    raw_model = model or Config.SEARCH_MODEL or "sonar"
    model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 1024,
        "temperature": 0.1,
    }
    try:
        async with aiohttp.ClientSession(timeout=_SEARCH_OPENROUTER_TIMEOUT) as sess:
            async with sess.post(
                "https://api.perplexity.ai/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.info("[data.web_search/perplexity] HTTP %s: %.200s", r.status, body)
                    return ToolResult.fail(f"search_http_{r.status}")
                data = await r.json()
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_search/perplexity] %s", exc)
        return ToolResult.fail(f"search_error: {type(exc).__name__}")

    choices = data.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    answer = (message.get("content") or "").strip()
    citations: list[str] = message.get("citations") or []

    if not answer:
        return ToolResult.fail("empty_response")

    # Fall back to URLs extracted from the answer text when the model
    # returns no structured citations.
    if not citations:
        citations = _extract_urls_from_text(answer, max_results - 1)

    results: list[dict] = [{
        "title": f"{query[:60]} (summary)",
        "url": citations[0] if citations else "",
        "snippet": answer[:600],
    }]
    for i, cite_url in enumerate(citations[1:max_results - 1]):
        results.append({"title": f"Source {i + 2}", "url": cite_url, "snippet": ""})

    return ToolResult.success({
        "query": query,
        "backend": model,
        "result_count": len(results),
        "results": results[:max_results],
    })


# ── Ollama search backend ──────────────────────────────────────────────────────
#
# Routes the query through the configured Ollama endpoint. Useful when running
# a local model that has live web access (e.g. backed by Searxng or a retrieval
# plugin). Strips the org prefix from SEARCH_MODEL so "perplexity/sonar" becomes
# "sonar" -- Ollama uses plain model names without provider namespacing.

async def _web_search_ollama(
    query: str, max_results: int, model: str | None = None
) -> ToolResult:
    base_url = os.getenv("OLLAMA_BASE_URL", "").rstrip("/")
    if not base_url:
        log.warning("[data.web_search/ollama] OLLAMA_BASE_URL not set")
        return ToolResult.fail("ollama_base_url_not_set")

    endpoint = (
        f"{base_url}/chat/completions"
        if base_url.endswith("/v1")
        else f"{base_url}/v1/chat/completions"
    )
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    raw_model = model or Config.SEARCH_MODEL or Config.TOOLS_MODEL or "llama3.2"
    model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise web search assistant. Answer the user query with "
                    "factual, up-to-date information. If you have access to real-time web "
                    "data, use it. Include source URLs when available."
                ),
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
    }
    try:
        async with aiohttp.ClientSession(timeout=_SEARCH_OPENROUTER_TIMEOUT) as sess:
            async with sess.post(endpoint, json=payload, headers=headers) as r:
                if r.status != 200:
                    body = await r.text()
                    log.info("[data.web_search/ollama] HTTP %s: %.200s", r.status, body)
                    return ToolResult.fail(f"search_http_{r.status}")
                data = await r.json()
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_search/ollama] %s", exc)
        return ToolResult.fail(f"search_error: {type(exc).__name__}")

    choices = data.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    answer = (message.get("content") or "").strip()

    if not answer:
        return ToolResult.fail("empty_response")

    # Extract any URLs the local model mentioned in its answer.
    extracted = _extract_urls_from_text(answer, _SEARCH_MAX_RESULTS - 1)
    results: list[dict] = [{"title": f"{query[:60]} (local)", "url": extracted[0] if extracted else "", "snippet": answer[:600]}]
    for i, cite_url in enumerate(extracted[1:]):
        results.append({"title": f"Source {i + 2}", "url": cite_url, "snippet": ""})

    return ToolResult.success({
        "query": query,
        "backend": model,
        "result_count": len(results),
        "results": results,
    })


@tool(
    name="data.web_search",
    summary=(
        "Search the public web and return the top results as a list of "
        "{title, url, snippet}. Use this when the player asks about real-world "
        "news, prices, docs, or anything that isn't stored in the game database. "
        "Follow up with data.web_fetch on a promising result URL if you need the "
        "full page text."
    ),
    risk=RiskLevel.READ,
    category="data",
    cooldown_s=3,
    params=[
        ParamSpec(
            "query", "str",
            description="Free-form search query.",
        ),
        ParamSpec(
            "max_results", "int", required=False, default=5,
            min=1, max=_SEARCH_MAX_RESULTS,
            description="How many results to return (1-10).",
        ),
    ],
)
async def web_search(ctx: ToolContext, args: dict) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult.fail("empty_query")
    max_results = int(args.get("max_results") or 5)
    if max_results < 1:
        max_results = 1
    if max_results > _SEARCH_MAX_RESULTS:
        max_results = _SEARCH_MAX_RESULTS

    # Guild-level backend override takes precedence over the global env var.
    try:
        _guild_backend = await ctx.db.fetch_val(
            "SELECT search_backend FROM guild_settings WHERE guild_id=$1",
            ctx.guild_id,
        )
    except Exception:
        _guild_backend = None
    backend = (_guild_backend or Config.SEARCH_BACKEND or "ddg").lower()

    # Resolve the guild search model (set via ,ai model set search).
    # Falls back to the SEARCH_MODEL env var inside each backend function
    # if no guild default is configured.
    _guild_search_model: str | None = None
    try:
        from core.framework.ai.models import get_guild_default as _ai_get_guild_default
        _guild_pick = await _ai_get_guild_default(ctx.db, ctx.guild_id, "search")
        if _guild_pick and _guild_pick.model:
            _guild_search_model = _guild_pick.model
    except Exception:
        pass

    # Dispatch to configured backend; all paths fall through to DDG on failure.
    if backend == "brave":
        result = await _web_search_brave(query, max_results)
        if result.ok:
            return result
        log.info("[data.web_search] brave backend failed (%s), falling back to DDG", result.error)
    elif backend == "perplexity":
        result = await _web_search_perplexity(query, max_results, model=_guild_search_model)
        if result.ok:
            return result
        log.info("[data.web_search] perplexity backend failed (%s), falling back to DDG", result.error)
    elif backend == "openrouter":
        result = await _web_search_openrouter(query, max_results, model=_guild_search_model)
        if result.ok:
            return result
        log.info("[data.web_search] openrouter backend failed (%s), falling back to DDG", result.error)
    elif backend == "ollama":
        result = await _web_search_ollama(query, max_results, model=_guild_search_model)
        if result.ok:
            return result
        log.info("[data.web_search] ollama backend failed (%s), falling back to DDG", result.error)

    # ── DDG HTML scraping (default) ───────────────────────────────────────────
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {
        # DDG's HTML endpoint 202s on a blank UA, so send a plausible one.
        "User-Agent": (
            "Mozilla/5.0 (compatible; Discoin-Agent/1.0; "
            "+https://github.com/HiLleywyn/Discoin)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with aiohttp.ClientSession(timeout=_SEARCH_DDG_TIMEOUT) as sess:
            async with sess.get(url, headers=headers) as r:
                # DDG sometimes returns 202 for automated requests but the
                # body still contains HTML results worth attempting to parse.
                if r.status not in (200, 202):
                    return ToolResult.fail(f"search_http_{r.status}")
                body = await r.content.read(_SEARCH_BODY_LIMIT)
                html = body.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.web_search] %s", exc)
        return ToolResult.fail(f"search_error: {type(exc).__name__}")

    parser = _DDGResultParser(max_results=max_results)
    try:
        parser.feed(html)
    except Exception:
        # Malformed HTML shouldn't drop the whole tool call; return whatever
        # rows we managed to collect up to the failure point.
        pass

    return ToolResult.success({
        "query": query,
        "result_count": len(parser.results),
        "results": parser.results[:max_results],
    })


# ── data.api_call ────────────────────────────────────────────────────────────

@tool(
    name="data.api_call",
    summary=(
        "Call a REST API and return the parsed JSON. Host must be in the "
        "API allowlist. Optional bearer token is read from the caller's "
        "guild secrets (never supplied by the AI directly)."
    ),
    risk=RiskLevel.SAFE,
    category="data",
    cooldown_s=1,
    params=[
        ParamSpec("url", "str", description="HTTPS URL."),
        ParamSpec("method", "str", required=False, default="GET",
                  choices=["GET", "POST"]),
        ParamSpec("json_body", "json", required=False, default=None,
                  description="JSON body for POST requests."),
        ParamSpec("auth_key", "str", required=False, default=None,
                  description=(
                      "Name of a guild secret to supply as Bearer auth. "
                      "The AI does not get to read or set the value."
                  )),
    ],
)
async def api_call(ctx: ToolContext, args: dict) -> ToolResult:
    url = args["url"]
    if not url.startswith("https://"):
        return ToolResult.fail("url must use https://")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.hostname not in _API_HOST_ALLOWLIST:
        return ToolResult.fail(
            f"host_not_allowed: {parsed.hostname} not in API allowlist"
        )
    method = (args.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return ToolResult.fail("unsupported_method")

    headers = {"Accept": "application/json", "User-Agent": "Discoin-Agent/1.0"}
    auth_key = args.get("auth_key")
    if auth_key:
        secret = await _load_guild_secret(ctx, auth_key)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

    body = args.get("json_body") if method == "POST" else None

    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as sess:
            async with sess.request(method, url, headers=headers, json=body) as r:
                text = await r.text()
                if len(text) > _WEB_MAX_BYTES:
                    return ToolResult.fail("response_too_large")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return ToolResult.success({
                        "status": r.status,
                        "raw_text": text[:2000],
                        "parsed": False,
                    })
                return ToolResult.success({
                    "status": r.status,
                    "parsed": True,
                    "data": data,
                })
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[data.api_call] %s", exc)
        return ToolResult.fail(f"api_error: {type(exc).__name__}")


async def _load_guild_secret(ctx: ToolContext, key: str) -> str | None:
    """Pull a named secret from a guild_secrets table if one exists.

    Fails closed -- a missing table or row returns None. The secret value is
    never echoed back to the caller; only the raw HTTP response is returned.
    """
    try:
        row = await ctx.db.fetch_one(
            "SELECT value FROM guild_secrets WHERE guild_id=$1 AND key=$2",
            int(ctx.guild_id), key,
        )
    except Exception:
        return None
    if row is None:
        return None
    return str(row.get("value") or "") or None


# ── data.db_query ────────────────────────────────────────────────────────────
#
# Strict allowlist of READ-ONLY templates. Any call from the agent must name
# one of these keys; arbitrary SQL is never accepted. New templates should be
# added here deliberately.
QUERY_TEMPLATES: dict[str, dict] = {
    "top_net_worth": {
        "summary": "Top players by wallet+bank in this guild.",
        "sql": (
            "SELECT user_id, wallet, bank FROM users "
            "WHERE guild_id=$1 ORDER BY (wallet+bank) DESC LIMIT $2"
        ),
        "params": ["guild_id", "limit"],
    },
    "active_loans": {
        "summary": "Active loans in this guild with outstanding debt.",
        "sql": (
            "SELECT user_id, outstanding, collateral FROM loans "
            "WHERE guild_id=$1 AND outstanding > 0 "
            "ORDER BY outstanding DESC LIMIT $2"
        ),
        "params": ["guild_id", "limit"],
    },
    "largest_pools": {
        "summary": "Pools by largest reserve_a (raw).",
        "sql": (
            "SELECT pool_id, token_a, token_b, reserve_a, reserve_b FROM pools "
            "WHERE guild_id=$1 ORDER BY reserve_a DESC LIMIT $2"
        ),
        "params": ["guild_id", "limit"],
    },
    "recent_transactions": {
        "summary": "Most recent transactions for a user.",
        "sql": (
            "SELECT tx_hash, kind, symbol, amount, created_at FROM transactions "
            "WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT $3"
        ),
        "params": ["guild_id", "user_id", "limit"],
    },
}


@tool(
    name="data.db_query",
    summary=(
        "Run a named, read-only query against the economy database. Only "
        "templates in QUERY_TEMPLATES are allowed -- no raw SQL. Every "
        "template scopes to the caller's guild_id automatically."
    ),
    risk=RiskLevel.READ,
    category="data",
    params=[
        ParamSpec("template", "str", description="Template key from QUERY_TEMPLATES."),
        ParamSpec("limit", "int", required=False, default=10, min=1, max=100),
        ParamSpec("user_id", "uid", required=False, default=None,
                  description="Required for templates that take a user_id."),
    ],
)
async def db_query(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["template"]
    tpl = QUERY_TEMPLATES.get(key)
    if tpl is None:
        return ToolResult.fail(
            f"unknown_template: {key!r}. allowed={sorted(QUERY_TEMPLATES.keys())}"
        )
    limit = int(args.get("limit") or 10)
    params_needed = tpl["params"]
    bound: list[Any] = []
    for p in params_needed:
        if p == "guild_id":
            bound.append(int(ctx.guild_id))
        elif p == "limit":
            bound.append(limit)
        elif p == "user_id":
            if args.get("user_id") is None:
                return ToolResult.fail(f"template {key!r} requires user_id")
            bound.append(int(args["user_id"]))
        else:
            return ToolResult.fail(f"template {key!r} unsupported param {p}")
    try:
        rows = await ctx.db.fetch_all(tpl["sql"], *bound)
    except Exception as exc:
        return ToolResult.fail(f"query_error: {type(exc).__name__}: {exc}")
    # Normalize raw types for JSON
    serialized: list[dict] = []
    for r in rows:
        serialized.append({k: _jsonable(v) for k, v in r.items()})
    return ToolResult.success({
        "template": key,
        "summary": tpl["summary"],
        "rows": serialized,
        "row_count": len(serialized),
    })


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
