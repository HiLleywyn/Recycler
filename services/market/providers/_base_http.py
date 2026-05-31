"""Shared HTTP helpers used by every provider adapter.

Avoids each adapter spinning up its own ``aiohttp.ClientSession`` and
duplicating retry/backoff logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..base import MarketError

log = logging.getLogger(__name__)


_SESSION_LOCK = asyncio.Lock()
_SESSION: aiohttp.ClientSession | None = None


async def get_shared_session(timeout: int = 12) -> aiohttp.ClientSession:
    global _SESSION
    async with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
        return _SESSION


async def close_shared_session() -> None:
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None


async def fetch_json(
    provider: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    timeout: int = 12,
) -> Any:
    """GET ``url`` with retry+backoff on 429/5xx/network errors. Raises
    :class:`MarketError` on definitive failure."""
    sess = await get_shared_session(timeout=timeout)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            async with sess.get(url, params=params, headers=headers) as resp:
                if resp.status == 429 or resp.status >= 500:
                    log.debug(
                        "[market.%s] %s -> %s (attempt %d/%d)",
                        provider, url, resp.status, attempt + 1, attempts,
                    )
                    last = MarketError(
                        f"{provider} {url} -> {resp.status}",
                        status=resp.status, provider=provider,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    raise MarketError(
                        f"{provider} {url} -> {resp.status}: {body}",
                        status=resp.status, provider=provider,
                    )
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return await resp.text()
        except aiohttp.ClientError as exc:
            last = exc
            log.debug(
                "[market.%s] network error (attempt %d/%d): %s",
                provider, attempt + 1, attempts, exc,
            )
            await asyncio.sleep(2 ** attempt)
            continue
        except asyncio.TimeoutError as exc:
            last = exc
            log.debug(
                "[market.%s] timeout (attempt %d/%d)",
                provider, attempt + 1, attempts,
            )
            await asyncio.sleep(2 ** attempt)
            continue
    if isinstance(last, MarketError):
        raise last
    raise MarketError(
        f"{provider} {url} failed after {attempts} attempts: {last}",
        provider=provider,
    )
