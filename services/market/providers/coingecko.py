"""CoinGecko adapter.

Wraps the existing :class:`services.real_market.RealMarketClient` so the
new provider architecture stays drop-in compatible with everything the
``$`` namespace already does. We delegate every call to the legacy
client (which holds the symbol-resolution cache, retry logic, and the
overview/news/tickers helpers ``$info`` depends on) and translate the
results into provider-agnostic types.
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

log = logging.getLogger(__name__)

# Map our 24-code timeframe table to what CoinGecko's free tier actually
# supports. Anything outside this list falls through to another provider.
_CG_TF_MAP: dict[str, str] = {
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}


class CoinGeckoProvider:
    name = "coingecko"
    asset_classes = (AssetClass.CRYPTO,)

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._enabled = bool(Config.REAL_MARKET_ENABLED)
        if not self._enabled:
            registry.health.mark_disabled(self.name, "REAL_MARKET_ENABLED=0")
        # Defer importing the legacy client to keep registry construction
        # cheap if the cog hasn't loaded it yet.
        self._legacy: Any = None

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({
            Capability.RESOLVE, Capability.QUOTE, Capability.OHLC,
            Capability.OVERVIEW, Capability.NEWS, Capability.MARKETS,
            Capability.TRENDING, Capability.GLOBAL, Capability.SEARCH,
        })

    def supports_timeframe(self, tf: str) -> bool:
        return tf in _CG_TF_MAP

    async def health(self) -> bool:
        return self._enabled

    # ── legacy client wiring ──────────────────────────────────────

    def _client(self) -> Any:
        if self._legacy is not None:
            return self._legacy
        bot = self._registry.bot
        client = getattr(bot, "real_market", None)
        if client is None:
            try:
                from services.real_market import RealMarketClient
                client = RealMarketClient(bot)
                bot.real_market = client
            except Exception as exc:
                raise MarketError(
                    f"coingecko: failed to construct legacy client: {exc}",
                    provider=self.name,
                ) from exc
        self._legacy = client
        return client

    # ── MarketProvider methods ────────────────────────────────────

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        rec = await self._client().resolve_symbol(symbol)
        if not rec:
            return None
        return ResolvedSymbol(
            symbol=(rec.get("symbol") or symbol).upper(),
            name=rec.get("name") or symbol,
            asset_class=AssetClass.CRYPTO,
            provider=self.name,
            provider_id=rec.get("id") or symbol.lower(),
            rank=rec.get("market_cap_rank"),
            image=rec.get("thumb") or "",
        )

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        # Reuse the overview payload; it has price + 24h volume + change.
        try:
            ov = await self._client().get_overview(resolved.provider_id)
        except Exception as exc:
            log.debug("[coingecko] overview failed for quote: %s", exc)
            return None
        md = ov.get("market_data") or {}
        price = (md.get("current_price") or {}).get("usd")
        if price is None:
            return None
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        return Quote(
            symbol=resolved.symbol,
            price_usd=price,
            asset_class=AssetClass.CRYPTO,
            provider=self.name,
            day_high=(md.get("high_24h") or {}).get("usd"),
            day_low=(md.get("low_24h") or {}).get("usd"),
            day_volume_usd=(md.get("total_volume") or {}).get("usd"),
            pct_24h=(md.get("price_change_percentage_24h_in_currency") or {}).get("usd"),
            market_cap_usd=(md.get("market_cap") or {}).get("usd"),
            extras={
                "ath": (md.get("ath") or {}).get("usd"),
                "atl": (md.get("atl") or {}).get("usd"),
                "circulating_supply": md.get("circulating_supply"),
                "total_supply": md.get("total_supply"),
                "max_supply": md.get("max_supply"),
            },
        )

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        if tf not in _CG_TF_MAP:
            raise MarketError(
                f"coingecko: timeframe {tf!r} not supported",
                provider=self.name,
            )
        raw = await self._client().get_ohlc(resolved.provider_id, _CG_TF_MAP[tf])
        out: list[Candle] = []
        for c in raw or []:
            try:
                out.append(Candle(
                    ts=int(c.get("ts", 0)),
                    open=float(c.get("open", 0.0)),
                    high=float(c.get("high", 0.0)),
                    low=float(c.get("low", 0.0)),
                    close=float(c.get("close", 0.0)),
                    volume=float(c.get("volume", 0.0)),
                ))
            except (TypeError, ValueError):
                continue
        return out

    # ── extras the router can call when this provider declares them ─

    async def overview(self, resolved: ResolvedSymbol) -> dict | None:
        try:
            return await self._client().get_overview(resolved.provider_id)
        except Exception:
            return None

    async def news(self, resolved: ResolvedSymbol, limit: int = 5) -> list[dict]:
        try:
            return await self._client().get_news(
                resolved.name, resolved.symbol, limit=limit,
            )
        except Exception:
            return []

    # ── market-wide helpers (used by $market handler) ─────────────

    async def global_(self) -> dict | None:
        try:
            return await self._client().get_global()
        except Exception:
            return None

    async def markets(self, per_page: int = 25, page: int = 1, order: str = "market_cap_desc") -> list[dict]:
        try:
            return await self._client().get_markets(
                order=order, per_page=per_page, page=page,
            )
        except Exception:
            return []

    async def trending(self) -> list[dict]:
        try:
            return await self._client().get_trending()
        except Exception:
            return []

    async def fear_greed(self) -> dict | None:
        try:
            return await self._client().get_fear_greed()
        except Exception:
            return None

    async def simple_price(self, ids: list[str], vs: str = "usd") -> dict[str, float]:
        try:
            return await self._client().get_simple_price(ids, vs=vs)
        except Exception:
            return {}
