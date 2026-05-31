"""Coinalyze adapter -- backup derivatives source.

Used when CoinGlass is unavailable or rate-limited. Free tier supplies
funding rates, OI, and perp basis with a key. Without
``COINALYZE_API_KEY`` the provider is disabled.
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


_CA_BASE = "https://api.coinalyze.net/v1"


class CoinalyzeProvider:
    name = "coinalyze"
    asset_classes = (AssetClass.PERP, AssetClass.CRYPTO)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._key = getattr(Config, "COINALYZE_API_KEY", "") or ""
        if not self._key:
            registry.health.mark_disabled(self.name, "COINALYZE_API_KEY unset")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({
            Capability.FUNDING, Capability.OPEN_INTEREST,
        })

    def supports_timeframe(self, tf: str) -> bool:
        return False

    async def health(self) -> bool:
        return bool(self._key)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._key:
            return None
        await self._registry.rate.acquire(self.name)
        p = dict(params or {})
        p["api_key"] = self._key
        return await fetch_json(self.name, f"{_CA_BASE}{path}", params=p)

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        return None

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        return None

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        return []

    async def funding(self, resolved: ResolvedSymbol) -> dict | None:
        """OI-weighted current funding rate across exchanges.

        Coinalyze symbols are exchange-specific (e.g. ``BTCUSD_PERP.A``
        for Binance, ``.6`` for Bybit). A bare ``MTA`` query returns
        empty, so we discover supported perp markets for the base via
        ``/future-markets`` and query funding for all of them in one
        batched call. Markets list is cached for 6h since exchanges
        rarely add new perp pairs.
        """
        if not self._key:
            return None
        sym = resolved.symbol.upper()
        key = f"market:coinalyze:funding:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None

        symbols = await self._discover_perp_symbols(sym)
        if not symbols:
            log.debug(
                "[coinalyze] no perp symbols discovered for base %s -- "
                "funding endpoint will return empty",
                sym,
            )
            return None

        try:
            data = await self._get(
                "/funding-rate",
                params={"symbols": ",".join(symbols[:30])},
            )
        except MarketError as exc:
            log.debug("[coinalyze] funding query failed: %s", exc)
            return None
        rows = data if isinstance(data, list) else []
        if not rows:
            log.debug(
                "[coinalyze] /funding-rate returned empty for %s "
                "(tried %d symbol(s): %s ...)",
                sym, len(symbols), symbols[:3],
            )
            return None
        rates = []
        for r in rows:
            try:
                rates.append(float(r.get("value") or 0.0))
            except (TypeError, ValueError):
                continue
        if not rates:
            return None
        avg = sum(rates) / len(rates)
        out = {
            "weighted_rate": avg,
            "per_exchange": [
                {"exchange": r.get("market"), "rate": r.get("value")}
                for r in rows[:8]
            ],
            "ts": int(time.time()),
        }
        await self._registry.cache.set(key, out, 30)
        return out

    async def _discover_perp_symbols(self, base: str) -> list[str]:
        """Return Coinalyze exchange-specific perp symbols for a base
        ticker (MTA -> [BTCUSD_PERP.A, BTCUSDT_PERP.A, BTCUSD_PERP.6, ...]).

        Cached for 6 hours -- exchanges add new perp pairs roughly never.
        """
        cache_key = f"market:coinalyze:markets:{base}"
        cached = await self._registry.cache.get(cache_key)
        if cached is not None:
            return list(cached) if isinstance(cached, list) else []
        try:
            data = await self._get("/future-markets")
        except MarketError as exc:
            log.debug("[coinalyze] /future-markets failed: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        base_lower = base.lower()
        symbols: list[str] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol") or ""
            row_base = (row.get("base_asset") or "").lower()
            # Match on the explicit base_asset field when present, or
            # fall back to a prefix-on-symbol check for older API rows
            # that don't carry the field.
            if row_base == base_lower or sym.lower().startswith(f"{base_lower}usd"):
                if "_PERP" in sym:
                    symbols.append(sym)
        if symbols:
            await self._registry.cache.set(cache_key, symbols, 6 * 3600)
        return symbols

    async def open_interest(self, resolved: ResolvedSymbol) -> dict | None:
        if not self._key:
            return None
        sym = resolved.symbol.upper()
        key = f"market:coinalyze:oi:{sym}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return cached or None

        symbols = await self._discover_perp_symbols(sym)
        if not symbols:
            return None
        try:
            data = await self._get(
                "/open-interest",
                params={"symbols": ",".join(symbols[:30])},
            )
        except MarketError as exc:
            log.debug("[coinalyze] open-interest query failed: %s", exc)
            return None
        rows = data if isinstance(data, list) else []
        if not rows:
            log.debug(
                "[coinalyze] /open-interest returned empty for %s "
                "(%d symbol(s) queried)", sym, len(symbols),
            )
            return None
        total = 0.0
        per_ex = []
        for r in rows:
            try:
                oi = float(r.get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            total += oi
            per_ex.append({"exchange": r.get("market"), "oi_usd": oi})
        out = {
            "total_usd": total,
            "per_exchange": sorted(per_ex, key=lambda x: x["oi_usd"], reverse=True)[:8],
            "ts": int(time.time()),
        }
        await self._registry.cache.set(key, out, 30)
        return out
