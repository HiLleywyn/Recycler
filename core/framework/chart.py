"""Pure chart helpers -- indicator math, candle aggregation, layout flag
parsing, and the headless-browser render pipeline.

The game chart (:mod:`cogs.trade`) and the real-crypto chart
(:mod:`cogs.realmarket`) both call into this module so the indicator math
and chart rendering live in exactly one place (per the CLAUDE.md
"Single source of truth" rule). Everything here is OHLC-shape agnostic
-- it doesn't know or care whether the candles came from the simulated
``price_candles`` table or from CoinGecko.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════════

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "charts" / "template.html"

_TIMEFRAMES = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}

_INDICATOR_RE = re.compile(
    r"^(ema|sma|wma|rsi|stoch|atr|roc|wpr|cci|mfi|don|kel)(\d+)$",
    re.IGNORECASE,
)
# Flags that change layout/theme/series rather than adding an indicator.
_LAYOUT_FLAGS = frozenset({
    "wide", "tall", "light", "dark", "minimal",
    "line", "area", "candles", "heikinashi", "heikin", "ha", "bars",
    "log", "linear",
})
# Single-token indicator keywords (no numeric suffix).
_INDICATOR_FLAGS = frozenset({
    "rsi", "macd", "bb", "vol", "volume", "vwap", "obv", "adx",
    "stoch", "atr", "supertrend", "st", "psar", "sar", "ichimoku",
    "ichi", "donchian", "don", "keltner", "kel", "pivot", "pivots",
    "roc", "wpr", "williams", "cci", "mfi", "mom", "momentum",
    "trend",   # convenience alias: ema20+ema50+ema200
    "all",     # everything sensible at once
})


def default_layout() -> dict:
    """Return a fresh layout dict with all the defaults the chart command
    starts from. Callers mutate this in place."""
    return {
        "view": "default",        # default | wide | tall
        "theme": "dark",          # dark | light
        "chrome": "default",      # default | minimal
        "candle_type": "candles", # candles | line | area | heikinashi | bars
        "log_scale": False,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Indicator math (pure Python / numpy-free)
# ══════════════════════════════════════════════════════════════════════════════

def _ema(closes: list[float], n: int) -> list[float]:
    if not closes or n <= 0:
        return []
    k = 2 / (n + 1)
    result = [closes[0]]
    for c in closes[1:]:
        result.append(c * k + result[-1] * (1 - k))
    return result


def _sma(closes: list[float], n: int) -> list[float | None]:
    if len(closes) < n or n <= 0:
        return [None] * len(closes)
    result: list[float | None] = [None] * (n - 1)
    for i in range(n - 1, len(closes)):
        result.append(sum(closes[i - n + 1: i + 1]) / n)
    return result


def _wma(closes: list[float], n: int) -> list[float | None]:
    """Linearly-weighted moving average -- weight 1..n, newest heaviest."""
    if len(closes) < n or n <= 0:
        return [None] * len(closes)
    result: list[float | None] = [None] * (n - 1)
    denom = n * (n + 1) / 2.0
    for i in range(n - 1, len(closes)):
        s = sum((j + 1) * closes[i - n + 1 + j] for j in range(n))
        result.append(s / denom)
    return result


def _rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs_values: list[float | None] = [None] * period
    for i in range(period, len(closes) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        rs_values.append(100 - 100 / (1 + rs))
    return rs_values


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram) as lists."""
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line = _ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, sig_line)]
    return macd_line, sig_line, histogram


def _bb(closes: list[float], period: int = 20, std_mult: float = 2.0):
    """Returns (upper, mid, lower) as lists."""
    mid = _sma(closes, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i, m in enumerate(mid):
        if m is None or i < period - 1:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1: i + 1]
            mean = sum(window) / period
            variance = sum((x - mean) ** 2 for x in window) / period
            std = math.sqrt(variance)
            upper.append(m + std_mult * std)
            lower.append(m - std_mult * std)
    return upper, mid, lower


