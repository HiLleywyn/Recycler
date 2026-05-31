"""Procedural frame renderer.

A "frame" is the decoration drawn AROUND the avatar disc. The plain
ring is already handled by :meth:`core.framework.render.RenderCanvas.avatar_circle`;
this module layers extra geometry on top -- claw marks, halo glows,
shell bumps, neon double-rings, etc. -- keyed by ``frame_id``.

The renderer entry point :func:`draw_frame` is a drop-in replacement
for ``canvas.avatar_circle``: it draws the ring + avatar exactly like
the original and THEN runs the per-frame decorator over the result.

Mirrors ``core/framework/banner_patterns.py`` and ``core/framework/sigil_art.py``
in style. No new canvas primitives; uses ``ImageDraw`` directly so the
canvas stays small.
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

from core.framework.render_primitives import hex_to_rgb


_Rect = Tuple[int, int, int, int]


# ── Decorators ────────────────────────────────────────────────────────


def _ring_decorator_tabby(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Four short claw-mark arcs at 45-deg corners."""
    rgb = hex_to_rgb(accent)
    arc_r = outer_r + max(6, outer_r // 12)
    arc_w = max(2, outer_r // 18)
    # Four 35-deg arcs centred on each diagonal
    for centre_deg in (45, 135, 225, 315):
        start = centre_deg - 18
        end = centre_deg + 18
        canvas.draw.arc(
            (cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r),
            start=start, end=end, fill=rgb, width=arc_w,
        )


def _ring_decorator_crescent(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """A small crescent moon hanging above the avatar at NE."""
    rgb = hex_to_rgb(accent)
    cm_r = max(8, outer_r // 4)
    cm_cx = cx + int(outer_r * 0.85)
    cm_cy = cy - int(outer_r * 0.85)
    canvas.draw.ellipse(
        (cm_cx - cm_r, cm_cy - cm_r, cm_cx + cm_r, cm_cy + cm_r),
        fill=rgb,
    )
    # Bite -- read the pixel directly under the bite so the punched
    # area blends with whatever's underneath (banner colour / gradient).
    try:
        bite_color = canvas.img.getpixel((cm_cx - cm_r // 2, cm_cy - cm_r // 4))
        if isinstance(bite_color, int):
            bite_color = (bite_color, bite_color, bite_color)
    except Exception:
        bite_color = (0, 0, 0)
    bite_r = int(cm_r * 0.85)
    canvas.draw.ellipse(
        (cm_cx - cm_r // 2 - bite_r, cm_cy - cm_r // 4 - bite_r,
         cm_cx - cm_r // 2 + bite_r, cm_cy - cm_r // 4 + bite_r),
        fill=bite_color,
    )


def _ring_decorator_shell_bumps(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Six evenly-spaced bumps around the rim (shell/coral)."""
    rgb = hex_to_rgb(accent)
    bump_r = max(4, outer_r // 12)
    ring_r = outer_r + bump_r // 2
    for i in range(6):
        a = math.radians(60 * i)
        bx = cx + int(math.cos(a) * ring_r)
        by = cy + int(math.sin(a) * ring_r)
        canvas.draw.ellipse(
            (bx - bump_r, by - bump_r, bx + bump_r, by + bump_r),
            fill=rgb,
        )


def _ring_decorator_comet_trail(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Bright comet head at NW with a dotted trail curving along the rim."""
    rgb = hex_to_rgb(accent)
    # Comet head
    head_r = max(5, outer_r // 9)
    head_x = cx - int(outer_r * 0.78)
    head_y = cy - int(outer_r * 0.78)
    canvas.draw.ellipse(
        (head_x - head_r, head_y - head_r, head_x + head_r, head_y + head_r),
        fill=rgb,
    )
    # Trail: 8 fading dots along the rim arc
    ring_r = outer_r + head_r // 2
    for i in range(1, 9):
        a = math.radians(225 - i * 18)
        dot_r = max(1, head_r - i // 2)
        dx = cx + int(math.cos(a) * ring_r)
        dy = cy + int(math.sin(a) * ring_r)
        canvas.draw.ellipse((dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r), fill=rgb)


def _ring_decorator_anchor_chain(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Chain-link ring: 12 small connected ellipses around the rim."""
    rgb = hex_to_rgb(accent)
    link_r = max(4, outer_r // 14)
    ring_r = outer_r + link_r
    for i in range(12):
        a = math.radians(30 * i)
        lx = cx + int(math.cos(a) * ring_r)
        ly = cy + int(math.sin(a) * ring_r)
        # Alternate orientations
        if i % 2 == 0:
            canvas.draw.ellipse(
                (lx - link_r, ly - link_r // 2, lx + link_r, ly + link_r // 2),
                outline=rgb, width=max(2, link_r // 3),
            )
        else:
            canvas.draw.ellipse(
                (lx - link_r // 2, ly - link_r, lx + link_r // 2, ly + link_r),
                outline=rgb, width=max(2, link_r // 3),
            )


def _ring_decorator_cards_pips(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Four suit-pip dots at N/E/S/W."""
    rgb = hex_to_rgb(accent)
    pip_r = max(4, outer_r // 11)
    ring_r = outer_r + pip_r + 2
    for a_deg in (0, 90, 180, 270):
        a = math.radians(a_deg)
        px = cx + int(math.cos(a) * ring_r)
        py = cy + int(math.sin(a) * ring_r)
        canvas.draw.ellipse((px - pip_r, py - pip_r, px + pip_r, py + pip_r), fill=rgb)


def _ring_decorator_eagle_laurel(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Two laurel sprigs curving up from the bottom (eagle frame)."""
    rgb = hex_to_rgb(accent)
    leaf_r = max(4, outer_r // 14)
    ring_r = outer_r + leaf_r // 2
    # Right sprig: 6 leaves from 240..330 deg
    for i, deg in enumerate((240, 255, 270, 285, 300, 315)):
        a = math.radians(deg)
        lx = cx + int(math.cos(a) * ring_r)
        ly = cy + int(math.sin(a) * ring_r)
        canvas.draw.ellipse(
            (lx - leaf_r, ly - leaf_r // 2, lx + leaf_r, ly + leaf_r // 2),
            fill=rgb,
        )
    # Left sprig (mirror)
    for i, deg in enumerate((300, 285, 270, 255, 240, 225)):
        a = math.radians(deg + 60)
        lx = cx + int(math.cos(a) * ring_r)
        ly = cy + int(math.sin(a) * ring_r)
        canvas.draw.ellipse(
            (lx - leaf_r, ly - leaf_r // 2, lx + leaf_r, ly + leaf_r // 2),
            fill=rgb,
        )


def _ring_decorator_diamond_facets(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Eight small facet triangles around the rim, alternating colours."""
    rgb_main = hex_to_rgb(color)
    rgb_accent = hex_to_rgb(accent)
    facet_r = max(5, outer_r // 10)
    ring_r = outer_r + facet_r
    for i in range(8):
        a = math.radians(45 * i)
        fx = cx + int(math.cos(a) * ring_r)
        fy = cy + int(math.sin(a) * ring_r)
        # Triangle pointing outward
        out_a = a
        side = facet_r
        tip = (fx + int(math.cos(out_a) * side),
               fy + int(math.sin(out_a) * side))
        # Two base corners perpendicular to the radius
        perp = a + math.pi / 2
        base_l = (fx + int(math.cos(perp) * side // 2),
                  fy + int(math.sin(perp) * side // 2))
        base_r = (fx - int(math.cos(perp) * side // 2),
                  fy - int(math.sin(perp) * side // 2))
        canvas.draw.polygon(
            [tip, base_l, base_r],
            fill=rgb_main if i % 2 == 0 else rgb_accent,
        )


def _ring_decorator_obsidian_double(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Multi-ring depth for the obsidian frame -- three concentric outlines."""
    main = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    for ring_off, w, rgb in (
        (max(4, outer_r // 10), max(2, outer_r // 16), detail),
        (max(8, outer_r // 6),  max(2, outer_r // 22), main),
        (max(12, outer_r // 5), max(2, outer_r // 28), detail),
    ):
        r = outer_r + ring_off
        canvas.draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            outline=rgb, width=w,
        )


def _ring_decorator_halo_glow(
    canvas, *, cx: int, cy: int, outer_r: int, color: int, accent: int,
) -> None:
    """Soft outer halo via gaussian-blurred ring (lunar / stellar style).

    Stamped onto the canvas via a temp RGBA layer composited under the
    avatar disc (the ring sits at outer_r so the avatar paints over the
    inner side of the halo).
    """
    halo_r = outer_r + max(10, outer_r // 4)
    pad = max(20, halo_r // 3)
    box = (cx - halo_r - pad, cy - halo_r - pad,
           cx + halo_r + pad, cy + halo_r + pad)
    w, h = box[2] - box[0], box[3] - box[1]
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    rgb = hex_to_rgb(accent) + (200,)
    ring_w = max(4, outer_r // 6)
    ldraw.ellipse(
        (pad, pad, w - pad, h - pad),
        outline=rgb, width=ring_w,
    )
    blurred = layer.filter(ImageFilter.GaussianBlur(radius=max(6, outer_r // 8)))
    canvas.img.paste(blurred, (box[0], box[1]), blurred)


# Default decorator does nothing extra (a plain ring is enough).
def _ring_decorator_noop(**_kwargs) -> None:
    return None


_FrameFn = Callable[..., None]

FRAMES: dict[str, _FrameFn] = {
    "tabby":         _ring_decorator_tabby,
    "crescent":      _ring_decorator_crescent,
    "shell":         _ring_decorator_shell_bumps,
    "coral":         _ring_decorator_shell_bumps,
    "comet":         _ring_decorator_comet_trail,
    "anchor_chain":  _ring_decorator_anchor_chain,
    "cards":         _ring_decorator_cards_pips,
    "eagle":         _ring_decorator_eagle_laurel,
    "diamond":       _ring_decorator_diamond_facets,
    "obsidian_ring": _ring_decorator_obsidian_double,
    # Mastery-track frames also get nicer effects
    "ember":         _ring_decorator_halo_glow,
    "frost":         _ring_decorator_halo_glow,
    "abyss":         _ring_decorator_obsidian_double,
    "rainbow":       _ring_decorator_diamond_facets,
    "platinum":      _ring_decorator_diamond_facets,
}


def has_frame(frame_id: str | None) -> bool:
    return bool(frame_id) and frame_id in FRAMES


def draw_frame(
    canvas,
    frame_id: str | None,
    xy: tuple[int, int],
    *,
    size: int = 96,
    avatar_bytes: Optional[bytes] = None,
    color: int,
    accent: int | None = None,
    ring_width: int = 4,
    fallback_color: int = 0x2c3e50,
) -> None:
    """Drop-in replacement for ``canvas.avatar_circle`` with per-frame flair.

    Renders the standard ring + avatar disc first (so the frame works for
    every cosmetic id, themed or not), then runs the per-id decorator
    over the surrounding pixels.
    """
    # Halo-style frames need to draw BEFORE the avatar so the glow sits
    # underneath the disc rather than on top.
    fn = FRAMES.get(frame_id) if frame_id else None
    if fn is _ring_decorator_halo_glow:
        cx = xy[0] + size // 2
        cy = xy[1] + size // 2
        outer_r = size // 2 + ring_width
        fn(canvas=canvas, cx=cx, cy=cy, outer_r=outer_r,
           color=color, accent=accent if accent is not None else color)
        fn = None  # already drawn; don't double-stamp

    canvas.avatar_circle(
        xy,
        size=size,
        avatar_bytes=avatar_bytes,
        ring_color=color,
        ring_width=ring_width,
        fallback_color=fallback_color,
    )

    if fn is None:
        return
    cx = xy[0] + size // 2
    cy = xy[1] + size // 2
    outer_r = size // 2 + ring_width
    try:
        fn(canvas=canvas, cx=cx, cy=cy, outer_r=outer_r,
           color=color, accent=accent if accent is not None else color)
    except Exception:
        # A broken decorator must never break the underlying avatar.
        return
