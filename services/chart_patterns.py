"""Chart pattern engine for the admin pump command.

Each pattern is a deterministic function ``f(t, magnitude_pct, seed) -> float``
that returns a price multiplier where ``start_price * f(t)`` gives the target
price at progress ``t`` in ``[0, 1]``. Every pattern satisfies ``f(0) == 1.0``
so the curve starts exactly at the live price the moment the event begins.

``magnitude_pct`` is the headline intensity in percent (e.g. ``30`` = 30%).
For directional patterns it controls the size of the move; for oscillating
patterns it controls peak deviation. Sign is generally absorbed by the pattern
(``dump`` always goes down) -- only ``linear`` honors the sign of magnitude.

``seed`` is a per-event integer used to drive deterministic randomness so
re-evaluating the same pattern returns the same value for the same ``t``.
"""
from __future__ import annotations

import math
import random


def _rng(seed: int, mix: int = 0) -> random.Random:
    return random.Random((seed * 1_000_003) ^ mix)


def linear(t: float, mag: float, seed: int) -> float:
    """Straight-line move. Honors sign of magnitude."""
    return 1.0 + (mag / 100.0) * t


def pump(t: float, mag: float, seed: int) -> float:
    """Aggressive J-curve up. Steady climb that accelerates into the finish.

    Lowered exponent from 1.8 -> 1.5 so the move is visible from the first
    drift tick instead of looking flat for the first 40-50% of the event.
    At t=0.5 you see ~35% of the magnitude (vs. ~28% with t^1.8) which is
    well above the noise floor of normal GBM ticks.
    """
    m = abs(mag) / 100.0
    return 1.0 + m * (t ** 1.5)


def moon(t: float, mag: float, seed: int) -> float:
    """Parabolic moonshot. Quiet first quarter, vertical takeoff.

    Lowered exponent from 3.0 -> 2.0 so the early phase isn't dead-flat
    (a t^3 curve at t=0.3 only delivers 2.7% of the magnitude -- below
    one tick of GBM noise -- so players assumed the moon "did nothing").
    With t^2 the same 30% mark delivers 9% of the move; the late-stage
    blowoff is still parabolic.
    """
    m = abs(mag) / 100.0
    return 1.0 + m * (t ** 2.0)


def dump(t: float, mag: float, seed: int) -> float:
    """Aggressive J-curve down. Steady leak that accelerates to capitulation.

    Lowered exponent from 1.8 -> 1.5 to match ``pump`` -- the prior tail-
    loaded curve hid the dump for the first half so players couldn't tell
    the event was firing.
    """
    m = abs(mag) / 100.0
    return 1.0 - m * (t ** 1.5)


def crash(t: float, mag: float, seed: int) -> float:
    """Stable then cliff drop with a small dead-cat bounce."""
    m = abs(mag) / 100.0
    if t < 0.45:
        return 1.0 - m * 0.04 * (t / 0.45)
    if t < 0.80:
        s = (t - 0.45) / 0.35
        return 1.0 - m * (0.04 + 0.96 * s)
    s = (t - 0.80) / 0.20
    bottom = 1.0 - m
    bounce = m * 0.18 * math.sin(math.pi * s)
    return bottom + bounce


def _edge_taper(t: float) -> float:
    """0 at t=0, 1 in the middle, 0 at t=1 -- tapers noise smoothly."""
    return max(0.0, 4.0 * t * (1.0 - t))


def bull(t: float, mag: float, seed: int) -> float:
    """Steady uptrend with shallow pullbacks (healthy bull market).

    The pullback amplitude is itself edge-tapered so the first ~10% of the
    event is dominated by the trend, not the oscillator. Without the taper
    the early phase looked random (sometimes flat, sometimes briefly
    negative on a "bull" pump) which players read as the pattern not
    firing.
    """
    m = abs(mag) / 100.0
    osc_taper = _edge_taper(t)
    pullback = m * 0.08 * math.sin(t * 4.0 * math.pi) * osc_taper
    rng = _rng(seed, int(t * 800))
    noise = rng.gauss(0.0, 1.0) * m * 0.015 * osc_taper
    return 1.0 + m * t + pullback + noise


def bear(t: float, mag: float, seed: int) -> float:
    """Steady downtrend with relief rallies.

    Relief rallies are edge-tapered so the first ~10% of the event reads
    as a clear downtrend instead of a tiny up-tick (the pre-fix curve
    could close t=0.05 *above* start because the rally amplitude
    dominated the trend).
    """
    m = abs(mag) / 100.0
    osc_taper = _edge_taper(t)
    rally = m * 0.08 * math.sin(t * 4.0 * math.pi) * osc_taper
    rng = _rng(seed, int(t * 800))
    noise = rng.gauss(0.0, 1.0) * m * 0.015 * osc_taper
    return 1.0 - m * t + rally + noise


