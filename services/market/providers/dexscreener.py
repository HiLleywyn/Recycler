"""DexScreener adapter.

Public REST API (no key) for DEX pair data across most EVM + Solana
chains. We use it for ``$info`` on tokens that don't show up on
CoinGecko (long-tail / brand-new launches) and as a sub-minute price
backup for the perp panel.
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


_DS_BASE = "https://api.dexscreener.com/latest/dex"


class DexScreenerProvider:
    name = "dexscreener"
    asset_classes = (AssetClass.DEX, AssetClass.CRYPTO)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = getattr(Config, "DEXSCREENER_ENABLED", True)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "DEXSCREENER_ENABLED=0")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({
            Capability.RESOLVE, Capability.QUOTE, Capability.SEARCH,
        })

    def supports_timeframe(self, tf: str) -> bool:
        return False  # no public OHLC; use other providers for charts

    async def health(self) -> bool:
        return self._enabled

    async def _get(self, path: str) -> Any:
        await self._registry.rate.acquire(self.name)
        return await fetch_json(self.name, f"{_DS_BASE}{path}", timeout=8)

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        sym = (symbol or "").strip()
        if not sym:
            return None
        key = f"market:dexscreener:resolve:{sym.lower()}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            if not cached:
                return None
            return ResolvedSymbol(
                symbol=cached["symbol"], name=cached["name"],
                asset_class=AssetClass.DEX, provider=self.name,
                provider_id=cached["pair_id"], image=cached.get("image", ""),
            )
        try:
            data = await self._get(f"/search?q={sym}")
        except MarketError:
            return None
        pairs = (data or {}).get("pairs") or []
        if not pairs:
            await self._registry.cache.set(key, {}, 600)
            return None
        # Prefer the pair with the largest 24h USD liquidity.
        def _liq(p: dict[str, Any]) -> float:
            try:
                return float((p.get("liquidity") or {}).get("usd") or 0.0)
            except Exception:
                return 0.0
        pairs.sort(key=_liq, reverse=True)
        top = pairs[0]
        base = (top.get("baseToken") or {})
        record = {
            "symbol": (base.get("symbol") or sym).upper(),
            "name": base.get("name") or base.get("symbol") or sym,
            "pair_id": top.get("pairAddress") or "",
            "chain": top.get("chainId") or "",
            "image": top.get("info", {}).get("imageUrl") or "",
        }
        await self._registry.cache.set(key, record, 3600)
        return ResolvedSymbol(
            symbol=record["symbol"], name=record["name"],
            asset_class=AssetClass.DEX, provider=self.name,
            provider_id=record["pair_id"], image=record["image"],
            extras={"chain": record["chain"]},
        )

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        if not resolved.provider_id:
            return None
        key = f"market:dexscreener:quote:{resolved.provider_id}"
        cached = await self._registry.cache.get(key)
        if cached is not None:
            return Quote(**cached) if cached else None
        chain = (resolved.extras or {}).get("chain") or ""
        path = f"/pairs/{chain}/{resolved.provider_id}" if chain else f"/pairs/{resolved.provider_id}"
        try:
            data = await self._get(path)
        except MarketError:
            return None
        pairs = (data or {}).get("pairs") or []
        if not pairs:
            return None
        p = pairs[0]
        try:
            price = float(p.get("priceUsd") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        q = Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=AssetClass.DEX,
            provider=self.name,
            ts=int(time.time()),
            day_volume_usd=float((p.get("volume") or {}).get("h24") or 0.0),
            pct_24h=float((p.get("priceChange") or {}).get("h24") or 0.0),
            market_cap_usd=p.get("fdv"),
            extras={"chain": p.get("chainId"), "dex": p.get("dexId")},
        )
        await self._registry.cache.set(key, to_cacheable(q), 20)
        return q

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        return []
