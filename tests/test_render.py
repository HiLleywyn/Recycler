"""Smoke tests for the Pillow renderer framework.

Verifies that:
- Fonts load (DejaVu Sans bundled in assets/fonts/ resolves).
- ``RenderCanvas`` composes without raising on every primitive.
- Output is a non-empty PNG (magic header check).
- Generic chart helpers run on small + empty inputs.

No pixel diffing -- just structural smoke tests so CI catches regressions
in the font path, PIL signature changes, or unhandled empty-input cases.
"""
from __future__ import annotations

from core.framework.render import (
    RenderCanvas,
    render_bar_chart,
    render_line_chart,
)
from core.framework.render_primitives import (
    font,
    gradient_fill,
    hex_to_rgb,
    mix,
    rgba,
    to_png_bytes,
)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_font_loads_at_multiple_sizes() -> None:
    f1 = font(14, bold=False)
    f2 = font(28, bold=True)
    assert f1 is not None
    assert f2 is not None
    assert f1 is font(14, bold=False)


def test_hex_to_rgb_and_mix() -> None:
    assert hex_to_rgb(0xFF8800) == (255, 136, 0)
    mid = mix(0x000000, 0xFFFFFF, 0.5)
    assert mid == (127, 127, 127)


def test_rgba_clamps_alpha() -> None:
    assert rgba(0x00FF00, 300) == (0, 255, 0, 255)
    assert rgba(0x00FF00, -10) == (0, 255, 0, 0)


def test_canvas_smoke() -> None:
    canvas = RenderCanvas(800, 400, bg=0x2c3e50, gradient_to=0x34495E)
    canvas.title("Smoke Test", subtitle="rendering primitives")
    canvas.rounded_panel((40, 120, 760, 360), color=0x161B22)
    canvas.pill_badge((60, 140), "ACTIVE", color=0x2ecc71)
    canvas.progress_bar((60, 200, 740, 224), 0.42, color=0xf1c40f, label="42%")
    canvas.stat_block((60, 240), label="LEVEL", value="42", color=0xf1c40f)
    canvas.divider(330)
    canvas.glyph_token((60, 340), "MTA", color=0xf39c12)
    canvas.text((180, 350), "hello world", color=0xFFFFFF, size=16)
    canvas.footer("smoke")
    out = canvas.to_png_bytes()
    assert isinstance(out, bytes)
    assert out.startswith(_PNG_MAGIC)
    assert len(out) > 1000


def test_avatar_fallback_disc() -> None:
    canvas = RenderCanvas(200, 200, bg=0x2c3e50)
    canvas.avatar_circle((40, 40), size=120, avatar_bytes=None)
    out = canvas.to_png_bytes()
    assert out.startswith(_PNG_MAGIC)


def test_avatar_bad_bytes_falls_back() -> None:
    canvas = RenderCanvas(200, 200, bg=0x2c3e50)
    canvas.avatar_circle((40, 40), size=120, avatar_bytes=b"not a valid image")
    out = canvas.to_png_bytes()
    assert out.startswith(_PNG_MAGIC)


def test_gradient_fill_does_not_raise() -> None:
    canvas = RenderCanvas(100, 100, bg=0x000000)
    gradient_fill(canvas.img, 0x000000, 0xFFFFFF)
    out = canvas.to_png_bytes()
    assert out.startswith(_PNG_MAGIC)


def test_line_chart_renders() -> None:
    pts = [(i, i * i) for i in range(20)]
    out = render_line_chart("Squares", pts, subtitle="quadratic")
    assert out.startswith(_PNG_MAGIC)


def test_line_chart_empty_does_not_raise() -> None:
    out = render_line_chart("Empty", [])
    assert out.startswith(_PNG_MAGIC)


def test_bar_chart_renders() -> None:
    out = render_bar_chart(
        "Bars", [("a", 1.0), ("b", 2.5), ("c", 0.5)],
        subtitle="three bars",
    )
    assert out.startswith(_PNG_MAGIC)


def test_bar_chart_empty_does_not_raise() -> None:
    out = render_bar_chart("Empty", [])
    assert out.startswith(_PNG_MAGIC)


def test_to_png_bytes_directly() -> None:
    canvas = RenderCanvas(50, 50, bg=0x000000)
    out = to_png_bytes(canvas.img)
    assert out.startswith(_PNG_MAGIC)
