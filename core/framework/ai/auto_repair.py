"""
core/framework/ai/auto_repair.py -- live probes + failover repair for AI backends.

The Doctor (``,ai doctor``) runs a small battery of real requests against every
configured AI backend the bot talks to (OpenRouter, Ollama, DuckDuckGo,
Perplexity) and, when one is down, rewrites the per-guild config so the
affected category falls over to a healthy alternative. Every probe and
repair is emitted as an event so the caller can render it live in Discord.

Why not just retry at call time? Because the user sees a truncated or empty
reply, and the only per-request fallback we currently have is for vision
(Ollama -> OpenRouter). The Doctor moves that logic up one level:

  1. Probe all backends -- one real call each, with a tight timeout.
  2. For each category whose configured backend failed, pick the best
     alternative that probed healthy and persist the override.
  3. Report back with latencies, repair actions, and a health score.

Event shape (all dicts so the event can be streamed over a message edit
loop without a custom type):

    {"type": "phase", "name": "probing", "status": "running"}
    {"type": "probe_start", "backend": "ollama"}
    {"type": "probe_result", "backend": "ollama", "ok": False,
                             "latency_ms": 8023, "error": "HTTP 503"}
    {"type": "repair_plan", "category": "vision",
                            "from": "ollama", "to": "openrouter"}
    {"type": "repair_result", "category": "vision", "ok": True,
                              "message": "vision now routed via openrouter"}
    {"type": "done", "summary": {...}}

Categories covered:
  - chat/tools: agent loop backend (per-guild guild_settings.tools_backend)
  - vision:     per-guild ai_model_defaults (category=vision)
  - websearch:  per-guild guild_settings.search_backend
  - heal_ai:    per-guild guild_settings.heal_ai_backend
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp

from core.config import Config
from core.framework.ai.client import _ollama_endpoint

log = logging.getLogger(__name__)


# ── Probe primitives ─────────────────────────────────────────────────────────

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=4, sock_read=8)

# 1x1 transparent PNG used for the vision probes. Embedded as bytes so we
# don't need to ship a resource file or hit the network to get it.
_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PIXEL_DATA_URI = f"data:image/png;base64,{_PIXEL_PNG_B64}"


@dataclass(frozen=True)
class ProbeResult:
    backend: str
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: str | None = None


async def _timed_post(url: str, *, headers: dict, payload: dict, timeout: aiohttp.ClientTimeout = _PROBE_TIMEOUT) -> tuple[bool, int, str | None]:
    """Send one POST and return (ok, latency_ms, error_summary)."""
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as r:
                latency_ms = int((time.monotonic() - start) * 1000)
                if r.status == 200:
                    return True, latency_ms, None
                body = await r.text()
                return False, latency_ms, f"HTTP {r.status}: {body[:120]}"
    except asyncio.TimeoutError:
        return False, int((time.monotonic() - start) * 1000), "timeout"
    except aiohttp.ClientError as exc:
        return False, int((time.monotonic() - start) * 1000), f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        return False, int((time.monotonic() - start) * 1000), f"unexpected: {exc}"


async def probe_openrouter() -> ProbeResult:
    """One-token chat completion against OpenRouter."""
    if not (Config.AI_ENABLED and (Config.AI_BACKEND_KEY or Config.OPENROUTER_API_KEY)):
        return ProbeResult("openrouter", False, 0, "no_api_key", "OPENROUTER_API_KEY not set")
    payload = {
        "model": Config.TOOLS_MODEL or "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {Config.AI_BACKEND_KEY or Config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    ok, ms, err = await _timed_post(
        f"{Config.AI_BACKEND_URL}/chat/completions",
        headers=headers, payload=payload,
    )
    return ProbeResult("openrouter", ok, ms, err)


async def probe_ollama() -> ProbeResult:
    """One-token chat completion against the configured Ollama endpoint."""
    if not os.getenv("OLLAMA_BASE_URL"):
        return ProbeResult("ollama", False, 0, "not_configured", "OLLAMA_BASE_URL not set")
    url, headers = _ollama_endpoint()
    payload = {
        "model": Config.TOOLS_MODEL or "llama3.2",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    ok, ms, err = await _timed_post(url, headers=headers, payload=payload)
    return ProbeResult("ollama", ok, ms, err)


async def probe_ollama_vision() -> ProbeResult:
    """Multimodal probe -- Ollama's vision endpoint with a 1x1 pixel."""
    if not os.getenv("OLLAMA_BASE_URL"):
        return ProbeResult("ollama_vision", False, 0, "not_configured", "OLLAMA_BASE_URL not set")
    url, headers = _ollama_endpoint()
    payload = {
        "model": Config.VISION_MODEL or Config.TOOLS_MODEL or "gemma3:27b",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": _PIXEL_DATA_URI}},
            ],
        }],
        "max_tokens": 1,
        "temperature": 0,
    }
    ok, ms, err = await _timed_post(url, headers=headers, payload=payload)
    return ProbeResult("ollama_vision", ok, ms, err)


