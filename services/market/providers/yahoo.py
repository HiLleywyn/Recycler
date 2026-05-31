"""Yahoo Finance adapter.

Public ``query1.finance.yahoo.com`` endpoints, no API key required.
Covers equities, ETFs, indices, forex, and commodities. We use the v8
chart endpoint for OHLCV and v10 quoteSummary for fundamentals.
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


_YH_BASE_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
_YH_BASE_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
# Note: the legacy v7 ``/finance/quote`` endpoint now sits behind
# Yahoo's crumb-and-cookie flow; we derive the quote from the v8 chart
# meta block instead. Kept undeclared so accidental references fail
# fast.

# Yahoo's intraday windows are gated by the ``range`` parameter. The
# ``interval`` parameter must be one of Yahoo's known values.
_YH_TF_INTERVAL: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "4h": "60m",   # synthesise 4h from 1h candles
    "1d": "1d", "1w": "1wk", "1mo": "1mo",
    "3mo": "1mo", "6mo": "1mo", "1y": "1mo", "all": "1mo",
}
_YH_TF_RANGE: dict[str, str] = {
    "1m": "5d", "5m": "1mo", "15m": "1mo", "30m": "1mo",
    "1h": "3mo", "4h": "6mo",
    "1d": "5y", "1w": "10y", "1mo": "max",
    "3mo": "max", "6mo": "max", "1y": "max", "all": "max",
}


def _asset_class_from_quote_type(qt: str) -> AssetClass:
    qt = (qt or "").upper()
    if qt in ("ETF", "MUTUALFUND"):
        return AssetClass.ETF
    if qt in ("EQUITY",):
        return AssetClass.EQUITY
    if qt in ("INDEX",):
        return AssetClass.INDEX
    if qt in ("CURRENCY",):
        return AssetClass.FOREX
    if qt in ("FUTURE", "COMMODITY"):
        return AssetClass.COMMODITY
    if qt in ("CRYPTOCURRENCY",):
        return AssetClass.CRYPTO
    return AssetClass.EQUITY


class YahooProvider:
    name = "yahoo"
    asset_classes = (
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.INDEX,
        AssetClass.FOREX,
        AssetClass.COMMODITY,
        AssetClass.CRYPTO,
    )

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = getattr(Config, "YAHOO_ENABLED", True)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "YAHOO_ENABLED=0")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({
            Capability.RESOLVE, Capability.QUOTE, Capability.OHLC,
            Capability.OVERVIEW, Capability.FUNDAMENTALS, Capability.SEARCH,
        })

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _YH_TF_INTERVAL

    async def health(self) -> bool:
        return self._enabled

    # ── helpers ──

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        await self._registry.rate.acquire(self.name)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Discoin/1.0; +https://discoin.app)",
            "Accept": "application/json",
        }
        return await fetch_json(self.name, url, params=params, headers=headers)

    # ── MarketProvider methods ────────────────────────────────────

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        sym = (symbol or "").strip()
        if not sym:
            return None
        sym_upper = sym.upper()
        key = f"market:yahoo:resolve:{sym.lower()}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            if not cached:
                return None
            return ResolvedSymbol(
                symbol=cached["symbol"],
                name=cached.get("name") or cached["symbol"],
                asset_class=AssetClass(cached.get("asset_class", "equity")),
                provider=self.name,
                provider_id=cached["symbol"],
                image=cached.get("image", ""),
            )
        try:
            data = await self._get(
                _YH_BASE_SEARCH,
                params={"q": sym_upper, "quotesCount": 20, "newsCount": 0},
            )
        except MarketError:
            return None
        quotes = (data or {}).get("quotes") or []
        # Ranking strategy. Yahoo's search ranks leveraged ETNs +
        # warrants + niche derivatives ABOVE the underlying common
        # stock for short tickers (``msft`` -> MSFTON), so we sort by
        # a tuple that explicitly prefers a primary listing on a major
        # exchange over an exact-symbol-string match.
        #
        # Priority:
        #   0. ``symbol`` exactly matches the query AND quoteType is
        #      EQUITY / ETF / INDEX / CURRENCY / FUTURE (the primary
        #      listing on a real exchange).
        #   1. ``symbol`` exactly matches the query (any quoteType).
        #   2. Highest Yahoo ``score`` value (their internal ranking).
        _PRIMARY_TYPES = {"EQUITY", "ETF", "INDEX", "CURRENCY", "FUTURE",
                          "MUTUALFUND", "CRYPTOCURRENCY"}
        def _rank(q: dict[str, Any]) -> tuple[int, int, int]:
            s = (q.get("symbol") or "").upper()
            qt = (q.get("quoteType") or "").upper()
            primary_exact = 0 if (s == sym_upper and qt in _PRIMARY_TYPES) else 1
            any_exact = 0 if s == sym_upper else 1
            return (primary_exact, any_exact, -int(q.get("score") or 0))
        quotes.sort(key=_rank)
        match = quotes[0] if quotes else None
        if not match:
            await self._registry.cache.set(key, {}, 600)
            return None
        record = {
            "symbol": (match.get("symbol") or sym).upper(),
            "name": match.get("longname") or match.get("shortname") or match.get("symbol"),
            "asset_class": _asset_class_from_quote_type(
                match.get("quoteType") or ""
            ).value,
            "image": "",
        }
        await self._registry.cache.set(key, record, 6 * 3600)
        return ResolvedSymbol(
            symbol=record["symbol"],
            name=record["name"],
            asset_class=AssetClass(record["asset_class"]),
            provider=self.name,
            provider_id=record["symbol"],
            image=record["image"],
        )

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        """Yahoo quote via the v8 ``chart`` endpoint.

        Yahoo's legacy v7 ``/finance/quote`` endpoint now requires a
        crumb + cookie flow (returns 200 with empty body otherwise),
        so we derive the quote from the v8 chart endpoint's ``meta``
        block instead -- it's the same data plane and still public.
        """
        key = f"market:yahoo:quote:{resolved.provider_id.lower()}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None

        url = f"{_YH_BASE_CHART}/{resolved.provider_id}"
        try:
            data = await self._get(
                url, params={"interval": "1d", "range": "5d"},
            )
        except MarketError:
            return None
        chart = ((data or {}).get("chart") or {}).get("result") or []
        if not chart:
            await self._registry.cache.set(key, {}, 60)
            return None
        result = chart[0]
        meta = result.get("meta") or {}
        try:
            price = float(meta.get("regularMarketPrice") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        try:
            prev_close_f = float(prev_close) if prev_close is not None else None
        except (TypeError, ValueError):
            prev_close_f = None
        pct_24h = None
        if prev_close_f and prev_close_f > 0:
            pct_24h = (price - prev_close_f) / prev_close_f * 100.0

        # Day high / low from the latest bar in the response (the meta
        # block doesn't always carry them across asset classes).
        timestamps = result.get("timestamp") or []
        ind_quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        highs = ind_quote.get("high") or []
        lows = ind_quote.get("low") or []
        vols = ind_quote.get("volume") or []
        day_high = day_low = day_vol = None
        if timestamps:
            try:
                day_high = float(highs[-1]) if highs and highs[-1] is not None else None
                day_low = float(lows[-1]) if lows and lows[-1] is not None else None
                last_vol = float(vols[-1]) if vols and vols[-1] is not None else 0.0
                day_vol = last_vol * price if last_vol else None
            except (TypeError, ValueError, IndexError):
                pass

        q = Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=resolved.asset_class,
            provider=self.name,
            ts=int(meta.get("regularMarketTime") or time.time()),
            day_high=day_high,
            day_low=day_low,
            day_volume_usd=day_vol,
            pct_24h=pct_24h,
            market_cap_usd=None,  # v8 chart meta doesn't carry market cap
            extras={
                "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"),
                "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
                "exchange": meta.get("exchangeName") or meta.get("fullExchangeName"),
                "currency": meta.get("currency"),
                "prev_close": prev_close_f,
            },
        )
        await self._registry.cache.set(key, to_cacheable(q), 30)
        return q

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        if tf not in _YH_TF_INTERVAL:
            raise MarketError(
                f"yahoo: timeframe {tf!r} not supported", provider=self.name,
            )
        interval = _YH_TF_INTERVAL[tf]
        range_ = _YH_TF_RANGE[tf]
        key = f"market:yahoo:ohlc:{resolved.provider_id}:{tf}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return [Candle(**c) for c in cached]
        await self._registry.rate.acquire(self.name)
        url = f"{_YH_BASE_CHART}/{resolved.provider_id}"
        try:
            data = await self._get(
                url, params={"interval": interval, "range": range_},
            )
        except MarketError:
            return []
        chart = ((data or {}).get("chart") or {}).get("result") or []
        if not chart:
            return []
        result = chart[0]
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        vols = quote.get("volume") or []
        candles: list[Candle] = []
        for i, ts in enumerate(timestamps):
            try:
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                l = lows[i] if i < len(lows) else None
                c = closes[i] if i < len(closes) else None
                v = vols[i] if i < len(vols) else 0
                if None in (o, h, l, c):
                    continue
                candles.append(Candle(
                    ts=int(ts),
                    open=float(o), high=float(h), low=float(l), close=float(c),
                    volume=float(v or 0),
                ))
            except (TypeError, ValueError):
                continue
        # Yahoo doesn't natively expose 4h candles. Aggregate from 1h.
        if tf == "4h":
            candles = _aggregate(candles, bucket=4 * 3600)
        await self._registry.cache.set(
            key, [to_cacheable(c) for c in candles],
            getattr(Config, "REAL_MARKET_CACHE_TTL_OHLC", 60),
        )
        return candles

    async def overview(self, resolved: ResolvedSymbol) -> dict | None:
        # Use the quote payload as the overview source -- it carries pe,
        # eps, 52w range, exchange, currency.
        q = await self.quote(resolved)
        if q is None:
            return None
        return {
            "id": resolved.provider_id,
            "symbol": resolved.symbol,
            "name": resolved.name,
            "asset_class": resolved.asset_class.value,
            "market_data": {
                "current_price": {"usd": q.price_usd},
                "high_24h": {"usd": q.day_high},
                "low_24h": {"usd": q.day_low},
                "total_volume": {"usd": q.day_volume_usd},
                "market_cap": {"usd": q.market_cap_usd},
                "price_change_percentage_24h_in_currency": {"usd": q.pct_24h},
            },
            **q.extras,
        }

    async def fundamentals(self, resolved: ResolvedSymbol) -> dict | None:
        q = await self.quote(resolved)
        return q.extras if q is not None else None


def _aggregate(candles: list[Candle], bucket: int) -> list[Candle]:
    """Re-bucket 1h candles into ``bucket``-second windows."""
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
            current = Candle(ts=b, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
            current_ts = b
        else:
            current.high = max(current.high, c.high)
            current.low = min(current.low, c.low)
            current.close = c.close
            current.volume += c.volume
    if current is not None:
        out.append(current)
    return out
