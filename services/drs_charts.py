"""Pillow chart renderer for the DRS audit surface.

Three public entry points, each returning a PNG ``bytes`` object the cog
wraps in ``discord.File``:

    render_value_bars(title, items)              -> horizontal bar chart
    render_winloss_bars(title, wins, losses)     -> win/loss twin bars
    render_timeline(title, events, subtitle="")  -> cumulative wealth flow

Charts are rendered server-side with Pillow only (no matplotlib) so the
container ships zero extra plotting deps. The colour palette is pulled
from ``constants/ui.py`` via the framework re-export so a future palette
change propagates automatically.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


# ── Layout constants ────────────────────────────────────────────────────
_W: int = 1000
_H: int = 520
_PAD_L: int = 80
_PAD_R: int = 30
_PAD_T: int = 70
_PAD_B: int = 80

# ── Palette ─────────────────────────────────────────────────────────────
# Matches ``core.framework.ui`` constants. Hard-coded RGB tuples so the
# renderer has no Discord runtime dependency.
_BG       = (22, 27, 34)        # C_CHART_BG
_PANEL    = (32, 38, 48)
_GRID     = (55, 65, 80)
_AXIS     = (140, 150, 165)
_TEXT     = (220, 225, 235)
_SUBTEXT  = (140, 150, 165)
_BULL     = (0, 255, 136)       # C_BULL  -- tax in / accumulation
_BEAR     = (255, 68, 68)       # C_BEAR  -- UBI out / depletion
_GOLD     = (241, 196, 15)      # C_GOLD
_INFO     = (52, 152, 219)      # C_INFO  -- Gini line
_NAVY     = (44, 62, 80)        # C_NAVY

# ── Font loading ───────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"
_FONT_BOLD_PATH = _FONT_DIR / "DejaVuSans-Bold.ttf"
_FONT_REG_PATH = _FONT_DIR / "DejaVuSans.ttf"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD_PATH if bold else _FONT_REG_PATH
    try:
        return ImageFont.truetype(str(path), size=size)
    except Exception:
        # Fallback to default bitmap font if bundled fonts are missing in
        # a dev container -- production has DejaVu under assets/.
        return ImageFont.load_default()


def _fmt_short_usd(v: float) -> str:
    """Compact USD label for axis ticks: $1.2M, $45K, $9.99."""
    av = abs(v)
    if av >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if av >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    if av >= 1:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def _fmt_date(ts) -> str:
    """Normalise a DB timestamp (epoch float or datetime) to MM/DD HH:MM."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    elif isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%m/%d %H:%M")


def _draw_panel(
    img: Image.Image,
    *,
    title: str,
    subtitle: str = "",
) -> tuple[ImageDraw.ImageDraw, int, int, int, int]:
    """Paint the background + title and return the inner plot bounds.

    Returns ``(draw, x0, y0, x1, y1)`` -- the rectangle the data series
    should be plotted into. The caller draws axes + series inside that
    box.
    """
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle((0, 0, _W, _H), fill=_BG)
    inset = 14
    draw.rounded_rectangle(
        (inset, inset, _W - inset, _H - inset),
        radius=18, fill=_PANEL,
    )
    title_font = _font(28, bold=True)
    sub_font = _font(15)
    draw.text((_PAD_L, 22), title, fill=_TEXT, font=title_font)
    if subtitle:
        draw.text((_PAD_L, 56), subtitle, fill=_SUBTEXT, font=sub_font)
    x0, y0 = _PAD_L, _PAD_T + 20
    x1, y1 = _W - _PAD_R, _H - _PAD_B
    return draw, x0, y0, x1, y1


