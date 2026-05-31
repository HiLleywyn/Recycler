"""Per-provider token-bucket rate limiter.

Each upstream API has its own RPS budget; we keep one bucket per provider
so a misbehaving free-tier limit on (say) CoinGecko can't starve the
Yahoo or Finnhub buckets.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    """Classic token bucket. ``capacity`` is the max burst, ``rate`` is
    tokens per second. ``acquire()`` sleeps until a token is available."""

    capacity: float
    rate: float
    _tokens: float = 0.0
    _last: float = 0.0
    _lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        assert self._lock is not None
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = max(0.01, deficit / self.rate)
                await asyncio.sleep(wait)


# Sensible defaults per provider. Free-tier limits.
_DEFAULTS: dict[str, tuple[float, float]] = {
    "coingecko":   (10.0,  10 / 60.0),  # 10 req per 60s on free
    "yahoo":       (10.0,  2.0),        # public endpoints, be polite
    "finnhub":     (30.0,  1.0),        # 60/min on free, leave headroom
    "dexscreener": (60.0,  5.0),        # generous public API
    "pyth":        (50.0, 10.0),        # Hermes is realtime, lenient
    "redstone":    (20.0,  2.0),
    "switchboard": (10.0,  1.0),
    "coinglass":   (10.0,  0.5),        # 30/min on free
    "coinalyze":   (20.0,  1.0),
    "tradingview": ( 8.0,  0.8),
    "alternative_me": (10.0, 0.5),
}


class RateLimiter:
    """Registry of per-provider buckets."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        for name, (cap, rate) in _DEFAULTS.items():
            self._buckets[name] = TokenBucket(capacity=cap, rate=rate)

    def for_provider(self, name: str) -> TokenBucket:
        b = self._buckets.get(name)
        if b is None:
            b = TokenBucket(capacity=5.0, rate=0.5)
            self._buckets[name] = b
        return b

    async def acquire(self, name: str, cost: float = 1.0) -> None:
        await self.for_provider(name).acquire(cost)