def _stoch(
    highs: list[float], lows: list[float], closes: list[float],
    k_period: int = 14, d_period: int = 3,
) -> tuple[list[float | None], list[float | None]]:
    """Stochastic oscillator -- returns (%K, %D)."""
    n = len(closes)
    if n < k_period:
        return [None] * n, [None] * n
    k_line: list[float | None] = [None] * (k_period - 1)
    for i in range(k_period - 1, n):
        hi = max(highs[i - k_period + 1: i + 1])
        lo = min(lows[i - k_period + 1: i + 1])
        denom = hi - lo
        k_line.append(100.0 * (closes[i] - lo) / denom if denom > 0 else 50.0)
    d_line: list[float | None] = []
    for i, _ in enumerate(k_line):
        if i < d_period - 1 or any(k_line[j] is None for j in range(i - d_period + 1, i + 1)):
            d_line.append(None)
        else:
            d_line.append(
                sum(k_line[j] for j in range(i - d_period + 1, i + 1)) / d_period
            )
    return k_line, d_line


def _true_range(highs, lows, closes) -> list[float]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    return tr


def _atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14,
) -> list[float | None]:
    """Average True Range -- Wilder smoothing."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    tr = _true_range(highs, lows, closes)
    atr: list[float | None] = [None] * (period - 1)
    first = sum(tr[:period]) / period
    atr.append(first)
    for i in range(period, n):
        atr.append((atr[-1] * (period - 1) + tr[i]) / period)
    return atr


def _adx(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """ADX with +DI and -DI. Returns (adx, plus_di, minus_di)."""
    n = len(closes)
    if n < period * 2:
        return [None] * n, [None] * n, [None] * n
    plus_dm, minus_dm, tr = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    sm_pdm = sum(plus_dm[1: period + 1])
    sm_mdm = sum(minus_dm[1: period + 1])
    sm_tr = sum(tr[1: period + 1])
    plus_di: list[float | None] = [None] * (period)
    minus_di: list[float | None] = [None] * (period)
    dx_series: list[float] = []
    for i in range(period, n):
        if i > period:
            sm_pdm = sm_pdm - (sm_pdm / period) + plus_dm[i]
            sm_mdm = sm_mdm - (sm_mdm / period) + minus_dm[i]
            sm_tr  = sm_tr  - (sm_tr / period)  + tr[i]
        if sm_tr <= 0:
            plus_di.append(0.0)
            minus_di.append(0.0)
            dx_series.append(0.0)
            continue
        p = 100.0 * sm_pdm / sm_tr
        m = 100.0 * sm_mdm / sm_tr
        plus_di.append(p)
        minus_di.append(m)
        denom = p + m
        dx_series.append(100.0 * abs(p - m) / denom if denom > 0 else 0.0)
    adx: list[float | None] = [None] * (period * 2 - 1)
    if len(dx_series) >= period:
        first = sum(dx_series[:period]) / period
        adx.append(first)
        for i in range(period, len(dx_series)):
            adx.append((adx[-1] * (period - 1) + dx_series[i]) / period)
    while len(adx) < n:
        adx.append(None)
    return adx[:n], plus_di[:n], minus_di[:n]


def _obv(closes: list[float], volumes: list[float]) -> list[float]:
    """On-Balance Volume -- running volume-weighted accumulation."""
    n = len(closes)
    if n == 0:
        return []
    obv = [0.0]
    for i in range(1, n):
        v = float(volumes[i] or 0.0)
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + v)
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - v)
        else:
            obv.append(obv[-1])
    return obv


def _vwap(
    highs: list[float], lows: list[float], closes: list[float],
    volumes: list[float],
) -> list[float | None]:
    """Cumulative VWAP over the entire visible window."""
    n = len(closes)
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        v = float(volumes[i] or 0.0)
        cum_pv += tp * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out


def _roc(closes: list[float], period: int = 10) -> list[float | None]:
    """Rate of Change -- pct momentum over ``period`` bars."""
    n = len(closes)
    if n <= period:
        return [None] * n
    out: list[float | None] = [None] * period
    for i in range(period, n):
        prev = closes[i - period]
        out.append(100.0 * (closes[i] - prev) / prev if prev else 0.0)
    return out


def _williams_r(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Williams %R -- inverted stochastic, -100..0."""
    n = len(closes)
    if n < period:
        return [None] * n
    out: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, n):
        hi = max(highs[i - period + 1: i + 1])
        lo = min(lows[i - period + 1: i + 1])
        denom = hi - lo
        out.append(-100.0 * (hi - closes[i]) / denom if denom > 0 else -50.0)
    return out


