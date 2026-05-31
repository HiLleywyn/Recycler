"""Canonical timeframe catalogue + per-provider fallback table.

The ``$`` namespace exposes 24 timeframes from ``1s`` to ``all``. Each entry
maps to a duration in seconds and a recommended provider-fan-out order.
Sub-minute and ultra-long horizons are best-effort: the router skips
providers that don't support the requested granularity, with the choice
visible in :func:`providers_for_timeframe`.
"""

from __future__ import annotations

from dataclasses import dataclass

ALL_HORIZON_SECONDS: int = 10 * 365 * 86400  # treat ``all`` as ~10 years


@dataclass(slots=True, frozen=True)
class Timeframe:
    code: str
    seconds: int           # bucket size for OHLC aggregation
    horizon_seconds: int   # how far back we fetch by default
    label: str


_TFS: tuple[Timeframe, ...] = (
    Timeframe("1s",   1,            5 * 60,             "1 second"),
    Timeframe("5s",   5,            30 * 60,            "5 seconds"),
    Timeframe("15s",  15,           60 * 60,            "15 seconds"),
    Timeframe("30s",  30,           2 * 60 * 60,        "30 seconds"),
    Timeframe("1m",   60,           4 * 60 * 60,        "1 minute"),
    Timeframe("3m",   3 * 60,       12 * 60 * 60,       "3 minutes"),
    Timeframe("5m",   5 * 60,       86400,              "5 minutes"),
    Timeframe("15m",  15 * 60,      3 * 86400,          "15 minutes"),
    Timeframe("30m",  30 * 60,      7 * 86400,          "30 minutes"),
    Timeframe("45m",  45 * 60,      10 * 86400,         "45 minutes"),
    Timeframe("1h",   3600,         14 * 86400,         "1 hour"),
    Timeframe("2h",   2 * 3600,     30 * 86400,         "2 hours"),
    Timeframe("4h",   4 * 3600,     60 * 86400,         "4 hours"),
    Timeframe("6h",   6 * 3600,     90 * 86400,         "6 hours"),
    Timeframe("8h",   8 * 3600,     120 * 86400,        "8 hours"),
    Timeframe("12h",  12 * 3600,    180 * 86400,        "12 hours"),
    Timeframe("1d",   86400,        365 * 86400,        "1 day"),
    Timeframe("3d",   3 * 86400,    2 * 365 * 86400,    "3 days"),
    Timeframe("1w",   7 * 86400,    3 * 365 * 86400,    "1 week"),
    Timeframe("1mo",  30 * 86400,   5 * 365 * 86400,    "1 month"),
    Timeframe("3mo",  90 * 86400,   8 * 365 * 86400,    "3 months"),
    Timeframe("6mo",  180 * 86400,  10 * 365 * 86400,   "6 months"),
    Timeframe("1y",   365 * 86400,  ALL_HORIZON_SECONDS, "1 year"),
    Timeframe("all",  365 * 86400,  ALL_HORIZON_SECONDS, "all-time"),
)

_BY_CODE: dict[str, Timeframe] = {tf.code: tf for tf in _TFS}

# Common aliases users type in chat.
_ALIASES: dict[str, str] = {
    "60s": "1m", "60sec": "1m",
    "5min": "5m", "15min": "15m", "30min": "30m",
    "1hr": "1h", "1hour": "1h", "1H": "1h",
    "4hr": "4h", "4hour": "4h", "4H": "4h",
    "12hr": "12h", "12hour": "12h",
    "daily": "1d", "day": "1d", "1D": "1d",
    "weekly": "1w", "week": "1w", "1W": "1w", "7d": "1w",
    "monthly": "1mo", "month": "1mo", "30d": "1mo", "M": "1mo",
    "quarter": "3mo", "q": "3mo", "90d": "3mo",
    "year": "1y", "1Y": "1y", "365d": "1y",
    "max": "all", "alltime": "all", "lifetime": "all",
}

SUPPORTED_TIMEFRAMES: tuple[str, ...] = tuple(tf.code for tf in _TFS)


def canonical_tf(raw: str | None) -> str | None:
    """Normalise user input to a canonical code.

    Returns ``None`` if the input doesn't match anything we understand --
    callers should hand back :data:`SUPPORTED_TIMEFRAMES` to the user as a
    hint via ``ctx.reply_error_hint``.
    """
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _BY_CODE:
        return key
    return _ALIASES.get(key) or _ALIASES.get(raw.strip())


def tf_seconds(code: str) -> int:
    tf = _BY_CODE.get(code)
    if tf is None:
        raise KeyError(f"unknown timeframe {code!r}")
    return tf.seconds


