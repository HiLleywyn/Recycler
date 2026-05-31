"""Provider registry. Single source of truth for what's available.

Lives on the bot as ``bot._market_registry``; created lazily by
:func:`get_registry`. Owns the cache, rate-limiter, and health table so
every provider shares them.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AssetClass, Capability, MarketProvider
from .cache import MarketCache
from .health import HealthRegistry
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)


class Registry:
    """Bot-scoped collection of providers + shared infra."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.cache = MarketCache(bot)
        self.rate = RateLimiter()
        self.health = HealthRegistry()
        self._providers: dict[str, MarketProvider] = {}

    # ── lifecycle ──────────────────────────────────────────────────

    def register(self, provider: MarketProvider) -> None:
        self._providers[provider.name] = provider
        log.info(
            "[market.registry] registered provider=%s classes=%s caps=%s",
            provider.name,
            [c.value for c in provider.asset_classes],
            sorted(c.value for c in provider.capabilities()),
        )

    def get(self, name: str) -> MarketProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> dict[str, MarketProvider]:
        return dict(self._providers)

    # ── filtering ──────────────────────────────────────────────────

    def for_asset_class(self, ac: AssetClass) -> list[MarketProvider]:
        return [p for p in self._providers.values() if ac in p.asset_classes]

    def with_capability(self, cap: Capability) -> list[MarketProvider]:
        return [p for p in self._providers.values() if cap in p.capabilities()]

    def available(self, cap: Capability, ac: AssetClass) -> list[MarketProvider]:
        return [
            p for p in self._providers.values()
            if cap in p.capabilities()
            and ac in p.asset_classes
            and self.health.is_available(p.name)
        ]

    # ── health snapshot for $help / diagnostics ────────────────────

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name in self._providers:
            entry = self.health.get(name)
            out[name] = {
                "status": entry.status.value,
                "consecutive_failures": entry.consecutive_failures,
                "reason": entry.reason,
            }
        return out


def _build_registry(bot: Any) -> Registry:
    """Wire up every provider we ship. Each adapter checks its own keys
    and marks itself disabled if it can't run -- no provider raises at
    construction time."""
    reg = Registry(bot)

    # Import lazily so a syntax error in one adapter doesn't kill the
    # whole namespace.
    try:
        from .providers.coingecko import CoinGeckoProvider
        reg.register(CoinGeckoProvider(reg))
    except Exception:
        log.exception("[market.registry] coingecko load failed")
    try:
        from .providers.coinbase import CoinbaseProvider
        reg.register(CoinbaseProvider(reg))
    except Exception:
        log.exception("[market.registry] coinbase load failed")
    try:
        from .providers.binance import BinanceProvider
        reg.register(BinanceProvider(reg))
    except Exception:
        log.exception("[market.registry] binance load failed")
    try:
        from .providers.bybit import BybitProvider
        reg.register(BybitProvider(reg))
    except Exception:
        log.exception("[market.registry] bybit load failed")
    try:
        from .providers.yahoo import YahooProvider
        reg.register(YahooProvider(reg))
    except Exception:
        log.exception("[market.registry] yahoo load failed")
    try:
        from .providers.finnhub import FinnhubProvider
        reg.register(FinnhubProvider(reg))
    except Exception:
        log.exception("[market.registry] finnhub load failed")
    try:
        from .providers.dexscreener import DexScreenerProvider
        reg.register(DexScreenerProvider(reg))
    except Exception:
        log.exception("[market.registry] dexscreener load failed")
    try:
        from .providers.pyth import PythProvider
        reg.register(PythProvider(reg))
    except Exception:
        log.exception("[market.registry] pyth load failed")
    try:
        from .providers.redstone import RedStoneProvider
        reg.register(RedStoneProvider(reg))
    except Exception:
        log.exception("[market.registry] redstone load failed")
    try:
        from .providers.switchboard import SwitchboardProvider
        reg.register(SwitchboardProvider(reg))
    except Exception:
        log.exception("[market.registry] switchboard load failed")
    try:
        from .providers.coinglass import CoinGlassProvider
        reg.register(CoinGlassProvider(reg))
    except Exception:
        log.exception("[market.registry] coinglass load failed")
    try:
        from .providers.coinalyze import CoinalyzeProvider
        reg.register(CoinalyzeProvider(reg))
    except Exception:
        log.exception("[market.registry] coinalyze load failed")
    try:
        from .providers.tradingview import TradingViewProvider
        reg.register(TradingViewProvider(reg))
    except Exception:
        log.exception("[market.registry] tradingview load failed")

    return reg


def get_registry(bot: Any) -> Registry:
    """Lazy singleton accessor. Caches on the bot so every cog sees the
    same registry instance."""
    existing = getattr(bot, "_market_registry", None)
    if existing is not None:
        return existing
    reg = _build_registry(bot)
    setattr(bot, "_market_registry", reg)
    return reg