def _draw_axes(
    draw: ImageDraw.ImageDraw,
    x0: int, y0: int, x1: int, y1: int,
    *,
    y_labels: Sequence[tuple[float, str]],
    x_labels: Sequence[tuple[float, str]],
) -> None:
    """Render axis lines, grid, and tick labels.

    ``y_labels`` is ``[(y_px, text)]`` and ``x_labels`` is
    ``[(x_px, text)]`` -- caller pre-computes the pixel positions so the
    axis helper doesn't have to know the data scale.
    """
    axis_font = _font(13)
    # Horizontal grid + Y labels
    for y_px, label in y_labels:
        draw.line((x0, y_px, x1, y_px), fill=_GRID, width=1)
        tw = draw.textlength(label, font=axis_font)
        draw.text(
            (x0 - tw - 8, y_px - 8), label,
            fill=_SUBTEXT, font=axis_font,
        )
    # Vertical guides + X labels (every other to keep readable)
    for i, (x_px, label) in enumerate(x_labels):
        if i % max(1, len(x_labels) // 8) == 0:
            draw.line((x_px, y0, x_px, y1), fill=_GRID, width=1)
            tw = draw.textlength(label, font=axis_font)
            draw.text(
                (x_px - tw / 2, y1 + 8), label,
                fill=_SUBTEXT, font=axis_font,
            )
    # Axis baselines
    draw.line((x0, y1, x1, y1), fill=_AXIS, width=2)
    draw.line((x0, y0, x0, y1), fill=_AXIS, width=2)


def _legend(
    draw: ImageDraw.ImageDraw,
    x: int, y: int,
    items: Sequence[tuple[tuple[int, int, int], str]],
) -> None:
    """Compact horizontal legend with colour swatches."""
    leg_font = _font(14, bold=True)
    cursor = x
    for colour, label in items:
        draw.rectangle((cursor, y + 3, cursor + 14, y + 17), fill=colour)
        draw.text((cursor + 22, y), label, fill=_TEXT, font=leg_font)
        cursor += int(draw.textlength(label, font=leg_font)) + 60


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Public renderers ────────────────────────────────────────────────────


def render_value_bars(
    title: str,
    items: Sequence[tuple[str, float]],
    *,
    subtitle: str = "",
    value_label: str = "USD",
) -> bytes:
    """Generic horizontal bar chart: label -> USD value.

    Used by DRS stakes / LP / stones / gamba surfaces so every audit
    view that breaks one player's portfolio into categories renders the
    same way. ``items`` is ``[(label, value)]``; the renderer sorts
    desc by value and clips to the top 12 to keep the chart legible.
    """
    img = Image.new("RGB", (_W, _H), _BG)
    if not items:
        _draw_panel(img, title=title, subtitle=subtitle or "No data.")
        return _to_png(img)
    rows = sorted(items, key=lambda x: float(x[1] or 0.0), reverse=True)[:12]
    max_val = max(float(v or 0.0) for _, v in rows) or 1.0
    total = sum(float(v or 0.0) for _, v in rows)
    sub = subtitle or (
        f"{len(rows)} categories   "
        f"Total: {_fmt_short_usd(total)}   "
        f"Top: {_fmt_short_usd(max_val)}"
    )
    draw, x0, y0, x1, y1 = _draw_panel(img, title=title, subtitle=sub)
    row_h = max(18, min(38, (y1 - y0) // max(1, len(rows))))
    label_w = 220  # px reserved for the row label on the left
    bar_x0 = x0 + label_w
    bar_x1 = x1 - 110
    bar_zone = max(20, bar_x1 - bar_x0)
    row_font = _font(14, bold=False)
    val_font = _font(13, bold=True)
    for i, (label, val) in enumerate(rows):
        val_f = float(val or 0.0)
        y_top = y0 + i * row_h + 4
        y_bot = y_top + row_h - 8
        # label
        lbl = label if len(label) <= 28 else label[:27] + "."
        draw.text((x0, y_top + 2), lbl, fill=_TEXT, font=row_font)
        # bar
        bw = int(bar_zone * (val_f / max_val))
        draw.rounded_rectangle(
            (bar_x0, y_top, bar_x0 + max(2, bw), y_bot),
            radius=4, fill=_INFO,
        )
        # value label to the right of the bar
        v_text = _fmt_short_usd(val_f)
        draw.text(
            (bar_x0 + max(2, bw) + 8, y_top + 2),
            v_text, fill=_GOLD, font=val_font,
        )
    return _to_png(img)


def render_winloss_bars(
    title: str,
    *,
    wins: Sequence[tuple[str, float]],
    losses: Sequence[tuple[str, float]],
    subtitle: str = "",
) -> bytes:
    """Per-game-type wins/losses twin-bar chart.

    ``wins`` and ``losses`` are ``[(game_type, usd)]`` with the same
    set of game types (missing entries default to 0). Used by
    ``,drs games`` so an auditor can see at a glance who's winning vs
    losing on which game.
    """
    img = Image.new("RGB", (_W, _H), _BG)
    keys = sorted({k for k, _ in wins} | {k for k, _ in losses})
    if not keys:
        _draw_panel(img, title=title, subtitle=subtitle or "No games yet.")
        return _to_png(img)
    win_map = {k: float(v or 0.0) for k, v in wins}
    loss_map = {k: float(v or 0.0) for k, v in losses}
    max_val = max(
        max((win_map.get(k, 0.0) for k in keys), default=0.0),
        max((loss_map.get(k, 0.0) for k in keys), default=0.0),
        1.0,
    )
    total_w = sum(win_map.values())
    total_l = sum(loss_map.values())
    sub = subtitle or (
        f"Won: {_fmt_short_usd(total_w)}   "
        f"Lost: {_fmt_short_usd(total_l)}   "
        f"Net: {_fmt_short_usd(total_w - total_l)}"
    )
    draw, x0, y0, x1, y1 = _draw_panel(img, title=title, subtitle=sub)
    n = len(keys)
    slot_w = (x1 - x0) / n
    bar_w = max(8.0, min(30.0, slot_w * 0.35))
    def y_for(v: float) -> int:
        return int(y1 - (v / max_val) * (y1 - y0))
    y_ticks = [
        (y_for(max_val * t), _fmt_short_usd(max_val * t))
        for t in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    x_ticks = [
        (int(x0 + (i + 0.5) * slot_w), k) for i, k in enumerate(keys)
    ]
    _draw_axes(draw, x0, y0, x1, y1, y_labels=y_ticks, x_labels=x_ticks)
    for i, k in enumerate(keys):
        cx = x0 + (i + 0.5) * slot_w
        w = win_map.get(k, 0.0)
        l = loss_map.get(k, 0.0)
        draw.rectangle(
            (cx - bar_w - 1, y_for(w), cx - 1, y1),
            fill=_BULL,
        )
        draw.rectangle(
            (cx + 1, y_for(l), cx + bar_w + 1, y1),
            fill=_BEAR,
        )
    _legend(draw, x0, _H - 32, [(_BULL, "Won"), (_BEAR, "Lost")])
    return _to_png(img)


def render_timeline(
    title: str,
    events: Sequence[dict],
    *,
    subtitle: str = "",
) -> bytes:
    """Wealth-over-time line chart for an account's transaction history.

    ``events`` is a list of ``{ts, delta_usd}`` rows sorted oldest
    first. The renderer plots a running cumulative line so an auditor
    sees inflow, drawdown, and recovery at a glance.
    """
    img = Image.new("RGB", (_W, _H), _BG)
    pts = [(e["ts"], float(e.get("delta_usd") or 0.0)) for e in events]
    if len(pts) < 2:
        _draw_panel(img, title=title, subtitle=subtitle or "Not enough events.")
        return _to_png(img)
    cum: list[float] = []
    running = 0.0
    for _, d in pts:
        running += d
        cum.append(running)
    max_val = max(cum) if cum else 0.0
    min_val = min(cum) if cum else 0.0
    if max_val == min_val:
        max_val += 1.0
        min_val -= 1.0
    sub = subtitle or (
        f"{len(pts)} events   "
        f"Range: {_fmt_short_usd(min_val)} to {_fmt_short_usd(max_val)}   "
        f"Final: {_fmt_short_usd(cum[-1])}"
    )
    draw, x0, y0, x1, y1 = _draw_panel(img, title=title, subtitle=sub)
    n = len(pts)
    span = max(max_val - min_val, 1e-9)
    def y_for(v: float) -> int:
        return int(y1 - (v - min_val) / span * (y1 - y0))
    def x_for(i: int) -> int:
        return int(x0 + i / max(1, n - 1) * (x1 - x0))
    y_ticks = [
        (y_for(min_val + (max_val - min_val) * t),
         _fmt_short_usd(min_val + (max_val - min_val) * t))
        for t in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    x_ticks = [(x_for(i), _fmt_date(pts[i][0])) for i in range(n)]
    _draw_axes(draw, x0, y0, x1, y1, y_labels=y_ticks, x_labels=x_ticks)
    line_pts = [(x_for(i), y_for(v)) for i, v in enumerate(cum)]
    # Fill underneath
    if min_val <= 0 <= max_val:
        zero_y = y_for(0.0)
        baseline = zero_y
    else:
        baseline = y1
    fill_poly = [(x0, baseline)] + line_pts + [(x1, baseline)]
    draw.polygon(fill_poly, fill=(_INFO[0], _INFO[1], _INFO[2], 60))
    for a, b in zip(line_pts[:-1], line_pts[1:]):
        draw.line((a[0], a[1], b[0], b[1]), fill=_INFO, width=3)
    return _to_png(img)