def _cci(
    highs: list[float], lows: list[float], closes: list[float], period: int = 20,
) -> list[float | None]:
    """Commodity Channel Index."""
    n = len(closes)
    if n < period:
        return [None] * n
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    out: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, n):
        window = tp[i - period + 1: i + 1]
        sma = sum(window) / period
        mad = sum(abs(x - sma) for x in window) / period
        out.append((tp[i] - sma) / (0.015 * mad) if mad > 0 else 0.0)
    return out


def _mfi(
    highs: list[float], lows: list[float], closes: list[float],
    volumes: list[float], period: int = 14,
) -> list[float | None]:
    """Money Flow Index -- volume-weighted RSI."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    pos = [0.0] * n
    neg = [0.0] * n
    for i in range(1, n):
        flow = tp[i] * (volumes[i] or 0.0)
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    out: list[float | None] = [None] * period
    for i in range(period, n):
        p = sum(pos[i - period + 1: i + 1])
        m = sum(neg[i - period + 1: i + 1])
        if m == 0:
            out.append(100.0)
        else:
            ratio = p / m
            out.append(100.0 - 100.0 / (1.0 + ratio))
    return out


def _donchian(
    highs: list[float], lows: list[float], period: int = 20,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Donchian channel -- highest high / lowest low / midline."""
    n = len(highs)
    upper: list[float | None] = [None] * (period - 1) if n >= period else [None] * n
    lower: list[float | None] = list(upper)
    mid:   list[float | None] = list(upper)
    for i in range(period - 1, n):
        hi = max(highs[i - period + 1: i + 1])
        lo = min(lows[i - period + 1: i + 1])
        upper.append(hi)
        lower.append(lo)
        mid.append((hi + lo) / 2.0)
    return upper[:n], mid[:n], lower[:n]