def volatile(t: float, mag: float, seed: int) -> float:
    """High-volatility chop. Big swings on top of a meaningful drift.

    Pre-fix the net drift was 10% of magnitude (a 30% volatile pump
    closed at +3%) so players assumed nothing fired. Drift is now
    ``0.30 * mag * t`` -- still well below directional patterns like
    ``pump`` (which deliver full magnitude) but visible on the chart.
    Swings still taper to zero at the edges and the gauss draw is
    clamped to ±2.5σ so a single tick can't whipsaw past the impact
    rails.
    """
    m = abs(mag) / 100.0
    rng = _rng(seed, int(t * 1500))
    raw = rng.gauss(0.0, 1.0)
    raw = max(-2.5, min(2.5, raw))
    swing = raw * m * 0.35 * _edge_taper(t)
    drift = m * 0.30 * t
    return 1.0 + drift + swing


def wave(t: float, mag: float, seed: int) -> float:
    """Clean sine wave. Peaks at t=0.25, troughs at t=0.75, returns to start."""
    m = abs(mag) / 100.0
    return 1.0 + m * math.sin(t * 2.0 * math.pi)


def rugpull(t: float, mag: float, seed: int) -> float:
    """Pump up, brief plateau, then catastrophic dump below start."""
    m = abs(mag) / 100.0
    if t < 0.55:
        s = t / 0.55
        return 1.0 + m * (s ** 0.7)
    if t < 0.65:
        return 1.0 + m
    s = (t - 0.65) / 0.35
    peak = 1.0 + m
    floor = 1.0 - m * 0.6
    return peak + (floor - peak) * (s ** 0.4)


def pumpdump(t: float, mag: float, seed: int) -> float:
    """Pump and dump back to start (smooth half-sine)."""
    m = abs(mag) / 100.0
    return 1.0 + m * math.sin(math.pi * t)


def vshape(t: float, mag: float, seed: int) -> float:
    """V-shape: dump to bottom at midpoint, full recovery by end."""
    m = abs(mag) / 100.0
    return 1.0 - m * (1.0 - 2.0 * abs(t - 0.5))


def hns(t: float, mag: float, seed: int) -> float:
    """Head and shoulders: small peak, big peak, small peak, breakdown."""
    m = abs(mag) / 100.0

    def bump(c: float, w: float, h: float) -> float:
        x = (t - c) / w
        return h * math.exp(-x * x)

    val = bump(0.18, 0.09, 0.55 * m)
    val += bump(0.45, 0.10, 1.00 * m)
    val += bump(0.72, 0.09, 0.55 * m)
    if t > 0.85:
        s = (t - 0.85) / 0.15
        val -= m * 0.55 * s
    return 1.0 + val


def double_top(t: float, mag: float, seed: int) -> float:
    """Two equal peaks then sell-off (M shape)."""
    m = abs(mag) / 100.0

    def bump(c: float, w: float, h: float) -> float:
        x = (t - c) / w
        return h * math.exp(-x * x)

    val = bump(0.25, 0.09, m) + bump(0.55, 0.09, m)
    if t > 0.70:
        s = (t - 0.70) / 0.30
        val -= m * 0.75 * s
    return 1.0 + val


def double_bottom(t: float, mag: float, seed: int) -> float:
    """Two equal troughs then breakout (W shape)."""
    m = abs(mag) / 100.0

    def trough(c: float, w: float, d: float) -> float:
        x = (t - c) / w
        return -d * math.exp(-x * x)

    val = trough(0.25, 0.09, m) + trough(0.55, 0.09, m)
    if t > 0.70:
        s = (t - 0.70) / 0.30
        val += m * 0.75 * s
    return 1.0 + val


def cup_handle(t: float, mag: float, seed: int) -> float:
    """Smooth cup, then small handle dip, then breakout."""
    m = abs(mag) / 100.0
    if t < 0.60:
        s = t / 0.60
        return 1.0 - m * 0.45 * (1.0 - (2.0 * s - 1.0) ** 2)
    if t < 0.80:
        s = (t - 0.60) / 0.20
        return 1.0 - m * 0.18 * math.sin(math.pi * s)
    s = (t - 0.80) / 0.20
    return 1.0 + m * (s ** 0.7)


