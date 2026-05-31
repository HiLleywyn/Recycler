"""Bybit public-data adapter.

Bybit's v5 public REST endpoints ship free OHLC at standard intervals
with no auth, no key, no geo-block (verified globally). Used as the
fallback for crypto OHLC when Binance.com is blocked from the bot's
deployment region (US datacentres frequently get 451'd by binance.com).

Endpoint: ``GET https://api.bybit.com/v5/market/kline``
Params:   ``category=linear&symbol=BTCUSDT&interval=<MIN>|D|W|M&limit=500``

Symbols are the standard ``<base>USDT`` perpetual shape.
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
    Quote,
    ResolvedSymbol,
)
from ..cache import to_cacheable
from ._base_http import fetch_json

log = logging.getLogger(__name__)


_BB_BASE = "https://api.bybit.com/v5"

# Bybit's klines ``interval`` field uses minute-integers for sub-day and
# letter codes for day/week/month. No 1s support -- if 1s is needed,
# Binance is the only public source.
_BB_INTERVAL: dict[str, str] = {
    "1m":   "1",
    "3m":   "3",
    "5m":   "5",
    "15m":  "15",
    "30m":  "30",
    "1h":   "60",
    "2h":   "120",
    "4h":   "240",
    "6h":   "360",
    "12h":  "720",
    "1d":   "D",
    "1w":   "W",
    "1mo":  "M",
}


def _symbol_for(base: str) -> str:
    """Bybit perp symbol for a base ticker."""
    b = (base or "").upper()
    if b in ("USDT", "USDC", "BUSD"):
        return "USDCUSDT"
    return f"{b}USDT"


class BybitProvider:
    name = "bybit"
    asset_classes = (AssetClass.CRYPTO,)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = getattr(Config, "BYBIT_ENABLED", True)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "BYBIT_ENABLED=0")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.OHLC, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _BB_INTERVAL

    async def health(self) -> bool:
        return self._enabled

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._registry.rate.acquire(self.name)
        return await fetch_json(
            self.name, f"{_BB_BASE}{path}", params=params, timeout=8,
        )

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        # We don't own crypto symbol resolution -- CoinGecko handles it.
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        if not self._enabled:
            return None
        sym = _symbol_for(resolved.symbol)
        key = f"market:bybit:quote:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None
        try:
            data = await self._get(
                "/market/tickers",
                params={"category": "linear", "symbol": sym},
            )
        except MarketError as exc:
            log.debug("[bybit] /market/tickers failed for %s: %s", sym, exc)
            return None
        result = (data or {}).get("result") or {}
        rows = result.get("list") or []
        if not rows:
            return None
        row = rows[0]
        try:
            price = float(row.get("lastPrice") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        try:
            high = float(row.get("highPrice24h") or 0.0) or None
            low = float(row.get("lowPrice24h") or 0.0) or None
            vol_usd = float(row.get("turnover24h") or 0.0) or None
            pct = float(row.get("price24hPcnt") or 0.0) * 100.0
        except (TypeError, ValueError):
            high = low = vol_usd = None
            pct = 0.0
        q = Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=AssetClass.CRYPTO,
            provider=self.name,
            ts=int(time.time()),
            day_high=high,
            day_low=low,
            day_volume_usd=vol_usd,
            pct_24h=pct,
        )
        await self._registry.cache.set(key, to_cacheable(q), 10)
        return q

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        if not self._enabled or tf not in _BB_INTERVAL:
            return []
        sym = _symbol_for(resolved.symbol)
        cache_key = f"market:bybit:ohlc:{sym}:{tf}"
        cached = await self._registry.cache.get(cache_key)
        if cached is not None:
            return [Candle(**c) for c in cached]
        try:
            data = await self._get(
                "/market/kline",
                params={
                    "category": "linear",
                    "symbol": sym,
                    "interval": _BB_INTERVAL[tf],
                    "limit": 500,
                },
            )
        except MarketError as exc:
            log.debug("[bybit] /market/kline failed for %s %s: %s", sym, tf, exc)
            return []
        result = (data or {}).get("result") or {}
        rows = result.get("list") or []
        if not rows:
            return []
        # Bybit returns klines NEWEST-first as a list of strings:
        #   [start_ms, open, high, low, close, volume, turnover]
        out: list[Candle] = []
        for row in rows:
            try:
                ts = int(row[0]) // 1000
                o = float(row[1]); h = float(row[2])
                l = float(row[3]); c = float(row[4])
                v = float(row[5])
            except (TypeError, ValueError, IndexError):
                continue
            out.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
        out.sort(key=lambda c: c.ts)
        if out:
            await self._registry.cache.set(
                cache_key, [to_cacheable(c) for c in out],
                getattr(Config, "REAL_MARKET_CACHE_TTL_OHLC", 60),
            )
        return out
