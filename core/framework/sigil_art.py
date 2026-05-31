"""Procedural sigil renderer.

Each sigil function takes a Pillow ``ImageDraw`` and a square bounding
rect ``(x0, y0, x1, y1)`` plus a foreground color and an accent (used
for highlights). The shape is drawn entirely inside the rect; the
caller has already painted the disc backdrop.

Mirrors ``core/framework/banner_patterns.py`` (same dispatch shape, same
primitives). Wired in via :func:`draw_sigil` from the three profile
renderers (profile / level / payout) plus the shop card grid.

Adding a new sigil:
  1. write a ``draw_<id>(draw, rect, color, accent)`` function below
  2. register it in :data:`SIGILS`
  3. add an entry to ``cosmetics_config.SIGILS`` with the matching id

The dispatch silently falls back to a "glyph-in-a-disc" path when the
sigil id has no draw function, so unthemed sigils (``star``, ``crown``,
``flame``, etc.) keep rendering with no extra wiring.
"""
from __future__ import annotations

import math
from typing import Callable, Tuple

from PIL import ImageDraw

from core.framework.render_primitives import hex_to_rgb


_Rect = Tuple[int, int, int, int]


def _center_and_radius(rect: _Rect) -> tuple[int, int, int]:
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    r = min(x1 - x0, y1 - y0) // 2
    return cx, cy, r


# ── Themed sigils ─────────────────────────────────────────────────────


