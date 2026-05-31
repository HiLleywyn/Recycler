"""V3 polish pass: pixel-art pattern renderer for legendary banners.

Each function takes a Pillow ``ImageDraw`` and the bounding rect to
fill, plus an accent color. Patterns intentionally stay simple
geometric primitives so they read cleanly at thumbnail size in the
shop card and full size on the profile banner.

Wired in via ``draw_pattern(draw, name, rect, accent)`` -- name keys
match the ``pattern`` field in ``cosmetics_config.BANNERS``.
"""
from __future__ import annotations

import math
import random
from typing import Tuple

from PIL import ImageDraw

from core.framework.render_primitives import hex_to_rgb


_Rect = Tuple[int, int, int, int]


def _seeded_random(rect: _Rect, name: str) -> random.Random:
    """Deterministic randomness so each banner renders identically."""
    return random.Random(f"{name}-{rect[0]}-{rect[1]}-{rect[2]}-{rect[3]}")


def draw_stars(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    rng = _seeded_random(rect, "stars")
    rgb = hex_to_rgb(accent)
    n = max(20, (x1 - x0) * (y1 - y0) // 4500)
    for _ in range(n):
        cx = rng.randint(x0 + 4, x1 - 4)
        cy = rng.randint(y0 + 4, y1 - 4)
        r = rng.choice([1, 1, 1, 2, 2, 3])
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb)
        if r >= 3:
            draw.line((cx - 5, cy, cx + 5, cy), fill=rgb)
            draw.line((cx, cy - 5, cx, cy + 5), fill=rgb)


def draw_moon(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cx = x1 - int(w * 0.22)
    cy = y0 + int(h * 0.35)
    r = max(20, min(w, h) // 4)
    rgb = hex_to_rgb(accent)
    # Full moon disc
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb)
    # Bite out a smaller offset disc to create the crescent
    bite_r = int(r * 0.88)
    bx = cx - int(r * 0.35)
    by = cy - int(r * 0.10)
    draw.ellipse(
        (bx - bite_r, by - bite_r, bx + bite_r, by + bite_r),
        fill=(0, 0, 0, 0),
    )
    # A few stars in the corner so the banner doesn't feel empty
    draw_stars(draw, (x0, y0, x0 + w // 2, y0 + h // 2), accent)


def draw_sun(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cx = x0 + int(w * 0.18)
    cy = y0 + int(h * 0.42)
    r = max(20, min(w, h) // 4)
    rgb = hex_to_rgb(accent)
    # Rays
    for angle_deg in range(0, 360, 12):
        a = math.radians(angle_deg)
        rx0 = cx + int(math.cos(a) * (r + 6))
        ry0 = cy + int(math.sin(a) * (r + 6))
        rx1 = cx + int(math.cos(a) * (r + 26))
        ry1 = cy + int(math.sin(a) * (r + 26))
        draw.line((rx0, ry0, rx1, ry1), fill=rgb, width=3)
    # Disc
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb)


def draw_trees(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rng = _seeded_random(rect, "trees")
    rgb = hex_to_rgb(accent)
    trunk = (0x4a, 0x2c, 0x1a)
    baseline = y1 - 4
    count = max(5, w // 60)
    for i in range(count):
        tx = x0 + 16 + int(i * (w - 32) / max(1, count - 1))
        tx += rng.randint(-8, 8)
        th = rng.randint(int(h * 0.45), int(h * 0.78))
        tw = th // 3
        # Trunk
        draw.rectangle(
            (tx - 3, baseline - th // 5, tx + 3, baseline),
            fill=trunk,
        )
        # Triangular canopy (stacked for fir-tree feel)
        for layer in range(3):
            offset = layer * (th // 6)
            half = max(8, tw - layer * 4)
            top = baseline - th + offset
            mid = baseline - th + offset + th // 3
            draw.polygon(
                [(tx - half, mid), (tx + half, mid), (tx, top)],
                fill=rgb,
            )


def draw_waves(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rgb = hex_to_rgb(accent)
    for i, ratio in enumerate([0.55, 0.68, 0.80, 0.90]):
        amp = 8 + i * 3
        period = max(80, w // 5)
        baseline = y0 + int(h * ratio)
        step = 4
        pts = []
        for x in range(x0, x1 + step, step):
            y = baseline + int(math.sin((x - x0) / period * math.tau) * amp)
            pts.append((x, y))
        draw.line(pts, fill=rgb, width=2)


def draw_pirate_ship(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rgb = hex_to_rgb(accent)
    sea = hex_to_rgb(0x05101c)
    # Sea line
    draw.rectangle((x0, y1 - 24, x1, y1), fill=sea)
    # Hull
    cx = x0 + w // 2
    hull_top = y1 - 60
    hull_bot = y1 - 24
    hull_w = max(120, w // 3)
    draw.polygon(
        [
            (cx - hull_w // 2, hull_top),
            (cx + hull_w // 2, hull_top),
            (cx + hull_w // 2 - 18, hull_bot),
            (cx - hull_w // 2 + 18, hull_bot),
        ],
        fill=(0x33, 0x22, 0x14),
        outline=rgb,
    )
    # Three masts
    mast_top = y0 + 20
    for dx in (-hull_w // 3, 0, hull_w // 3):
        draw.line((cx + dx, hull_top, cx + dx, mast_top), fill=rgb, width=2)
        # Sail
        sail_w = hull_w // 4
        sail_h = hull_top - mast_top
        draw.rectangle(
            (cx + dx - sail_w // 2, mast_top + 4, cx + dx + sail_w // 2, hull_top - 4),
            fill=rgb,
        )
    # Skull flag on the centre mast
    fx, fy = cx, mast_top - 16
    draw.ellipse((fx - 10, fy - 10, fx + 10, fy + 10), fill=(0xee, 0xee, 0xee))


def draw_cats(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rng = _seeded_random(rect, "cats")
    rgb = hex_to_rgb(accent)
    count = max(4, w // 130)
    for i in range(count):
        cx = x0 + 40 + int(i * (w - 80) / max(1, count - 1))
        cy = y0 + h - rng.randint(40, 80)
        # Ears
        size = rng.randint(14, 22)
        draw.polygon(
            [(cx - size, cy), (cx - size + 10, cy - size),
             (cx - 2, cy - 4)],
            fill=rgb,
        )
        draw.polygon(
            [(cx + size, cy), (cx + size - 10, cy - size),
             (cx + 2, cy - 4)],
            fill=rgb,
        )
        # Head
        draw.ellipse((cx - size, cy - 4, cx + size, cy + size), fill=rgb)


def draw_cards(draw: ImageDraw.ImageDraw, rect: _Rect, accent: int) -> None:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rgb = hex_to_rgb(accent)
    suits = ["spade", "heart", "diamond", "club"]
    count = 4
    sw = w // (count + 1)
    for i, suit in enumerate(suits):
        cx = x0 + sw * (i + 1)
        cy = y0 + h // 2
        s = min(sw, h) // 3
        if suit == "diamond":
            draw.polygon(
                [(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)],
                fill=rgb,
            )
        elif suit == "heart":
            draw.ellipse((cx - s, cy - s, cx, cy), fill=rgb)
            draw.ellipse((cx, cy - s, cx + s, cy), fill=rgb)
            draw.polygon(
                [(cx - s + 2, cy - s // 4),
                 (cx + s - 2, cy - s // 4),
                 (cx, cy + s)],
                fill=rgb,
            )
        elif suit == "spade":
            draw.polygon(
                [(cx, cy - s), (cx + s, cy + s // 3), (cx - s, cy + s // 3)],
                fill=rgb,
            )
            draw.rectangle((cx - 3, cy + s // 3, cx + 3, cy + s), fill=rgb)
        else:  # club
            r = s * 2 // 3
            draw.ellipse((cx - r, cy - s, cx + r, cy - s + 2 * r), fill=rgb)
            draw.ellipse((cx - s, cy - r, cx - s + 2 * r, cy + r), fill=rgb)
            draw.ellipse((cx, cy - r, cx + 2 * r - 2 * (r - s), cy + r), fill=rgb)
            draw.rectangle((cx - 3, cy, cx + 3, cy + s), fill=rgb)


PATTERNS = {
    "stars": draw_stars,
    "moon": draw_moon,
    "sun": draw_sun,
    "trees": draw_trees,
    "waves": draw_waves,
    "pirate_ship": draw_pirate_ship,
    "cats": draw_cats,
    "cards": draw_cards,
}


def draw_pattern(
    draw: ImageDraw.ImageDraw,
    pattern: str | None,
    rect: _Rect,
    accent: int,
) -> bool:
    """Dispatch to the named pattern. Returns True if a pattern drew."""
    if not pattern:
        return False
    fn = PATTERNS.get(pattern)
    if fn is None:
        return False
    try:
        fn(draw, rect, accent)
        return True
    except Exception:
        return False
