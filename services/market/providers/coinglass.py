"""CoinGlass adapter.

Funding rates, open interest, liquidations, long/short ratio for perps.
Free tier requires ``COINGLASS_API_KEY``; without one the provider is
disabled and the perp panel inside ``$info`` shows a "n/a" notice.
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
from ._base_http import fetch_json

log = logging.getLogger(__name__)


_CG_BASE = "https://open-api-v4.coinglass.com/api"


class CoinGlassProvider:
    name = "coinglass"
    asset_classes = (AssetClass.PERP, AssetClass.CRYPTO)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._key = getattr(Config, "COINGLASS_API_KEY", "") or ""
        if not self._key:
            registry.health.mark_disabled(self.name, "COINGLASS_API_KEY unset")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({
            Capability.FUNDING, Capability.OPEN_INTEREST,
            Capability.LIQUIDATIONS, Capability.LONG_SHORT,
        })

    def supports_timeframe(self, tf: str) -> bool:
        return False

    async def health(self) -> bool:
        return bool(self._key)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._key:
            return None
        await self._registry.rate.acquire(self.name)
        headers = {"accept": "application/json", "CG-API-KEY": self._key}
        return await fetch_json(
            self.name, f"{_CG_BASE}{path}", params=params, headers=headers,
        )

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        return None

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        return []

    async def funding(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        sym = resolved.symbol.upper().replace("USDT", "")
        key = f"market:coinglass:funding:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            data = await self._get(
                "/futures/funding-rate/exchange-list",
                params={"symbol": sym},
            )
        except MarketError as exc:
            log.debug("[coinglass] funding query failed: %s", exc)
            return None
        rows = (data or {}).get("data") or []
        if not rows:
            # Diagnostic: surface the raw response shape when we get
            # back nothing. Common causes: wrong symbol shape (bare MTA
            # vs BTCUSDT), endpoint moved, free tier doesn't include
            # this path. The key/code/msg fields give the operator a
            # concrete signal to debug against.
            if isinstance(data, dict):
                log.debug(
                    "[coinglass] funding empty for %s -- code=%s msg=%s "
                    "keys=%s",
                    sym, data.get("code"), data.get("msg"),
                    sorted(data.keys()),
                )
            else:
                log.debug(
                    "[coinglass] funding empty for %s -- response type %s",
                    sym, type(data).__name__,
                )
            return None
        # Weighted-average current funding rate across exchanges.
        total_oi = 0.0
        weighted = 0.0
        per_exchange: list[dict[str, Any]] = []
        for r in rows:
            try:
                rate = float(r.get("fundingRate") or 0.0)
                oi = float(r.get("openInterestUsd") or 0.0)
            except (TypeError, ValueError):
                continue
            if oi <= 0:
                continue
            per_exchange.append({
                "exchange": r.get("exchangeName"),
                "rate": rate,
                "oi_usd": oi,
            })
            weighted += rate * oi
            total_oi += oi
        avg = (weighted / total_oi) if total_oi else 0.0
        out = {
            "weighted_rate": avg,
            "per_exchange": per_exchange[:8],
            "ts": int(time.time()),
        }
        await self._registry.cache.set(key, out, 30)
        return out

    async def open_interest(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        sym = resolved.symbol.upper().replace("USDT", "")
        key = f"market:coinglass:oi:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            data = await self._get(
                "/futures/open-interest/exchange-list",
                params={"symbol": sym},
            )
        except MarketError:
            return None
        rows = (data or {}).get("data") or []
        total = 0.0
        per_ex: list[dict[str, Any]] = []
        for r in rows:
            try:
                oi = float(r.get("openInterestUsd") or 0.0)
            except (TypeError, ValueError):
                continue
            if oi > 0:
                total += oi
                per_ex.append({
                    "exchange": r.get("exchangeName"),
                    "oi_usd": oi,
                    "pct_24h": r.get("oiChangePercent24h"),
                })
        out = {
            "total_usd": total,
            "per_exchange": sorted(per_ex, key=lambda x: x["oi_usd"], reverse=True)[:8],
            "ts": int(time.time()),
        }
        await self._registry.cache.set(key, out, 30)
        return out

    async def liquidations(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        sym = resolved.symbol.upper().replace("USDT", "")
        key = f"market:coinglass:liq:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            data = await self._get(
                "/futures/liquidation/aggregated-history",
                params={"symbol": sym, "interval": "h1", "limit": 24},
            )
        except MarketError:
            return None
        rows = (data or {}).get("data") or []
        long_total = 0.0
        short_total = 0.0
        for r in rows:
            try:
                long_total += float(r.get("longLiquidationUsd") or 0.0)
                short_total += float(r.get("shortLiquidationUsd") or 0.0)
            except (TypeError, ValueError):
                continue
        out = {
            "long_usd_24h": long_total,
            "short_usd_24h": short_total,
            "ts": int(time.time()),
        }
        await self._registry.cache.set(key, out, 60)
        return out

    async def long_short(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        sym = resolved.symbol.upper().replace("USDT", "")
        key = f"market:coinglass:ls:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            data = await self._get(
                "/futures/global-long-short-account-ratio",
                params={"symbol": sym, "interval": "h1", "limit": 1},
            )
        except MarketError:
            return None
        rows = (data or {}).get("data") or []
        if not rows:
            return None
        r = rows[-1]
        try:
            out = {
                "long_pct": float(r.get("longAccount") or 0.0),
                "short_pct": float(r.get("shortAccount") or 0.0),
                "ratio": float(r.get("longShortRatio") or 0.0),
                "ts": int(r.get("createTime") or time.time()),
            }
        except (TypeError, ValueError):
            return None
        await self._registry.cache.set(key, out, 60)
        return out
