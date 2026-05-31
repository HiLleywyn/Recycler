"""Heuristic chart-pattern detector for live OHLC.

Powers the ``$scan`` command and the ``$chart`` auto-tag in
:mod:`cogs.realmarket`. Consumes the same candle shape produced by
:func:`core.framework.chart._aggregate` and :class:`services.real_market.RealMarketClient`:
``[{"ts", "open", "high", "low", "close", "volume"}, ...]``.

The detector finds swing highs/lows with N-bar confirmation, fits
trendlines via least-squares regression, and applies per-pattern rules
(flagpole + consolidation channel for flags, equal-height extrema for
double tops/bottoms, three-peak ratio test for head-and-shoulders, etc.)
to score each candidate on a 0-100 confidence scale.

Returns the single highest-confidence candidate above ``MIN_CONFIDENCE``
or ``None`` if nothing fits. The caller decides how to render the result
-- the detector itself is presentation-agnostic and contains no Discord
or embed code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Minimum candles needed to even attempt detection. Anything shorter and the
# swing-point search returns garbage -- 30 is enough for the smallest pattern
# windows (flag with a 6-bar pole and a 12-bar consolidation).
MIN_CANDLES = 30

# Patterns below this confidence are suppressed -- the scanner returns None
# rather than emitting a low-quality "spotted" alert that erodes trust.
MIN_CONFIDENCE = 55.0


@dataclass
class PatternMatch:
    """One detected pattern. Presentation-agnostic -- the cog turns this
    into the pattern-alert embed."""
    key: str                 # canonical key, e.g. "bear_flag"
    name: str                # display name, e.g. "Bear Flag"
    bias: str                # "bullish" | "bearish" | "neutral"
    status: str              # "Forming" | "Confirmed" | "Breakout" | "Breakdown"
    confidence: float        # 0-100
    description: str         # one-sentence summary for the embed body
    stats: dict[str, str] = field(default_factory=dict)   # label -> value chips
    support_touches: int = 0
    resistance_touches: int = 0
    # Geometry the chart renderer uses to annotate the pattern:
    #   {"trendlines": [{"label": str, "role": "resistance"|"support"|"neckline",
    #                    "points": [{"time": int, "value": float}, ...]}, ...],
    #    "markers":    [{"time": int, "value": float, "label": str,
    #                    "role": "head"|"shoulder"|"top"|"bottom"|"pole",
    #                    "above": bool}, ...]}
    overlay: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
#  Swing-point + trendline primitives
# ══════════════════════════════════════════════════════════════════════════════

def _swing_highs(highs: list[float], k: int = 3) -> list[int]:
    """Indices where ``highs[i]`` is the strict max of its ``±k`` neighbours."""
    out: list[int] = []
    n = len(highs)
    for i in range(k, n - k):
        h = highs[i]
        if all(h > highs[j] for j in range(i - k, i)) and \
           all(h >= highs[j] for j in range(i + 1, i + k + 1)):
            out.append(i)
    return out


def _swing_lows(lows: list[float], k: int = 3) -> list[int]:
    """Indices where ``lows[i]`` is the strict min of its ``±k`` neighbours."""
    out: list[int] = []
    n = len(lows)
    for i in range(k, n - k):
        l = lows[i]
        if all(l < lows[j] for j in range(i - k, i)) and \
           all(l <= lows[j] for j in range(i + 1, i + k + 1)):
            out.append(i)
    return out


def _linreg(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares fit. Returns ``(slope, intercept, r_squared)``."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    if den_x == 0:
        return 0.0, mean_y, 0.0
    slope = num / den_x
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return slope, intercept, max(0.0, min(1.0, r2))


def _trendline_touches(
    indices: list[int],
    values: list[float],
    slope: float,
    intercept: float,
    tolerance_pct: float,
) -> int:
    """Count how many ``(index, value)`` pairs sit within ``tolerance_pct``
    of the fitted line. Used to validate support/resistance lines."""
    if not indices:
        return 0
    touches = 0
    for i, v in zip(indices, values):
        line = slope * i + intercept
        if line == 0:
            continue
        if abs(v - line) / abs(line) <= tolerance_pct:
            touches += 1
    return touches


def _pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return (b - a) / a * 100.0


# ══════════════════════════════════════════════════════════════════════════════
#  Per-pattern detectors
# ══════════════════════════════════════════════════════════════════════════════

def _detect_flag(candles: list[dict], *, bearish: bool) -> PatternMatch | None:
    """Bear flag: sharp dump (flagpole), then upward-sloping consolidation
    channel that forms 3+ resistance and 3+ support touches.

    Bull flag: mirror -- sharp pump, then downward-sloping consolidation.

    The flagpole is the last ~15-25% of the window before the consolidation.
    Confidence rewards: clean parallel channel (R^2 high on both lines),
    flagpole magnitude, touch count.
    """
    n = len(candles)
    if n < 30:
        return None

    # Pole = first ~25% of recent window, consolidation = remaining ~75%.
    # We work on the last ~40 candles so the pattern is recent.
    window = candles[-min(n, 50):]
    closes = [c["close"] for c in window]
    highs  = [c["high"]  for c in window]
    lows   = [c["low"]   for c in window]
    w = len(window)
    pole_end = max(4, w // 4)

    pole_start_price = closes[0]
    pole_end_price   = closes[pole_end]
    pole_pct = _pct_change(pole_start_price, pole_end_price)

    # Pole has to actually be a pole.
    if bearish and pole_pct > -1.5:
        return None
    if not bearish and pole_pct < 1.5:
        return None

    # Consolidation phase indices (relative to window).
    cons_idx = list(range(pole_end, w))
    cons_highs = [highs[i] for i in cons_idx]
    cons_lows  = [lows[i]  for i in cons_idx]
    if len(cons_idx) < 8:
        return None

    # Fit upper (resistance) and lower (support) trendlines on the
    # swing extrema of the consolidation phase.
    sh = [pole_end + i for i in _swing_highs(cons_highs, k=2)]
    sl = [pole_end + i for i in _swing_lows(cons_lows, k=2)]
    if len(sh) < 2 or len(sl) < 2:
        return None

    res_slope, res_int, res_r2 = _linreg([float(i) for i in sh], [highs[i] for i in sh])
    sup_slope, sup_int, sup_r2 = _linreg([float(i) for i in sl], [lows[i] for i in sl])

    # Bear flag: both trendlines should slope UP (relief into the pole).
    # Bull flag: both should slope DOWN.
    if bearish and (res_slope <= 0 or sup_slope <= 0):
        return None
    if not bearish and (res_slope >= 0 or sup_slope >= 0):
        return None

    # Roughly parallel: slopes within 50% of each other.
    if min(abs(res_slope), abs(sup_slope)) == 0:
        return None
    slope_ratio = min(abs(res_slope), abs(sup_slope)) / max(abs(res_slope), abs(sup_slope))
    if slope_ratio < 0.4:
        return None

    res_touches = _trendline_touches(sh, [highs[i] for i in sh], res_slope, res_int, 0.012)
    sup_touches = _trendline_touches(sl, [lows[i]  for i in sl], sup_slope, sup_int, 0.012)
    if res_touches < 2 or sup_touches < 2:
        return None

    # Status: "Confirmed" once we have 4+ touches per side, "Forming" otherwise.
    # "Breakout"/"Breakdown" if the very last close has pierced the line.
    last = closes[-1]
    last_i = w - 1
    res_line_now = res_slope * last_i + res_int
    sup_line_now = sup_slope * last_i + sup_int
    if bearish and last < sup_line_now * 0.997:
        status = "Breakdown"
    elif not bearish and last > res_line_now * 1.003:
        status = "Breakout"
    elif res_touches >= 4 and sup_touches >= 4:
        status = "Confirmed"
    else:
        status = "Forming"

    confidence = (
        50.0
        + 15.0 * slope_ratio                    # parallelism reward
        + 10.0 * (res_r2 + sup_r2) / 2.0        # cleanliness of fit
        + 2.5 * (res_touches + sup_touches)     # touch count
        + min(15.0, abs(pole_pct) * 0.8)        # pole strength
    )
    confidence = max(0.0, min(99.0, confidence))

    name = "Bear Flag" if bearish else "Bull Flag"
    direction = "drop" if bearish else "rally"
    channel_dir = "upward-sloping" if bearish else "downward-sloping"
    desc = (
        f"Sharp {pole_pct:+.1f}% flagpole {direction} followed by a slow "
        f"{channel_dir} consolidation channel -- {res_touches} resistance "
        f"and {sup_touches} support touches confirmed."
    )

    # Trendlines + flagpole markers in absolute candle-time coordinates so
    # the renderer can drop them straight onto the lightweight-charts series.
    # ``window`` is the trailing slice of the source candles, so a window
    # index maps back to ``candles[base_offset + i]``.
    base_offset = len(candles) - w
    pole_a = candles[base_offset]
    pole_b = candles[base_offset + pole_end]
    last_c = candles[-1]
    res_a_x = sh[0]
    res_line = [
        {"time": candles[base_offset + res_a_x]["ts"], "value": res_slope * res_a_x + res_int},
        {"time": last_c["ts"],                          "value": res_slope * (w - 1) + res_int},
    ]
    sup_a_x = sl[0]
    sup_line = [
        {"time": candles[base_offset + sup_a_x]["ts"], "value": sup_slope * sup_a_x + sup_int},
        {"time": last_c["ts"],                          "value": sup_slope * (w - 1) + sup_int},
    ]
    overlay = {
        "trendlines": [
            {"label": "Resistance", "role": "resistance", "points": res_line},
            {"label": "Support",    "role": "support",    "points": sup_line},
            {"label": "Flagpole",   "role": "pole", "points": [
                {"time": pole_a["ts"], "value": pole_a["close"]},
                {"time": pole_b["ts"], "value": pole_b["close"]},
            ]},
        ],
        "markers": [
            {"time": pole_a["ts"], "value": pole_a["close"],
             "label": "Pole", "role": "pole", "above": not bearish},
            {"time": pole_b["ts"], "value": pole_b["close"],
             "label": "Flag", "role": "pole", "above": bearish},
        ],
    }

    return PatternMatch(
        key="bear_flag" if bearish else "bull_flag",
        name=name,
        bias="bearish" if bearish else "bullish",
        status=status,
        confidence=confidence,
        description=desc,
        stats={"Flagpole": f"{pole_pct:+.1f}%"},
        support_touches=sup_touches,
        resistance_touches=res_touches,
        overlay=overlay,
    )


def _detect_double(candles: list[dict], *, top: bool) -> PatternMatch | None:
    """Double top: two swing highs at near-equal levels with a clear trough
    between them. Double bottom: mirror.

    Confirmation: latest close pierces the neckline (the trough between
    the two extrema for tops, or the peak for bottoms).
    """
    n = len(candles)
    if n < 30:
        return None
    window = candles[-min(n, 60):]
    w = len(window)
    closes = [c["close"] for c in window]
    highs  = [c["high"]  for c in window]
    lows   = [c["low"]   for c in window]

    if top:
        extrema_idx = _swing_highs(highs, k=3)
        extrema_v = [highs[i] for i in extrema_idx]
    else:
        extrema_idx = _swing_lows(lows, k=3)
        extrema_v = [lows[i] for i in extrema_idx]
    if len(extrema_idx) < 2:
        return None

    # Look at the last two extrema -- they need to be similar height.
    i1, i2 = extrema_idx[-2], extrema_idx[-1]
    v1, v2 = extrema_v[-2], extrema_v[-1]
    if v1 == 0:
        return None
    height_diff = abs(v2 - v1) / abs(v1)
    if height_diff > 0.025:                 # tops must be within 2.5% of each other
        return None
    if i2 - i1 < 6:                          # need real space between them
        return None

    # Neckline = the most extreme between i1 and i2.
    between = list(range(i1, i2 + 1))
    if top:
        neck = min(lows[i] for i in between)
    else:
        neck = max(highs[i] for i in between)

    # Reject shapes where the neckline is barely a dip (under 1.5% of the peak).
    extreme_avg = (v1 + v2) / 2.0
    if extreme_avg == 0 or abs(extreme_avg - neck) / abs(extreme_avg) < 0.015:
        return None

    last = closes[-1]
    if top:
        status = "Breakdown" if last < neck * 0.998 else (
            "Confirmed" if (w - 1 - i2) >= 3 else "Forming"
        )
    else:
        status = "Breakout" if last > neck * 1.002 else (
            "Confirmed" if (w - 1 - i2) >= 3 else "Forming"
        )

    confidence = (
        55.0
        + (1.0 - height_diff / 0.025) * 18.0
        + min(15.0, (i2 - i1))
        + (10.0 if status in ("Breakout", "Breakdown") else 0.0)
    )
    confidence = max(0.0, min(99.0, confidence))

    name = "Double Top" if top else "Double Bottom"
    bias = "bearish" if top else "bullish"
    move_word = "tops" if top else "bottoms"
    desc = (
        f"Two near-equal {move_word} ({height_diff*100:.1f}% apart) with a clear "
        f"neckline between them. Bias is {bias} on a confirmed neckline break."
    )

    base_offset = len(candles) - w
    p1 = candles[base_offset + i1]
    p2 = candles[base_offset + i2]
    last_c = candles[-1]
    neck_role = "neckline"
    overlay = {
        "trendlines": [
            {"label": "Neckline", "role": neck_role, "points": [
                {"time": p1["ts"], "value": neck},
                {"time": last_c["ts"], "value": neck},
            ]},
        ],
        "markers": [
            {"time": p1["ts"], "value": v1,
             "label": "Top 1" if top else "Bot 1",
             "role": "top" if top else "bottom",
             "above": top},
            {"time": p2["ts"], "value": v2,
             "label": "Top 2" if top else "Bot 2",
             "role": "top" if top else "bottom",
             "above": top},
        ],
    }

    return PatternMatch(
        key="double_top" if top else "double_bottom",
        name=name,
        bias=bias,
        status=status,
        confidence=confidence,
        description=desc,
        stats={
            "Neckline": f"{neck:,.4f}",
            "Spread": f"{height_diff*100:.2f}%",
        },
        support_touches=2 if top else 2,
        resistance_touches=2 if top else 2,
        overlay=overlay,
    )


def _detect_head_and_shoulders(candles: list[dict], *, inverse: bool) -> PatternMatch | None:
    """Three swing extrema where the middle one is the most extreme.
    Standard H&S = three peaks (bearish), inverse = three troughs (bullish).
    """
    n = len(candles)
    if n < 35:
        return None
    window = candles[-min(n, 70):]
    w = len(window)
    closes = [c["close"] for c in window]
    highs  = [c["high"]  for c in window]
    lows   = [c["low"]   for c in window]

    if inverse:
        idxs = _swing_lows(lows, k=3)
        vals = [lows[i] for i in idxs]
    else:
        idxs = _swing_highs(highs, k=3)
        vals = [highs[i] for i in idxs]
    if len(idxs) < 3:
        return None

    # Try last three swing extrema.
    i1, i2, i3 = idxs[-3], idxs[-2], idxs[-1]
    v1, v2, v3 = vals[-3], vals[-2], vals[-1]

    if inverse:
        # Middle (head) must be the lowest; shoulders at similar height.
        if not (v2 < v1 and v2 < v3):
            return None
    else:
        # Middle (head) must be the highest; shoulders at similar height.
        if not (v2 > v1 and v2 > v3):
            return None

    if v1 == 0 or v2 == 0:
        return None
    shoulder_diff = abs(v3 - v1) / abs(v1)
    head_pop = abs(v2 - (v1 + v3) / 2.0) / abs(v2)
    if shoulder_diff > 0.04:               # shoulders within 4% of each other
        return None
    if head_pop < 0.02:                    # head clearly above/below shoulders
        return None

    # Neckline drawn between the two pivots between the peaks/troughs.
    if inverse:
        b1 = max(highs[i1:i2 + 1]) if i2 > i1 else highs[i1]
        b2 = max(highs[i2:i3 + 1]) if i3 > i2 else highs[i3]
    else:
        b1 = min(lows[i1:i2 + 1]) if i2 > i1 else lows[i1]
        b2 = min(lows[i2:i3 + 1]) if i3 > i2 else lows[i3]
    neck = (b1 + b2) / 2.0

    last = closes[-1]
    if inverse:
        status = "Breakout" if last > neck * 1.003 else (
            "Confirmed" if (w - 1 - i3) >= 3 else "Forming"
        )
    else:
        status = "Breakdown" if last < neck * 0.997 else (
            "Confirmed" if (w - 1 - i3) >= 3 else "Forming"
        )

    confidence = (
        58.0
        + (1.0 - shoulder_diff / 0.04) * 18.0
        + min(15.0, head_pop * 200.0)
        + (8.0 if status in ("Breakout", "Breakdown") else 0.0)
    )
    confidence = max(0.0, min(99.0, confidence))

    name = "Inverse Head & Shoulders" if inverse else "Head & Shoulders"
    bias = "bullish" if inverse else "bearish"
    desc = (
        f"Three pivots with the head {head_pop*100:.1f}% beyond the shoulders "
        f"(shoulders {shoulder_diff*100:.1f}% apart). Neckline at {neck:,.4f}."
    )

    base_offset = len(candles) - w
    s1 = candles[base_offset + i1]
    h  = candles[base_offset + i2]
    s2 = candles[base_offset + i3]
    last_c = candles[-1]
    above_for_marker = not inverse
    overlay = {
        "trendlines": [
            {"label": "Neckline", "role": "neckline", "points": [
                {"time": s1["ts"], "value": neck},
                {"time": last_c["ts"], "value": neck},
            ]},
        ],
        "markers": [
            {"time": s1["ts"], "value": v1, "label": "LS",
             "role": "shoulder", "above": above_for_marker},
            {"time": h["ts"],  "value": v2, "label": "Head",
             "role": "head",     "above": above_for_marker},
            {"time": s2["ts"], "value": v3, "label": "RS",
             "role": "shoulder", "above": above_for_marker},
        ],
    }

    return PatternMatch(
        key="inv_head_shoulders" if inverse else "head_shoulders",
        name=name,
        bias=bias,
        status=status,
        confidence=confidence,
        description=desc,
        stats={
            "Neckline": f"{neck:,.4f}",
            "Head pop": f"{head_pop*100:.2f}%",
        },
        support_touches=2,
        resistance_touches=3 if not inverse else 2,
        overlay=overlay,
    )


def _detect_triangle(candles: list[dict]) -> PatternMatch | None:
    """Ascending / descending / symmetrical triangle.

    Returns the single best triangle variant (or None) -- decided by
    inspecting the slopes of the upper and lower trendlines fitted to
    swing highs/lows in the recent window.
    """
    n = len(candles)
    if n < 30:
        return None
    window = candles[-min(n, 50):]
    w = len(window)
    closes = [c["close"] for c in window]
    highs  = [c["high"]  for c in window]
    lows   = [c["low"]   for c in window]

    sh = _swing_highs(highs, k=2)
    sl = _swing_lows(lows, k=2)
    if len(sh) < 3 or len(sl) < 3:
        return None

    res_slope, res_int, res_r2 = _linreg([float(i) for i in sh], [highs[i] for i in sh])
    sup_slope, sup_int, sup_r2 = _linreg([float(i) for i in sl], [lows[i]  for i in sl])

    if min(res_r2, sup_r2) < 0.45:
        return None

    avg = sum(closes[-10:]) / 10.0
    if avg == 0:
        return None
    # Per-bar slope as a percentage of the recent price -- normalises across
    # tickers so the same threshold works for MTA and DOGE.
    res_pct = res_slope / avg * 100.0
    sup_pct = sup_slope / avg * 100.0

    flat_thresh = 0.05      # < 0.05% per bar = flat
    slope_thresh = 0.10     # > 0.10% per bar = clearly sloping

    if abs(res_pct) < flat_thresh and sup_pct > slope_thresh:
        kind, bias, name = "ascending", "bullish", "Ascending Triangle"
        desc = "Flat resistance with rising support. Bullish on a clean breakout."
    elif abs(sup_pct) < flat_thresh and res_pct < -slope_thresh:
        kind, bias, name = "descending", "bearish", "Descending Triangle"
        desc = "Flat support with falling resistance. Bearish on a breakdown."
    elif res_pct < -slope_thresh and sup_pct > slope_thresh:
        kind, bias, name = "symmetrical", "neutral", "Symmetrical Triangle"
        desc = "Lower highs and higher lows squeezing into apex. Direction TBD."
    else:
        return None

    res_touches = _trendline_touches(sh, [highs[i] for i in sh], res_slope, res_int, 0.015)
    sup_touches = _trendline_touches(sl, [lows[i]  for i in sl], sup_slope, sup_int, 0.015)
    if res_touches < 2 or sup_touches < 2:
        return None

    last = closes[-1]
    last_i = w - 1
    res_line = res_slope * last_i + res_int
    sup_line = sup_slope * last_i + sup_int
    if last > res_line * 1.003:
        status = "Breakout"
    elif last < sup_line * 0.997:
        status = "Breakdown"
    elif res_touches + sup_touches >= 7:
        status = "Confirmed"
    else:
        status = "Forming"

    confidence = (
        55.0
        + 15.0 * (res_r2 + sup_r2) / 2.0
        + 2.0 * (res_touches + sup_touches)
        + (10.0 if status in ("Breakout", "Breakdown") else 0.0)
    )
    confidence = max(0.0, min(99.0, confidence))

    base_offset = len(candles) - w
    last_c = candles[-1]
    res_a_x = sh[0]
    sup_a_x = sl[0]
    overlay = {
        "trendlines": [
            {"label": "Resistance", "role": "resistance", "points": [
                {"time": candles[base_offset + res_a_x]["ts"],
                 "value": res_slope * res_a_x + res_int},
                {"time": last_c["ts"],
                 "value": res_slope * (w - 1) + res_int},
            ]},
            {"label": "Support", "role": "support", "points": [
                {"time": candles[base_offset + sup_a_x]["ts"],
                 "value": sup_slope * sup_a_x + sup_int},
                {"time": last_c["ts"],
                 "value": sup_slope * (w - 1) + sup_int},
            ]},
        ],
        "markers": [],
    }
    return PatternMatch(
        key=f"{kind}_triangle",
        name=name,
        bias=bias,
        status=status,
        confidence=confidence,
        description=desc,
        stats={
            "Upper": f"{res_pct:+.3f}%/bar",
            "Lower": f"{sup_pct:+.3f}%/bar",
        },
        support_touches=sup_touches,
        resistance_touches=res_touches,
        overlay=overlay,
    )


def _detect_wedge(candles: list[dict], *, rising: bool) -> PatternMatch | None:
    """Rising wedge (bearish): both trendlines slope up, but resistance
    rises slower than support so they converge.
    Falling wedge (bullish): mirror.
    """
    n = len(candles)
    if n < 30:
        return None
    window = candles[-min(n, 50):]
    w = len(window)
    closes = [c["close"] for c in window]
    highs  = [c["high"]  for c in window]
    lows   = [c["low"]   for c in window]

    sh = _swing_highs(highs, k=2)
    sl = _swing_lows(lows, k=2)
    if len(sh) < 3 or len(sl) < 3:
        return None

    res_slope, res_int, res_r2 = _linreg([float(i) for i in sh], [highs[i] for i in sh])
    sup_slope, sup_int, sup_r2 = _linreg([float(i) for i in sl], [lows[i]  for i in sl])
    if min(res_r2, sup_r2) < 0.5:
        return None

    if rising:
        if res_slope <= 0 or sup_slope <= 0:
            return None
        # Support must be steeper -- lines converge from below.
        if sup_slope <= res_slope * 1.15:
            return None
    else:
        if res_slope >= 0 or sup_slope >= 0:
            return None
        # Resistance must fall faster -- lines converge from above.
        if abs(res_slope) <= abs(sup_slope) * 1.15:
            return None

    res_touches = _trendline_touches(sh, [highs[i] for i in sh], res_slope, res_int, 0.015)
    sup_touches = _trendline_touches(sl, [lows[i]  for i in sl], sup_slope, sup_int, 0.015)
    if res_touches < 2 or sup_touches < 2:
        return None

    last = closes[-1]
    last_i = w - 1
    res_line = res_slope * last_i + res_int
    sup_line = sup_slope * last_i + sup_int
    if rising and last < sup_line * 0.997:
        status = "Breakdown"
    elif not rising and last > res_line * 1.003:
        status = "Breakout"
    elif res_touches + sup_touches >= 7:
        status = "Confirmed"
    else:
        status = "Forming"

    confidence = (
        55.0
        + 15.0 * (res_r2 + sup_r2) / 2.0
        + 2.0 * (res_touches + sup_touches)
        + (10.0 if status in ("Breakout", "Breakdown") else 0.0)
    )
    confidence = max(0.0, min(99.0, confidence))

    name = "Rising Wedge" if rising else "Falling Wedge"
    bias = "bearish" if rising else "bullish"
    desc = (
        f"Both trendlines {'rising' if rising else 'falling'} but converging -- "
        f"a {'rising wedge typically resolves down' if rising else 'falling wedge typically resolves up'}."
    )

    base_offset = len(candles) - w
    last_c = candles[-1]
    res_a_x = sh[0]
    sup_a_x = sl[0]
    overlay = {
        "trendlines": [
            {"label": "Upper", "role": "resistance", "points": [
                {"time": candles[base_offset + res_a_x]["ts"],
                 "value": res_slope * res_a_x + res_int},
                {"time": last_c["ts"],
                 "value": res_slope * (w - 1) + res_int},
            ]},
            {"label": "Lower", "role": "support", "points": [
                {"time": candles[base_offset + sup_a_x]["ts"],
                 "value": sup_slope * sup_a_x + sup_int},
                {"time": last_c["ts"],
                 "value": sup_slope * (w - 1) + sup_int},
            ]},
        ],
        "markers": [],
    }
    return PatternMatch(
        key="rising_wedge" if rising else "falling_wedge",
        name=name,
        bias=bias,
        status=status,
        confidence=confidence,
        description=desc,
        stats={
            "Upper": f"{res_slope:+.4f}/bar",
            "Lower": f"{sup_slope:+.4f}/bar",
        },
        support_touches=sup_touches,
        resistance_touches=res_touches,
        overlay=overlay,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Pattern lore -- educational blurb shown in the $scan embed
# ══════════════════════════════════════════════════════════════════════════════
#
# One short paragraph per pattern explaining what it is, what historically
# follows it, and what the "Forming / Confirmed / Breakout / Breakdown"
# status implies for the next move. Shown verbatim in the scan embed's
# "📖 What this means" field so a player who doesn't recognise the pattern
# still gets the takeaway. Keep each entry under ~400 chars so two of them
# never push the embed past the 4096-description / 1024-field limit.

_PATTERN_LORE: dict[str, str] = {
    "bear_flag":
        "A **bear flag** is a continuation pattern: a sharp drop (the "
        "flagpole) followed by a tight upward-sloping consolidation. "
        "Bears are usually pausing to catch their breath -- the prevailing "
        "downtrend tends to resume on a clean break below the lower line. "
        "Forming = wait for the breakdown; Confirmed/Breakdown = expect "
        "the move to extend by roughly the flagpole's length.",
    "bull_flag":
        "A **bull flag** is a continuation pattern: a sharp rally (the "
        "flagpole) followed by a tight downward-sloping consolidation. "
        "Bulls are catching their breath, not reversing. The prevailing "
        "uptrend usually resumes on a clean break above the upper line. "
        "Forming = wait for the breakout; Confirmed/Breakout = expect the "
        "move to extend by roughly the flagpole's length.",
    "double_top":
        "A **double top** (M-shape) is a bearish reversal: price tests a "
        "resistance level twice and fails, then sells off through the "
        "neckline (the trough between the two peaks). A confirmed break "
        "below the neckline targets a move equal to the distance from the "
        "peaks down to the neckline -- projected from the breakdown point.",
    "double_bottom":
        "A **double bottom** (W-shape) is a bullish reversal: price tests "
        "a support level twice and holds, then breaks out through the "
        "neckline (the peak between the two troughs). A confirmed break "
        "above the neckline targets a move equal to the distance from the "
        "troughs up to the neckline -- projected from the breakout point.",
    "head_shoulders":
        "A **head and shoulders** top is a bearish reversal: three peaks "
        "with the middle one (head) highest and the outer two (shoulders) "
        "near-equal. A break below the neckline -- the line connecting "
        "the two pivot lows between the peaks -- confirms the reversal "
        "and typically targets the head-to-neckline distance, projected "
        "downward from the break.",
    "inv_head_shoulders":
        "An **inverse head and shoulders** is a bullish reversal: three "
        "troughs with the middle one (head) lowest and the outer two "
        "(shoulders) near-equal. A break above the neckline -- the line "
        "connecting the two pivot highs between the troughs -- confirms "
        "the reversal and typically targets the neckline-to-head distance, "
        "projected upward from the break.",
    "ascending_triangle":
        "An **ascending triangle** is a bullish continuation: flat "
        "resistance with rising support. Buyers keep stepping in at "
        "higher lows while sellers defend the same ceiling -- the "
        "imbalance usually resolves with a breakout through the flat top. "
        "A confirmed break targets a move equal to the triangle's "
        "starting height.",
    "descending_triangle":
        "A **descending triangle** is a bearish continuation: flat "
        "support with falling resistance. Sellers keep stepping in at "
        "lower highs while buyers defend the same floor -- the imbalance "
        "usually resolves with a breakdown through the flat bottom. A "
        "confirmed break targets a move equal to the triangle's starting "
        "height, projected downward.",
    "symmetrical_triangle":
        "A **symmetrical triangle** is a neutral coil: lower highs and "
        "higher lows squeezing toward the apex. The pattern itself doesn't "
        "predict direction -- whichever side breaks first usually triggers "
        "a fast move equal to the triangle's starting height. Wait for "
        "the break; trading inside the coil is low-conviction.",
    "rising_wedge":
        "A **rising wedge** is bearish despite both lines sloping up -- "
        "support is rising faster than resistance, so the structure is "
        "running out of room. Most rising wedges break **down** through "
        "the lower line. A confirmed break targets a move back to the "
        "wedge's starting price.",
    "falling_wedge":
        "A **falling wedge** is bullish despite both lines sloping down "
        "-- resistance is falling faster than support, so selling is "
        "exhausting itself. Most falling wedges break **up** through the "
        "upper line. A confirmed break targets a move back to the wedge's "
        "starting price.",
}


def lore(key: str) -> str:
    """Return the educational blurb for a pattern key, or a generic
    fallback if the key has no entry yet."""
    return _PATTERN_LORE.get(key, "")


# ══════════════════════════════════════════════════════════════════════════════
#  Market-context helpers (move + volatility + volume from the same candles)
# ══════════════════════════════════════════════════════════════════════════════

def market_context(candles: list[dict]) -> dict:
    """Summarise the candle window into a few numbers the scan embed
    surfaces alongside the pattern. All values are derived from the
    candles already in hand -- no extra API calls.

    Returns ``{"window_bars", "move_pct", "high", "low", "range_pct",
               "volatility_pct", "volume_total", "volume_avg",
               "volume_trend_pct", "has_volume"}`` -- ``has_volume`` is
    False when CoinGecko's free tier returns ``volume=0`` for every
    candle on this timeframe (common for sub-hourly), in which case
    callers should suppress the volume chips.
    """
    if not candles:
        return {}
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    vols   = [float(c.get("volume", 0.0) or 0.0) for c in candles]

    open_p, close_p = closes[0], closes[-1]
    move_pct = _pct_change(open_p, close_p)
    hi, lo = max(highs), min(lows)
    range_pct = ((hi - lo) / lo * 100.0) if lo > 0 else 0.0

    # True-range-ish volatility chip: mean candle range as % of close.
    tr_pcts: list[float] = []
    for h, l, c in zip(highs, lows, closes):
        if c > 0:
            tr_pcts.append((h - l) / c * 100.0)
    vola_pct = (sum(tr_pcts) / len(tr_pcts)) if tr_pcts else 0.0

    vol_total = sum(vols)
    vol_avg = (vol_total / len(vols)) if vols else 0.0
    has_volume = vol_total > 0.0

    # Volume trend chip: last quartile vs first quartile of the window.
    n = len(vols)
    q = max(1, n // 4)
    early = sum(vols[:q]) / q if q else 0.0
    late  = sum(vols[-q:]) / q if q else 0.0
    vol_trend_pct = _pct_change(early, late) if has_volume and early > 0 else 0.0

    return {
        "window_bars": len(candles),
        "move_pct": move_pct,
        "high": hi,
        "low": lo,
        "range_pct": range_pct,
        "volatility_pct": vola_pct,
        "volume_total": vol_total,
        "volume_avg": vol_avg,
        "volume_trend_pct": vol_trend_pct,
        "has_volume": has_volume,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

_DETECTORS: list[Callable[[list[dict]], PatternMatch | None]] = [
    lambda c: _detect_flag(c, bearish=True),
    lambda c: _detect_flag(c, bearish=False),
    lambda c: _detect_double(c, top=True),
    lambda c: _detect_double(c, top=False),
    lambda c: _detect_head_and_shoulders(c, inverse=False),
    lambda c: _detect_head_and_shoulders(c, inverse=True),
    _detect_triangle,
    lambda c: _detect_wedge(c, rising=True),
    lambda c: _detect_wedge(c, rising=False),
]


def detect_best(candles: list[dict]) -> PatternMatch | None:
    """Run every detector and return the single highest-confidence match
    above :data:`MIN_CONFIDENCE`. Returns ``None`` if nothing fits."""
    if not candles or len(candles) < MIN_CANDLES:
        return None
    best: PatternMatch | None = None
    for det in _DETECTORS:
        try:
            m = det(candles)
        except Exception:
            # Detection is best-effort -- one detector raising shouldn't
            # poison the others. Caller logs the broader failure if needed.
            continue
        if m is None:
            continue
        if m.confidence < MIN_CONFIDENCE:
            continue
        if best is None or m.confidence > best.confidence:
            best = m
    return best


def detect_all(candles: list[dict]) -> list[PatternMatch]:
    """Return every candidate above :data:`MIN_CONFIDENCE`, sorted by
    confidence descending. Useful for debugging / future ``$scan all``."""
    if not candles or len(candles) < MIN_CANDLES:
        return []
    out: list[PatternMatch] = []
    for det in _DETECTORS:
        try:
            m = det(candles)
        except Exception:
            continue
        if m and m.confidence >= MIN_CONFIDENCE:
            out.append(m)
    out.sort(key=lambda x: x.confidence, reverse=True)
    return out