async def probe_openrouter_vision() -> ProbeResult:
    """Multimodal probe -- OpenRouter with a 1x1 pixel."""
    if not (Config.AI_ENABLED and (Config.AI_BACKEND_KEY or Config.OPENROUTER_API_KEY)):
        return ProbeResult("openrouter_vision", False, 0, "no_api_key", "OPENROUTER_API_KEY not set")
    payload = {
        "model": Config.VISION_MODEL or "openai/gpt-4o-mini",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": _PIXEL_DATA_URI}},
            ],
        }],
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {Config.AI_BACKEND_KEY or Config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    ok, ms, err = await _timed_post(
        f"{Config.AI_BACKEND_URL}/chat/completions",
        headers=headers, payload=payload,
    )
    return ProbeResult("openrouter_vision", ok, ms, err)


async def probe_ddg() -> ProbeResult:
    """HTML search works = the DDG backend is alive. No key required."""
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as sess:
            async with sess.get(
                "https://duckduckgo.com/html/?q=ping",
                headers={"User-Agent": "Mozilla/5.0 Discoin-Doctor/1.0"},
            ) as r:
                latency_ms = int((time.monotonic() - start) * 1000)
                if r.status == 200:
                    return ProbeResult("ddg", True, latency_ms)
                return ProbeResult("ddg", False, latency_ms, f"HTTP {r.status}")
    except asyncio.TimeoutError:
        return ProbeResult("ddg", False, int((time.monotonic() - start) * 1000), "timeout")
    except Exception as exc:
        return ProbeResult(
            "ddg", False, int((time.monotonic() - start) * 1000),
            f"{type(exc).__name__}: {exc}",
        )


async def probe_brave() -> ProbeResult:
    """Single canned query against the Brave Search API. Skipped cleanly if no key."""
    if not Config.BRAVE_SEARCH_API_KEY:
        return ProbeResult("brave", False, 0, "no_api_key", "BRAVE_SEARCH_API_KEY not set")
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as sess:
            async with sess.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "ping", "count": "1"},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": Config.BRAVE_SEARCH_API_KEY,
                },
            ) as r:
                latency_ms = int((time.monotonic() - start) * 1000)
                if r.status == 200:
                    return ProbeResult("brave", True, latency_ms)
                body = await r.text()
                return ProbeResult("brave", False, latency_ms, f"HTTP {r.status}", body[:120])
    except asyncio.TimeoutError:
        return ProbeResult("brave", False, int((time.monotonic() - start) * 1000), "timeout")
    except Exception as exc:
        return ProbeResult(
            "brave", False, int((time.monotonic() - start) * 1000),
            f"{type(exc).__name__}: {exc}",
        )


