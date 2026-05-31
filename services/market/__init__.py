"""Cross-asset market data layer.

Provider-adapter architecture sitting under the ``$`` namespace. Talks to
CoinGecko, Yahoo Finance, Finnhub, DexScreener, Pyth Hermes, RedStone,
Switchboard, CoinGlass, Coinalyze, and TradingView-compatible feeds behind a
single :class:`MarketRouter` so every ``$`` handler asks the registry for a
provider that supports its ``(asset_class, timeframe, feature)`` instead of
hard-coding a vendor.

Anything that fans out across multiple providers (oracle medianisation,
derivatives aggregation, cross-asset comparison) lives here too, not in the
cog. The cog is just the dispatch + embed layer.
"""

from __future__ import annotations

from .base import (
    AssetClass,
    Candle,
    Capability,
    MarketProvider,
    OracleQuote,
    Quote,
    ResolvedSymbol,
)
from .registry import Registry, get_registry
from .router import MarketRouter, get_router
from .timeframes import (
    SUPPORTED_TIMEFRAMES,
    Timeframe,
    canonical_tf,
    tf_seconds,
)

__all__ = [
    "AssetClass",
    "Candle",
    "Capability",
    "MarketProvider",
    "MarketRouter",
    "OracleQuote",
    "Quote",
    "Registry",
    "ResolvedSymbol",
    "SUPPORTED_TIMEFRAMES",
    "Timeframe",
    "canonical_tf",
    "get_registry",
    "get_router",
    "tf_seconds",
]
