"""Low-level Pillow drawing primitives shared by every PNG renderer.

``core.framework.render`` composes these into ready-to-attach Discord files.
Anything that touches PIL directly lives here so the rest of the codebase
never imports PIL in its own modules -- mirrors the rule that no cog
imports ``discord.Embed`` directly.

Pure Python plus Pillow. No discord.py, framework, or service imports
beyond the palette in ``constants.ui``.
"""
from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)


# ── Font loading ───────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).parent.parent.parent / "assets" / "fonts"
_FONT_BOLD_PATH = _FONT_DIR / "DejaVuSans-Bold.ttf"
_FONT_REG_PATH = _FONT_DIR / "DejaVuSans.ttf"


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Cached DejaVu Sans loader.

    Falls back to Pillow's default bitmap font if the bundled TTF is
    missing (dev containers). Production has the file under
    ``assets/fonts/``.
    """
    path = _FONT_BOLD_PATH if bold else _FONT_REG_PATH
    try:
        return ImageFont.truetype(str(path), size=size)
    except Exception:
        return ImageFont.load_default()


# ── Color helpers ──────────────────────────────────────────────────────
def hex_to_rgb(color: int) -> tuple[int, int, int]:
    """Convert a 0xRRGGBB int (the project's color constants) to RGB."""
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def rgba(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    """Convert a 0xRRGGBB constant to an RGBA tuple with the given alpha."""
    r, g, b = hex_to_rgb(color)
    return (r, g, b, max(0, min(255, alpha)))


def mix(c1: int, c2: int, t: float) -> tuple[int, int, int]:
    """Linear mix between two 0xRRGGBB colors. ``t`` in [0.0, 1.0]."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return (
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


# ── Drawing primitives ────────────────────────────────────────────────
def gradient_fill(
    img: Image.Image,
    top_color: int,
    bottom_color: int,
    *,
    rect: Optional[tuple[int, int, int, int]] = None,
) -> None:
    """Vertical linear gradient between two project color constants.

    Mutates ``img`` in place. ``rect`` defaults to the full image.
    """
    if rect is None:
        x0, y0, x1, y1 = 0, 0, img.width, img.height
    else:
        x0, y0, x1, y1 = rect
    height = max(1, y1 - y0)
    draw = ImageDraw.Draw(img)
    for i in range(height):
        t = i / max(1, height - 1)
        draw.line(((x0, y0 + i), (x1 - 1, y0 + i)), fill=mix(top_color, bottom_color, t))


def text_with_outline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font_obj: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int] = (0, 0, 0),
    outline_width: int = 2,
) -> None:
    """Draw text with a solid outline so it reads on any background.

    Cheaper than Pillow's stroke kwarg on older versions, and produces
    consistent kerning across font sizes.
    """
    x, y = xy
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font_obj, fill=outline)
    draw.text((x, y), text, font=font_obj, fill=fill)


def glow(
    img: Image.Image,
    rect: tuple[int, int, int, int],
    color: int,
    *,
    radius: int = 12,
    alpha: int = 140,
) -> None:
    """Soft halo behind an element. Mutates ``img`` in place.

    Renders a blurred rounded-rect on a transparent overlay then alpha-
    composites it back onto ``img``.
    """
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(rect, radius=radius, fill=rgba(color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=radius))
    img.alpha_composite(overlay) if img.mode == "RGBA" else img.paste(
        Image.alpha_composite(img.convert("RGBA"), overlay), (0, 0)
    )


def inner_shadow(
    img: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    depth: int = 4,
    color: int = 0x000000,
    alpha: int = 80,
) -> None:
    """Soft inner shadow under the top edge of a rounded rect.

    Used to add depth to flat panels without committing to a full
    drop-shadow.
    """
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    x0, y0, x1, y1 = rect
    od.rounded_rectangle((x0, y0, x1, y0 + depth * 4), radius=6, fill=rgba(color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=depth))
    if img.mode != "RGBA":
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay), (0, 0))
    else:
        img.alpha_composite(overlay)


def avatar_mask(size: int) -> Image.Image:
    """Return a circular mask sized ``size x size`` for avatar clipping."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    return mask


def to_png_bytes(img: Image.Image) -> bytes:
    """Serialise a Pillow image to PNG bytes."""
    buf = io.BytesIO()
    save_img = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA")
    save_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = [
    "font",
    "hex_to_rgb",
    "rgba",
    "mix",
    "gradient_fill",
    "text_with_outline",
    "glow",
    "inner_shadow",
    "avatar_mask",
    "to_png_bytes",
]
