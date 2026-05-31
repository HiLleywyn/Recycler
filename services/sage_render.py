"""services/sage_render.py  -  Pillow renderers for the three Sage games.

Renders three PNG cards:

* ``render_pattern(pattern_key, seed) -> bytes`` - candlestick-style chart
  drawing the requested chart pattern with light price noise. Used by
  ``,pattern`` for the multi-choice quiz.
* ``render_gauge(indicator_key, seed) -> bytes`` - indicator-reading card
  with the title, the three rows of (label, value, hint), and a small
  decorative sparkline. Used by ``,gauge``.
* ``render_tknom(tknom_key, seed) -> bytes`` - tokenomics card showing
  supply / mint / burn / lock / founder rows in a labelled stat grid.
  Used by ``,tknom``.

All three return raw PNG bytes so the cog wraps them in ``discord.File``
without round-tripping through disc.
"""
from __future__ import annotations

import io
import logging
import random


from constants.ui import (
    C_AMBER,
    C_BEAR,
    C_BULL,
    C_CHART_BG,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_NEUTRAL,
    C_SUBTLE,
)
from core.framework.render import RenderCanvas
from core.framework.render_primitives import (
    font,
    hex_to_rgb,
)

import configs.sage_config as sc

log = logging.getLogger(__name__)


# ============================================================================
# Pattern renderer
# ============================================================================
#
# Each pattern shape is built from a control-point recipe (key x-positions
# in 0..1, key y-positions in 0..1 where 1 is the top of the chart). The
# renderer then samples a smooth curve through the points, adds light noise,
# and draws a candle-by-candle view so the chart looks like a real coin
# chart rather than a textbook diagram.

_CHART_W = 960
_CHART_H = 540
_CANDLES = 60