def bullflag(t: float, mag: float, seed: int) -> float:
    """Flagpole pump, tight sideways consolidation, breakout."""
    m = abs(mag) / 100.0
    if t < 0.20:
        s = t / 0.20
        return 1.0 + m * 0.55 * s
    if t < 0.70:
        s = (t - 0.20) / 0.50
        rng = _rng(seed, int(t * 600))
        noise = rng.gauss(0.0, 1.0) * m * 0.02 * _edge_taper(s)
        return 1.0 + m * 0.55 - m * 0.10 * s + noise
    s = (t - 0.70) / 0.30
    return 1.0 + m * 0.45 + m * 0.55 * s


def bearflag(t: float, mag: float, seed: int) -> float:
    """Flagpole dump, tight sideways relief, breakdown."""
    m = abs(mag) / 100.0
    if t < 0.20:
        s = t / 0.20
        return 1.0 - m * 0.55 * s
    if t < 0.70:
        s = (t - 0.20) / 0.50
        rng = _rng(seed, int(t * 600))
        noise = rng.gauss(0.0, 1.0) * m * 0.02 * _edge_taper(s)
        return 1.0 - m * 0.55 + m * 0.10 * s + noise
    s = (t - 0.70) / 0.30
    return 1.0 - m * 0.45 - m * 0.55 * s


def chaos(t: float, mag: float, seed: int) -> float:
    """Bounded random walk -- unpredictable shape, but the swing is
    constrained to ``±mag`` so the chart can't tunnel to zero or
    overshoot wildly.

    Pre-fix the walk was ``cum / sqrt(n) * 2.2`` with no clamp -- with
    ``mag=30`` a 1.2σ event could drive the price -80% from start (and
    a 2σ event would push the multiplier negative, clipping to ``1e-12``
    in compute_price). The walk is now normalized to a tanh-bounded
    swing within ``[-mag, +mag]`` and tapered so it returns near the
    start at t=1, matching the "no net trend guarantee" promise without
    blowing through the impact rails.
    """
    m = abs(mag) / 100.0
    rng = _rng(seed)
    n = 240
    step = int(t * n)
    cum = 0.0
    for _ in range(step):
        cum += rng.gauss(0.0, 1.0)
    # Normalize to ~[-1, 1] via tanh so extreme draws can't blow up.
    bounded = math.tanh(cum / math.sqrt(n))
    # Edge-taper towards 0 so the walk starts and ends near start_price.
    taper = math.sin(math.pi * t) ** 0.5 if 0.0 < t < 1.0 else 0.0
    return 1.0 + m * bounded * taper


def zigzag(t: float, mag: float, seed: int) -> float:
    """Triangular zigzag, five oscillations across the duration.

    Uses asin(sin(...)) to render an exact triangle wave that starts at 0,
    rises to +1, falls through 0 to -1, and returns to 0 each cycle.
    """
    m = abs(mag) / 100.0
    cycles = 5.0
    return 1.0 + m * (2.0 / math.pi) * math.asin(math.sin(2.0 * math.pi * cycles * t))


def spike(t: float, mag: float, seed: int) -> float:
    """Single sharp wick spike at midpoint, otherwise quiet.

    Pre-fix width was 0.04 in t-units -- on a 30 min event with
    PRICE_TICK_SECONDS=15s that was ~4 ticks of visible spike, easy to
    miss on a chart and indistinguishable from noise on slower
    timeframes. Widened to 0.10 (~6 min on a 30 min event, ~12 ticks)
    so the wick survives 5m/15m candle aggregation.
    """
    m = abs(mag) / 100.0
    x = (t - 0.5) / 0.10
    return 1.0 + m * math.exp(-x * x)


def accumulate(t: float, mag: float, seed: int) -> float:
    """Slow upward accumulation, accelerating breakout in the final stretch.

    The pre-rewrite version returned pure noise around 1.0 for the first
    70% of the event so the chart looked flat -- players reported the
    pattern "wasn't doing anything" until the last few minutes. The
    accumulation phase now drifts up to 25% of magnitude over the first
    70% (with light noise on top so the line still wiggles), then the
    final 30% accelerates the remaining 75% of the move into the
    breakout. Total still hits +mag at t=1.0.
    """
    m = abs(mag) / 100.0
    if t < 0.70:
        ramp = t / 0.70
        rng = _rng(seed, int(t * 800))
        noise = rng.gauss(0.0, 1.0) * m * 0.02 * _edge_taper(t)
        return 1.0 + m * 0.25 * ramp + noise
    s = (t - 0.70) / 0.30
    return 1.0 + m * 0.25 + m * 0.75 * (s ** 0.6)


