"""Binance public-data adapter.

Binance's public Spot REST API ships free OHLC at every interval from
``1s`` upward with no key, no auth, no header. We use it as the
primary crypto OHLC source so:

- ``$chart mta 1m`` actually works (CoinGecko's free tier only goes
  down to 5m and rate-limits aggressively).
- ``$chart mta 5m`` doesn't 429 against CoinGecko's free tier.
- ``$scan`` on majors at any timeframe has real candles.

Symbol mapping is the standard ``<base>USDT`` shape (MTA -> BTCUSDT,
ARC -> ETHUSDT, ...). Stables and exotic pairs that aren't on Binance
fall through to the next provider in the router's fan-out chain.

Endpoint: https://api.binance.com/api/v3/klines

If Railway's region is blocked from binance.com (some IPs are), the
adapter cleanly returns ``[]`` and the router moves on -- no crashes.
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


_BN_BASE = "https://api.binance.com/api/v3"

# Map our canonical timeframe codes to Binance's klines interval
# strings. Binance supports 1s/1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/
# 1d/3d/1w/1M -- a near-perfect match for our 24-tier canonical table.
# Anything outside this map falls through to the router's next
# provider.
_BN_INTERVAL: dict[str, str] = {
    "1s":   "1s",
    "1m":   "1m",
    "3m":   "3m",
    "5m":   "5m",
    "15m":  "15m",
    "30m":  "30m",
    "1h":   "1h",
    "2h":   "2h",
    "4h":   "4h",
    "6h":   "6h",
    "8h":   "8h",
    "12h":  "12h",
    "1d":   "1d",
    "3d":   "3d",
    "1w":   "1w",
    "1mo":  "1M",
}


def _symbol_for(base: str) -> str:
    """Binance pair symbol for a base ticker. Stables route to USDC."""
    b = (base or "").upper()
    if b in ("USDT", "USDC", "BUSD", "DAI"):
        # Quoting a stable in USDT is meaningless; use a stable->stable
        # pair so the chart renders flat 1.0 instead of crashing.
        return "USDCUSDT" if b != "USDC" else "USDCUSDT"
    return f"{b}USDT"


class BinanceProvider:
    name = "binance"
    asset_classes = (AssetClass.CRYPTO,)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = getattr(Config, "BINANCE_ENABLED", True)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "BINANCE_ENABLED=0")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.OHLC, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _BN_INTERVAL

    async def health(self) -> bool:
        return self._enabled

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._registry.rate.acquire(self.name)
        return await fetch_json(
            self.name, f"{_BN_BASE}{path}", params=params, timeout=8,
        )

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        # We don't own crypto symbol resolution -- CoinGecko's resolver
        # is the canonical answer (it carries the slug + name + thumb).
        # Returning None here lets the router move on to CoinGecko
        # for resolve; we get reached for OHLC via the fan-out table.
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        if not self._enabled:
            return None
        sym = _symbol_for(resolved.symbol)
        key = f"market:binance:quote:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None
        try:
            data = await self._get("/ticker/24hr", params={"symbol": sym})
        except MarketError as exc:
            log.debug("[binance] /ticker/24hr failed for %s: %s", sym, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            price = float(data.get("lastPrice") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        try:
            high = float(data.get("highPrice") or 0.0) or None
            low = float(data.get("lowPrice") or 0.0) or None
            vol_usd = float(data.get("quoteVolume") or 0.0) or None
            pct = float(data.get("priceChangePercent") or 0.0)
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
        if not self._enabled or tf not in _BN_INTERVAL:
            return []
        sym = _symbol_for(resolved.symbol)
        cache_key = f"market:binance:ohlc:{sym}:{tf}"
        cached = await self._registry.cache.get(cache_key)
        if cached is not None:
            return [Candle(**c) for c in cached]
        try:
            data = await self._get(
                "/klines",
                params={
                    "symbol": sym,
                    "interval": _BN_INTERVAL[tf],
                    "limit": 500,
                },
            )
        except MarketError as exc:
            log.debug("[binance] /klines failed for %s %s: %s", sym, tf, exc)
            return []
        if not isinstance(data, list) or not data:
            return []
        # Each kline: [open_time(ms), open, high, low, close, volume,
        # close_time(ms), quote_asset_volume, trades, ...].
        out: list[Candle] = []
        for row in data:
            try:
                ts = int(row[0]) // 1000
                o = float(row[1]); h = float(row[2])
                l = float(row[3]); c = float(row[4])
                v = float(row[5])
            except (TypeError, ValueError, IndexError):
                continue
            out.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
        if out:
            await self._registry.cache.set(
                cache_key, [to_cacheable(c) for c in out],
                getattr(Config, "REAL_MARKET_CACHE_TTL_OHLC", 60),
            )
        return out
