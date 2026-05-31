"""Finnhub adapter.

Free tier (60 req/min): equity quotes, company news, earnings calendar,
basic fundamentals. Used by ``$info MSFT`` for the fundamentals + earnings
panel that Yahoo can't reliably provide.
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


_FH_BASE = "https://finnhub.io/api/v1"


class FinnhubProvider:
    name = "finnhub"
    asset_classes = (AssetClass.EQUITY, AssetClass.ETF, AssetClass.INDEX)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._key = getattr(Config, "FINNHUB_API_KEY", "") or ""
        if not self._key:
            registry.health.mark_disabled(self.name, "FINNHUB_API_KEY unset")

    def capabilities(self) -> frozenset[Capability]:
        caps = {
            Capability.QUOTE, Capability.NEWS, Capability.OVERVIEW,
            Capability.FUNDAMENTALS, Capability.EARNINGS,
        }
        return frozenset(caps)

    def supports_timeframe(self, tf: str) -> bool:
        # We rely on Yahoo for OHLC. Finnhub's candle endpoint is
        # paywalled on the free tier, so we don't advertise OHLC support
        # here -- the router will pick Yahoo or TradingView for charts.
        return False

    async def health(self) -> bool:
        return bool(self._key)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._key:
            return None
        await self._registry.rate.acquire(self.name)
        p = dict(params or {})
        p["token"] = self._key
        return await fetch_json(self.name, f"{_FH_BASE}{path}", params=p)

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        # Resolution path: rely on Yahoo. Finnhub's /search is fine but
        # Yahoo is broader. Returning None here lets the router move on.
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        if not self._key:
            return None
        key = f"market:finnhub:quote:{resolved.provider_id}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None
        try:
            data = await self._get("/quote", params={"symbol": resolved.provider_id})
        except MarketError:
            return None
        if not isinstance(data, dict):
            return None
        price = data.get("c")
        try:
            price = float(price or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        prev = float(data.get("pc") or price)
        pct = ((price - prev) / prev * 100.0) if prev else 0.0
        q = Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=resolved.asset_class,
            provider=self.name,
            ts=int(data.get("t") or time.time()),
            day_high=data.get("h"),
            day_low=data.get("l"),
            pct_24h=pct,
        )
        await self._registry.cache.set(key, to_cacheable(q), 30)
        return q

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        return []

    async def news(self, resolved: ResolvedSymbol, limit: int = 5) -> list[dict]:
        if not self._key:
            return []
        key = f"market:finnhub:news:{resolved.provider_id}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached[:limit]
        # Last 14 days of company news.
        end = time.strftime("%Y-%m-%d", time.gmtime())
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 14 * 86400))
        try:
            data = await self._get(
                "/company-news",
                params={
                    "symbol": resolved.provider_id,
                    "from": start,
                    "to": end,
                },
            )
        except MarketError:
            return []
        items: list[dict] = []
        for entry in (data or [])[:50]:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or "").strip()
            title = (entry.get("headline") or "").strip()
            if not (url and title):
                continue
            items.append({
                "title": title,
                "url": url,
                "source": (entry.get("source") or "").strip(),
                "ts": int(entry.get("datetime") or 0),
            })
            if len(items) >= limit * 2:
                break
        await self._registry.cache.set(
            key, items,
            getattr(Config, "REAL_MARKET_CACHE_TTL_NEWS", 300),
        )
        return items[:limit]

    async def fundamentals(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        key = f"market:finnhub:fund:{resolved.provider_id}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            profile = await self._get("/stock/profile2", params={"symbol": resolved.provider_id})
            metrics = await self._get(
                "/stock/metric",
                params={"symbol": resolved.provider_id, "metric": "all"},
            )
        except MarketError:
            return None
        out: dict[str, Any] = {}
        if isinstance(profile, dict):
            out["profile"] = {
                "name": profile.get("name"),
                "industry": profile.get("finnhubIndustry"),
                "country": profile.get("country"),
                "ipo": profile.get("ipo"),
                "marketCap": profile.get("marketCapitalization"),
                "url": profile.get("weburl"),
                "logo": profile.get("logo"),
            }
        if isinstance(metrics, dict):
            m = (metrics.get("metric") or {}) if isinstance(metrics.get("metric"), dict) else {}
            out["metric"] = {
                "pe": m.get("peNormalizedAnnual") or m.get("peBasicExclExtraTTM"),
                "pb": m.get("pbAnnual") or m.get("pbQuarterly"),
                "ev_ebitda": m.get("currentEv/freeCashFlowAnnual"),
                "52wk_high": m.get("52WeekHigh"),
                "52wk_low": m.get("52WeekLow"),
                "beta": m.get("beta"),
                "dividend_yield": m.get("dividendYieldIndicatedAnnual"),
            }
        await self._registry.cache.set(key, out, 3600)
        return out or None

    async def earnings(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        key = f"market:finnhub:earn:{resolved.provider_id}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        # Next two weeks of earnings.
        start = time.strftime("%Y-%m-%d", time.gmtime())
        end = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 30 * 86400))
        try:
            data = await self._get(
                "/calendar/earnings",
                params={
                    "symbol": resolved.provider_id,
                    "from": start, "to": end,
                },
            )
        except MarketError:
            return None
        rows = ((data or {}).get("earningsCalendar") or []) if isinstance(data, dict) else []
        out = {"upcoming": rows[:3]}
        await self._registry.cache.set(key, out, 6 * 3600)
        return out
