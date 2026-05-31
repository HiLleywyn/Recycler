"""Cross-provider oracle aggregation.

Used by ``$oracle`` and the oracle panel of ``$info`` to medianise prices
across Pyth, RedStone, and Switchboard. Detects:

- **Stale feeds** -- publish age above threshold per asset class.
- **Divergence** -- per-provider price more than ``DIVERGENCE_PCT`` away
  from the median.
- **Volatility anomaly** -- confidence interval > ``VOL_ANOMALY_PCT`` of
  the price.

The router uses this when callers want the cross-validated answer rather
than a single provider's quote.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any

from .base import OracleQuote, ResolvedSymbol
from .router import MarketRouter

log = logging.getLogger(__name__)


STALE_AGE_SECONDS = 30.0
DIVERGENCE_PCT = 0.5      # 0.5% spread between providers triggers a flag
VOL_ANOMALY_PCT = 1.0     # confidence > 1% of price


@dataclass(slots=True)
class OracleAggregate:
    symbol: str
    median_usd: float
    quotes: list[OracleQuote]
    divergence_pct: float
    max_age: float
    has_stale: bool
    has_divergence: bool
    has_vol_anomaly: bool


async def aggregate_oracle(
    router: MarketRouter,
    resolved: ResolvedSymbol,
) -> OracleAggregate | None:
    """Fan out to every oracle provider and return a medianised quote.

    Returns ``None`` when no oracle provider can answer. The caller can
    fall back to a spot quote in that case (and display "Oracle: n/a").
    """
    names = ("pyth", "redstone", "switchboard")
    quotes: list[OracleQuote] = []
    for name in names:
        p = router.registry.get(name)
        if p is None or not router.registry.health.is_available(name):
            continue
        fn = getattr(p, "oracle_quote", None)
        if fn is None:
            continue
        try:
            q: OracleQuote | None = await fn(resolved)
        except Exception as exc:
            router.registry.health.mark_failure(name, f"oracle: {exc}")
            continue
        if q is not None:
            quotes.append(q)
            router.registry.health.mark_success(name)
    if not quotes:
        return None

    prices = [q.price_usd for q in quotes if q.price_usd > 0]
    if not prices:
        return None
    median = statistics.median(prices)
    spread = (max(prices) - min(prices)) / median if median else 0.0
    divergence_pct = spread * 100.0
    max_age = max(q.publish_age for q in quotes)
    has_stale = any(q.is_stale or q.publish_age > STALE_AGE_SECONDS for q in quotes)
    has_divergence = divergence_pct > DIVERGENCE_PCT
    has_vol_anomaly = any(
        median > 0 and (q.confidence / median) * 100.0 > VOL_ANOMALY_PCT
        for q in quotes
    )
    return OracleAggregate(
        symbol=resolved.symbol,
        median_usd=median,
        quotes=quotes,
        divergence_pct=divergence_pct,
        max_age=max_age,
        has_stale=has_stale,
        has_divergence=has_divergence,
        has_vol_anomaly=has_vol_anomaly,
    )


def twap(candles: list[dict[str, Any]], window_seconds: int) -> float | None:
    """Time-weighted average price over the trailing ``window_seconds``
    of the supplied candle list. Returns ``None`` if there's nothing in
    the window.
    """
    if not candles:
        return None
    now = int(time.time())
    cutoff = now - window_seconds
    in_window = [c for c in candles if int(c.get("ts", 0)) >= cutoff]
    if not in_window:
        return None
    total_weight = 0.0
    weighted = 0.0
    for idx, c in enumerate(in_window):
        ts = int(c.get("ts", 0))
        next_ts = int(in_window[idx + 1]["ts"]) if idx + 1 < len(in_window) else now
        dt = max(1, next_ts - ts)
        weighted += float(c.get("close", 0.0)) * dt
        total_weight += dt
    if total_weight <= 0:
        return None
    return weighted / total_weight
