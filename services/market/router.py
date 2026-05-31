"""High-level entry point for cogs. ``MarketRouter`` resolves a symbol to
an asset class, picks providers from the registry, retries on failure,
and falls back to the next provider in the chain.

Cogs should call ``router.resolve(symbol)`` then any of ``.quote()``,
``.ohlc()``, ``.overview()``, ``.oracle()``, ``.funding()`` etc.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    AssetClass,
    Candle,
    Capability,
    MarketError,
    OracleQuote,
    Quote,
    ResolvedSymbol,
)
from .registry import Registry, get_registry
from .timeframes import (
    SUPPORTED_TIMEFRAMES,
    canonical_tf,
    providers_for_timeframe,
)

log = logging.getLogger(__name__)


# Heuristic ticker classifier used when no provider returns a direct match
# during symbol-resolution. Crypto tickers tend to be 3-5 letters; ETF /
# equity tickers are 1-5 letters. We disambiguate by trying CoinGecko
# first for known-major-crypto symbols and falling through to Yahoo.
_KNOWN_CRYPTO_TOP_SYMBOLS: frozenset[str] = frozenset({
    "mta", "arc", "sol", "doge", "ada", "xrp", "ltc", "bch", "etc",
    "bnb", "matic", "avax", "atom", "dot", "near", "link", "trx",
    "ton", "shib", "uni", "fil", "icp", "apt", "arb", "op", "sui",
    "str", "wif", "bonk", "rndr", "inj", "tia", "sei", "kas",
    "fet", "stx", "imx", "ldo", "mnt", "vet", "vtr",
})


class MarketRouter:
    """Single per-bot router. ``get_router(bot)`` returns the cached
    instance."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    # ── symbol resolution ─────────────────────────────────────────

    async def resolve(self, raw: str) -> ResolvedSymbol | None:
        """Find the best provider+id pair for a user-typed symbol."""
        sym = (raw or "").strip()
        if not sym:
            return None
        key = sym.lower()

        # Order of attack: known-major-crypto first to keep MTA/ARC/etc
        # routing to CoinGecko, then walk through providers that expose
        # RESOLVE in registry order, finishing with a Yahoo fallback for
        # equities/ETFs/forex.
        order: list[str] = []
        if key in _KNOWN_CRYPTO_TOP_SYMBOLS:
            order.extend(["coingecko", "dexscreener", "yahoo", "finnhub"])
        else:
            order.extend(["yahoo", "finnhub", "coingecko", "dexscreener"])

        for name in order:
            p = self.registry.get(name)
            if p is None or not self.registry.health.is_available(p.name):
                continue
            if Capability.RESOLVE not in p.capabilities():
                continue
            try:
                hit = await p.resolve(sym)
            except Exception as exc:
                self.registry.health.mark_failure(p.name, f"resolve: {exc}")
                continue
            if hit is not None:
                self.registry.health.mark_success(p.name)
                return hit

        return None

    # ── quote ─────────────────────────────────────────────────────

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        provider = self.registry.get(resolved.provider)
        if provider is not None and self.registry.health.is_available(provider.name):
            try:
                q = await provider.quote(resolved)
            except Exception as exc:
                self.registry.health.mark_failure(provider.name, f"quote: {exc}")
                q = None
            if q is not None:
                self.registry.health.mark_success(provider.name)
                return q
        # Fall back to any provider in the same asset class.
        for p in self.registry.available(Capability.QUOTE, resolved.asset_class):
            if p.name == resolved.provider:
                continue
            try:
                q = await p.quote(resolved)
            except Exception as exc:
                self.registry.health.mark_failure(p.name, f"quote-fallback: {exc}")
                continue
            if q is not None:
                self.registry.health.mark_success(p.name)
                return q
        return None

    # ── OHLC ──────────────────────────────────────────────────────

    async def ohlc(
        self,
        resolved: ResolvedSymbol,
        tf_raw: str,
    ) -> tuple[list[Candle], str]:
        """Return ``(candles, provider_name)``. Raises :class:`MarketError`
        if the timeframe is invalid or no provider supports the
        combination."""
        tf = canonical_tf(tf_raw)
        if tf is None:
            raise MarketError(
                f"timeframe {tf_raw!r} not supported "
                f"(supported: {', '.join(SUPPORTED_TIMEFRAMES)})"
            )
        order = providers_for_timeframe(resolved.asset_class.value, tf)
        if not order:
            raise MarketError(
                f"no provider supports {tf} candles for "
                f"{resolved.asset_class.value}",
            )
        last_exc: Exception | None = None
        for name in order:
            p = self.registry.get(name)
            if p is None or not self.registry.health.is_available(p.name):
                continue
            if Capability.OHLC not in p.capabilities():
                continue
            if not p.supports_timeframe(tf):
                continue
            try:
                candles = await p.ohlc(resolved, tf)
            except Exception as exc:
                last_exc = exc
                self.registry.health.mark_failure(p.name, f"ohlc({tf}): {exc}")
                continue
            if candles:
                self.registry.health.mark_success(p.name)
                return candles, p.name
        if last_exc is not None:
            raise MarketError(
                f"all providers failed for {resolved.symbol} {tf}: {last_exc}",
            )
        raise MarketError(
            f"no healthy provider for {resolved.symbol} {tf}",
        )

    # ── feature-specific accessors (best-effort, may return None) ─

    async def _call_optional(
        self, cap: Capability, ac: AssetClass, method: str, *args: Any,
    ) -> Any:
        for p in self.registry.available(cap, ac):
            fn = getattr(p, method, None)
            if fn is None:
                continue
            try:
                out = await fn(*args)
            except Exception as exc:
                self.registry.health.mark_failure(p.name, f"{method}: {exc}")
                continue
            if out is not None:
                self.registry.health.mark_success(p.name)
                return out
        return None

    async def overview(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.OVERVIEW, resolved.asset_class, "overview", resolved,
        )

    async def funding(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.FUNDING, AssetClass.PERP, "funding", resolved,
        )

    async def open_interest(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.OPEN_INTEREST, AssetClass.PERP, "open_interest", resolved,
        )

    async def liquidations(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.LIQUIDATIONS, AssetClass.PERP, "liquidations", resolved,
        )

    async def long_short(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.LONG_SHORT, AssetClass.PERP, "long_short", resolved,
        )

    async def news(self, resolved: ResolvedSymbol, limit: int = 5) -> list[dict]:
        result = await self._call_optional(
            Capability.NEWS, resolved.asset_class, "news", resolved, limit,
        )
        return result or []

    async def fundamentals(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.FUNDAMENTALS, resolved.asset_class, "fundamentals", resolved,
        )

    async def earnings(self, resolved: ResolvedSymbol) -> dict | None:
        return await self._call_optional(
            Capability.EARNINGS, resolved.asset_class, "earnings", resolved,
        )

    async def oracle(self, resolved: ResolvedSymbol) -> OracleQuote | None:
        """Pyth first, RedStone second, Switchboard third. Returns the
        first quote that's not stale, or the freshest available even if
        marked stale (with ``is_stale=True``)."""
        order = ("pyth", "redstone", "switchboard")
        fallback: OracleQuote | None = None
        for name in order:
            p = self.registry.get(name)
            if p is None or not self.registry.health.is_available(p.name):
                continue
            fn = getattr(p, "oracle_quote", None)
            if fn is None:
                continue
            try:
                q: OracleQuote | None = await fn(resolved)
            except Exception as exc:
                self.registry.health.mark_failure(p.name, f"oracle: {exc}")
                continue
            if q is None:
                continue
            self.registry.health.mark_success(p.name)
            if not q.is_stale:
                return q
            if fallback is None or q.publish_age < fallback.publish_age:
                fallback = q
        return fallback


def get_router(bot: Any) -> MarketRouter:
    """Per-bot cached router. Pairs 1:1 with :func:`get_registry`."""
    existing = getattr(bot, "_market_router", None)
    if existing is not None:
        return existing
    router = MarketRouter(get_registry(bot))
    setattr(bot, "_market_router", router)
    return router
