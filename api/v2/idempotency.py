"""Idempotency key store for trading endpoints.

Uses Redis when available, falls back to an in-memory dict.
Keys expire after 60 seconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger("discoin.idempotency")

_LOCK = asyncio.Lock()
_STORE: dict[str, tuple[float, Any]] = {}  # in-memory fallback
IDEMPOTENCY_TTL = 60  # seconds
_REDIS_PREFIX = "discoin:idem:"

# Module-level Redis client  -  set via init()
_redis = None


def init(redis) -> None:
    """Inject a redis.asyncio client (or None to use in-memory only)."""
    global _redis
    _redis = redis


async def check(key: str) -> Any | None:
    """Return cached response if key exists and hasn't expired, else None."""
    # Try Redis first
    if _redis is not None:
        try:
            raw = await _redis.get(f"{_REDIS_PREFIX}{key}")
            if raw is not None:
                return json.loads(raw)
            return None
        except Exception as exc:
            log.warning("Redis idempotency check failed (%s), falling back to memory", exc)

    # In-memory fallback
    async with _LOCK:
        entry = _STORE.get(key)
        if entry is None:
            return None
        expires_at, response = entry
        if time.time() > expires_at:
            del _STORE[key]
            return None
        return response


async def store(key: str, response: Any) -> None:
    """Store a response for the given idempotency key."""
    # Try Redis first
    if _redis is not None:
        try:
            await _redis.setex(
                f"{_REDIS_PREFIX}{key}",
                IDEMPOTENCY_TTL,
                json.dumps(response, default=str),
            )
            return
        except Exception as exc:
            log.warning("Redis idempotency store failed (%s), falling back to memory", exc)

    # In-memory fallback
    async with _LOCK:
        _STORE[key] = (time.time() + IDEMPOTENCY_TTL, response)
        # Cleanup expired entries
        now = time.time()
        expired = [k for k, (exp, _) in _STORE.items() if now > exp]
        for k in expired:
            del _STORE[k]