def _keltner(
    closes: list[float], highs: list[float], lows: list[float],
    period: int = 20, mult: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Keltner channel -- EMA midline +/- mult * ATR."""
    mid = _ema(closes, period)
    atr = _atr(highs, lows, closes, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for m, a in zip(mid, atr):
        if a is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(m + mult * a)
            lower.append(m - mult * a)
    return upper, list(mid), lower


def _supertrend(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 10, mult: float = 3.0,
) -> tuple[list[float | None], list[int]]:
    """SuperTrend -- trailing trend line with explicit bull/bear flag."""
    n = len(closes)
    atr = _atr(highs, lows, closes, period)
    line: list[float | None] = [None] * n
    direction: list[int] = [0] * n
    upper = [0.0] * n
    lower = [0.0] * n
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        upper[i] = hl2 + mult * atr[i]
        lower[i] = hl2 - mult * atr[i]
        if i == 0 or atr[i - 1] is None:
            final_upper[i] = upper[i]
            final_lower[i] = lower[i]
            direction[i] = 1
            line[i] = final_lower[i]
            continue
        final_upper[i] = (
            upper[i] if (upper[i] < final_upper[i - 1] or closes[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower[i] if (lower[i] > final_lower[i - 1] or closes[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
        prev_dir = direction[i - 1] or 1
        if prev_dir == 1 and closes[i] < final_lower[i]:
            direction[i] = -1
        elif prev_dir == -1 and closes[i] > final_upper[i]:
            direction[i] = 1
        else:
            direction[i] = prev_dir
        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
    return line, direction


def _psar(
    highs: list[float], lows: list[float],
    accel: float = 0.02, max_accel: float = 0.2,
) -> tuple[list[float | None], list[int]]:
    """Parabolic SAR -- returns (sar, direction)."""
    n = len(highs)
    if n < 2:
        return [None] * n, [0] * n
    sar: list[float | None] = [None] * n
    direction: list[int] = [0] * n
    bull = highs[1] >= highs[0]
    af = accel
    ep = highs[1] if bull else lows[1]
    sar[1] = lows[0] if bull else highs[0]
    direction[1] = 1 if bull else -1
    for i in range(2, n):
        prev_sar = sar[i - 1]
        if bull:
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = min(new_sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < new_sar:
                bull = False
                new_sar = ep
                ep = lows[i]
                af = accel
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + accel, max_accel)
        else:
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = max(new_sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > new_sar:
                bull = True
                new_sar = ep
                ep = highs[i]
                af = accel
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + accel, max_accel)
        sar[i] = new_sar
        direction[i] = 1 if bull else -1
    return sar, direction


def _ichimoku(
    highs: list[float], lows: list[float], closes: list[float],
    conv: int = 9, base: int = 26, span_b: int = 52, displ: int = 26,
) -> dict:
    """Ichimoku Kinko Hyo."""
    n = len(closes)
    def _midrange(period: int, idx: int) -> float | None:
        if idx < period - 1:
            return None
        hi = max(highs[idx - period + 1: idx + 1])
        lo = min(lows[idx - period + 1: idx + 1])
        return (hi + lo) / 2.0
    tenkan = [_midrange(conv, i) for i in range(n)]
    kijun = [_midrange(base, i) for i in range(n)]
    senkou_a: list[float | None] = []
    for t, k in zip(tenkan, kijun):
        senkou_a.append((t + k) / 2.0 if (t is not None and k is not None) else None)
    senkou_b = [_midrange(span_b, i) for i in range(n)]
    chikou: list[float | None] = [None] * n
    for i in range(n - displ):
        chikou[i] = closes[i + displ]
    return {
        "tenkan": tenkan, "kijun": kijun,
        "senkou_a": senkou_a, "senkou_b": senkou_b,
        "chikou": chikou, "displacement": displ,
    }


def _heikin_ashi(candles: list[dict]) -> list[dict]:
    """Convert OHLC candles into Heikin-Ashi smoothed candles."""
    if not candles:
        return []
    out: list[dict] = []
    prev_open: float | None = None
    prev_close: float | None = None
    for c in candles:
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        ha_close = (o + h + l + cl) / 4.0
        ha_open = (prev_open + prev_close) / 2.0 if prev_open is not None else (o + cl) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        out.append({
            "ts": c["ts"],
            "open": ha_open, "high": ha_high,
            "low": ha_low, "close": ha_close,
            "volume": c.get("volume", 0.0),
        })
        prev_open, prev_close = ha_open, ha_close
    return out


def _pivots_classic(highs: list[float], lows: list[float], closes: list[float]) -> dict | None:
    """Classic floor-trader pivots from the most recent fully-closed bar."""
    if not closes:
        return None
    h, l, c = highs[-1], lows[-1], closes[-1]
    p = (h + l + c) / 3.0
    return {
        "P":  p,
        "R1": 2 * p - l,
        "R2": p + (h - l),
        "R3": h + 2 * (p - l),
        "S1": 2 * p - h,
        "S2": p - (h - l),
        "S3": l - 2 * (h - p),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Candle aggregation + render
# ══════════════════════════════════════════════════════════════════════════════

def _aggregate(candles: list[dict], tf_seconds: int) -> list[dict]:
    """Aggregate 1-min candles into larger timeframe candles."""
    if not candles:
        return []
    buckets: dict[int, dict] = {}
    for c in candles:
        bucket_ts = (c["ts"] // tf_seconds) * tf_seconds
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {
                "ts": bucket_ts,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0.0),
            }
        else:
            b = buckets[bucket_ts]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] = b["volume"] + c.get("volume", 0.0)
    return sorted(buckets.values(), key=lambda x: x["ts"])


def _to_lwc(candles: list[dict]) -> list[dict]:
    """Convert to Lightweight Charts format (time key)."""
    return [{"time": c["ts"], "open": c["open"], "high": c["high"],
             "low": c["low"], "close": c["close"]} for c in candles]


async def _render_chart(
    html_path: str, *, width: int = 1200, height: int = 700,
) -> bytes:
    """Open HTML in headless Chromium, screenshot, return PNG bytes."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(
            viewport={"width": int(width), "height": int(height)},
            device_scale_factor=2,
        )
        await page.goto(f"file:///{html_path}")
        await page.wait_for_timeout(500)
        screenshot = await page.screenshot(type="png", full_page=False)
        await browser.close()
    return screenshot


