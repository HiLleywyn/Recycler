"""RedStone adapter.

RedStone's REST gateway exposes medianised oracle prices for a long tail
of crypto + RWA feeds. Used as oracle backup behind Pyth.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.config import Config

from ..base import (
    AssetClass,
    Capability,
    MarketError,
    OracleQuote,
    Quote,
    ResolvedSymbol,
)
from ..cache import to_cacheable
from ._base_http import fetch_json

log = logging.getLogger(__name__)


class RedStoneProvider:
    name = "redstone"
    asset_classes = (AssetClass.CRYPTO, AssetClass.ORACLE)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._base = getattr(
            Config, "REDSTONE_GATEWAY_URL",
            "https://oracle-gateway-1.a.redstone.finance",
        ).rstrip("/")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ORACLE_PRICE, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return False

    async def health(self) -> bool:
        return True

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        return ResolvedSymbol(
            symbol=sym, name=sym,
            asset_class=AssetClass.ORACLE,
            provider=self.name,
            provider_id=sym,
        )

    async def oracle_quote(self, resolved: ResolvedSymbol) -> OracleQuote | None:
        sym = resolved.provider_id.upper()
        key = f"market:redstone:feed:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return OracleQuote(**cached) if cached else None
        await self._registry.rate.acquire(self.name)
        url = f"{self._base}/data-packages/latest/redstone-primary-prod"
        try:
            data = await fetch_json(self.name, url, params={"symbol": sym}, timeout=6)
        except MarketError:
            return None
        # Response shape: { "SYMBOL": [ { "dataPoints": [ { "value": ..., "metadata": ... } ], "timestampMilliseconds": ... } ] }
        entries = (data or {}).get(sym) if isinstance(data, dict) else None
        if not entries:
            return None
        entry = entries[0]
        try:
            ts_ms = int(entry.get("timestampMilliseconds") or 0)
            point = (entry.get("dataPoints") or [{}])[0]
            price = float(point.get("value") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        publish_ts = ts_ms // 1000
        age = max(0.0, time.time() - publish_ts)
        q = OracleQuote(
            symbol=resolved.symbol,
            price_usd=price,
            confidence=0.0,
            publish_ts=publish_ts,
            publish_age=age,
            provider=self.name,
            feed_id=sym,
            is_stale=(age > 60.0),
        )
        await self._registry.cache.set(key, to_cacheable(q), 5)
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
        )

    async def ohlc(self, resolved: ResolvedSymbol, tf: str):
        return []
