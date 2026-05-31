"""High-level Pillow renderer framework.

Every PNG renderer in the codebase composes against ``RenderCanvas``
plus the helpers exported here, so no cog or service has to copy-paste
the chess board scaffold or the equalizer chart scaffold again.

Public surface:

    canvas = RenderCanvas(1200, 600, bg=C_NAVY, gradient_to=C_DARK_BLUE)
    canvas.title("Apex Mastery", subtitle="Cross-system progression")
    canvas.rounded_panel((40, 120, 1160, 580), color=C_CHART_BG)
    canvas.pill_badge((60, 140), "FISHER L42", color=C_INFO)
    canvas.progress_bar((60, 200, 1140, 220), 0.62, color=C_SUCCESS)
    canvas.stat_block((60, 240), label="MASTERY", value="L42", color=C_GOLD)
    canvas.divider(280)
    canvas.avatar_circle((60, 320), size=120, avatar_bytes=raw)
    canvas.glyph_token((220, 320), "MTA", color=C_AMBER)
    file = canvas.to_discord_file("mastery.png")

Drawing primitives forward into ``core.framework.render_primitives`` so PIL
lives in one place. All colors flow through ``constants.ui`` so a
palette change propagates automatically.

This module also exposes the small generic chart routines
(``render_line_chart``, ``render_bar_chart``) so future systems can plot
without spinning up a dedicated chart module per surface -- the
``services/drs_charts.py`` pattern stays for renderer-specific
work, but generic charts live here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from PIL import Image, ImageDraw

from constants.ui import (
    C_CHART_BG,
    C_GOLD,
    C_GRAY,
    C_INFO,
    C_NAVY,
    C_NEUTRAL,
    C_SUBTLE,
    C_SUCCESS,
)
from core.framework.render_primitives import (
    avatar_mask,
    font,
    gradient_fill,
    glow,
    hex_to_rgb,
    inner_shadow,
    mix,
    rgba,
    text_with_outline,
    to_png_bytes,
)

log = logging.getLogger(__name__)


# ── Layout tokens ──────────────────────────────────────────────────────
# Default text colors for the dark project palette. Mirror the constants
# in ``services/drs_charts.py`` so charts produced here look the same
# as charts produced there.
_TEXT = (220, 225, 235)
_SUBTEXT = (140, 150, 165)
_AXIS = (140, 150, 165)
_GRID = (55, 65, 80)


# ── Canvas ─────────────────────────────────────────────────────────────
@dataclass
class _AvatarCache:
    """Tiny per-process cache for downloaded avatar bytes."""
    by_user: dict[int, Image.Image]


_AVATAR_CACHE = _AvatarCache(by_user={})


class RenderCanvas:
    """A Pillow image plus a high-level drawing toolkit.

    Always pair with ``core.framework.embed.card()`` -- the PNG is the visual
    body, the embed is the narration. No PNG should ship without a
    short text embed alongside, because Discord screen readers can't
    OCR PNGs.
    """

    __slots__ = ("img", "draw", "width", "height")

    def __init__(
        self,
        width: int,
        height: int,
        *,
        bg: int = C_NAVY,
        gradient_to: Optional[int] = None,
    ) -> None:
        self.width = width
        self.height = height
        self.img = Image.new("RGBA", (width, height), rgba(bg, 255))
        if gradient_to is not None:
            gradient_fill(self.img, bg, gradient_to)
        self.draw = ImageDraw.Draw(self.img, "RGBA")

    # ── high-level composition ───────────────────────────────────────
    def title(
        self,
        text: str,
        *,
        subtitle: str = "",
        x: int = 40,
        y: int = 28,
        color: int = C_GOLD,
    ) -> None:
        """Big top-line title plus optional subtitle."""
        title_font = font(34, bold=True)
        self.draw.text((x, y), text, fill=hex_to_rgb(color), font=title_font)
        if subtitle:
            sub_font = font(16)
            self.draw.text((x, y + 44), subtitle, fill=_SUBTEXT, font=sub_font)

    def rounded_panel(
        self,
        rect: tuple[int, int, int, int],
        *,
        color: int = C_CHART_BG,
        radius: int = 16,
        outline: Optional[int] = None,
        outline_width: int = 2,
    ) -> None:
        """Soft rounded rectangle. Use for grouping content into cards."""
        self.draw.rounded_rectangle(
            rect, radius=radius, fill=hex_to_rgb(color),
            outline=hex_to_rgb(outline) if outline is not None else None,
            width=outline_width if outline is not None else 0,
        )
        inner_shadow(self.img, rect, depth=3)

    def pill_badge(
        self,
        xy: tuple[int, int],
        text: str,
        *,
        color: int = C_INFO,
        text_color: int = 0xFFFFFF,
        padding: tuple[int, int] = (12, 6),
        font_size: int = 14,
        bold: bool = True,
    ) -> tuple[int, int, int, int]:
        """Capsule-shaped colored badge with text.

        Returns the bounding rect so callers can lay out neighbouring
        elements relative to it.
        """
        f = font(font_size, bold=bold)
        text_w = int(self.draw.textlength(text, font=f))
        x, y = xy
        pad_x, pad_y = padding
        ascent, descent = f.getmetrics()
        text_h = ascent + descent
        rect = (x, y, x + text_w + pad_x * 2, y + text_h + pad_y * 2)
        radius = (rect[3] - rect[1]) // 2
        self.draw.rounded_rectangle(rect, radius=radius, fill=hex_to_rgb(color))
        self.draw.text(
            (x + pad_x, y + pad_y - descent // 2),
            text, fill=hex_to_rgb(text_color), font=f,
        )
        return rect

    def progress_bar(
        self,
        rect: tuple[int, int, int, int],
        fraction: float,
        *,
        color: int = C_SUCCESS,
        bg_color: int = C_GRAY,
        radius: int = 8,
        label: Optional[str] = None,
        label_color: int = 0xFFFFFF,
    ) -> None:
        """Horizontal progress bar. ``fraction`` clamped to [0, 1]."""
        fraction = max(0.0, min(1.0, float(fraction)))
        x0, y0, x1, y1 = rect
        self.draw.rounded_rectangle(rect, radius=radius, fill=hex_to_rgb(bg_color))
        if fraction > 0:
            fill_x = x0 + int((x1 - x0) * fraction)
            if fill_x > x0:
                self.draw.rounded_rectangle(
                    (x0, y0, fill_x, y1), radius=radius, fill=hex_to_rgb(color),
                )
        if label:
            f = font(13, bold=True)
            tw = int(self.draw.textlength(label, font=f))
            tx = x0 + ((x1 - x0) - tw) // 2
            ty = y0 + ((y1 - y0) - 16) // 2
            text_with_outline(
                self.draw, (tx, ty), label,
                font_obj=f, fill=hex_to_rgb(label_color), outline=(0, 0, 0),
                outline_width=1,
            )

    def stat_block(
        self,
        xy: tuple[int, int],
        *,
        label: str,
        value: str,
        color: int = C_GOLD,
        label_color: int = C_NEUTRAL,
        size: tuple[int, int] = (200, 76),
        bg_color: int = C_CHART_BG,
    ) -> None:
        """Card-style label-over-value block.

        ``label`` is small, in muted color. ``value`` is large, in the
        primary color. Use for headline numbers.
        """
        x, y = xy
        w, h = size
        self.rounded_panel((x, y, x + w, y + h), color=bg_color, radius=10)
        lbl_font = font(12, bold=True)
        val_font = font(24, bold=True)
        self.draw.text(
            (x + 14, y + 10), label.upper(),
            fill=hex_to_rgb(label_color), font=lbl_font,
        )
        self.draw.text(
            (x + 14, y + 30), value,
            fill=hex_to_rgb(color), font=val_font,
        )

    def divider(
        self,
        y: int,
        *,
        x0: int = 40,
        x1: Optional[int] = None,
        color: int = C_SUBTLE,
        width: int = 1,
    ) -> None:
        """Thin horizontal divider line."""
        if x1 is None:
            x1 = self.width - 40
        self.draw.line(((x0, y), (x1, y)), fill=hex_to_rgb(color), width=width)

    def avatar_circle(
        self,
        xy: tuple[int, int],
        *,
        size: int = 96,
        avatar_bytes: Optional[bytes] = None,
        ring_color: int = C_GOLD,
        ring_width: int = 4,
        fallback_color: int = C_NAVY,
    ) -> None:
        """Circular avatar with a coloured ring.

        ``avatar_bytes`` should be raw PNG/JPEG from
        ``discord.Member.display_avatar.read()``. Pass ``None`` to draw
        the fallback disc (used when an avatar download fails or the
        user has no custom avatar).
        """
        x, y = xy
        ring_rect = (x - ring_width, y - ring_width,
                     x + size + ring_width, y + size + ring_width)
        self.draw.ellipse(ring_rect, fill=hex_to_rgb(ring_color))
        if avatar_bytes:
            try:
                import io as _io
                av = Image.open(_io.BytesIO(avatar_bytes)).convert("RGBA")
                av = av.resize((size, size), Image.LANCZOS)
                mask = avatar_mask(size)
                self.img.paste(av, (x, y), mask=mask)
                return
            except Exception:
                log.debug("avatar render failed; falling back to disc", exc_info=True)
        self.draw.ellipse(
            (x, y, x + size, y + size), fill=hex_to_rgb(fallback_color),
        )

    def glyph_token(
        self,
        xy: tuple[int, int],
        symbol: str,
        *,
        color: int = C_GOLD,
        text_color: int = 0xFFFFFF,
        diameter: int = 48,
        font_size: int = 16,
    ) -> None:
        """Small circular token badge with the symbol stamped on it."""
        x, y = xy
        self.draw.ellipse(
            (x, y, x + diameter, y + diameter), fill=hex_to_rgb(color),
        )
        f = font(font_size, bold=True)
        tw = int(self.draw.textlength(symbol, font=f))
        ascent, descent = f.getmetrics()
        th = ascent
        self.draw.text(
            (x + (diameter - tw) // 2, y + (diameter - th) // 2),
            symbol, fill=hex_to_rgb(text_color), font=f,
        )

    def text(
        self,
        xy: tuple[int, int],
        text: str,
        *,
        color: int = 0xFFFFFF,
        size: int = 16,
        bold: bool = False,
        outline: bool = False,
    ) -> int:
        """Plain text. Returns the width drawn so callers can chain layout."""
        f = font(size, bold=bold)
        if outline:
            text_with_outline(
                self.draw, xy, text,
                font_obj=f, fill=hex_to_rgb(color), outline=(0, 0, 0),
                outline_width=2,
            )
        else:
            self.draw.text(xy, text, fill=hex_to_rgb(color), font=f)
        return int(self.draw.textlength(text, font=f))

    def footer(
        self,
        text: str,
        *,
        color: int = C_SUBTLE,
        size: int = 12,
    ) -> None:
        """Bottom-right small footer (timestamps, watermark, etc.)."""
        f = font(size)
        tw = int(self.draw.textlength(text, font=f))
        self.draw.text(
            (self.width - tw - 20, self.height - size - 14),
            text, fill=hex_to_rgb(color), font=f,
        )

    def halo(
        self,
        rect: tuple[int, int, int, int],
        color: int,
        *,
        radius: int = 14,
        alpha: int = 130,
    ) -> None:
        """Soft glow behind a rect. Use to highlight an active element."""
        glow(self.img, rect, color, radius=radius, alpha=alpha)

    # ── output ───────────────────────────────────────────────────────
    def to_png_bytes(self) -> bytes:
        """Serialise to PNG bytes."""
        return to_png_bytes(self.img)

    def to_discord_file(self, filename: str):
        """Wrap the PNG in a ``discord.File`` for direct embed attachment.

        Lazy-imports ``discord`` so this module is safe to import from
        non-Discord contexts (API renderer, CLI, tests).
        """
        import io
        import discord
        buf = io.BytesIO(self.to_png_bytes())
        buf.seek(0)
        return discord.File(buf, filename=filename)


# ── Generic chart helpers ──────────────────────────────────────────────
# Small standalone functions for systems that need a chart but don't
# want to drop a whole renderer module. Heavier surfaces (mastery board,
# war map, profile card) build on RenderCanvas directly.


def render_line_chart(
    title_text: str,
    points: Sequence[tuple[float, float]],
    *,
    width: int = 1000,
    height: int = 520,
    subtitle: str = "",
    line_color: int = C_INFO,
    fill: bool = True,
    y_label_fmt=lambda v: f"{v:.2f}",
    x_label_fmt=lambda i, v: str(i),
) -> bytes:
    """Generic line chart. ``points`` is ``[(x, y)]``."""
    canvas = RenderCanvas(width, height, bg=C_CHART_BG)
    canvas.title(title_text, subtitle=subtitle)
    if len(points) < 2:
        canvas.text((40, 120), "Not enough data.", color=C_NEUTRAL, size=18)
        return canvas.to_png_bytes()
    pad_l, pad_r, pad_t, pad_b = 80, 30, 90, 60
    x0, y0 = pad_l, pad_t
    x1, y1 = width - pad_r, height - pad_b
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min:
        x_max = x_min + 1.0
    if y_max == y_min:
        y_max = y_min + 1.0

    def x_for(v: float) -> int:
        return int(x0 + (v - x_min) / (x_max - x_min) * (x1 - x0))

    def y_for(v: float) -> int:
        return int(y1 - (v - y_min) / (y_max - y_min) * (y1 - y0))

    # Gridlines + y ticks
    axis_font = font(12)
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = int(y1 - t * (y1 - y0))
        canvas.draw.line(((x0, gy), (x1, gy)), fill=_GRID, width=1)
        label = y_label_fmt(y_min + t * (y_max - y_min))
        canvas.draw.text(
            (8, gy - 8), label, fill=_SUBTEXT, font=axis_font,
        )
    # Axis baselines
    canvas.draw.line(((x0, y1), (x1, y1)), fill=_AXIS, width=2)
    canvas.draw.line(((x0, y0), (x0, y1)), fill=_AXIS, width=2)
    # X tick labels (5 evenly spaced)
    n = len(points)
    for i in range(5):
        idx = int(i / 4 * (n - 1))
        px = x_for(xs[idx])
        label = x_label_fmt(idx, xs[idx])
        tw = int(canvas.draw.textlength(label, font=axis_font))
        canvas.draw.text(
            (px - tw // 2, y1 + 6), label, fill=_SUBTEXT, font=axis_font,
        )
    # Polyline + optional fill
    line_pts = [(x_for(x), y_for(y)) for x, y in points]
    if fill:
        poly = [(x0, y1)] + line_pts + [(x1, y1)]
        canvas.draw.polygon(poly, fill=rgba(line_color, 60))
    for a, b in zip(line_pts[:-1], line_pts[1:]):
        canvas.draw.line((a, b), fill=hex_to_rgb(line_color), width=3)
    return canvas.to_png_bytes()


def render_bar_chart(
    title_text: str,
    bars: Sequence[tuple[str, float]],
    *,
    width: int = 1000,
    height: int = 520,
    subtitle: str = "",
    color: int = C_INFO,
    value_fmt=lambda v: f"{v:.2f}",
) -> bytes:
    """Generic vertical bar chart. ``bars`` is ``[(label, value)]``."""
    canvas = RenderCanvas(width, height, bg=C_CHART_BG)
    canvas.title(title_text, subtitle=subtitle)
    if not bars:
        canvas.text((40, 120), "No data.", color=C_NEUTRAL, size=18)
        return canvas.to_png_bytes()
    pad_l, pad_r, pad_t, pad_b = 80, 30, 90, 70
    x0, y0 = pad_l, pad_t
    x1, y1 = width - pad_r, height - pad_b
    max_v = max(float(v or 0) for _, v in bars) or 1.0
    n = len(bars)
    slot_w = (x1 - x0) / n
    bar_w = max(4.0, min(60.0, slot_w * 0.6))
    axis_font = font(12)
    # Y gridlines + ticks
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = int(y1 - t * (y1 - y0))
        canvas.draw.line(((x0, gy), (x1, gy)), fill=_GRID, width=1)
        label = value_fmt(max_v * t)
        canvas.draw.text((8, gy - 8), label, fill=_SUBTEXT, font=axis_font)
    canvas.draw.line(((x0, y1), (x1, y1)), fill=_AXIS, width=2)
    canvas.draw.line(((x0, y0), (x0, y1)), fill=_AXIS, width=2)
    # Bars + x labels
    for i, (label, val) in enumerate(bars):
        cx = x0 + (i + 0.5) * slot_w
        v = float(val or 0)
        bh = int((v / max_v) * (y1 - y0))
        canvas.draw.rounded_rectangle(
            (cx - bar_w / 2, y1 - bh, cx + bar_w / 2, y1),
            radius=4, fill=hex_to_rgb(color),
        )
        tw = int(canvas.draw.textlength(label, font=axis_font))
        canvas.draw.text(
            (cx - tw // 2, y1 + 6), label,
            fill=_SUBTEXT, font=axis_font,
        )
    return canvas.to_png_bytes()


# Re-export the most useful primitives so callers only have to import
# ``core.framework.render``.
__all__ = [
    "RenderCanvas",
    "render_line_chart",
    "render_bar_chart",
    "font",
    "hex_to_rgb",
    "rgba",
    "mix",
    "gradient_fill",
    "text_with_outline",
    "glow",
    "inner_shadow",
    "to_png_bytes",
]
