"""Switchboard adapter via Crossbar REST gateway.

Switchboard On-Demand exposes a public oracle gateway at
``crossbar.switchboard.xyz`` that returns medianised feed values over
plain HTTP -- no Solana SDK, no on-chain RPC, no keypair. We use that
gateway when the operator has wired one or more feed hashes via
``SWITCHBOARD_FEEDS`` (JSON ``{"MTA/USD": "0x...", ...}``); without
that map we cleanly return ``None`` so the router falls through to
Pyth + RedStone (which between them cover every major).

Feed hashes are stable per-feed and discoverable in the Switchboard
On-Demand explorer (https://ondemand.switchboard.xyz/) -- there's no
canonical symbol->hash registry to ship, which is why this provider
stays opt-in. We don't fabricate hashes; the adapter is fully
operational the moment an operator drops real ones in.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from core.config import Config

from ..base import (
    AssetClass,
    Candle,
    Capability,
    MarketError,
    OracleQuote,
    Quote,
    ResolvedSymbol,
)
from ..cache import to_cacheable
from ._base_http import fetch_json

log = logging.getLogger(__name__)


def _feed_key(symbol: str) -> str:
    s = (symbol or "").upper()
    if "/" in s:
        return s
    return f"{s}/USD"


_HEX_RE = __import__("re").compile(r"^[0-9a-f]+$", __import__("re").IGNORECASE)


def _normalise_feed_hash(value: str) -> str | None:
    """Accept feed hashes with or without the ``0x`` prefix.

    Switchboard's on-chain explorer renders hashes prefix-less; the SDK
    and most docs render them ``0x``-prefixed. We accept both and emit
    the canonical ``0x``-prefixed form so downstream code (Crossbar
    request path, cache key) sees one consistent shape.

    Returns ``None`` if the value isn't a plausible hex feed hash.
    """
    s = value.strip()
    if s.lower().startswith("0x"):
        body = s[2:]
    else:
        body = s
    if not _HEX_RE.match(body):
        return None
    # Standard feed hash is 32 bytes (64 hex chars). Allow 8-64 so
    # truncated test fixtures still work in dev; anything outside that
    # window is malformed.
    if not (8 <= len(body) <= 64):
        return None
    return "0x" + body.lower()


def _load_feed_map() -> dict[str, str]:
    """Parse ``Config.SWITCHBOARD_FEEDS`` into a ``{SYMBOL/USD: 0xHASH}``
    dict. Tolerant of empty / malformed values -- returns ``{}`` and
    logs a debug line instead of raising at adapter construction time.
    """
    raw = getattr(Config, "SWITCHBOARD_FEEDS", "") or ""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("[switchboard] SWITCHBOARD_FEEDS isn't valid JSON; ignoring")
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        canonical = _normalise_feed_hash(v)
        if canonical is None:
            log.debug(
                "[switchboard] feed %r value %r is not a valid hex feed hash; skipping",
                k, v,
            )
            continue
        out[_feed_key(k)] = canonical
    return out


class SwitchboardProvider:
    name = "switchboard"
    asset_classes = (AssetClass.ORACLE, AssetClass.CRYPTO)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._crossbar = (
            getattr(Config, "SWITCHBOARD_CROSSBAR_URL", "")
            or "https://crossbar.switchboard.xyz"
        ).rstrip("/")
        self._network = (
            getattr(Config, "SWITCHBOARD_NETWORK", "") or "solana/mainnet"
        ).strip("/")
        self._feeds: dict[str, str] = _load_feed_map()
        if not self._feeds:
            # Not strictly disabled -- ``resolve`` returns None for any
            # symbol we don't have a hash for, which routes the request
            # to Pyth / RedStone transparently. Logging this once on
            # construction makes the situation visible to operators.
            registry.health.mark_disabled(
                self.name,
                "SWITCHBOARD_FEEDS unset; no feed hashes wired",
            )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ORACLE_PRICE, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return False

    async def health(self) -> bool:
        return bool(self._feeds)

    # ── helpers ─────────────────────────────────────────────────────

    async def _crossbar_get(self, path: str) -> Any:
        await self._registry.rate.acquire(self.name)
        url = f"{self._crossbar}{path}"
        return await fetch_json(self.name, url, timeout=8)

    # ── MarketProvider methods ──────────────────────────────────────

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        sym = (symbol or "").strip().upper()
        if not sym or not self._feeds:
            return None
        feed = self._feeds.get(_feed_key(sym))
        if not feed:
            return None
        return ResolvedSymbol(
            symbol=sym, name=sym,
            asset_class=AssetClass.ORACLE,
            provider=self.name,
            provider_id=feed,
        )

    async def oracle_quote(self, resolved: ResolvedSymbol) -> OracleQuote | None:
        if not self._feeds:
            return None
        key = _feed_key(resolved.symbol)
        feed_hash = self._feeds.get(key) or resolved.provider_id
        if not feed_hash or not feed_hash.startswith("0x"):
            return None
        cache_key = f"market:switchboard:feed:{feed_hash}"
        cached = await self._registry.cache.get(cache_key)
        if cached is not None:
            return OracleQuote(**cached) if cached else None
        # Crossbar's simulate endpoint returns the medianised oracle
        # value at request time -- no on-chain tx needed. The shape is
        # ``{"results": ["<decimal-as-string>"], ...}``.
        path = f"/simulate/{self._network}/{feed_hash}"
        try:
            data = await self._crossbar_get(path)
        except MarketError:
            return None
        if not isinstance(data, dict):
            return None
        results = data.get("results") or []
        if not results:
            return None
        try:
            price = float(results[0])
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        publish_ts = int(time.time())
        q = OracleQuote(
            symbol=resolved.symbol,
            price_usd=price,
            confidence=0.0,
            publish_ts=publish_ts,
            publish_age=0.0,
            provider=self.name,
            feed_id=feed_hash,
            is_stale=False,
        )
        await self._registry.cache.set(
            cache_key, to_cacheable(q),
            getattr(Config, "CACHE_TTL_ORACLE", 5),
        )
        return q

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        oq = await self.oracle_quote(resolved)
        if oq is None:
            return None
        return Quote(
            symbol=resolved.symbol,
            price_usd=oq.price_usd,
            asset_class=resolved.asset_class,
            provider=self.name,
            ts=oq.publish_ts,
            extras={"feed_id": oq.feed_id},
        )

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        return []
