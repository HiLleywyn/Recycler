"""Coinbase Exchange public-data adapter.

Coinbase Exchange (the institutional/Pro API) ships free OHLC candles
at standard intervals over a public no-auth REST endpoint that's
explicitly designed to work from US IP ranges -- unlike binance.com
and bybit.com which 451 most US datacentre IPs (including most Railway
regions).

Endpoint: ``GET https://api.exchange.coinbase.com/products/<pair>/candles``
Params:   ``granularity=<seconds>&start=<iso>&end=<iso>``
Granularity: 60, 300, 900, 3600, 21600, 86400 (1m, 5m, 15m, 1h, 6h, 1d)

Symbols are ``<base>-USD`` (MTA-USD, ARC-USD, SOL-USD, ...). Coinbase
returns candles NEWEST-first as ``[time, low, high, open, close, volume]``.

Tradeoffs vs Binance/Bybit:
- No 1s candles. Smallest granularity is 1m.
- No 2h / 3m / 30m / 4h / 8h / 12h / 1w / 1mo native -- we synthesise
  4h from 1h and reject the others, letting the router move on.
- Has MTA-USD / ARC-USD / SOL-USD / etc; long-tail meme tokens may
  not be listed -- router falls through to CoinGecko for those.
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


_CB_BASE = "https://api.exchange.coinbase.com"

# Coinbase granularities (seconds). Anything else is rejected upstream.
_CB_NATIVE: dict[str, int] = {
    "1m":   60,
    "5m":   300,
    "15m":  900,
    "1h":   3600,
    "6h":   21600,
    "1d":   86400,
}
# Synthesised buckets (we fetch a finer granularity and re-aggregate
# on the client). Maps target tf -> (source tf code, target bucket secs).
_CB_SYNTH: dict[str, tuple[str, int]] = {
    "3m":   ("1m", 3 * 60),
    "30m":  ("15m", 30 * 60),
    "2h":   ("1h", 2 * 3600),
    "4h":   ("1h", 4 * 3600),
    "8h":   ("1h", 8 * 3600),
    "12h":  ("6h", 12 * 3600),
    "3d":   ("1d", 3 * 86400),
    "1w":   ("1d", 7 * 86400),
}


def _symbol_for(base: str) -> str:
    b = (base or "").upper()
    if b in ("USDT", "USDC", "USD"):
        return "USDC-USD"
    return f"{b}-USD"


def _aggregate(candles: list[Candle], bucket: int) -> list[Candle]:
    """Re-bucket finer-granularity candles into a coarser timeframe."""
    if not candles or bucket <= 0:
        return candles
    out: list[Candle] = []
    current: Candle | None = None
    current_ts = 0
    for c in candles:
        b = (c.ts // bucket) * bucket
        if current is None or b != current_ts:
            if current is not None:
                out.append(current)
            current = Candle(
                ts=b, open=c.open, high=c.high, low=c.low,
                close=c.close, volume=c.volume,
            )
            current_ts = b
        else:
            current.high = max(current.high, c.high)
            current.low = min(current.low, c.low)
            current.close = c.close
            current.volume += c.volume
    if current is not None:
        out.append(current)
    return out


class CoinbaseProvider:
    name = "coinbase"
    asset_classes = (AssetClass.CRYPTO,)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = getattr(Config, "COINBASE_ENABLED", True)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "COINBASE_ENABLED=0")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.OHLC, Capability.QUOTE})

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _CB_NATIVE or tf in _CB_SYNTH

    async def health(self) -> bool:
        return self._enabled

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._registry.rate.acquire(self.name)
        return await fetch_json(
            self.name, f"{_CB_BASE}{path}", params=params, timeout=8,
        )

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        # CoinGecko owns crypto resolve. Returning None lets the router
        # move on for symbol resolution; we still get reached for OHLC
        # via the fan-out table.
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        if not self._enabled:
            return None
        pair = _symbol_for(resolved.symbol)
        key = f"market:coinbase:quote:{pair}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None
        # /products/<pair>/ticker -> {price, size, bid, ask, volume, time}
        try:
            data = await self._get(f"/products/{pair}/ticker")
        except MarketError as exc:
            log.debug("[coinbase] ticker failed for %s: %s", pair, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            price = float(data.get("price") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        try:
            bid = float(data.get("bid") or 0.0) or None
            ask = float(data.get("ask") or 0.0) or None
            vol = float(data.get("volume") or 0.0) or None
        except (TypeError, ValueError):
            bid = ask = vol = None
        # 24h-stats endpoint for high / low / pct change. Best-effort:
        # if it 429s or fails, just skip those fields.
        day_high = day_low = pct_24h = None
        try:
            stats = await self._get(f"/products/{pair}/stats")
            if isinstance(stats, dict):
                try:
                    day_high = float(stats.get("high") or 0.0) or None
                    day_low = float(stats.get("low") or 0.0) or None
                    open_24h = float(stats.get("open") or 0.0)
                    if open_24h > 0:
                        pct_24h = (price - open_24h) / open_24h * 100.0
                except (TypeError, ValueError):
                    pass
        except MarketError:
            pass
        q = Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=AssetClass.CRYPTO,
            provider=self.name,
            ts=int(time.time()),
            bid=bid,
            ask=ask,
            day_high=day_high,
            day_low=day_low,
            day_volume_usd=(vol * price) if vol else None,
            pct_24h=pct_24h,
        )
        await self._registry.cache.set(key, to_cacheable(q), 10)
        return q

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        if not self._enabled:
            return []
        # Pick native granularity or synthesise from a finer one.
        if tf in _CB_NATIVE:
            granularity = _CB_NATIVE[tf]
            bucket = 0
        elif tf in _CB_SYNTH:
            source_tf, bucket = _CB_SYNTH[tf]
            granularity = _CB_NATIVE[source_tf]
        else:
            return []
        pair = _symbol_for(resolved.symbol)
        cache_key = f"market:coinbase:ohlc:{pair}:{tf}"
        cached = await self._registry.cache.get(cache_key)
        if cached is not None:
            return [Candle(**c) for c in cached]
        try:
            data = await self._get(
                f"/products/{pair}/candles",
                params={"granularity": granularity},
            )
        except MarketError as exc:
            log.debug("[coinbase] candles failed for %s %s: %s", pair, tf, exc)
            return []
        if not isinstance(data, list) or not data:
            return []
        # Coinbase returns each row as [time, low, high, open, close, volume]
        # NEWEST-first. Convert + sort ascending.
        out: list[Candle] = []
        for row in data:
            try:
                ts = int(row[0])
                low = float(row[1]); high = float(row[2])
                o = float(row[3]); c = float(row[4])
                v = float(row[5])
            except (TypeError, ValueError, IndexError):
                continue
            out.append(Candle(ts=ts, open=o, high=high, low=low, close=c, volume=v))
        out.sort(key=lambda c: c.ts)
        if bucket:
            out = _aggregate(out, bucket)
        if out:
            await self._registry.cache.set(
                cache_key, [to_cacheable(c) for c in out],
                getattr(Config, "REAL_MARKET_CACHE_TTL_OHLC", 60),
            )
        return out