# ══════════════════════════════════════════════════════════════════════════════
#  Flag parser + chart-data assembly
# ══════════════════════════════════════════════════════════════════════════════

def parse_chart_args(
    args_iter: list[str],
    valid_symbols: frozenset[str] | set[str] | None,
    *,
    primary: str | None = None,
) -> tuple[dict, list[str], str | None, list[str]]:
    """Parse the flag/indicator token stream from a chart command.

    Returns ``(layout, compare_syms, quote_in, clean_inds)`` where:

    - ``layout`` is the layout dict (view/theme/chrome/candle_type/log_scale)
    - ``compare_syms`` is a list of normalised-to-100 overlay symbols
    - ``quote_in`` is an optional re-quote symbol (or None)
    - ``clean_inds`` is the leftover indicator keywords (rsi, macd, ema20, ...)

    ``valid_symbols`` is the set of recognised tokens for compare:/in:
    lookups. Pass ``None`` to accept any symbol (live chart path -- the
    caller resolves them against an external lookup like CoinGecko).
    ``primary`` is the chart's main symbol -- compare/in self references
    are filtered out.
    """
    layout = default_layout()
    compare_syms: list[str] = []
    quote_in: str | None = None
    clean_inds: list[str] = []
    primary_upper = (primary or "").upper()
    accept_any = valid_symbols is None

    for raw in args_iter:
        tok = raw.strip()
        if not tok:
            continue
        low = tok.lower()
        if low.startswith("compare:") or low.startswith("vs:"):
            sym = tok.split(":", 1)[1].strip().upper()
            if not sym or sym == primary_upper:
                continue
            if accept_any or sym in valid_symbols:
                compare_syms.append(sym)
            continue
        if low.startswith("in:") or low.startswith("vs="):
            sym = tok.split(":", 1)[1].strip().upper() if ":" in tok else tok.split("=", 1)[1].strip().upper()
            if not sym:
                continue
            if accept_any or sym in valid_symbols:
                quote_in = sym
            continue
        if low in _LAYOUT_FLAGS:
            if low == "wide":
                layout["view"] = "wide"
            elif low == "tall":
                layout["view"] = "tall"
            elif low == "light":
                layout["theme"] = "light"
            elif low == "dark":
                layout["theme"] = "dark"
            elif low == "minimal":
                layout["chrome"] = "minimal"
            elif low in ("line", "area", "candles", "bars"):
                layout["candle_type"] = low
            elif low in ("heikinashi", "heikin", "ha"):
                layout["candle_type"] = "heikinashi"
            elif low == "log":
                layout["log_scale"] = True
            elif low == "linear":
                layout["log_scale"] = False
            continue
        clean_inds.append(low)

    return layout, compare_syms, quote_in, clean_inds


def _series(values: list, ts: list[int]) -> list[dict]:
    """Pair each non-None ``v`` with its timestamp into a LWC series."""
    return [
        {"time": t, "value": v}
        for t, v in zip(ts, values)
        if v is not None
    ]