def draw_cat(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # Two triangular ears
    ear_h = int(r * 0.65)
    ear_w = int(r * 0.55)
    # Left ear
    draw.polygon(
        [
            (cx - int(r * 0.65), cy - int(r * 0.05)),
            (cx - int(r * 0.05), cy - int(r * 0.15)),
            (cx - int(r * 0.65) + ear_w // 2, cy - ear_h),
        ],
        fill=fg,
    )
    # Right ear
    draw.polygon(
        [
            (cx + int(r * 0.65), cy - int(r * 0.05)),
            (cx + int(r * 0.05), cy - int(r * 0.15)),
            (cx + int(r * 0.65) - ear_w // 2, cy - ear_h),
        ],
        fill=fg,
    )
    # Head (slightly squished ellipse)
    head_w = int(r * 0.85)
    head_h = int(r * 0.72)
    draw.ellipse(
        (cx - head_w, cy - int(head_h * 0.5),
         cx + head_w, cy + int(head_h * 1.05)),
        fill=fg,
    )
    # Eyes
    eye_dx = int(r * 0.30)
    eye_r = max(2, r // 12)
    draw.ellipse(
        (cx - eye_dx - eye_r, cy - eye_r, cx - eye_dx + eye_r, cy + eye_r),
        fill=detail,
    )
    draw.ellipse(
        (cx + eye_dx - eye_r, cy - eye_r, cx + eye_dx + eye_r, cy + eye_r),
        fill=detail,
    )
    # Nose (small triangle)
    nose_h = max(3, r // 10)
    draw.polygon(
        [
            (cx - nose_h, cy + int(r * 0.18)),
            (cx + nose_h, cy + int(r * 0.18)),
            (cx, cy + int(r * 0.18) + nose_h),
        ],
        fill=detail,
    )
    # Whiskers
    wy = cy + int(r * 0.25)
    wlen = int(r * 0.7)
    draw.line((cx - int(r * 0.15), wy, cx - wlen, wy - 2), fill=detail, width=1)
    draw.line((cx - int(r * 0.15), wy + 4, cx - wlen, wy + 4), fill=detail, width=1)
    draw.line((cx + int(r * 0.15), wy, cx + wlen, wy - 2), fill=detail, width=1)
    draw.line((cx + int(r * 0.15), wy + 4, cx + wlen, wy + 4), fill=detail, width=1)


def draw_moon(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Full disc tilted slightly off-centre
    disc_r = int(r * 0.85)
    draw.ellipse(
        (cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r),
        fill=fg,
    )
    # A few stars around the crescent
    star_color = hex_to_rgb(accent)
    for dx, dy, s in (
        (-r + 4, -r + 8, 2),
        (r - 6, -r + 12, 2),
        (r - 10, r - 8, 1),
        (-r + 10, r - 4, 1),
    ):
        draw.ellipse(
            (cx + dx - s, cy + dy - s, cx + dx + s, cy + dy + s),
            fill=star_color,
        )


def draw_moon_crescent(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
    *, bg_rgb: tuple[int, int, int],
) -> None:
    """Crescent variant that bites with the disc backdrop colour.

    Used directly when ``bg_rgb`` (the disc backing colour) is known so
    we can punch a real notch instead of relying on alpha compositing.
    """
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    star_color = hex_to_rgb(accent)
    disc_r = int(r * 0.85)
    draw.ellipse(
        (cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r),
        fill=fg,
    )
    # Bite a smaller offset disc using the backdrop colour
    bite_r = int(disc_r * 0.85)
    bx = cx - int(disc_r * 0.35)
    by = cy - int(disc_r * 0.18)
    draw.ellipse(
        (bx - bite_r, by - bite_r, bx + bite_r, by + bite_r),
        fill=bg_rgb,
    )
    # Stars in the corner
    for dx, dy, s in (
        (r - 6, -r + 12, 2),
        (r - 10, r - 8, 1),
    ):
        draw.ellipse(
            (cx + dx - s, cy + dy - s, cx + dx + s, cy + dy + s),
            fill=star_color,
        )


def draw_turtle(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # Hexagonal shell
    shell_r = int(r * 0.7)
    pts: list[tuple[int, int]] = []
    for i in range(6):
        a = math.radians(60 * i + 30)
        pts.append((cx + int(math.cos(a) * shell_r), cy + int(math.sin(a) * shell_r)))
    draw.polygon(pts, fill=fg)
    # Scute lines from centre to each vertex
    for vx, vy in pts:
        draw.line((cx, cy, vx, vy), fill=detail, width=1)
    # Centre scute
    inner_r = int(shell_r * 0.35)
    inner_pts = []
    for i in range(6):
        a = math.radians(60 * i + 30)
        inner_pts.append((cx + int(math.cos(a) * inner_r), cy + int(math.sin(a) * inner_r)))
    draw.polygon(inner_pts, outline=detail)
    # Head (poking out the top-right)
    head_r = max(4, r // 6)
    head_x = cx + int(shell_r * 0.85)
    head_y = cy - int(shell_r * 0.55)
    draw.ellipse(
        (head_x - head_r, head_y - head_r, head_x + head_r, head_y + head_r),
        fill=fg,
    )
    # Two flippers (bottom)
    flip_r = max(4, r // 7)
    for fx_off in (-int(shell_r * 0.55), int(shell_r * 0.55)):
        draw.ellipse(
            (cx + fx_off - flip_r, cy + int(shell_r * 0.75) - flip_r,
             cx + fx_off + flip_r, cy + int(shell_r * 0.75) + flip_r),
            fill=fg,
        )


def draw_five_star(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
) -> None:
    """Classic 5-pointed star polygon. Used for both ``star_shop`` and the
    generic ``star`` sigil so an unthemed star still looks like a star."""
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # 10 vertices: alternating outer (point) and inner (notch) radii.
    outer = int(r * 0.85)
    inner = int(outer * 0.40)
    pts: list[tuple[int, int]] = []
    for i in range(10):
        a = math.radians(-90 + 36 * i)
        rad = outer if i % 2 == 0 else inner
        pts.append((cx + int(math.cos(a) * rad), cy + int(math.sin(a) * rad)))
    draw.polygon(pts, fill=fg, outline=hex_to_rgb(accent))


def draw_tidewave(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    highlight = hex_to_rgb(accent)
    # Three stacked wave lines
    for i, (ratio, amp) in enumerate(((0.30, 6), (0.55, 8), (0.78, 6))):
        baseline = rect[1] + int((rect[3] - rect[1]) * ratio)
        period = max(20, r)
        pts: list[tuple[int, int]] = []
        step = max(2, r // 12)
        for x in range(rect[0] + 4, rect[2] - 4, step):
            y = baseline + int(math.sin((x - rect[0]) / period * math.tau) * amp)
            pts.append((x, y))
        if len(pts) >= 2:
            draw.line(pts, fill=fg, width=3)
    # Crest highlight on the top wave
    crest_y = rect[1] + int((rect[3] - rect[1]) * 0.30)
    draw.line(
        (cx - r // 2, crest_y - 4, cx + r // 2, crest_y - 4),
        fill=highlight, width=2,
    )


def draw_cross_bones(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    eye = hex_to_rgb(accent)
    # Crossed bones first (under the skull)
    bone_len = int(r * 0.85)
    bone_w = max(3, r // 10)
    # Each bone: thin rectangle rotated 45deg, drawn as a polygon
    for angle_deg in (45, -45):
        a = math.radians(angle_deg)
        dx, dy = math.cos(a), math.sin(a)
        # End-cap circles
        end1x = cx - int(dx * bone_len)
        end1y = cy - int(dy * bone_len)
        end2x = cx + int(dx * bone_len)
        end2y = cy + int(dy * bone_len)
        cap_r = bone_w + 2
        draw.ellipse((end1x - cap_r, end1y - cap_r, end1x + cap_r, end1y + cap_r), fill=fg)
        draw.ellipse((end2x - cap_r, end2y - cap_r, end2x + cap_r, end2y + cap_r), fill=fg)
        # Bone shaft
        nx, ny = -dy, dx  # perpendicular
        pts = [
            (end1x + int(nx * bone_w), end1y + int(ny * bone_w)),
            (end2x + int(nx * bone_w), end2y + int(ny * bone_w)),
            (end2x - int(nx * bone_w), end2y - int(ny * bone_w)),
            (end1x - int(nx * bone_w), end1y - int(ny * bone_w)),
        ]
        draw.polygon(pts, fill=fg)
    # Skull on top
    skull_w = int(r * 0.70)
    skull_h = int(r * 0.65)
    draw.ellipse(
        (cx - skull_w, cy - int(skull_h * 0.95),
         cx + skull_w, cy + int(skull_h * 0.55)),
        fill=fg,
    )
    # Jaw
    jaw_w = int(skull_w * 0.55)
    jaw_h = int(skull_h * 0.30)
    draw.rectangle(
        (cx - jaw_w, cy + int(skull_h * 0.45),
         cx + jaw_w, cy + int(skull_h * 0.45) + jaw_h),
        fill=fg,
    )
    # Eye sockets
    eye_dx = int(skull_w * 0.40)
    eye_r = max(3, r // 9)
    draw.ellipse((cx - eye_dx - eye_r, cy - eye_r, cx - eye_dx + eye_r, cy + eye_r), fill=eye)
    draw.ellipse((cx + eye_dx - eye_r, cy - eye_r, cx + eye_dx + eye_r, cy + eye_r), fill=eye)
    # Nose (inverted triangle)
    nose = max(3, r // 12)
    draw.polygon(
        [
            (cx - nose, cy + int(r * 0.10)),
            (cx + nose, cy + int(r * 0.10)),
            (cx, cy + int(r * 0.10) + nose * 2),
        ],
        fill=eye,
    )


def draw_dice(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    pip = hex_to_rgb(accent)
    # Rounded square face
    side = int(r * 1.4)
    face = (cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)
    draw.rounded_rectangle(face, radius=max(4, side // 8), fill=fg)
    # Five pips (face of "5")
    pip_r = max(2, side // 12)
    quarter = side // 4
    for px, py in (
        (cx - quarter, cy - quarter),
        (cx + quarter, cy - quarter),
        (cx, cy),
        (cx - quarter, cy + quarter),
        (cx + quarter, cy + quarter),
    ):
        draw.ellipse((px - pip_r, py - pip_r, px + pip_r, py + pip_r), fill=pip)


def draw_gavel(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # Mallet head (rotated -30deg by stacking primitives)
    head_w = int(r * 0.95)
    head_h = int(r * 0.45)
    head_cx = cx - int(r * 0.18)
    head_cy = cy - int(r * 0.35)
    draw.rectangle(
        (head_cx - head_w // 2, head_cy - head_h // 2,
         head_cx + head_w // 2, head_cy + head_h // 2),
        fill=fg,
        outline=detail,
    )
    # Handle (diagonal)
    handle_w = max(3, r // 9)
    # Pixel line from head center toward bottom-right
    bx, by = cx + int(r * 0.65), cy + int(r * 0.65)
    for off in range(-handle_w, handle_w + 1):
        draw.line((head_cx + off, head_cy + off, bx + off, by + off), fill=fg, width=2)
    # Base block (where the gavel strikes)
    base_w = int(r * 0.85)
    base_h = max(4, r // 8)
    draw.rectangle(
        (cx - base_w // 2, cy + int(r * 0.75),
         cx + base_w // 2, cy + int(r * 0.75) + base_h),
        fill=detail,
    )


def draw_cards(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    s = int(r * 0.32)
    # Four suits arranged in a 2x2 grid
    positions = (
        (cx - s, cy - s, "spade"),
        (cx + s, cy - s, "heart"),
        (cx - s, cy + s, "diamond"),
        (cx + s, cy + s, "club"),
    )
    half = s - 2
    for px, py, suit in positions:
        if suit == "diamond":
            draw.polygon(
                [(px, py - half), (px + half, py), (px, py + half), (px - half, py)],
                fill=fg,
            )
        elif suit == "heart":
            draw.ellipse((px - half, py - half, px, py), fill=fg)
            draw.ellipse((px, py - half, px + half, py), fill=fg)
            draw.polygon(
                [
                    (px - half + 1, py - half // 4),
                    (px + half - 1, py - half // 4),
                    (px, py + half),
                ],
                fill=fg,
            )
        elif suit == "spade":
            draw.polygon(
                [(px, py - half), (px + half, py + half // 3),
                 (px - half, py + half // 3)],
                fill=fg,
            )
            draw.rectangle((px - 2, py + half // 3, px + 2, py + half), fill=fg)
        else:  # club
            cr = half * 2 // 3
            draw.ellipse((px - cr, py - half, px + cr, py - half + 2 * cr), fill=fg)
            draw.ellipse((px - half, py - cr // 2, px - half + 2 * cr, py + cr * 3 // 2), fill=fg)
            draw.ellipse((px + half - 2 * cr, py - cr // 2, px + half, py + cr * 3 // 2), fill=fg)
            draw.rectangle((px - 2, py + cr // 2, px + 2, py + half), fill=fg)
    # Centre dot in accent
    draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=detail)


def draw_eagle(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # Body / wings as a stylised V (eagle silhouette in flight)
    wing_span = int(r * 0.95)
    wing_drop = int(r * 0.65)
    # Left wing
    draw.polygon(
        [
            (cx, cy - int(r * 0.20)),
            (cx - wing_span, cy - int(r * 0.05)),
            (cx - int(wing_span * 0.55), cy + int(wing_drop * 0.55)),
            (cx - int(r * 0.10), cy + int(r * 0.10)),
        ],
        fill=fg,
    )
    # Right wing (mirror)
    draw.polygon(
        [
            (cx, cy - int(r * 0.20)),
            (cx + wing_span, cy - int(r * 0.05)),
            (cx + int(wing_span * 0.55), cy + int(wing_drop * 0.55)),
            (cx + int(r * 0.10), cy + int(r * 0.10)),
        ],
        fill=fg,
    )
    # Body / head
    body_w = max(4, r // 6)
    draw.ellipse(
        (cx - body_w, cy - int(r * 0.30),
         cx + body_w, cy + int(r * 0.65)),
        fill=fg,
    )
    # Head dot
    draw.ellipse(
        (cx - body_w + 1, cy - int(r * 0.45),
         cx + body_w - 1, cy - int(r * 0.18)),
        fill=detail,
    )


def draw_phoenix(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    """Legendary bird with flame plumes -- borrows the eagle silhouette
    plus flame tongues rising above the wings."""
    cx, cy, r = _center_and_radius(rect)
    detail = hex_to_rgb(accent)
    draw_eagle(draw, rect, color, accent)
    # Flame tongues above each wing tip
    flame_w = max(3, r // 10)
    for sign in (-1, 1):
        tip_x = cx + sign * int(r * 0.78)
        for layer, height in enumerate((int(r * 0.55), int(r * 0.35))):
            draw.polygon(
                [
                    (tip_x - flame_w, cy - int(r * 0.05)),
                    (tip_x + flame_w, cy - int(r * 0.05)),
                    (tip_x, cy - int(r * 0.05) - height),
                ],
                fill=detail,
            )
            flame_w = max(2, flame_w - 1)


def draw_dragon(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    """Legendary coiled-S dragon silhouette with horns and a spike crest."""
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # S-shaped body via two arcs
    body_w = max(4, r // 7)
    # Top loop of the S (upper-left to centre)
    pts_top = [
        (cx - int(r * 0.70), cy - int(r * 0.55)),
        (cx - int(r * 0.20), cy - int(r * 0.70)),
        (cx + int(r * 0.40), cy - int(r * 0.30)),
        (cx + int(r * 0.05), cy + int(r * 0.05)),
        (cx - int(r * 0.55), cy - int(r * 0.05)),
    ]
    # Bottom loop (centre to lower-right tail)
    pts_bot = [
        (cx - int(r * 0.30), cy + int(r * 0.05)),
        (cx + int(r * 0.30), cy + int(r * 0.30)),
        (cx + int(r * 0.75), cy + int(r * 0.70)),
        (cx + int(r * 0.55), cy + int(r * 0.30)),
        (cx + int(r * 0.05), cy + int(r * 0.10)),
    ]
    draw.polygon(pts_top, fill=fg)
    draw.polygon(pts_bot, fill=fg)
    # Spike crest along the top
    for off in range(-3, 4):
        sx = cx - int(r * 0.20) + off * 6
        sy = cy - int(r * 0.55)
        draw.polygon(
            [(sx - 3, sy + 4), (sx + 3, sy + 4), (sx, sy - 6)],
            fill=detail,
        )
    # Horn / eye spot
    draw.ellipse(
        (cx - int(r * 0.60) - 2, cy - int(r * 0.55) - 2,
         cx - int(r * 0.60) + 2, cy - int(r * 0.55) + 2),
        fill=detail,
    )


def draw_infinity(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Two stacked rings forming an infinity loop
    loop_r = int(r * 0.45)
    band = max(4, r // 9)
    for sign in (-1, 1):
        ox = cx + sign * loop_r
        draw.ellipse(
            (ox - loop_r, cy - loop_r, ox + loop_r, cy + loop_r),
            outline=fg, width=band,
        )
    # Sparkle in the middle
    sparkle = hex_to_rgb(accent)
    draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=sparkle)


# ── Mastery-track sigils ──────────────────────────────────────────────


def draw_anchor(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Ring at top
    ring_r = max(4, r // 5)
    ring_y = cy - int(r * 0.65)
    draw.ellipse(
        (cx - ring_r, ring_y - ring_r, cx + ring_r, ring_y + ring_r),
        outline=fg, width=max(2, r // 12),
    )
    # Vertical stem
    stem_w = max(3, r // 12)
    draw.rectangle(
        (cx - stem_w, ring_y + ring_r, cx + stem_w, cy + int(r * 0.65)),
        fill=fg,
    )
    # Crossbar
    bar_w = int(r * 0.55)
    bar_y = cy - int(r * 0.30)
    draw.rectangle(
        (cx - bar_w, bar_y - stem_w, cx + bar_w, bar_y + stem_w),
        fill=fg,
    )
    # Bottom curve (arc approximated by two short segments)
    base_y = cy + int(r * 0.65)
    arc_x = int(r * 0.55)
    draw.line((cx - arc_x, base_y, cx, base_y + max(4, r // 8)), fill=fg, width=max(3, r // 10))
    draw.line((cx + arc_x, base_y, cx, base_y + max(4, r // 8)), fill=fg, width=max(3, r // 10))


def draw_leaf(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    vein = hex_to_rgb(accent)
    # Almond / leaf shape: two arcs back-to-back via a polygon
    pts = [
        (cx, cy - int(r * 0.85)),
        (cx + int(r * 0.55), cy - int(r * 0.20)),
        (cx + int(r * 0.30), cy + int(r * 0.55)),
        (cx, cy + int(r * 0.85)),
        (cx - int(r * 0.30), cy + int(r * 0.55)),
        (cx - int(r * 0.55), cy - int(r * 0.20)),
    ]
    draw.polygon(pts, fill=fg)
    # Centre vein
    draw.line((cx, cy - int(r * 0.80), cx, cy + int(r * 0.80)), fill=vein, width=2)


def draw_sword(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    hilt = hex_to_rgb(accent)
    blade_w = max(3, r // 8)
    # Blade
    draw.polygon(
        [
            (cx - blade_w, cy - int(r * 0.85)),
            (cx + blade_w, cy - int(r * 0.85)),
            (cx + blade_w, cy + int(r * 0.30)),
            (cx, cy + int(r * 0.42)),
            (cx - blade_w, cy + int(r * 0.30)),
        ],
        fill=fg,
    )
    # Crossguard
    guard_w = int(r * 0.55)
    draw.rectangle(
        (cx - guard_w, cy + int(r * 0.30), cx + guard_w, cy + int(r * 0.42)),
        fill=hilt,
    )
    # Grip
    grip_w = max(3, r // 9)
    draw.rectangle(
        (cx - grip_w, cy + int(r * 0.42), cx + grip_w, cy + int(r * 0.80)),
        fill=hilt,
    )
    # Pommel
    pom_r = max(3, r // 10)
    draw.ellipse(
        (cx - pom_r, cy + int(r * 0.80) - pom_r, cx + pom_r, cy + int(r * 0.80) + pom_r),
        fill=hilt,
    )


def draw_skull(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    # Reuse cross_bones but without the bones
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    eye = hex_to_rgb(accent)
    skull_w = int(r * 0.78)
    skull_h = int(r * 0.72)
    draw.ellipse(
        (cx - skull_w, cy - int(skull_h * 0.95),
         cx + skull_w, cy + int(skull_h * 0.55)),
        fill=fg,
    )
    jaw_w = int(skull_w * 0.55)
    jaw_h = int(skull_h * 0.30)
    draw.rectangle(
        (cx - jaw_w, cy + int(skull_h * 0.45),
         cx + jaw_w, cy + int(skull_h * 0.45) + jaw_h),
        fill=fg,
    )
    eye_dx = int(skull_w * 0.40)
    eye_r = max(3, r // 9)
    draw.ellipse((cx - eye_dx - eye_r, cy - eye_r, cx - eye_dx + eye_r, cy + eye_r), fill=eye)
    draw.ellipse((cx + eye_dx - eye_r, cy - eye_r, cx + eye_dx + eye_r, cy + eye_r), fill=eye)


def draw_paw(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Main pad
    pad_w = int(r * 0.65)
    pad_h = int(r * 0.50)
    draw.ellipse(
        (cx - pad_w, cy - int(pad_h * 0.10),
         cx + pad_w, cy + int(pad_h * 1.30)),
        fill=fg,
    )
    # Four toe beans
    toe_r = max(4, r // 6)
    for dx, dy in (
        (-int(r * 0.50), -int(r * 0.55)),
        (-int(r * 0.18), -int(r * 0.75)),
        (int(r * 0.18), -int(r * 0.75)),
        (int(r * 0.50), -int(r * 0.55)),
    ):
        draw.ellipse(
            (cx + dx - toe_r, cy + dy - toe_r,
             cx + dx + toe_r, cy + dy + toe_r),
            fill=fg,
        )


def draw_shield(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    rim = hex_to_rgb(accent)
    # Heraldic shield outline
    pts = [
        (cx - int(r * 0.70), cy - int(r * 0.80)),
        (cx + int(r * 0.70), cy - int(r * 0.80)),
        (cx + int(r * 0.70), cy + int(r * 0.20)),
        (cx, cy + int(r * 0.85)),
        (cx - int(r * 0.70), cy + int(r * 0.20)),
    ]
    draw.polygon(pts, fill=fg, outline=rim)
    # Diagonal stripe (bend)
    band_w = max(4, r // 6)
    draw.line(
        (cx - int(r * 0.65), cy - int(r * 0.65),
         cx + int(r * 0.50), cy + int(r * 0.40)),
        fill=rim, width=band_w,
    )


def draw_lightning(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Z-bolt
    pts = [
        (cx - int(r * 0.10), cy - int(r * 0.85)),
        (cx + int(r * 0.45), cy - int(r * 0.85)),
        (cx + int(r * 0.05), cy - int(r * 0.10)),
        (cx + int(r * 0.45), cy - int(r * 0.10)),
        (cx - int(r * 0.30), cy + int(r * 0.85)),
        (cx, cy + int(r * 0.05)),
        (cx - int(r * 0.40), cy + int(r * 0.05)),
    ]
    draw.polygon(pts, fill=fg)


def draw_flame(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    inner = hex_to_rgb(accent)
    # Outer flame
    pts_outer = [
        (cx, cy - int(r * 0.90)),
        (cx + int(r * 0.50), cy - int(r * 0.25)),
        (cx + int(r * 0.60), cy + int(r * 0.50)),
        (cx + int(r * 0.10), cy + int(r * 0.85)),
        (cx - int(r * 0.55), cy + int(r * 0.55)),
        (cx - int(r * 0.55), cy - int(r * 0.10)),
    ]
    draw.polygon(pts_outer, fill=fg)
    # Inner flame
    pts_inner = [
        (cx, cy - int(r * 0.55)),
        (cx + int(r * 0.30), cy - int(r * 0.05)),
        (cx + int(r * 0.20), cy + int(r * 0.45)),
        (cx - int(r * 0.25), cy + int(r * 0.40)),
        (cx - int(r * 0.30), cy + int(r * 0.10)),
    ]
    draw.polygon(pts_inner, fill=inner)


def draw_wave(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    """Single bold wave glyph for the unthemed ``wave`` sigil."""
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    period = max(20, r)
    amp = int(r * 0.35)
    step = max(2, r // 18)
    pts = []
    for x in range(cx - r + 2, cx + r - 2, step):
        y = cy + int(math.sin((x - (cx - r)) / period * math.tau) * amp)
        pts.append((x, y))
    if len(pts) >= 2:
        draw.line(pts, fill=fg, width=max(4, r // 7))


def draw_snowflake(
    draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int,
) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    # Six radial arms
    arm = int(r * 0.78)
    w = max(2, r // 14)
    for i in range(6):
        a = math.radians(60 * i)
        x1 = cx + int(math.cos(a) * arm)
        y1 = cy + int(math.sin(a) * arm)
        draw.line((cx, cy, x1, y1), fill=fg, width=w)
        # Two short side spurs at 2/3 of the arm
        sx = cx + int(math.cos(a) * arm * 0.65)
        sy = cy + int(math.sin(a) * arm * 0.65)
        for spur_off in (-30, 30):
            a2 = a + math.radians(spur_off)
            x2 = sx + int(math.cos(a2) * arm * 0.30)
            y2 = sy + int(math.sin(a2) * arm * 0.30)
            draw.line((sx, sy, x2, y2), fill=fg, width=w)


def draw_crown(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    gem = hex_to_rgb(accent)
    # Base band
    band_w = int(r * 1.55)
    band_h = max(6, r // 4)
    by = cy + int(r * 0.30)
    draw.rectangle(
        (cx - band_w // 2, by, cx + band_w // 2, by + band_h),
        fill=fg,
    )
    # Three points + two notches
    pts = [
        (cx - band_w // 2, by),
        (cx - band_w // 2 + band_w // 6, by - int(r * 0.55)),
        (cx - band_w // 4, by - int(r * 0.20)),
        (cx, by - int(r * 0.75)),
        (cx + band_w // 4, by - int(r * 0.20)),
        (cx + band_w // 2 - band_w // 6, by - int(r * 0.55)),
        (cx + band_w // 2, by),
    ]
    draw.polygon(pts, fill=fg)
    # Three gems on the band
    gem_r = max(2, r // 12)
    for dx in (-int(r * 0.45), 0, int(r * 0.45)):
        draw.ellipse(
            (cx + dx - gem_r, by + band_h // 2 - gem_r,
             cx + dx + gem_r, by + band_h // 2 + gem_r),
            fill=gem,
        )


def draw_chart(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    """Three rising candles."""
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    body_w = max(4, r // 7)
    base_y = cy + int(r * 0.55)
    candles = [
        (-int(r * 0.50), int(r * 0.30)),
        (0, int(r * 0.65)),
        (int(r * 0.50), int(r * 0.95)),
    ]
    for dx, height in candles:
        x = cx + dx
        # Wick
        draw.line((x, base_y - height - max(4, r // 8), x, base_y), fill=fg, width=2)
        # Body
        draw.rectangle(
            (x - body_w, base_y - height, x + body_w, base_y),
            fill=fg,
        )
    # Floor
    draw.line(
        (cx - int(r * 0.85), base_y + 2, cx + int(r * 0.85), base_y + 2),
        fill=hex_to_rgb(accent), width=2,
    )


def draw_hammer(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    handle = hex_to_rgb(accent)
    # Head
    head_w = int(r * 0.85)
    head_h = int(r * 0.45)
    head_cy = cy - int(r * 0.30)
    draw.rectangle(
        (cx - head_w // 2, head_cy - head_h // 2,
         cx + head_w // 2, head_cy + head_h // 2),
        fill=fg,
    )
    # Cheek (rounded triangle on the back of the head)
    draw.polygon(
        [
            (cx - head_w // 2, head_cy - head_h // 2),
            (cx - head_w // 2 - int(r * 0.25), head_cy),
            (cx - head_w // 2, head_cy + head_h // 2),
        ],
        fill=fg,
    )
    # Handle
    handle_w = max(4, r // 8)
    draw.rectangle(
        (cx - handle_w, head_cy + head_h // 2,
         cx + handle_w, cy + int(r * 0.85)),
        fill=handle,
    )


def draw_scale(draw: ImageDraw.ImageDraw, rect: _Rect, color: int, accent: int) -> None:
    """Justice scale: vertical post, two pans."""
    cx, cy, r = _center_and_radius(rect)
    fg = hex_to_rgb(color)
    detail = hex_to_rgb(accent)
    # Post
    post_w = max(3, r // 10)
    draw.rectangle(
        (cx - post_w, cy - int(r * 0.85), cx + post_w, cy + int(r * 0.75)),
        fill=fg,
    )
    # Base
    draw.rectangle(
        (cx - int(r * 0.55), cy + int(r * 0.75),
         cx + int(r * 0.55), cy + int(r * 0.85)),
        fill=fg,
    )
    # Crossbar
    bar_y = cy - int(r * 0.70)
    draw.rectangle(
        (cx - int(r * 0.75), bar_y - post_w,
         cx + int(r * 0.75), bar_y + post_w),
        fill=fg,
    )
    # Two pans (semi-circle approximations)
    for dx in (-int(r * 0.65), int(r * 0.65)):
        pan_w = int(r * 0.30)
        pan_y = cy - int(r * 0.20)
        draw.line((cx + dx, bar_y, cx + dx, pan_y), fill=fg, width=2)
        draw.polygon(
            [
                (cx + dx - pan_w, pan_y),
                (cx + dx + pan_w, pan_y),
                (cx + dx, pan_y + int(r * 0.30)),
            ],
            fill=detail,
        )


# ── Dispatch ──────────────────────────────────────────────────────────


_SigilFn = Callable[[ImageDraw.ImageDraw, _Rect, int, int], None]

SIGILS: dict[str, _SigilFn] = {
    # Themed
    "cat":          draw_cat,
    "moon":         draw_moon,
    "turtle":       draw_turtle,
    "star_shop":    draw_five_star,
    "ocean_wave":   draw_tidewave,
    "pirate_skull": draw_cross_bones,
    "dice":         draw_dice,
    "gavel":        draw_gavel,
    # Legendary
    "phoenix":      draw_phoenix,
    "dragon":       draw_dragon,
    "infinity":     draw_infinity,
    # Mastery / general
    "star":         draw_five_star,
    "anchor":       draw_anchor,
    "leaf":         draw_leaf,
    "sword":        draw_sword,
    "skull":        draw_skull,
    "paw":          draw_paw,
    "shield":       draw_shield,
    "lightning":    draw_lightning,
    "flame":        draw_flame,
    "wave":         draw_wave,
    "snowflake":    draw_snowflake,
    "crown":        draw_crown,
    "chart":        draw_chart,
    "hammer":       draw_hammer,
    "scale":        draw_scale,
}


def has_sigil(sigil_id: str | None) -> bool:
    """True if ``sigil_id`` has a procedural drawing function."""
    return bool(sigil_id) and sigil_id in SIGILS


def draw_sigil(
    canvas,
    sigil_id: str | None,
    xy: tuple[int, int],
    *,
    diameter: int,
    color: int,
    accent: int | None = None,
    bg: int = 0x111111,
) -> bool:
    """Render a sigil at ``xy`` (top-left) within a ``diameter`` square.

    Layout:
        1. Disc backdrop in ``bg`` (slightly darker than the parent
           panel so the sigil pops off the banner).
        2. Highlight ring in ``color`` around the disc.
        3. Procedural silhouette from :data:`SIGILS` inside the disc.

    Falls back silently when ``sigil_id`` has no procedural function --
    the caller is expected to draw a glyph_token in that case (we
    don't do it here so the caller controls the glyph + font).

    Returns ``True`` when art was drawn; ``False`` when the caller
    should fall back to ``canvas.glyph_token``.
    """
    x, y = xy
    fn = SIGILS.get(sigil_id) if sigil_id else None
    if fn is None:
        return False
    accent_color = accent if accent is not None else 0xFFFFFF
    bg_rgb = hex_to_rgb(bg)
    # Backdrop disc
    rim = (x, y, x + diameter, y + diameter)
    canvas.draw.ellipse(rim, fill=bg_rgb)
    # Highlight ring (1-2 px)
    ring_w = max(2, diameter // 24)
    canvas.draw.ellipse(rim, outline=hex_to_rgb(color), width=ring_w)
    # Silhouette inside an 80% rect
    pad = max(2, diameter // 14)
    inner = (x + pad, y + pad, x + diameter - pad, y + diameter - pad)
    # Crescent moon needs the bg colour to bite a clean notch.
    if sigil_id == "moon":
        draw_moon_crescent(canvas.draw, inner, color, accent_color, bg_rgb=bg_rgb)
    else:
        fn(canvas.draw, inner, color, accent_color)
    return True
