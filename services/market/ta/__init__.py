"""Technical-analysis utilities consumed by ``$scan``.

Wraps the existing :mod:`services.chart_patterns` / :mod:`services.pattern_scout`
detectors so the new dispatcher can pull a structured snapshot of:

- indicator readings (RSI, MACD, BB, ADX, ATR, EMAs, VWAP)
- pattern matches (geometry + confidence)
- momentum + trend strength + liquidity scores
- derivatives state (funding, OI, liquidations) when available

The :func:`build_scan_snapshot` function is the single entry-point the
scan handler calls. AI mode receives the same dataclass and feeds it to
the LLM.
"""

from .scoring import ScanSnapshot, build_scan_snapshot

__all__ = ["ScanSnapshot", "build_scan_snapshot"]
