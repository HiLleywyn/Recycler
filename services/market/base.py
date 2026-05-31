"""Core types + protocol for market providers.

Every adapter under :mod:`services.market.providers` implements
:class:`MarketProvider`. The router uses ``capabilities()`` and ``health()``
to decide which provider answers a given request.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class AssetClass(str, enum.Enum):
    """High-level asset taxonomy. Drives which provider fan-out runs."""

    CRYPTO = "crypto"
    DEX = "dex"
    EQUITY = "equity"
    ETF = "etf"
    FOREX = "forex"
    COMMODITY = "commodity"
    INDEX = "index"
    PERP = "perp"
    ORACLE = "oracle"
    UNKNOWN = "unknown"


class Capability(str, enum.Enum):
    """Granular capability flags. Used by :func:`MarketRouter.pick` to
    short-circuit providers that don't expose the requested feature."""

    RESOLVE = "resolve"
    QUOTE = "quote"
    OHLC = "ohlc"
    OVERVIEW = "overview"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"
    EARNINGS = "earnings"
    ORACLE_PRICE = "oracle_price"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    LIQUIDATIONS = "liquidations"
    LONG_SHORT = "long_short"
    MARKETS = "markets"
    TRENDING = "trending"
    GLOBAL = "global"
    FEAR_GREED = "fear_greed"
    SEARCH = "search"


@dataclass(slots=True)
class ResolvedSymbol:
    """Provider-agnostic symbol record. ``provider_id`` is the upstream
    identifier (e.g. ``"moneta"`` for CoinGecko, ``"MSFT"`` for Yahoo).
    """

    symbol: str
    name: str
    asset_class: AssetClass
    provider: str
    provider_id: str
    rank: int | None = None
    image: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Quote:
    """Spot quote in USD. ``ts`` is unix epoch seconds (provider's clock)."""

    symbol: str
    price_usd: float
    asset_class: AssetClass
    provider: str
    ts: int = 0
    bid: float | None = None
    ask: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_volume_usd: float | None = None
    pct_24h: float | None = None
    market_cap_usd: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Candle:
    """Single OHLCV bar. ``ts`` is the bar's open time in unix epoch
    seconds. ``volume`` is best-effort USD (or base-unit volume when USD
    is unavailable from the provider)."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_chart_dict(self) -> dict[str, float | int]:
        """Shape expected by :mod:`core.framework.chart`."""
        return {
            "ts": int(self.ts),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
        }


@dataclass(slots=True)
class OracleQuote:
    """Cross-validated oracle quote with confidence interval and freshness.
    ``publish_age`` is seconds since the upstream feed last published.
    ``confidence`` is the provider's reported ±band in USD."""

    symbol: str
    price_usd: float
    confidence: float
    publish_ts: int
    publish_age: float
    provider: str
    feed_id: str = ""
    is_stale: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


class MarketError(Exception):
    """Generic upstream-failure marker. Carries the upstream HTTP status
    when available so the router can decide whether to retry / fall back."""

    def __init__(self, message: str, *, status: int = 0, provider: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


@runtime_checkable
class MarketProvider(Protocol):
    """Minimum surface every provider exposes. Optional methods raise
    :class:`NotImplementedError` -- the router only calls methods that the
    provider declares in :meth:`capabilities`."""

    name: str
    asset_classes: tuple[AssetClass, ...]

    def capabilities(self) -> frozenset[Capability]:
        ...

    def supports_timeframe(self, tf: str) -> bool:
        ...

    async def health(self) -> bool:
        ...

    async def resolve(self, symbol: str) -> ResolvedSymbol | None:
        ...

    async def quote(self, resolved: ResolvedSymbol) -> Quote | None:
        ...

    async def ohlc(self, resolved: ResolvedSymbol, tf: str) -> list[Candle]:
        ...