_SHAPE_RECIPES: dict[str, list[tuple[float, float]]] = {
    # x,y in 0..1.  y closer to 1 = higher price.
    "head_and_shoulders": [
        (0.00, 0.30), (0.12, 0.55), (0.22, 0.40),
        (0.34, 0.55), (0.45, 0.78), (0.55, 0.55),
        (0.66, 0.40), (0.78, 0.55), (0.92, 0.30),
        (1.00, 0.22),
    ],
    "inverse_head_and_shoulders": [
        (0.00, 0.70), (0.12, 0.45), (0.22, 0.60),
        (0.34, 0.45), (0.45, 0.22), (0.55, 0.45),
        (0.66, 0.60), (0.78, 0.45), (0.92, 0.70),
        (1.00, 0.78),
    ],
    "double_top": [
        (0.00, 0.30), (0.20, 0.78), (0.40, 0.55),
        (0.58, 0.78), (0.80, 0.45), (1.00, 0.30),
    ],
    "double_bottom": [
        (0.00, 0.70), (0.20, 0.22), (0.40, 0.45),
        (0.58, 0.22), (0.80, 0.55), (1.00, 0.70),
    ],
    "ascending_triangle": [
        (0.00, 0.35), (0.18, 0.72), (0.30, 0.45),
        (0.46, 0.74), (0.58, 0.55), (0.74, 0.74),
        (0.85, 0.62), (1.00, 0.74),
    ],
    "descending_triangle": [
        (0.00, 0.65), (0.18, 0.28), (0.30, 0.55),
        (0.46, 0.26), (0.58, 0.45), (0.74, 0.26),
        (0.85, 0.38), (1.00, 0.26),
    ],
    "symmetrical_triangle": [
        (0.00, 0.78), (0.14, 0.28), (0.28, 0.70),
        (0.42, 0.34), (0.56, 0.62), (0.70, 0.40),
        (0.84, 0.54), (1.00, 0.48),
    ],
    "cup_and_handle": [
        (0.00, 0.72), (0.10, 0.55), (0.20, 0.38),
        (0.32, 0.28), (0.46, 0.26), (0.58, 0.32),
        (0.70, 0.55), (0.82, 0.72), (0.88, 0.62),
        (0.94, 0.65), (1.00, 0.78),
    ],
    "rounding_bottom": [
        (0.00, 0.72), (0.12, 0.52), (0.24, 0.35),
        (0.38, 0.25), (0.50, 0.22), (0.62, 0.25),
        (0.76, 0.35), (0.88, 0.52), (1.00, 0.72),
    ],
    "rounding_top": [
        (0.00, 0.28), (0.12, 0.48), (0.24, 0.65),
        (0.38, 0.75), (0.50, 0.78), (0.62, 0.75),
        (0.76, 0.65), (0.88, 0.48), (1.00, 0.28),
    ],
    "rising_wedge": [
        (0.00, 0.30), (0.12, 0.55), (0.22, 0.38),
        (0.34, 0.62), (0.46, 0.48), (0.58, 0.66),
        (0.70, 0.55), (0.82, 0.70), (0.92, 0.62),
        (1.00, 0.70),
    ],
    "falling_wedge": [
        (0.00, 0.72), (0.12, 0.46), (0.22, 0.62),
        (0.34, 0.38), (0.46, 0.55), (0.58, 0.34),
        (0.70, 0.46), (0.82, 0.32), (0.92, 0.40),
        (1.00, 0.32),
    ],
    "bull_flag": [
        (0.00, 0.30), (0.18, 0.70), (0.32, 0.66),
        (0.46, 0.62), (0.60, 0.58), (0.74, 0.55),
        (0.86, 0.62), (1.00, 0.72),
    ],
    "bear_flag": [
        (0.00, 0.72), (0.18, 0.30), (0.32, 0.36),
        (0.46, 0.40), (0.60, 0.44), (0.74, 0.48),
        (0.86, 0.40), (1.00, 0.28),
    ],
    "pennant": [
        (0.00, 0.30), (0.18, 0.72), (0.30, 0.60),
        (0.42, 0.55), (0.54, 0.58), (0.66, 0.55),
        (0.78, 0.58), (0.90, 0.56), (1.00, 0.62),
    ],
    "triple_top": [
        (0.00, 0.30), (0.16, 0.75), (0.30, 0.50),
        (0.44, 0.76), (0.58, 0.50), (0.72, 0.75),
        (0.86, 0.50), (1.00, 0.32),
    ],
    "triple_bottom": [
        (0.00, 0.70), (0.16, 0.25), (0.30, 0.50),
        (0.44, 0.24), (0.58, 0.50), (0.72, 0.25),
        (0.86, 0.50), (1.00, 0.68),
    ],
    # ── Expansion bank ────────────────────────────────────────────────────
    "broadening_top": [
        (0.00, 0.50), (0.14, 0.62), (0.28, 0.42),
        (0.42, 0.70), (0.56, 0.34), (0.70, 0.76),
        (0.84, 0.26), (1.00, 0.30),
    ],
    "broadening_bottom": [
        (0.00, 0.50), (0.14, 0.38), (0.28, 0.58),
        (0.42, 0.30), (0.56, 0.66), (0.70, 0.24),
        (0.84, 0.74), (1.00, 0.70),
    ],
    "diamond_top": [
        (0.00, 0.50), (0.12, 0.66), (0.24, 0.40),
        (0.36, 0.78), (0.50, 0.30), (0.64, 0.70),
        (0.76, 0.46), (0.88, 0.56), (1.00, 0.30),
    ],
    "diamond_bottom": [
        (0.00, 0.50), (0.12, 0.34), (0.24, 0.60),
        (0.36, 0.22), (0.50, 0.70), (0.64, 0.30),
        (0.76, 0.54), (0.88, 0.44), (1.00, 0.70),
    ],
    "island_reversal_top": [
        (0.00, 0.30), (0.20, 0.55), (0.36, 0.68),
        (0.40, 0.78),
        (0.50, 0.78), (0.60, 0.78),
        (0.64, 0.78),
        (0.66, 0.55), (0.80, 0.40), (1.00, 0.22),
    ],
    "island_reversal_bottom": [
        (0.00, 0.70), (0.20, 0.45), (0.36, 0.32),
        (0.40, 0.22),
        (0.50, 0.22), (0.60, 0.22),
        (0.64, 0.22),
        (0.66, 0.45), (0.80, 0.60), (1.00, 0.78),
    ],
    "flag_pole": [
        (0.00, 0.28), (0.10, 0.32), (0.20, 0.38),
        (0.32, 0.50), (0.46, 0.66), (0.60, 0.74),
        (0.72, 0.78), (0.84, 0.78), (1.00, 0.78),
    ],
    "bart_pattern": [
        (0.00, 0.30), (0.10, 0.30), (0.20, 0.32),
        (0.28, 0.74),
        (0.40, 0.74), (0.55, 0.74), (0.70, 0.74),
        (0.78, 0.32),
        (0.88, 0.30), (1.00, 0.30),
    ],
    "three_drives_top": [
        (0.00, 0.34), (0.14, 0.58), (0.24, 0.42),
        (0.38, 0.66), (0.50, 0.50), (0.64, 0.72),
        (0.78, 0.56), (0.92, 0.68), (1.00, 0.30),
    ],
    "bump_and_run_top": [
        (0.00, 0.30), (0.16, 0.38), (0.32, 0.46),
        (0.48, 0.58), (0.58, 0.74), (0.66, 0.80),
        (0.74, 0.66), (0.84, 0.48), (0.92, 0.36),
        (1.00, 0.28),
    ],
}