async def probe_perplexity() -> ProbeResult:
    """Tiny chat completion against Perplexity. Skipped cleanly if no key."""
    if not Config.PERPLEXITY_API_KEY:
        return ProbeResult("perplexity", False, 0, "no_api_key", "PERPLEXITY_API_KEY not set")
    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    headers = {
        "Authorization": f"Bearer {Config.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }
    ok, ms, err = await _timed_post(
        "https://api.perplexity.ai/chat/completions",
        headers=headers, payload=payload,
    )
    return ProbeResult("perplexity", ok, ms, err)


# ── Repair plan ──────────────────────────────────────────────────────────────
#
# Each probe maps to one or more "categories" (subsystems the bot uses). When
# the backend currently wired up for a category is unhealthy, we pick the
# healthiest alternative whose probe succeeded.

# Ordered preference list per category. The first healthy backend wins.
# Chat is intentionally omitted -- it's wired through the tools loop, so
# repairing "tools" already recovers chat replies.
_FALLBACK_CHAIN: dict[str, tuple[str, ...]] = {
    "tools":     ("openrouter", "ollama"),
    "vision":    ("openrouter_vision", "ollama_vision"),
    "heal_ai":   ("openrouter", "ollama"),
    "websearch": ("brave", "ddg", "openrouter", "perplexity", "ollama"),
}

# Per-provider default model used when the Doctor writes a fresh
# ai_model_defaults row. The vision tool in core/framework/agent_tools/tools/
# vision.py only honours guild overrides whose ``model`` is truthy, so we
# populate a sensible default for each provider rather than leaving it blank.
_DEFAULT_VISION_MODEL = {
    "openrouter": "openai/gpt-4o-mini",
    "ollama":     "gemma3:27b",
}


async def _current_backend(db, guild_id: int, category: str) -> str | None:
    """Return the backend currently in effect for a category, or None if unset."""
    if category == "tools":
        row = await db.fetch_one(
            "SELECT tools_backend FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        return (row or {}).get("tools_backend") or Config.TOOLS_BACKEND or "openrouter"
    if category == "websearch":
        row = await db.fetch_one(
            "SELECT search_backend FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        return (row or {}).get("search_backend") or Config.SEARCH_BACKEND or "ddg"
    if category == "heal_ai":
        row = await db.fetch_one(
            "SELECT heal_ai_backend FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        return (row or {}).get("heal_ai_backend") or Config.TOOLS_BACKEND or "openrouter"
    if category == "vision":
        # ai_model_defaults keyed by (guild_id, category). provider field is the
        # backend ("openrouter"/"ollama"). The vision tool only honours the
        # override when ``model`` is truthy; if the row has an empty model
        # the effective backend falls back to the env default.
        row = await db.fetch_one(
            "SELECT provider, model FROM ai_model_defaults WHERE guild_id=$1 AND category=$2",
            guild_id, "vision",
        )
        if row and row.get("model"):
            return str(row["provider"])
        return Config.VISION_BACKEND or "ollama"
    return None


async def _apply_repair(db, guild_id: int, category: str, new_backend: str) -> None:
    """Persist the repair: flip the stored backend for the category."""
    if category == "tools":
        await db.update_guild_setting(guild_id, "tools_backend", new_backend)
    elif category == "websearch":
        await db.update_guild_setting(guild_id, "search_backend", new_backend)
    elif category == "heal_ai":
        await db.update_guild_setting(guild_id, "heal_ai_backend", new_backend)
    elif category == "vision":
        # vision.describe_image only honours a guild override when its model
        # field is truthy -- otherwise it silently falls back to the env
        # default, which is the broken backend we're trying to flip away
        # from. Keep any custom model the operator already set; only fall
        # back to a sensible per-provider default when there's nothing to
        # preserve.
        existing = await db.fetch_one(
            "SELECT model FROM ai_model_defaults WHERE guild_id=$1 AND category='vision'",
            guild_id,
        )
        existing_model = str((existing or {}).get("model") or "").strip()
        model = existing_model or _DEFAULT_VISION_MODEL.get(new_backend, "")
        if not model:
            model = _DEFAULT_VISION_MODEL["openrouter"]
        await db.execute(
            """
            INSERT INTO ai_model_defaults
                (guild_id, category, provider, model, updated_by, updated_at)
            VALUES ($1, 'vision', $2, $3, NULL, NOW())
            ON CONFLICT (guild_id, category) DO UPDATE SET
                provider   = EXCLUDED.provider,
                model      = EXCLUDED.model,
                updated_at = NOW()
            """,
            guild_id, new_backend, model,
        )
    else:
        raise ValueError(f"unknown category: {category}")


def _category_backend_from_probe(cat: str, probe_name: str) -> str:
    """Map the probe tag back to the value we persist for a category.

    Vision probes carry a ``_vision`` suffix that the fallback chain keeps
    so we can distinguish multimodal health from plain chat health. When
    writing the repair for the vision category we need to strip that suffix
    so ``ai_model_defaults.provider`` gets a clean "openrouter"/"ollama".
    """
    if cat == "vision":
        return probe_name.replace("_vision", "")
    return probe_name


# ── Event generator ──────────────────────────────────────────────────────────


async def run_auto_repair(
    db, guild_id: int, *,
    dry_run: bool = False,
    probe_concurrency: int = 3,
) -> AsyncIterator[dict]:
    """Probe every backend and repair the broken ones. Yields events.

    Consumers (e.g. ``,ai doctor``) typically render each event into a live
    embed. Order of events is deterministic per run but probe_result events
    may arrive in any order within a probe wave.
    """
    yield {"type": "phase", "name": "probing", "status": "running"}

    probes: dict[str, "asyncio.Future[ProbeResult]"] = {}
    probe_fns = {
        "openrouter":        probe_openrouter,
        "ollama":             probe_ollama,
        "openrouter_vision":  probe_openrouter_vision,
        "ollama_vision":      probe_ollama_vision,
        "ddg":                probe_ddg,
        "brave":              probe_brave,
        "perplexity":         probe_perplexity,
    }
    sem = asyncio.Semaphore(max(1, probe_concurrency))

    async def _guarded(fn):
        async with sem:
            return await fn()

    for backend, fn in probe_fns.items():
        yield {"type": "probe_start", "backend": backend}
        probes[backend] = asyncio.create_task(_guarded(fn))

    results: dict[str, ProbeResult] = {}
    for backend, task in probes.items():
        res: ProbeResult = await task
        results[backend] = res
        yield {
            "type": "probe_result",
            "backend": backend,
            "ok": res.ok,
            "latency_ms": res.latency_ms,
            "error": res.error,
            "detail": res.detail,
        }

    yield {"type": "phase", "name": "probing", "status": "done"}
    yield {"type": "phase", "name": "repairing", "status": "running"}

    repairs_performed: list[dict] = []
    for category, chain in _FALLBACK_CHAIN.items():
        current = await _current_backend(db, guild_id, category)
        current_probe_name = current
        if category == "vision" and current in ("ollama", "openrouter"):
            current_probe_name = f"{current}_vision"

        # Healthy already? nothing to do.
        current_probe = results.get(current_probe_name)
        if current_probe is not None and current_probe.ok:
            yield {
                "type": "category_ok",
                "category": category,
                "backend": current,
                "latency_ms": current_probe.latency_ms,
            }
            continue

        # Pick the first healthy alternative from the fallback chain.
        candidate: str | None = None
        for cand in chain:
            probe = results.get(cand)
            if probe is not None and probe.ok and cand != current_probe_name:
                candidate = cand
                break

        if candidate is None:
            yield {
                "type": "category_stuck",
                "category": category,
                "backend": current,
                "reason": (
                    current_probe.error if current_probe else "no probe data"
                ),
            }
            continue

        target_backend = _category_backend_from_probe(category, candidate)
        yield {
            "type": "repair_plan",
            "category": category,
            "from": current,
            "to": target_backend,
            "reason": current_probe.error if current_probe else "no probe data",
        }

        if dry_run:
            yield {
                "type": "repair_result",
                "category": category,
                "ok": False,
                "dry_run": True,
                "message": f"would flip {category}: {current} -> {target_backend}",
            }
            continue

        try:
            await _apply_repair(db, guild_id, category, target_backend)
            repairs_performed.append({
                "category": category, "from": current, "to": target_backend,
            })
            yield {
                "type": "repair_result",
                "category": category,
                "ok": True,
                "from": current,
                "to": target_backend,
                "message": f"{category}: routed to {target_backend}",
            }
        except Exception as exc:
            log.exception("[auto_repair] apply_repair failed for %s", category)
            yield {
                "type": "repair_result",
                "category": category,
                "ok": False,
                "message": f"repair crashed: {type(exc).__name__}: {exc}",
            }

    yield {"type": "phase", "name": "repairing", "status": "done"}

    healthy = sum(1 for r in results.values() if r.ok)
    total = len(results)
    yield {
        "type": "done",
        "summary": {
            "backends_probed": total,
            "backends_healthy": healthy,
            "repairs_performed": repairs_performed,
            "dry_run": dry_run,
            "results": {
                k: {
                    "ok": v.ok,
                    "latency_ms": v.latency_ms,
                    "error": v.error,
                    "detail": v.detail,
                } for k, v in results.items()
            },
        },
    }


__all__ = (
    "ProbeResult",
    "run_auto_repair",
    "probe_openrouter",
    "probe_ollama",
    "probe_ollama_vision",
    "probe_openrouter_vision",
    "probe_ddg",
    "probe_perplexity",
)