def distribute(t: float, mag: float, seed: int) -> float:
    """Slow downward distribution, accelerating breakdown in the final stretch.

    Mirror of ``accumulate``: pre-rewrite the first 70% was pure noise
    around 1.0 so the dump only became visible in the last 30%, which
    looked like a "broken" event. Distribution phase now drifts down to
    -25% of magnitude over the first 70% (light noise included), then the
    final 30% accelerates the remaining -75% of the dump. Total still
    hits -mag at t=1.0.
    """
    m = abs(mag) / 100.0
    if t < 0.70:
        ramp = t / 0.70
        rng = _rng(seed, int(t * 800))
        noise = rng.gauss(0.0, 1.0) * m * 0.02 * _edge_taper(t)
        return 1.0 - m * 0.25 * ramp + noise
    s = (t - 0.70) / 0.30
    return 1.0 - m * 0.25 - m * 0.75 * (s ** 0.6)


def stairstep(t: float, mag: float, seed: int) -> float:
    """Stair-step up: four discrete jumps with flat consolidation between.

    Each step rises during the first 25% of its window, then plateaus.
    f(0) = 1.0, f(1) = 1 + m.
    """
    m = abs(mag) / 100.0
    steps = 4
    s = t * steps
    idx = int(s)
    if idx >= steps:
        return 1.0 + m
    pos = s - idx
    if pos < 0.25:
        progress = (idx + pos / 0.25) / steps
    else:
        progress = (idx + 1) / steps
    return 1.0 + m * progress


def fakeout(t: float, mag: float, seed: int) -> float:
    """Fake breakout up, then dump straight back through to a real breakdown."""
    m = abs(mag) / 100.0
    if t < 0.30:
        s = t / 0.30
        return 1.0 + m * 0.5 * (s ** 0.6)
    if t < 0.45:
        s = (t - 0.30) / 0.15
        peak = 1.0 + m * 0.5
        return peak - m * 0.5 * (s ** 0.7)
    s = (t - 0.45) / 0.55
    return 1.0 - m * (s ** 0.8)


# ── Pattern registry ────────────────────────────────────────────────────────
# bias: "bull" | "bear" | "vol" -- color hint and direction-sense for help cards
# default_mag: sane default magnitude when the user supplies a pattern but no number
# blurb: one-line player-facing hype description
PATTERNS: dict[str, dict] = {
    "linear":        {"fn": linear,        "bias": "vol",  "default_mag": 25.0, "blurb": "Straight-line drift -- honors the sign you pass."},
    "pump":          {"fn": pump,          "bias": "bull", "default_mag": 40.0, "blurb": "FOMO J-curve. Quiet, then rip."},
    "moon":          {"fn": moon,          "bias": "bull", "default_mag": 80.0, "blurb": "Parabolic moonshot. Vertical takeoff."},
    "dump":          {"fn": dump,          "bias": "bear", "default_mag": 30.0, "blurb": "Slow leak into capitulation candle."},
    "crash":         {"fn": crash,         "bias": "bear", "default_mag": 50.0, "blurb": "Cliff drop with a small dead-cat bounce."},
    "bull":          {"fn": bull,          "bias": "bull", "default_mag": 30.0, "blurb": "Steady uptrend with shallow pullbacks."},
    "bear":          {"fn": bear,          "bias": "bear", "default_mag": 30.0, "blurb": "Steady downtrend with relief rallies."},
    "volatile":      {"fn": volatile,      "bias": "vol",  "default_mag": 20.0, "blurb": "Wild chop. Big swings, small net drift."},
    "wave":          {"fn": wave,          "bias": "vol",  "default_mag": 18.0, "blurb": "Clean sine wave -- ends where it started."},
    "rugpull":       {"fn": rugpull,       "bias": "bear", "default_mag": 45.0, "blurb": "Pump, plateau, catastrophic dump below start."},
    "pumpdump":      {"fn": pumpdump,      "bias": "vol",  "default_mag": 30.0, "blurb": "Pump and dump round-trip back to start."},
    "vshape":        {"fn": vshape,        "bias": "vol",  "default_mag": 25.0, "blurb": "Dump to the floor, full recovery by close."},
    "hns":           {"fn": hns,           "bias": "bear", "default_mag": 28.0, "blurb": "Head & shoulders top, neckline breakdown."},
    "double_top":    {"fn": double_top,    "bias": "bear", "default_mag": 25.0, "blurb": "M-shape. Two peaks, sell-off into close."},
    "double_bottom": {"fn": double_bottom, "bias": "bull", "default_mag": 25.0, "blurb": "W-shape. Two troughs, breakout into close."},
    "cup_handle":    {"fn": cup_handle,    "bias": "bull", "default_mag": 30.0, "blurb": "Cup, handle dip, breakout."},
    "bullflag":      {"fn": bullflag,      "bias": "bull", "default_mag": 35.0, "blurb": "Flagpole, tight consolidation, breakout."},
    "bearflag":      {"fn": bearflag,      "bias": "bear", "default_mag": 35.0, "blurb": "Flagpole dump, weak relief, breakdown."},
    "chaos":         {"fn": chaos,         "bias": "vol",  "default_mag": 25.0, "blurb": "Pure random walk. No promises."},
    "zigzag":        {"fn": zigzag,        "bias": "vol",  "default_mag": 20.0, "blurb": "Sharp triangle waves, five cycles."},
    "spike":         {"fn": spike,         "bias": "vol",  "default_mag": 60.0, "blurb": "Single huge wick at midpoint."},
    "accumulate":    {"fn": accumulate,    "bias": "bull", "default_mag": 30.0, "blurb": "Quiet base, late-stage breakout."},
    "distribute":    {"fn": distribute,    "bias": "bear", "default_mag": 25.0, "blurb": "Quiet top, late-stage breakdown."},
    "stairstep":     {"fn": stairstep,     "bias": "bull", "default_mag": 30.0, "blurb": "Discrete step-ups with flat plateaus."},
    "fakeout":       {"fn": fakeout,       "bias": "bear", "default_mag": 30.0, "blurb": "Fake breakout, then real breakdown."},
}