def compute_indicators(
    candles: list[dict],
    clean_inds: list[str],
    tf_seconds: int,
) -> dict:
    """Compute every indicator the user asked for from aggregated candles.

    Returns the ``indicators`` payload that gets embedded into
    ``window.CHART_DATA.indicators``.
    """
    opens   = [c["open"]  for c in candles]
    highs   = [c["high"]  for c in candles]
    lows    = [c["low"]   for c in candles]
    closes  = [c["close"] for c in candles]
    volumes = [c.get("volume", 0.0) for c in candles]
    times   = [c["ts"]    for c in candles]

    indicators_data: dict = {}
    ema_series: dict = {}
    sma_series: dict = {}
    wma_series: dict = {}
    rsi_period = 14
    stoch_period = 14
    atr_period = 14
    roc_period = 10
    wpr_period = 14
    cci_period = 20
    mfi_period = 14
    don_period = 20
    kel_period = 20

    wants = {k: False for k in (
        "rsi", "macd", "bb", "vol", "vwap", "obv", "adx", "stoch",
        "atr", "supertrend", "psar", "ichimoku", "donchian",
        "keltner", "pivots", "roc", "wpr", "cci", "mfi", "mom",
    )}

    for low in clean_inds:
        m = _INDICATOR_RE.match(low)
        if m:
            kind = m.group(1)
            n = int(m.group(2))
            if kind == "ema":
                ema_series[f"EMA{n}"] = _series(_ema(closes, n), times)
                continue
            if kind == "sma":
                sma_series[f"SMA{n}"] = _series(_sma(closes, n), times)
                continue
            if kind == "wma":
                wma_series[f"WMA{n}"] = _series(_wma(closes, n), times)
                continue
            if kind == "rsi":
                rsi_period = n; wants["rsi"] = True; continue
            if kind == "stoch":
                stoch_period = n; wants["stoch"] = True; continue
            if kind == "atr":
                atr_period = n; wants["atr"] = True; continue
            if kind == "roc":
                roc_period = n; wants["roc"] = True; continue
            if kind == "wpr":
                wpr_period = n; wants["wpr"] = True; continue
            if kind == "cci":
                cci_period = n; wants["cci"] = True; continue
            if kind == "mfi":
                mfi_period = n; wants["mfi"] = True; continue
            if kind == "don":
                don_period = n; wants["donchian"] = True; continue
            if kind == "kel":
                kel_period = n; wants["keltner"] = True; continue
        if low == "trend":
            for n in (20, 50, 200):
                ema_series[f"EMA{n}"] = _series(_ema(closes, n), times)
            continue
        if low == "all":
            for n in (20, 50, 200):
                ema_series.setdefault(f"EMA{n}", _series(_ema(closes, n), times))
            for key in ("rsi", "macd", "bb", "vol", "vwap", "stoch", "adx", "supertrend"):
                wants[key] = True
            continue
        if low == "bb":         wants["bb"] = True; continue
        if low == "rsi":        wants["rsi"] = True; continue
        if low == "macd":       wants["macd"] = True; continue
        if low in ("vol", "volume"): wants["vol"] = True; continue
        if low == "vwap":       wants["vwap"] = True; continue
        if low == "obv":        wants["obv"] = True; continue
        if low == "adx":        wants["adx"] = True; continue
        if low == "stoch":      wants["stoch"] = True; continue
        if low == "atr":        wants["atr"] = True; continue
        if low in ("supertrend", "st"): wants["supertrend"] = True; continue
        if low in ("psar", "sar"):      wants["psar"] = True; continue
        if low in ("ichimoku", "ichi"): wants["ichimoku"] = True; continue
        if low in ("donchian", "don"):  wants["donchian"] = True; continue
        if low in ("keltner", "kel"):   wants["keltner"] = True; continue
        if low in ("pivot", "pivots"):  wants["pivots"] = True; continue
        if low == "roc":        wants["roc"] = True; continue
        if low in ("wpr", "williams"):  wants["wpr"] = True; continue
        if low == "cci":        wants["cci"] = True; continue
        if low == "mfi":        wants["mfi"] = True; continue
        if low in ("mom", "momentum"): wants["mom"] = True; continue

    any_overlay = bool(
        ema_series or sma_series or wma_series
        or wants["bb"] or wants["vwap"] or wants["supertrend"]
        or wants["donchian"] or wants["keltner"] or wants["ichimoku"]
        or wants["psar"] or wants["pivots"]
    )
    if not any_overlay:
        ema_series["EMA20"] = _series(_ema(closes, 20), times)

    if ema_series: indicators_data["ema"] = ema_series
    if sma_series: indicators_data["sma"] = sma_series
    if wma_series: indicators_data["wma"] = wma_series

    if wants["bb"]:
        upper, mid, lower = _bb(closes)
        indicators_data["bb"] = {
            "upper": _series(upper, times),
            "mid":   _series(mid, times),
            "lower": _series(lower, times),
        }
    if wants["donchian"]:
        upper, mid, lower = _donchian(highs, lows, don_period)
        indicators_data["donchian"] = {
            "upper": _series(upper, times),
            "mid":   _series(mid, times),
            "lower": _series(lower, times),
            "period": don_period,
        }
    if wants["keltner"]:
        upper, mid, lower = _keltner(closes, highs, lows, kel_period, 2.0)
        indicators_data["keltner"] = {
            "upper": _series(upper, times),
            "mid":   _series(mid, times),
            "lower": _series(lower, times),
            "period": kel_period,
        }
    if wants["vwap"]:
        indicators_data["vwap"] = _series(_vwap(highs, lows, closes, volumes), times)
    if wants["supertrend"]:
        line, direction = _supertrend(highs, lows, closes, 10, 3.0)
        up = [v if d == 1 else None for v, d in zip(line, direction)]
        dn = [v if d == -1 else None for v, d in zip(line, direction)]
        indicators_data["supertrend"] = {
            "up":   _series(up, times),
            "down": _series(dn, times),
        }
    if wants["psar"]:
        sar, _direction = _psar(highs, lows)
        indicators_data["psar"] = _series(sar, times)
    if wants["ichimoku"]:
        ich = _ichimoku(highs, lows, closes)
        displ = int(ich["displacement"])
        future_t = [t + displ * tf_seconds for t in times]
        indicators_data["ichimoku"] = {
            "tenkan":   _series(ich["tenkan"], times),
            "kijun":    _series(ich["kijun"], times),
            "senkou_a": _series(ich["senkou_a"], future_t),
            "senkou_b": _series(ich["senkou_b"], future_t),
            "chikou":   _series(
                ich["chikou"],
                [t - displ * tf_seconds for t in times],
            ),
        }
    if wants["pivots"]:
        piv = _pivots_classic(highs, lows, closes)
        if piv:
            indicators_data["pivots"] = piv

    if wants["rsi"]:
        indicators_data["rsi"] = {
            "value": _series(_rsi(closes, rsi_period), times),
            "period": rsi_period,
        }
    if wants["macd"]:
        macd_line, sig_line, histogram = _macd(closes)
        indicators_data["macd"] = {
            "macd":      _series(macd_line, times),
            "signal":    _series(sig_line, times),
            "histogram": _series(histogram, times),
        }
    if wants["stoch"]:
        k, d = _stoch(highs, lows, closes, stoch_period, 3)
        indicators_data["stoch"] = {
            "k": _series(k, times), "d": _series(d, times),
            "period": stoch_period,
        }
    if wants["atr"]:
        indicators_data["atr"] = {
            "value": _series(_atr(highs, lows, closes, atr_period), times),
            "period": atr_period,
        }
    if wants["adx"]:
        adx, p_di, m_di = _adx(highs, lows, closes, 14)
        indicators_data["adx"] = {
            "adx":     _series(adx, times),
            "plus_di": _series(p_di, times),
            "minus_di": _series(m_di, times),
        }
    if wants["obv"]:
        indicators_data["obv"] = _series(_obv(closes, volumes), times)
    if wants["mfi"]:
        indicators_data["mfi"] = {
            "value": _series(_mfi(highs, lows, closes, volumes, mfi_period), times),
            "period": mfi_period,
        }
    if wants["cci"]:
        indicators_data["cci"] = {
            "value": _series(_cci(highs, lows, closes, cci_period), times),
            "period": cci_period,
        }
    if wants["wpr"]:
        indicators_data["wpr"] = {
            "value": _series(_williams_r(highs, lows, closes, wpr_period), times),
            "period": wpr_period,
        }
    if wants["roc"]:
        indicators_data["roc"] = {
            "value": _series(_roc(closes, roc_period), times),
            "period": roc_period,
        }
    if wants["mom"]:
        mom = [None] * 10 + [closes[i] - closes[i - 10] for i in range(10, len(closes))]
        indicators_data["momentum"] = _series(mom, times)

    if wants["vol"]:
        indicators_data["vol"] = [
            {"time": t, "value": v, "close": c, "open": o}
            for t, v, c, o in zip(times, volumes, closes, opens)
        ]

    return indicators_data


