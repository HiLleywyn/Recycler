"""Structured scan snapshot builder.

Pulls indicators from the bot's existing chart engine (``core.framework.chart``)
and pattern matches from :mod:`services.pattern_scout`, packages them into
a single :class:`ScanSnapshot` dataclass that both the embed builder and
the AI mode consume.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IndicatorReadout:
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_middle: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    adx14: float | None = None
    atr14: float | None = None
    vwap: float | None = None


@dataclass(slots=True)
class DerivativesSnapshot:
    funding_rate: float | None = None
    open_interest_usd: float | None = None
    long_pct: float | None = None
    short_pct: float | None = None
    liq_long_24h_usd: float | None = None
    liq_short_24h_usd: float | None = None
    long_short_ratio: float | None = None


@dataclass(slots=True)
class OracleSnapshot:
    median_usd: float | None = None
    divergence_pct: float | None = None
    max_age_sec: float | None = None
    has_stale: bool = False
    has_divergence: bool = False
    provider_count: int = 0


@dataclass(slots=True)
class ScanSnapshot:
    symbol: str
    asset_class: str
    timeframe: str
    provider: str
    price_usd: float
    candles_count: int

    indicators: IndicatorReadout = field(default_factory=IndicatorReadout)
    derivatives: DerivativesSnapshot | None = None
    oracle: OracleSnapshot | None = None
    patterns: list[dict[str, Any]] = field(default_factory=list)

    momentum_score: float = 0.0     # -1..1
    trend_strength: float = 0.0     # 0..1 (ADX-normalised)
    volume_score: float = 0.0       # 0..1 relative to 20-bar SMA
    support: float | None = None
    resistance: float | None = None
    bias: str = "neutral"           # bullish / bearish / neutral

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_last(seq: list[float]) -> float | None:
    if not seq:
        return None
    val = seq[-1]
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return float(val)


def _compute_indicators(candles: list[dict[str, Any]]) -> IndicatorReadout:
    """Best-effort indicators using the framework's chart engine helpers.

    We import lazily so a missing optional dep doesn't crash the import
    chain at registry-build time.
    """
    out = IndicatorReadout()
    if not candles:
        return out

    closes = [float(c.get("close", 0.0)) for c in candles]
    highs = [float(c.get("high", 0.0)) for c in candles]
    lows = [float(c.get("low", 0.0)) for c in candles]
    vols = [float(c.get("volume", 0.0)) for c in candles]

    try:
        from core.framework import chart as fc  # type: ignore[attr-defined]
        ind = getattr(fc, "_indicators", None) or getattr(fc, "indicators", None)
    except Exception:
        ind = None

    if ind is not None:
        # Use the framework helpers when available.
        try:
            out.rsi14 = _safe_last(ind.rsi(closes, 14))
        except Exception:
            pass
        try:
            macd_line, signal, _hist = ind.macd(closes)
            out.macd = _safe_last(macd_line)
            out.macd_signal = _safe_last(signal)
        except Exception:
            pass
        try:
            upper, middle, lower = ind.bollinger(closes, 20, 2.0)
            out.bb_upper = _safe_last(upper)
            out.bb_middle = _safe_last(middle)
            out.bb_lower = _safe_last(lower)
        except Exception:
            pass
        try:
            out.ema20 = _safe_last(ind.ema(closes, 20))
            out.ema50 = _safe_last(ind.ema(closes, 50))
            out.ema200 = _safe_last(ind.ema(closes, 200))
        except Exception:
            pass
        try:
            out.adx14 = _safe_last(ind.adx(highs, lows, closes, 14))
        except Exception:
            pass
        try:
            out.atr14 = _safe_last(ind.atr(highs, lows, closes, 14))
        except Exception:
            pass
        try:
            out.vwap = _safe_last(ind.vwap(highs, lows, closes, vols))
        except Exception:
            pass
        return out

    # Fallback: minimal pure-python EMA/RSI when the framework helpers
    # aren't reachable. Keeps $scan returning something useful even on
    # boot-broken installs.
    if len(closes) >= 14:
        gains = []
        losses = []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            if d >= 0:
                gains.append(d)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-d)
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14 or 1e-12
        rs = avg_gain / avg_loss
        out.rsi14 = 100 - (100 / (1 + rs))
    if len(closes) >= 20:
        out.ema20 = sum(closes[-20:]) / 20
    if len(closes) >= 50:
        out.ema50 = sum(closes[-50:]) / 50
    if len(closes) >= 200:
        out.ema200 = sum(closes[-200:]) / 200
    return out


def _compute_scores(
    candles: list[dict[str, Any]],
    indicators: IndicatorReadout,
) -> tuple[float, float, float, str]:
    """Return (momentum, trend_strength, volume_score, bias)."""
    if not candles:
        return 0.0, 0.0, 0.0, "neutral"

    closes = [float(c.get("close", 0.0)) for c in candles]
    last = closes[-1] if closes else 0.0

    # Momentum: 14-bar percentage change clamped to -1..1.
    if len(closes) >= 14 and closes[-14] > 0:
        chg = (last - closes[-14]) / closes[-14]
        momentum = max(-1.0, min(1.0, chg * 5))   # scale so 20% move = +1.0
    else:
        momentum = 0.0

    # Trend strength from ADX (capped at 50 -> 1.0).
    if indicators.adx14 is not None:
        trend_strength = max(0.0, min(1.0, indicators.adx14 / 50.0))
    elif indicators.ema20 is not None and indicators.ema50 is not None:
        gap = abs(indicators.ema20 - indicators.ema50)
        trend_strength = max(0.0, min(1.0, gap / max(last, 1e-9) * 20))
    else:
        trend_strength = 0.0

    # Volume score: latest bar versus 20-bar SMA.
    vols = [float(c.get("volume", 0.0)) for c in candles[-20:]]
    if vols and sum(vols) > 0:
        avg = sum(vols) / len(vols)
        latest = float(candles[-1].get("volume", 0.0))
        volume_score = max(0.0, min(1.5, latest / max(avg, 1e-9))) / 1.5
    else:
        volume_score = 0.0

    bias = "neutral"
    if indicators.ema20 is not None and indicators.ema50 is not None:
        if last > indicators.ema20 > indicators.ema50:
            bias = "bullish"
        elif last < indicators.ema20 < indicators.ema50:
            bias = "bearish"
    return momentum, trend_strength, volume_score, bias


def _support_resistance(candles: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    if not candles:
        return None, None
    window = candles[-60:]
    highs = [float(c.get("high", 0.0)) for c in window]
    lows = [float(c.get("low", 0.0)) for c in window]
    if not highs or not lows:
        return None, None
    return min(lows), max(highs)


async def build_scan_snapshot(
    *,
    symbol: str,
    asset_class: str,
    timeframe: str,
    provider: str,
    candles: list[dict[str, Any]],
    patterns: list[dict[str, Any]] | None = None,
    derivatives: DerivativesSnapshot | None = None,
    oracle: OracleSnapshot | None = None,
) -> ScanSnapshot:
    """Single entry-point for the scan handler."""
    indicators = _compute_indicators(candles)
    momentum, trend_strength, volume_score, bias = _compute_scores(candles, indicators)
    support, resistance = _support_resistance(candles)
    price = float(candles[-1].get("close", 0.0)) if candles else 0.0
    return ScanSnapshot(
        symbol=symbol.upper(),
        asset_class=asset_class,
        timeframe=timeframe,
        provider=provider,
        price_usd=price,
        candles_count=len(candles),
        indicators=indicators,
        derivatives=derivatives,
        oracle=oracle,
        patterns=list(patterns or []),
        momentum_score=momentum,
        trend_strength=trend_strength,
        volume_score=volume_score,
        support=support,
        resistance=resistance,
        bias=bias,
    )