# ----------------------------------------------------------------------------
# Pattern guide lines
# ----------------------------------------------------------------------------
# Each pattern carries a list of structural guide lines drawn over the candles
# so the geometry is legible at a glance (necklines, support / resistance,
# trendlines, flag channels, flagpoles). Coordinates are (x1, y1, x2, y2) in
# the same 0..1 recipe space as _SHAPE_RECIPES (y closer to 1 = higher price).
# The trailing token is the line "kind", which picks a colour:
#   sup   -> support     (green)
#   res   -> resistance  (red)
#   neck  -> neckline    (gold)
#   trend -> trendline   (blue)
#   pole  -> impulse leg (gold)
# Lines name no patterns -- they only highlight the structure, so the quiz is
# still a real read (ascending vs descending vs symmetrical triangle etc.).
_ANNOT_COLOR: dict[str, int] = {
    "sup":   C_BULL,
    "res":   C_BEAR,
    "neck":  C_GOLD,
    "trend": C_INFO,
    "pole":  C_GOLD,
}

_PATTERN_ANNOTATIONS: dict[str, list[tuple[float, float, float, float, str]]] = {
    "head_and_shoulders": [
        (0.18, 0.40, 0.72, 0.40, "neck"),
    ],
    "inverse_head_and_shoulders": [
        (0.18, 0.60, 0.72, 0.60, "neck"),
    ],
    "double_top": [
        (0.14, 0.78, 0.64, 0.78, "res"),
        (0.20, 0.55, 0.80, 0.55, "neck"),
    ],
    "double_bottom": [
        (0.14, 0.22, 0.64, 0.22, "sup"),
        (0.20, 0.45, 0.80, 0.45, "neck"),
    ],
    "ascending_triangle": [
        (0.14, 0.74, 1.00, 0.74, "res"),
        (0.00, 0.35, 0.85, 0.62, "trend"),
    ],
    "descending_triangle": [
        (0.14, 0.26, 1.00, 0.26, "sup"),
        (0.00, 0.65, 0.85, 0.38, "trend"),
    ],
    "symmetrical_triangle": [
        (0.00, 0.78, 0.84, 0.54, "trend"),
        (0.14, 0.28, 0.70, 0.40, "trend"),
    ],
    "cup_and_handle": [
        (0.00, 0.72, 0.94, 0.72, "res"),
    ],
    "rounding_bottom": [
        (0.00, 0.72, 1.00, 0.72, "res"),
    ],
    "rounding_top": [
        (0.00, 0.28, 1.00, 0.28, "sup"),
    ],
    "rising_wedge": [
        (0.12, 0.55, 0.82, 0.70, "trend"),
        (0.00, 0.30, 0.92, 0.62, "trend"),
    ],
    "falling_wedge": [
        (0.00, 0.72, 0.92, 0.40, "trend"),
        (0.12, 0.46, 0.82, 0.32, "trend"),
    ],
    "bull_flag": [
        (0.00, 0.30, 0.18, 0.70, "pole"),
        (0.18, 0.72, 0.74, 0.57, "trend"),
        (0.18, 0.67, 0.74, 0.52, "trend"),
    ],
    "bear_flag": [
        (0.00, 0.72, 0.18, 0.30, "pole"),
        (0.18, 0.34, 0.74, 0.52, "trend"),
        (0.18, 0.27, 0.74, 0.45, "trend"),
    ],
    "pennant": [
        (0.00, 0.30, 0.18, 0.72, "pole"),
        (0.18, 0.72, 0.90, 0.60, "trend"),
        (0.30, 0.54, 0.90, 0.56, "trend"),
    ],
    "triple_top": [
        (0.12, 0.755, 0.78, 0.755, "res"),
        (0.24, 0.50, 0.90, 0.50, "neck"),
    ],
    "triple_bottom": [
        (0.12, 0.245, 0.78, 0.245, "sup"),
        (0.24, 0.50, 0.90, 0.50, "neck"),
    ],
    "broadening_top": [
        (0.14, 0.62, 0.70, 0.76, "trend"),
        (0.28, 0.42, 0.84, 0.26, "trend"),
    ],
    "broadening_bottom": [
        (0.28, 0.58, 0.84, 0.74, "trend"),
        (0.14, 0.38, 0.70, 0.24, "trend"),
    ],
    "diamond_top": [
        (0.00, 0.50, 0.36, 0.78, "trend"),
        (0.36, 0.78, 0.88, 0.56, "trend"),
        (0.00, 0.50, 0.50, 0.30, "trend"),
        (0.50, 0.30, 0.88, 0.56, "trend"),
    ],
    "diamond_bottom": [
        (0.00, 0.50, 0.50, 0.70, "trend"),
        (0.50, 0.70, 0.88, 0.44, "trend"),
        (0.00, 0.50, 0.36, 0.22, "trend"),
        (0.36, 0.22, 0.88, 0.44, "trend"),
    ],
    "island_reversal_top": [
        (0.38, 0.78, 0.66, 0.78, "res"),
    ],
    "island_reversal_bottom": [
        (0.38, 0.22, 0.66, 0.22, "sup"),
    ],
    "flag_pole": [
        (0.00, 0.28, 0.72, 0.78, "pole"),
    ],
    "bart_pattern": [
        (0.28, 0.74, 0.70, 0.74, "res"),
        (0.00, 0.30, 1.00, 0.30, "sup"),
    ],
    "three_drives_top": [
        (0.14, 0.58, 0.64, 0.72, "trend"),
        (0.24, 0.42, 0.78, 0.56, "trend"),
    ],
    "bump_and_run_top": [
        (0.00, 0.30, 0.48, 0.58, "trend"),
    ],
}