ALIASES: dict[str, str] = {
    "j":               "pump",
    "rocket":          "moon",
    "rug":             "rugpull",
    "v":               "vshape",
    "hs":              "hns",
    "dt":              "double_top",
    "db":              "double_bottom",
    "cup":             "cup_handle",
    "ch":              "cup_handle",
    "flag":            "bullflag",
    "bullfl":          "bullflag",
    "bearfl":          "bearflag",
    "chop":            "volatile",
    "vol":             "volatile",
    "sine":            "wave",
    "sin":             "wave",
    "rw":              "chaos",
    "randomwalk":      "chaos",
    "zz":              "zigzag",
    "saw":             "zigzag",
    "splash":          "pumpdump",
    "pd":              "pumpdump",
    "capitulation":    "crash",
    "fall":            "dump",
    "fakebreakout":    "fakeout",
    "trap":            "fakeout",
    "stairs":          "stairstep",
    "step":            "stairstep",
    "acc":             "accumulate",
    "dist":            "distribute",
    "headshoulders":   "hns",
    "doubletop":       "double_top",
    "doublebottom":    "double_bottom",
    "cuphandle":       "cup_handle",
}


def resolve_pattern(name: str) -> str | None:
    """Resolve a pattern name (or alias) to a canonical key in PATTERNS, or None."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in PATTERNS:
        return key
    if key in ALIASES:
        return ALIASES[key]
    return None


def compute_price(
    pattern: str, t: float, magnitude_pct: float, seed: int, start_price: float,
) -> float:
    """Compute the live oracle price for a pattern at progress t in [0, 1].

    Anchors every curve to ``start_price`` at ``t == 0`` by subtracting the
    raw value at zero -- patterns with bump tails (head & shoulders, double
    top/bottom) or phase-shifted oscillators (zigzag, stairstep) would
    otherwise nudge the chart by a fraction of a percent the moment the
    event begins. Anchoring keeps the visible chart continuous.
    """
    t = max(0.0, min(1.0, t))
    fn = PATTERNS[pattern]["fn"]
    multiplier = fn(t, magnitude_pct, seed)
    anchor_offset = fn(0.0, magnitude_pct, seed) - 1.0
    multiplier -= anchor_offset
    return max(1e-12, start_price * multiplier)


def random_pattern(rng: random.Random | None = None) -> str:
    """Pick a uniformly random pattern key (excludes ``linear``)."""
    rng = rng or random.Random()
    keys = [k for k in PATTERNS.keys() if k != "linear"]
    return rng.choice(keys)


def random_magnitude(pattern: str, rng: random.Random | None = None) -> float:
    """Pick a sensible random magnitude for a given pattern (60-160% of default)."""
    rng = rng or random.Random()
    base = PATTERNS[pattern]["default_mag"]
    return round(base * rng.uniform(0.6, 1.6), 1)


def random_duration(rng: random.Random | None = None) -> float:
    """Pick a random duration in minutes from a fun spread (10-180 min)."""
    rng = rng or random.Random()
    return float(rng.choice([10, 15, 20, 30, 45, 60, 90, 120, 180]))
