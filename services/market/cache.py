"""Redis-backed TTL cache shared by every market provider.

Wraps the bot's existing ``bus._redis`` client so we keep one connection
pool. Falls through to a no-op when Redis isn't available (local dev
without the embedded Redis) -- providers still work, they just lose the
cache layer.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def to_cacheable(obj: Any) -> Any:
    """Convert a dataclass instance (slots-or-not) to a Redis-cacheable
    dict. Recursively unwraps nested dataclasses and coerces enums to
    their underlying value so :func:`json.dumps` can serialize them.

    Use this instead of ``obj.__dict__`` -- ``@dataclass(slots=True)``
    instances don't have ``__dict__`` and the latter raises
    ``AttributeError``.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = to_cacheable(getattr(obj, f.name))
        return out
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_cacheable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_cacheable(v) for v in obj]
    return obj


class MarketCache:
    """Async JSON cache. One instance per bot, owned by the registry."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def _client(self):
        bus = getattr(self._bot, "bus", None)
        return getattr(bus, "_redis", None) if bus is not None else None

    async def get(self, key: str) -> Any | None:
        r = self._client()
        if r is None:
            return None
        try:
            raw = await r.get(key)
        except Exception:
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode()
            except Exception:
                return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        r = self._client()
        if r is None:
            return
        try:
            await r.setex(key, ttl, json.dumps(value, default=_json_default))
        except Exception:
            pass

    async def delete(self, key: str) -> None:
        r = self._client()
        if r is None:
            return
        try:
            await r.delete(key)
        except Exception:
            pass


def _json_default(obj: Any) -> Any:
    """Pickle the dataclasses we cache by hand so json.dumps doesn't choke."""
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serializable")