def _dashed_line(
    draw, p1: tuple[float, float], p2: tuple[float, float],
    fill, width: int = 2, dash: int = 9, gap: int = 6,
) -> None:
    """Draw a dashed line from p1 to p2 on a Pillow ImageDraw."""
    import math
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        return
    ux, uy = dx / dist, dy / dist
    pos = 0.0
    while pos < dist:
        end = min(pos + dash, dist)
        draw.line(
            ((x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)),
            fill=fill, width=width,
        )
        pos += dash + gap


def _smooth_path(
    control_points: list[tuple[float, float]],
    n: int,
) -> list[float]:
    """Interpolate y-values along a curve through ``control_points``.

    Linear in x between adjacent control points, with a smoothstep on the
    fractional position so corners feel rounded rather than poly-line angular.
    Returns a list of ``n`` y-values in 0..1.
    """
    if not control_points:
        return [0.5] * n
    pts = sorted(control_points, key=lambda p: p[0])
    out: list[float] = []
    for i in range(n):
        x = i / max(1, n - 1)
        # Find bracketing segment.
        j = 0
        while j < len(pts) - 1 and pts[j + 1][0] < x:
            j += 1
        if j >= len(pts) - 1:
            out.append(pts[-1][1])
            continue
        x0, y0 = pts[j]
        x1, y1 = pts[j + 1]
        denom = max(1e-6, x1 - x0)
        t = (x - x0) / denom
        # smoothstep: 3t^2 - 2t^3
        s = t * t * (3.0 - 2.0 * t)
        out.append(y0 + (y1 - y0) * s)
    return out


def _splice_compound_recipe(
    stages: list[dict],
) -> tuple[list[tuple[float, float]], list[float], list[dict]]:
    """Concatenate two shape recipes into one normalized control-point list.

    Each ``stage`` dict has:
      shape    -- key in ``_SHAPE_RECIPES``
      x_range  -- (x0, x1) where the stage's 0..1 recipe is rescaled to

    Stage 2 is y-shifted so its first control point matches stage 1's last
    control point, eliminating the visual jump at the seam. Returns the
    combined recipe, the list of seam x-positions, and a per-stage transform
    list (shape / x0 / x1 / y_offset) so the renderer can place each stage's
    guide-line annotations into the same spliced coordinate space.
    """
    combined: list[tuple[float, float]] = []
    seams: list[float] = []
    transforms: list[dict] = []
    anchor_y: float | None = None
    for idx, stage in enumerate(stages):
        recipe = _SHAPE_RECIPES.get(stage["shape"])
        if recipe is None:
            continue
        x0, x1 = stage["x_range"]
        x0 = float(x0)
        x1 = float(x1)
        if x1 <= x0:
            continue
        sorted_pts = sorted(recipe, key=lambda p: p[0])
        # Compute y offset so stage's first point matches the anchor.
        if anchor_y is not None and sorted_pts:
            y_offset = anchor_y - float(sorted_pts[0][1])
        else:
            y_offset = 0.0
        transforms.append({
            "shape": stage["shape"], "x0": x0, "x1": x1, "y_offset": y_offset,
        })
        last_y = anchor_y if anchor_y is not None else 0.5
        for (rx, ry) in sorted_pts:
            sx = x0 + float(rx) * (x1 - x0)
            sy = max(0.05, min(0.95, float(ry) + y_offset))
            combined.append((sx, sy))
            last_y = sy
        anchor_y = last_y
        if idx < len(stages) - 1:
            seams.append(x1)
    if not combined:
        combined = list(_SHAPE_RECIPES["double_bottom"])
    return combined, seams, transforms


