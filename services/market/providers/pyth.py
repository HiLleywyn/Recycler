"""Pyth Hermes adapter.

Hermes is Pyth's price-feed REST gateway -- public, no API key. We use it
for sub-minute crypto/forex/equity oracle ticks and the oracle panel
inside ``$info`` / ``$oracle``.
"""

from __future__ import annotations

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


# A small curated map of Pyth feed IDs for the most-used symbols. Pyth's
# full feed list is ~600 items; we keep the common ones here and resolve
# anything else dynamically via the price_feeds index endpoint.
_FEED_IDS: dict[str, str] = {
    "MTA/USD": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ARC/USD": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL/USD": "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "DOGE/USD": "0xdcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "XRP/USD": "0xec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8",
    "ADA/USD": "0x2a01deaec9e51a579277b34b122399984d0bbf57e2458a7e42fecd2829867a0d",
    "BNB/USD": "0x2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "AVAX/USD": "0x93da3352f9f1d105fdfe4971cfa80e9dd777bfc5d0f683ebb6e1294b92137bb7",
    "MATIC/USD": "0x5de33a9112c2b700b8d30b8a3402c103578ccfa2765696471cc672bd5cf6ac52",
    "LINK/USD": "0x8ac0c70fff57e9aefdf5edf44b51d62c2d433653cbb2cf5cc06bb115af04d221",
    "DOT/USD": "0xca3eed9b267293f6595901c734c7525ce8ef49adafe8284606ceb307afa2ca5b",
    "ATOM/USD": "0xb00b60f88b03a6a625a8d1c048c3f66653edf217439983d037e7222c4e612819",
    "AAPL/USD": "0x49f6b65cb1de6b10eaf75e7c03ca029c306d0357e91b5311b175084a5ad55688",
    "TSLA/USD": "0x16dad506d7db8da01c87581c87ca897a012a153557d4d578c3b9c9e1bc0632f1",
    "MSFT/USD": "0xd0ca23c1cc005e004ccf1db5bf76aeb6a49218f43dac3d4b275e92de12ded4d1",
    "EUR/USD": "0xa995d00bb36a63cef7fd2c287dc105fc8f3d93779f062f09551b0af3e81ec30b",
    "GBP/USD": "0x84c2dde9633d93d1bcad84e7dc41c9d56578b7ec52fabedc1f335d673df0a7c1",
    "XAU/USD": "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
}


class PythProvider:
    name = "pyth"
    asset_classes = (
        AssetClass.CRYPTO,
        AssetClass.ORACLE,
        AssetClass.FOREX,
        AssetClass.EQUITY,
        AssetClass.COMMODITY,
    )

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._base = getattr(
            Config, "PYTH_HERMES_URL", "https://hermes.pyth.network",
        ).rstrip("/")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ORACLE_PRICE, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return tf in {"1s", "5s", "15s", "30s", "1m"}

    async def health(self) -> bool:
        return True

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._registry.rate.acquire(self.name)
        return await fetch_json(
            self.name, f"{self._base}{path}", params=params, timeout=6,
        )

    def _feed_id(self, resolved: ResolvedSymbol) -> str | None:
        key = f"{resolved.symbol.upper()}/USD"
        return _FEED_IDS.get(key)

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        # Pyth doesn't resolve free-form symbols on its own -- we rely on
        # the curated feed map. Anything outside that map returns None and
        # the router moves to the next provider.
        sym = (symbol or "").strip().upper()
        if f"{sym}/USD" not in _FEED_IDS:
            return None
        return ResolvedSymbol(
            symbol=sym, name=sym,
            asset_class=AssetClass.ORACLE,
            provider=self.name,
            provider_id=_FEED_IDS[f"{sym}/USD"],
        )

    async def oracle_quote(self, resolved: ResolvedSymbol) -> OracleQuote | None:
        feed = self._feed_id(resolved) or resolved.provider_id
        if not feed:
            return None
        key = f"market:pyth:feed:{feed}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return OracleQuote(**cached) if cached else None
        try:
            data = await self._get(
                "/v2/updates/price/latest",
                params={"ids[]": feed},
            )
        except MarketError:
            return None
        parsed = (data or {}).get("parsed") or []
        if not parsed:
            return None
        entry = parsed[0]
        price_block = entry.get("price") or {}
        try:
            price_raw = int(price_block.get("price"))
            expo = int(price_block.get("expo"))
            conf_raw = int(price_block.get("conf"))
            publish_ts = int(price_block.get("publish_time"))
        except (TypeError, ValueError):
            return None
        scale = 10 ** expo
        price = price_raw * scale
        confidence = conf_raw * abs(scale)
        now = int(time.time())
        age = max(0.0, now - publish_ts)
        q = OracleQuote(
            symbol=resolved.symbol,
            price_usd=float(price),
            confidence=float(confidence),
            publish_ts=publish_ts,
            publish_age=age,
            provider=self.name,
            feed_id=feed,
            is_stale=(age > 30.0),
        )
        # Tiny TTL: pyth is realtime.
        await self._registry.cache.set(key, to_cacheable(q), 3)
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
            extras={
                "confidence": oq.confidence,
                "publish_age": oq.publish_age,
                "feed_id": oq.feed_id,
            },
        )

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        # Pyth Hermes doesn't ship OHLC -- the local tick stream would
        # need an aggregator. We leave that to the OHLCV worker; OHLC
        # is empty here so the router moves on.
        return []
