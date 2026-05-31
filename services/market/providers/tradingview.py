"""TradingView-compatible adapter (UDF protocol).

Activated only when ``TRADINGVIEW_UDF_URL`` is configured. Useful for
self-hosted Charting Library data backends or third-party UDF feeds.
Disabled by default so a vanilla deploy has zero TradingView dependency.
"""

from __future__ import annotations

import logging
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
from ._base_http import fetch_json

log = logging.getLogger(__name__)


# UDF interval mapping. TradingView's Charting Library treats numeric
# values as minutes; "D" = 1 day, "W" = 1 week, "M" = 1 month.
_TV_INTERVAL: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "45m": "45",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "8h": "480", "12h": "720",
    "1d": "D", "3d": "3D", "1w": "W", "1mo": "M", "3mo": "3M",
}


class TradingViewProvider:
    name = "tradingview"
    asset_classes = (
        AssetClass.CRYPTO,
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.FOREX,
        AssetClass.INDEX,
        AssetClass.COMMODITY,
    )

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._base = (getattr(Config, "TRADINGVIEW_UDF_URL", "") or "").rstrip("/")
        if not self._base:
            registry.health.mark_disabled(self.name, "TRADINGVIEW_UDF_URL unset")

    def capabilities(self) -> frozenset[Capability]:
        if not self._base:
            return frozenset()
        return frozenset({Capability.OHLC, Capability.QUOTE, Capability.SEARCH})

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _TV_INTERVAL

    async def health(self) -> bool:
        return bool(self._base)

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        return None

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        if not self._base or tf not in _TV_INTERVAL:
            return []
        await self._registry.rate.acquire(self.name)
        # UDF /history?symbol=...&resolution=...&from=...&to=...
        import time
        end = int(time.time())
        # Pull a reasonable window per timeframe.
        windows = {
            "1m": 86400, "3m": 3 * 86400, "5m": 7 * 86400, "15m": 14 * 86400,
            "30m": 30 * 86400, "45m": 30 * 86400, "1h": 60 * 86400,
            "2h": 90 * 86400, "4h": 120 * 86400, "6h": 180 * 86400,
            "8h": 180 * 86400, "12h": 365 * 86400,
            "1d": 5 * 365 * 86400, "3d": 5 * 365 * 86400,
            "1w": 10 * 365 * 86400, "1mo": 10 * 365 * 86400,
            "3mo": 10 * 365 * 86400,
        }
        start = end - windows.get(tf, 30 * 86400)
        try:
            data = await fetch_json(
                self.name, f"{self._base}/history",
                params={
                    "symbol": resolved.provider_id,
                    "resolution": _TV_INTERVAL[tf],
                    "from": start, "to": end,
                },
            )
        except MarketError:
            return []
        if not isinstance(data, dict) or data.get("s") != "ok":
            return []
        ts = data.get("t") or []
        o = data.get("o") or []
        h = data.get("h") or []
        l = data.get("l") or []
        c = data.get("c") or []
        v = data.get("v") or []
        out: list[Candle] = []
        for i, t in enumerate(ts):
            try:
                out.append(Candle(
                    ts=int(t),
                    open=float(o[i]), high=float(h[i]),
                    low=float(l[i]), close=float(c[i]),
                    volume=float(v[i] if i < len(v) else 0),
                ))
            except (TypeError, ValueError, IndexError):
                continue
        return out