def render_pattern(
    pattern_key: str,
    seed: int = 0,
    *,
    compound_stages: list[dict] | None = None,
) -> bytes:
    """Render the named chart pattern as a candlestick PNG.

    ``seed`` controls the per-candle noise so repeat plays of the same
    pattern look different (the recipe / shape is identical, only the
    high/low wicks and small intracandle wobble change).

    When ``compound_stages`` is provided, the chart is spliced from two
    or more single-pattern recipes (used by Pattern Lab's compound rounds).
    Each stage dict carries ``shape`` and ``x_range``; the spliced recipe
    is rendered through the same candle pipeline as a single pattern, with
    a faint vertical guide at each seam.
    """
    rng = random.Random(int(seed) or 1)
    seams: list[float] = []
    # Guide-line annotations in 0..1 recipe space: (x1, y1, x2, y2, kind).
    annotations: list[tuple[float, float, float, float, str]] = []
    if compound_stages:
        recipe, seams, transforms = _splice_compound_recipe(list(compound_stages))
        # Re-place each stage's guide lines into the spliced coordinate space.
        for tr in transforms:
            for (ax1, ay1, ax2, ay2, kind) in _PATTERN_ANNOTATIONS.get(tr["shape"], []):
                span = tr["x1"] - tr["x0"]
                yo = tr["y_offset"]
                annotations.append((
                    tr["x0"] + ax1 * span,
                    max(0.05, min(0.95, ay1 + yo)),
                    tr["x0"] + ax2 * span,
                    max(0.05, min(0.95, ay2 + yo)),
                    kind,
                ))
    else:
        recipe = _SHAPE_RECIPES.get(pattern_key)
        if recipe is None:
            recipe = _SHAPE_RECIPES["double_bottom"]
        annotations = list(_PATTERN_ANNOTATIONS.get(pattern_key, []))

    canvas = RenderCanvas(_CHART_W, _CHART_H, bg=C_CHART_BG)
    canvas.title("Identify the Chart Pattern", subtitle="Pattern Lab · Sage Network", color=C_GOLD)

    pad_l, pad_r, pad_t, pad_b = 70, 30, 100, 60
    x0, y0 = pad_l, pad_t
    x1, y1 = _CHART_W - pad_r, _CHART_H - pad_b

    # Synthesize close-prices, then build candles.
    closes_norm = _smooth_path(recipe, _CANDLES)
    base_price = rng.uniform(10.0, 250.0)
    span = rng.uniform(0.20, 0.60)   # fraction price range vs base
    lo_price = base_price * (1.0 - span / 2.0)
    hi_price = base_price * (1.0 + span / 2.0)
    closes: list[float] = []
    for cy in closes_norm:
        p = lo_price + cy * (hi_price - lo_price)
        # Small per-candle noise so the curve isn't unnaturally smooth.
        p *= 1.0 + rng.uniform(-0.012, 0.012)
        closes.append(p)

    opens: list[float] = [closes[0]]
    for c in closes[:-1]:
        opens.append(c * (1.0 + rng.uniform(-0.006, 0.006)))
    highs: list[float] = []
    lows: list[float] = []
    for o, c in zip(opens, closes):
        wick = max(o, c) * rng.uniform(0.005, 0.020)
        highs.append(max(o, c) + wick)
        lows.append(min(o, c) - wick)

    y_min = min(lows) * 0.98
    y_max = max(highs) * 1.02
    if y_max <= y_min:
        y_max = y_min + 1.0

    def y_for(p: float) -> int:
        return int(y1 - (p - y_min) / (y_max - y_min) * (y1 - y0))

    grid_color = (55, 65, 80)
    subtext = (140, 150, 165)
    axis_font = font(11)

    # Gridlines + y-axis labels.
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = int(y1 - t * (y1 - y0))
        canvas.draw.line(((x0, gy), (x1, gy)), fill=grid_color, width=1)
        price = y_min + t * (y_max - y_min)
        canvas.draw.text((6, gy - 7), f"{price:,.2f}", fill=subtext, font=axis_font)

    canvas.draw.line(((x0, y1), (x1, y1)), fill=subtext, width=1)
    canvas.draw.line(((x0, y0), (x0, y1)), fill=subtext, width=1)

    # Faint vertical seam guides for compound charts so the player can
    # see where the two patterns meet.
    if seams:
        seam_color = (90, 100, 120)
        available = x1 - x0
        for seam_x in seams:
            sx = x0 + int(float(seam_x) * available)
            for yy in range(y0, y1, 6):
                canvas.draw.line(((sx, yy), (sx, yy + 3)), fill=seam_color, width=1)

    # Candles.
    n = len(closes)
    available = x1 - x0
    candle_w = max(3, int(available / n) - 2)
    for i in range(n):
        cx = x0 + int((i + 0.5) * available / n)
        oy = y_for(opens[i])
        cy = y_for(closes[i])
        hy = y_for(highs[i])
        ly = y_for(lows[i])
        bullish = closes[i] >= opens[i]
        body_color = hex_to_rgb(C_BULL if bullish else C_BEAR)
        # Wick.
        canvas.draw.line(((cx, hy), (cx, ly)), fill=body_color, width=1)
        # Body.
        top = min(oy, cy)
        bot = max(oy, cy)
        if bot - top < 2:
            bot = top + 2
        canvas.draw.rectangle(
            (cx - candle_w // 2, top, cx + candle_w // 2, bot),
            fill=body_color,
            outline=body_color,
        )

    # Pattern guide lines, drawn over the candles. y is mapped through the
    # noise-free recipe price so the dashed lines trace the clean structure
    # while the candles wobble around them.
    price_span = hi_price - lo_price

    def y_for_norm(r: float) -> int:
        return y_for(lo_price + r * price_span)

    for (ax1, ay1, ax2, ay2, kind) in annotations:
        col = hex_to_rgb(_ANNOT_COLOR.get(kind, C_INFO))
        _dashed_line(
            canvas.draw,
            (x0 + ax1 * available, y_for_norm(ay1)),
            (x0 + ax2 * available, y_for_norm(ay2)),
            col, width=2,
        )

    # Footer hint.
    foot_font = font(13)
    canvas.draw.text(
        (pad_l, _CHART_H - pad_b + 20),
        "Dashed lines mark the key structure. Pick the matching pattern.",
        fill=subtext, font=foot_font,
    )
    return canvas.to_png_bytes()


# ============================================================================
# Gauge (indicator card) renderer
# ============================================================================

def _color_for_bias(bias: str) -> int:
    return {"bull": C_BULL, "bear": C_BEAR, "neutral": C_AMBER}.get(bias, C_NEUTRAL)


def render_gauge(indicator_key: str, seed: int = 0) -> bytes:
    """Render an indicator card with title + three labelled rows.

    The card layout is column-based:
      Title (large) on top.
      Three rows of (Label, Value, optional Hint chip) below.
      Decorative sparkline filling the right column.
    """
    rng = random.Random(int(seed) or 1)
    ind = sc.INDICATOR_BY_KEY.get(indicator_key)
    if ind is None:
        ind = sc.INDICATORS[0]
    width, height = 960, 460
    canvas = RenderCanvas(width, height, bg=C_CHART_BG)
    canvas.title("Read the Indicator", subtitle="Indicator Gauge · Sage Network", color=C_GOLD)

    # Card panel.
    panel = (40, 110, width - 40, height - 40)
    canvas.rounded_panel(panel, color=C_NAVY, radius=14)

    # Title row.
    title_font = font(22, bold=True)
    canvas.draw.text((60, 130), ind["title"], fill=hex_to_rgb(C_INFO), font=title_font)

    # Rows.
    row_y = 180
    row_h = 56
    label_font = font(15, bold=True)
    value_font = font(20, bold=True)
    hint_font = font(13, bold=True)
    subtext = (140, 150, 165)
    for label, value, hint in ind["rows"]:
        canvas.draw.text((70, row_y), label.upper(), fill=subtext, font=label_font)
        canvas.draw.text((280, row_y - 4), value, fill=hex_to_rgb(0xFFFFFF), font=value_font)
        if hint:
            hint_w = int(canvas.draw.textlength(hint, font=hint_font)) + 18
            hint_rect = (560, row_y - 4, 560 + hint_w, row_y + 22)
            chip_color = (
                C_BEAR if "overbought" in hint or "max longs" in hint or "climax" in hint
                or "bearish divergence" in hint or "death cross" in hint
                else C_BULL if "oversold" in hint or "max shorts" in hint
                or "bullish divergence" in hint or "golden cross" in hint
                else C_AMBER
            )
            canvas.draw.rounded_rectangle(
                hint_rect, radius=10, fill=hex_to_rgb(chip_color),
            )
            canvas.draw.text(
                (568, row_y - 2), hint, fill=hex_to_rgb(0x111111), font=hint_font,
            )
        row_y += row_h

    # Decorative mini sparkline (random walk, just for vibe).
    spark_rect = (700, 150, width - 60, 360)
    sx0, sy0, sx1, sy1 = spark_rect
    canvas.draw.rounded_rectangle(spark_rect, radius=10, fill=hex_to_rgb(C_CHART_BG))
    pts: list[tuple[int, int]] = []
    walk = 0.5
    n = 40
    for i in range(n):
        walk = max(0.05, min(0.95, walk + rng.uniform(-0.10, 0.10)))
        px = sx0 + int(i * (sx1 - sx0) / (n - 1))
        py = sy1 - int(walk * (sy1 - sy0))
        pts.append((px, py))
    for a, b in zip(pts[:-1], pts[1:]):
        canvas.draw.line((a, b), fill=hex_to_rgb(C_INFO), width=2)

    # Footer hint.
    foot_font = font(13)
    canvas.draw.text(
        (60, height - 30),
        "Is this signal Bearish, Neutral, or Bullish? Pick fast -- 30s clock.",
        fill=subtext, font=foot_font,
    )
    return canvas.to_png_bytes()


# ============================================================================
# Tokenomics card renderer
# ============================================================================

def render_tknom(tknom_key: str, seed: int = 0) -> bytes:
    """Render a tokenomics card with a 5-row labelled stat grid."""
    rng = random.Random(int(seed) or 1)
    t = sc.TOKENOMICS_BY_KEY.get(tknom_key)
    if t is None:
        t = sc.TOKENOMICS[0]
    width, height = 920, 520
    canvas = RenderCanvas(width, height, bg=C_CHART_BG)
    canvas.title("Read the Tokenomics", subtitle="Tokenomics Card · Sage Network", color=C_GOLD)

    panel = (40, 110, width - 40, height - 40)
    canvas.rounded_panel(panel, color=C_NAVY, radius=14)

    title_font = font(24, bold=True)
    canvas.draw.text((60, 130), t["title"], fill=hex_to_rgb(C_AMBER), font=title_font)

    # Stat rows.
    label_font = font(14, bold=True)
    value_font = font(20, bold=True)
    subtext = (140, 150, 165)
    row_y = 190
    row_h = 52
    for label, value in t["stats"].items():
        canvas.draw.text((70, row_y + 4), label.upper(), fill=subtext, font=label_font)
        # Value color hints to severity if obvious.
        v_color = 0xFFFFFF
        v_lower = value.lower()
        if "uncapped" in v_lower or "owner-discretion" in v_lower or "no cliff" in v_lower or "unlocked" in v_lower:
            v_color = C_BEAR
        elif "hard cap" in v_lower and "%" not in v_lower:
            v_color = C_BULL
        canvas.draw.text((320, row_y), value, fill=hex_to_rgb(v_color), font=value_font)
        # Divider line.
        canvas.draw.line(((70, row_y + row_h - 8), (width - 70, row_y + row_h - 8)),
                         fill=hex_to_rgb(C_SUBTLE), width=1)
        row_y += row_h

    foot_font = font(13)
    canvas.draw.text(
        (60, height - 30),
        "Inflationary, Deflationary, Stable, or Rug Risk? Pick carefully.",
        fill=subtext, font=foot_font,
    )
    return canvas.to_png_bytes()


# ============================================================================
# Cycle Phase card renderer
# ============================================================================

def render_cycle(phase_key: str, seed: int = 0) -> bytes:
    """Render a Cycle Phase card with the snapshot title + four metric rows.

    Mirrors the gauge card layout (title row + label/value/hint rows on the
    left, decorative panel on the right), tuned for a 4-row metric grid.
    """
    rng = random.Random(int(seed) or 1)
    phase = sc.CYCLE_BY_KEY.get(phase_key)
    if phase is None:
        phase = sc.CYCLE_PHASES[0]
    width, height = 960, 500
    canvas = RenderCanvas(width, height, bg=C_CHART_BG)
    canvas.title(
        "Classify the Cycle Phase",
        subtitle="Cycle Phase · Sage Network",
        color=C_GOLD,
    )

    panel = (40, 110, width - 40, height - 40)
    canvas.rounded_panel(panel, color=C_NAVY, radius=14)

    title_font = font(22, bold=True)
    canvas.draw.text((60, 130), phase["title"], fill=hex_to_rgb(C_INFO), font=title_font)

    # Stat rows.
    label_font = font(15, bold=True)
    value_font = font(20, bold=True)
    hint_font = font(13, bold=True)
    subtext = (140, 150, 165)
    row_y = 180
    row_h = 56
    for label, value, hint in phase["rows"]:
        canvas.draw.text((70, row_y), label.upper(), fill=subtext, font=label_font)
        canvas.draw.text((280, row_y - 4), value, fill=hex_to_rgb(0xFFFFFF), font=value_font)
        if hint:
            hint_w = int(canvas.draw.textlength(hint, font=hint_font)) + 18
            hint_rect = (560, row_y - 4, 560 + hint_w, row_y + 22)
            chip_color = (
                C_BEAR if any(k in hint for k in (
                    "extreme greed", "alt mania", "max longs", "max exposure",
                    "capitulation",
                ))
                else C_BULL if any(k in hint for k in (
                    "deep value", "extreme fear", "max fear", "generational",
                    "alt season", "rotation", "regime shift",
                ))
                else C_AMBER
            )
            canvas.draw.rounded_rectangle(
                hint_rect, radius=10, fill=hex_to_rgb(chip_color),
            )
            canvas.draw.text(
                (568, row_y - 2), hint, fill=hex_to_rgb(0x111111), font=hint_font,
            )
        row_y += row_h

    # Decorative cycle-wheel sketch on the right: a square panel with four
    # quadrant labels for the four phase buckets, with the answer's quadrant
    # drawn faintly (the actual answer is hidden via the label-only layout).
    wheel_rect = (700, 150, width - 60, 400)
    wx0, wy0, wx1, wy1 = wheel_rect
    canvas.draw.rounded_rectangle(wheel_rect, radius=12, fill=hex_to_rgb(C_CHART_BG))
    # Crosshair.
    mid_x = (wx0 + wx1) // 2
    mid_y = (wy0 + wy1) // 2
    canvas.draw.line(((wx0 + 12, mid_y), (wx1 - 12, mid_y)), fill=hex_to_rgb(C_SUBTLE), width=1)
    canvas.draw.line(((mid_x, wy0 + 12), (mid_x, wy1 - 12)), fill=hex_to_rgb(C_SUBTLE), width=1)
    # Quadrant labels (Wyckoff order).
    label_q = font(11, bold=True)
    canvas.draw.text((wx0 + 16, wy0 + 16), "ACCUM", fill=subtext, font=label_q)
    canvas.draw.text((mid_x + 16, wy0 + 16), "MARKUP", fill=subtext, font=label_q)
    canvas.draw.text((mid_x + 16, mid_y + 8), "DIST", fill=subtext, font=label_q)
    canvas.draw.text((wx0 + 16, mid_y + 8), "MARKDOWN", fill=subtext, font=label_q)
    # Small abstract path through the wheel (decorative noise).
    pts: list[tuple[int, int]] = []
    n = 24
    angle0 = rng.uniform(0.0, 6.28)
    for i in range(n):
        t = i / (n - 1)
        radius = (1.0 - 0.25 * t) * min((wx1 - wx0), (wy1 - wy0)) / 2.5
        ang = angle0 + t * 6.28 * 1.5
        import math as _math
        px = mid_x + int(radius * _math.cos(ang))
        py = mid_y + int(radius * _math.sin(ang))
        pts.append((px, py))
    for a, b in zip(pts[:-1], pts[1:]):
        canvas.draw.line((a, b), fill=hex_to_rgb(C_GOLD), width=2)

    foot_font = font(13)
    canvas.draw.text(
        (60, height - 30),
        "Accumulation, Markup, Distribution, or Markdown? Read the metrics.",
        fill=subtext, font=foot_font,
    )
    return canvas.to_png_bytes()


# ============================================================================
# discord.File helpers
# ============================================================================

def _bytes_to_file(png: bytes, filename: str):
    import discord
    return discord.File(io.BytesIO(png), filename=filename)


def pattern_file(pattern_key: str, seed: int):
    return _bytes_to_file(
        render_pattern(pattern_key, seed),
        f"pattern_{pattern_key}_{seed}.png",
    )


def pattern_compound_file(compound_key: str, stages: list[dict], seed: int):
    """Render a compound (spliced) pattern chart and wrap as discord.File."""
    return _bytes_to_file(
        render_pattern("", seed, compound_stages=stages),
        f"compound_{compound_key}_{seed}.png",
    )


def gauge_file(indicator_key: str, seed: int):
    return _bytes_to_file(
        render_gauge(indicator_key, seed),
        f"gauge_{indicator_key}_{seed}.png",
    )


def tknom_file(tknom_key: str, seed: int):
    return _bytes_to_file(
        render_tknom(tknom_key, seed),
        f"tknom_{tknom_key}_{seed}.png",
    )


def cycle_file(phase_key: str, seed: int):
    return _bytes_to_file(
        render_cycle(phase_key, seed),
        f"cycle_{phase_key}_{seed}.png",
    )


__all__ = [
    "render_pattern", "render_gauge", "render_tknom", "render_cycle",
    "pattern_file", "pattern_compound_file",
    "gauge_file", "tknom_file", "cycle_file",
]