def compute_stats(candles: list[dict]) -> dict:
    """Header stats: open/high/low/close/pct_change/volume_total."""
    opens   = [c["open"]  for c in candles]
    highs   = [c["high"]  for c in candles]
    lows    = [c["low"]   for c in candles]
    closes  = [c["close"] for c in candles]
    volumes = [c.get("volume", 0.0) for c in candles]
    last_close = closes[-1]
    first_open = opens[0]
    pct = (last_close - first_open) / first_open * 100 if first_open else 0.0
    return {
        "open": first_open,
        "high": max(highs),
        "low":  min(lows),
        "close": last_close,
        "pct_change": pct,
        "volume_total": sum(volumes),
    }


def _viewport(layout: dict) -> tuple[int, int]:
    if layout["view"] == "wide":
        return 1800, 800
    if layout["view"] == "tall":
        return 1200, 1100
    return 1200, 760


async def build_chart_png(
    candles: list[dict],
    *,
    layout: dict,
    clean_inds: list[str],
    tf_seconds: int,
    pair: str,
    timeframe: str,
    comparisons: list[dict] | None = None,
    base_norm: list[dict] | None = None,
    quoted_in: str | None = None,
    pattern_overlay: dict | None = None,
    live: bool = False,
) -> tuple[bytes, dict]:
    """Build a chart PNG from already-aggregated candles + indicator flags.

    ``comparisons`` is a list of ``{"symbol": str, "points": [{"time", "value"}]}``
    already normalised to 100 by the caller (we don't fetch anything here).
    ``base_norm`` is the primary series normalised to 100 to match.

    ``pattern_overlay`` is an optional dict produced by
    :mod:`services.pattern_scout` of the shape
    ``{"trendlines": [{"label", "role", "points": [{"time", "value"}, ...]}],
       "markers":    [{"time", "value", "label", "role", "above"}]}`` -- the
    detected pattern's support/resistance/neckline lines and key pivot
    markers, drawn over the main candle series.

    Returns ``(png_bytes, stats)``.
    """
    indicators_data = compute_indicators(candles, clean_inds, tf_seconds)
    stats = compute_stats(candles)

    candle_payload = _to_lwc(
        _heikin_ashi(candles) if layout["candle_type"] == "heikinashi" else candles
    )

    chart_data = {
        "pair": pair,
        "tf": timeframe.upper(),
        "candles": candle_payload,
        "indicators": indicators_data,
        "layout": layout,
        "stats": stats,
        "compare": comparisons or [],
        "base_norm": base_norm or [],
        "quoted_in": quoted_in or "USD",
        "pattern_overlay": pattern_overlay or {},
        "live": bool(live),
    }

    vp_w, vp_h = _viewport(layout)
    chart_data["viewport"] = {"width": vp_w, "height": vp_h}

    tmpl = _TEMPLATE_PATH.read_text(encoding="utf-8")
    injection = (
        "<script>window.CHART_DATA = "
        + json.dumps(chart_data)
        + ";</script>"
    )
    html_content = tmpl.replace("</head>", injection + "\n</head>")

    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8",
    ) as f:
        f.write(html_content)
        tmp_path = f.name
    try:
        png_bytes = await _render_chart(
            tmp_path.replace("\\", "/"),
            width=vp_w, height=vp_h,
        )
    finally:
        os.unlink(tmp_path)

    return png_bytes, stats


def build_footer_chips(
    *,
    compare_syms: list[str],
    quote_in: str | None,
    layout: dict,
    clean_inds: list[str],
) -> str:
    """Build the chip-list footer string used under chart embeds."""
    chip_parts: list[str] = []
    if compare_syms:
        chip_parts.append("vs " + "/".join(compare_syms))
    if quote_in:
        chip_parts.append(f"in {quote_in}")
    if layout["candle_type"] != "candles":
        chip_parts.append(layout["candle_type"].upper())
    if layout["view"] != "default":
        chip_parts.append(layout["view"].upper())
    if layout["theme"] != "dark":
        chip_parts.append(layout["theme"].upper())
    chip_parts.extend(i.upper() for i in clean_inds)
    return " · ".join(chip_parts) if chip_parts else "EMA20"