def tf_horizon_seconds(code: str) -> int:
    tf = _BY_CODE.get(code)
    if tf is None:
        raise KeyError(f"unknown timeframe {code!r}")
    return tf.horizon_seconds


def tf_label(code: str) -> str:
    tf = _BY_CODE.get(code)
    return tf.label if tf else code


# ── Provider-fanout hints ───────────────────────────────────────────────
#
# The router walks each ordered tuple top-to-bottom and skips providers
# that are unhealthy, missing keys, or don't support the asset class for
# the resolved symbol. ``coingecko`` stays first for crypto OHLC because
# the existing service has the symbol-resolution cache warmed.

_CRYPTO_OHLC_BY_TF: dict[str, tuple[str, ...]] = {
    # 1s: Binance is the ONLY public source globally. Geo-blocked from
    # most US datacentres -- if Binance is 🔴 in $status, 1s candles
    # aren't available.
    "1s":   ("binance",),
    "5s":   ("binance",),
    "15s":  ("binance",),
    "30s":  ("binance",),
    # 1m+: Coinbase first (US-friendly, public, no geo-block), then
    # Binance + Bybit (faster + more symbols but US-blocked), then
    # CoinGecko (rate-limited free tier), then TradingView UDF
    # (recursion-safe self-hosted bridge).
    "1m":   ("coinbase", "binance", "bybit", "tradingview"),
    "3m":   ("coinbase", "binance", "bybit", "tradingview", "coingecko"),
    "5m":   ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "15m":  ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "30m":  ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "45m":  ("binance", "coingecko", "tradingview"),
    "1h":   ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "2h":   ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "4h":   ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "6h":   ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "8h":   ("coinbase", "binance", "coingecko", "tradingview"),
    "12h":  ("coinbase", "binance", "bybit", "coingecko", "tradingview"),
    "1d":   ("coinbase", "binance", "bybit", "coingecko", "tradingview", "yahoo"),
    "3d":   ("coinbase", "binance", "coingecko", "tradingview"),
    "1w":   ("coinbase", "binance", "bybit", "coingecko", "tradingview", "yahoo"),
    "1mo":  ("binance", "bybit", "coingecko", "yahoo", "tradingview"),
    "3mo":  ("coingecko", "yahoo"),
    "6mo":  ("coingecko", "yahoo"),
    "1y":   ("coingecko", "yahoo"),
    "all":  ("coingecko", "yahoo"),
}

_EQUITY_OHLC_BY_TF: dict[str, tuple[str, ...]] = {
    "1m":   ("yahoo", "finnhub", "tradingview"),
    "5m":   ("yahoo", "finnhub", "tradingview"),
    "15m":  ("yahoo", "finnhub", "tradingview"),
    "30m":  ("yahoo", "finnhub", "tradingview"),
    "1h":   ("yahoo", "finnhub", "tradingview"),
    "4h":   ("yahoo", "finnhub", "tradingview"),
    "1d":   ("yahoo", "finnhub", "tradingview"),
    "1w":   ("yahoo", "finnhub", "tradingview"),
    "1mo":  ("yahoo", "finnhub", "tradingview"),
    "3mo":  ("yahoo", "tradingview"),
    "6mo":  ("yahoo", "tradingview"),
    "1y":   ("yahoo", "tradingview"),
    "all":  ("yahoo", "tradingview"),
}

_FOREX_OHLC_BY_TF: dict[str, tuple[str, ...]] = {
    "1m":   ("yahoo", "tradingview"),
    "5m":   ("yahoo", "tradingview"),
    "15m":  ("yahoo", "tradingview"),
    "30m":  ("yahoo", "tradingview"),
    "1h":   ("yahoo", "tradingview"),
    "4h":   ("yahoo", "tradingview"),
    "1d":   ("yahoo", "tradingview"),
    "1w":   ("yahoo", "tradingview"),
    "1mo":  ("yahoo", "tradingview"),
    "1y":   ("yahoo", "tradingview"),
    "all":  ("yahoo", "tradingview"),
}


def providers_for_timeframe(asset_class: str, tf: str) -> tuple[str, ...]:
    """Ordered fallback list of provider names for the given asset class +
    timeframe. The router consults this when picking a provider for an
    OHLC request. Unknown combinations yield ``()`` so the router can
    return a clear "no provider supports this timeframe for that asset"
    error rather than silently failing."""
    ac = (asset_class or "").lower()
    if ac in ("crypto", "perp", "dex"):
        return _CRYPTO_OHLC_BY_TF.get(tf, ())
    if ac in ("equity", "etf", "index", "commodity"):
        return _EQUITY_OHLC_BY_TF.get(tf, ())
    if ac == "forex":
        return _FOREX_OHLC_BY_TF.get(tf, ())
    return ()
