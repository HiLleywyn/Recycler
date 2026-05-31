"""services/buddy_portrait.py -- Pillow portrait renderer for buddies.

Renders a square PNG portrait of a buddy keyed off species + mood +
level. Used by ``services.buddy_battle_scene`` to compose the two
fighters in the battle scene.

The illustration is *procedural*: no external sprite assets ship with
Discoin (assets/ contains only fonts), so each buddy silhouette is
composed of simple Pillow primitives. Each species has its own
``_draw_<species>`` function so a fox actually reads as a fox (snout,
pointed ears, bushy tail with white tip) and a shrimp actually reads
as a shrimp (curved segmented body, antennae, fan tail). Adding a new
species means writing a new ``_draw_<species>`` and registering it in
``_SPECIES_DRAW``.

Public surface:

    render_buddy_portrait(row, *, mood, action=None, theme="neutral",
                          size=480) -> bytes
    render_attack_burst_portrait(row, *, frame_idx, total_frames,
                                  size=480) -> bytes

``row`` is the cc_buddies dict (services.buddy_lifecycle returns these).
``mood`` is one of the keys understood by
``buddies_config.frame_key_for_mood`` (happy/neutral/hungry/sad).
``action`` overrides mood with a battle frame
(attack/hurt/victory/down/using_item) for one render. ``theme`` paints
the gradient backdrop -- "neutral" / "battle" / a zone theme key.

The renderer NEVER writes text inside the portrait. The battle scene
flips the right-hand portrait horizontally so the two fighters face
each other; in-portrait text would invert and read backwards. Name
and level labels live outside the portrait in
``services.buddy_battle_scene._draw_player_side``.

A tiny in-process LRU cache keys on (species, mood, action, level/10,
theme, size) so the battle scene's repeated re-renders of the same
buddy don't pay the full draw cost every frame.
"""
from __future__ import annotations

import logging
import math
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFilter

from core.framework.render import RenderCanvas
from core.framework.render_primitives import hex_to_rgb, rgba, to_png_bytes

log = logging.getLogger(__name__)


_PORTRAIT_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_PORTRAIT_CACHE_MAX = 64


# Per-species color palette. Each species only stores its body, accent,
# and eye colors here -- the *shape* of the silhouette is encoded in
# the matching ``_draw_<species>`` function below, dispatched via
# ``_SPECIES_DRAW``.
_DEFAULT_SPECIES_STYLE: dict = {
    "body_color":  0xC0A476,
    "accent":      0x6E4B1E,
    "belly":       0xE6D2A8,
    "eye_color":   0x1A1F2A,
}

_SPECIES_STYLE: dict[str, dict] = {
    "zenny":     {"body_color": 0xFFDC4A, "accent": 0xD89A0E,
                  "belly":      0xFFF59D, "eye_color": 0x1A1F2A},
    "pyper":     {"body_color": 0x7CB342, "accent": 0x33691E,
                  "belly":      0xC5E1A5, "eye_color": 0xFFF59D},
    "cobble":    {"body_color": 0xA1887F, "accent": 0x5D4037,
                  "belly":      0xD7CCC8, "eye_color": 0x000000},
    "shrimp":    {"body_color": 0xFF7F7F, "accent": 0xC62828,
                  "belly":      0xFFCDD2, "eye_color": 0x1A1F2A},
    "wecco":     {"body_color": 0xE3F2FD, "accent": 0x90CAF9,
                  "belly":      0xFFFFFF, "eye_color": 0x1A1F2A},
    "spiderlenny": {"body_color": 0x4E342E, "accent": 0x1B0F0B,
                    "belly":      0xA1887F, "eye_color": 0xFFEB3B},
    "fox":       {"body_color": 0xFF8A50, "accent": 0xBF360C,
                  "belly":      0xFFE0CC, "eye_color": 0x1A1F2A},
    # Cat is intentionally a soft pastel-grey with pink belly to read
    # as cute / friendly (every other small mammal already covers the
    # warm-orange palette via fox).
    "cat":       {"body_color": 0xCFD8DC, "accent": 0x546E7A,
                  "belly":      0xFCE4EC, "eye_color": 0x4FC3F7},
    "wolf":      {"body_color": 0x90A4AE, "accent": 0x37474F,
                  "belly":      0xCFD8DC, "eye_color": 0xFFEB3B},
    "crab":      {"body_color": 0xEC407A, "accent": 0x880E4F,
                  "belly":      0xF8BBD0, "eye_color": 0x1A1F2A},
    "lobster":   {"body_color": 0xC62828, "accent": 0x7B1F1F,
                  "belly":      0xEF9A9A, "eye_color": 0x1A1F2A},
    "octopus":   {"body_color": 0x8E24AA, "accent": 0x4A148C,
                  "belly":      0xCE93D8, "eye_color": 0xFFF59D},
    "nimbus":    {"body_color": 0xECEFF1, "accent": 0x78909C,
                  "belly":      0xFFFFFF, "eye_color": 0x1A1F2A},
    "blazer":    {"body_color": 0xFF7043, "accent": 0xBF360C,
                  "belly":      0xFFCCBC, "eye_color": 0xFFF59D},
    "thornling": {"body_color": 0x2E7D32, "accent": 0x1B5E20,
                  "belly":      0xA5D6A7, "eye_color": 0xFFEB3B},
    "draclet":   {"body_color": 0x6A1B9A, "accent": 0x4A148C,
                  "belly":      0xCE93D8, "eye_color": 0xFFEB3B},
    "glitch":    {"body_color": 0x00ACC1, "accent": 0x004D40,
                  "belly":      0x80DEEA, "eye_color": 0xFFF59D},
}


# Theme backdrops: top + bottom gradient colors for the portrait disc.
_THEME_BG: dict[str, tuple[int, int]] = {
    "neutral":    (0x2c3e50, 0x1a1f2a),
    "battle":     (0x6e2e2e, 0x1a0a0a),
    "victory":    (0x4caf50, 0x1b5e20),
    "defeat":     (0x546e7a, 0x263238),
    "plains":     (0x82c97d, 0x355c2b),
    "stone":      (0xa49080, 0x3b332c),
    "tide":       (0x4dd0e1, 0x0e3a4a),
    "tournament": (0xf1c40f, 0x2c1f00),
    "side":       (0xa987d8, 0x1d1633),
}


# Mood-to-pose tweak: small per-mood offsets to the eyes / mouth so
# the same silhouette reads as happy/sad/hungry. Keys mirror the
# buddies_config frame keys.
_MOOD_TWEAK: dict[str, dict] = {
    "happy":      {"eye_offset_y": -2, "mouth": "smile"},
    "neutral":    {"eye_offset_y":  0, "mouth": "flat"},
    "hungry":     {"eye_offset_y":  1, "mouth": "drool"},
    "sad":        {"eye_offset_y":  3, "mouth": "frown"},
    "eating":     {"eye_offset_y":  0, "mouth": "open"},
    "petted":     {"eye_offset_y": -3, "mouth": "smile"},
    "talking":    {"eye_offset_y":  0, "mouth": "open"},
    # Battle frames (Buddy Battles expansion)
    "attack":     {"eye_offset_y": -2, "mouth": "snarl"},
    "hurt":       {"eye_offset_y":  4, "mouth": "ouch"},
    "victory":    {"eye_offset_y": -3, "mouth": "smile"},
    "down":       {"eye_offset_y":  6, "mouth": "x"},
    "using_item": {"eye_offset_y":  0, "mouth": "open"},
}


def render_buddy_portrait(
    row: dict,
    *,
    mood: str = "neutral",
    action: str | None = None,
    theme: str = "neutral",
    size: int = 480,
) -> bytes:
    """Render the buddy portrait PNG.

    Cached. Returns raw PNG bytes ready for ``discord.File(BytesIO(...),
    'buddy_portrait.png')``. The PNG contains NO text -- it's safe to
    flip horizontally without ending up with mirrored captions.

    If the row carries a ``boss_zone_id``, a per-boss overlay (crown,
    helm, antlers, etc.) is painted on top of the species silhouette so
    captured bosses read as visually unique (a tamed Meadow King is NOT
    just a regular wolf in your roster).
    """
    species = str((row or {}).get("species") or "default").strip().lower()
    level = max(1, int((row or {}).get("level") or 1))
    pose = str(action or mood or "neutral").strip().lower()
    boss_zid = str((row or {}).get("boss_zone_id") or "").strip()
    cache_key = (species, pose, theme, level // 10, int(size), boss_zid)
    cached = _PORTRAIT_CACHE.get(cache_key)
    if cached is not None:
        _PORTRAIT_CACHE.move_to_end(cache_key)
        return cached

    style = _SPECIES_STYLE.get(species, _DEFAULT_SPECIES_STYLE)
    # Boss variants tint the accent + ring colour to make the overlay
    # read against the base portrait.
    if boss_zid:
        try:
            from configs.buddies_config import boss_variant as _bv
            bv = _bv(boss_zid)
            tint = int(bv.get("accent_tint") or 0)
            if tint:
                style = dict(style)
                style["accent"] = tint
        except Exception:
            log.debug("boss variant tint lookup failed", exc_info=True)
    bg_top, bg_bot = _THEME_BG.get(theme, _THEME_BG["neutral"])

    canvas = RenderCanvas(size, size, bg=bg_bot, gradient_to=bg_top)

    # Outer ring -- accent-tinted halo behind the silhouette.
    ring_pad = int(size * 0.04)
    canvas.draw.ellipse(
        (ring_pad, ring_pad, size - ring_pad, size - ring_pad),
        fill=rgba(bg_bot, 220),
        outline=hex_to_rgb(style["accent"]),
        width=4,
    )

    # Hurt / down poses shake the whole silhouette downward.
    pose_shift_y = 0
    if pose == "hurt":
        pose_shift_y = int(size * 0.02)
    elif pose == "down":
        pose_shift_y = int(size * 0.06)

    fn = _SPECIES_DRAW.get(species, _draw_default)
    fn(canvas, style, pose, size, pose_shift_y)

    # Boss overlay (crown / helm / antlers / etc.) sits ABOVE the
    # species silhouette so the player can tell at a glance which boss
    # they tamed.
    if boss_zid:
        _draw_boss_overlay(canvas, boss_zid, size, pose_shift_y)

    # Pose-specific overlay (motion lines, star burst, impact stars).
    _draw_action_overlay(canvas, pose, size)

    png = canvas.to_png_bytes()
    _PORTRAIT_CACHE[cache_key] = png
    if len(_PORTRAIT_CACHE) > _PORTRAIT_CACHE_MAX:
        _PORTRAIT_CACHE.popitem(last=False)
    return png


def render_attack_burst_portrait(
    row: dict,
    *,
    frame_idx: int,
    total_frames: int = 6,
    size: int = 480,
) -> bytes:
    """One frame of a 6-frame attack burst.

    Frames: 0=prepare, 1=strike-arc, 2=impact-flash, 3=recoil,
    4=settle, 5=final. The portrait pose flips and a swoosh / flash
    overlay is added per frame.
    """
    frame_idx = max(0, min(int(total_frames) - 1, int(frame_idx)))
    poses = ["attack", "attack", "victory", "hurt", "neutral", "neutral"]
    pose = poses[frame_idx] if frame_idx < len(poses) else "neutral"
    bytes_ = render_buddy_portrait(row, mood=pose, theme="battle", size=size)
    return _apply_burst_overlay(bytes_, frame_idx, total_frames, size)


# ── Shared drawing helpers ────────────────────────────────────────────

def _draw_face_at(
    canvas: RenderCanvas,
    style: dict,
    pose: str,
    cx: int,
    cy: int,
    *,
    eye_dx: int,
    eye_r: int,
    mouth_dy: int,
    size: int,
    mouth_override: str | None = None,
) -> None:
    """Draw eyes + mouth centered at (cx, cy).

    ``mouth_override`` lets a species (e.g. cat with a ``:3`` mouth)
    pin a specific mouth shape regardless of mood. Battle-only poses
    (down / hurt) still win since those are gameplay signals; idle
    moods route through the override.
    """
    tweak = _MOOD_TWEAK.get(pose, _MOOD_TWEAK["neutral"])
    eye_color = hex_to_rgb(style["eye_color"])
    eye_y = cy + int(tweak["eye_offset_y"])

    if pose == "down":
        for sgn in (-1, 1):
            ex = cx + sgn * eye_dx
            canvas.draw.line(((ex - eye_r, eye_y - eye_r),
                              (ex + eye_r, eye_y + eye_r)),
                             fill=eye_color, width=3)
            canvas.draw.line(((ex - eye_r, eye_y + eye_r),
                              (ex + eye_r, eye_y - eye_r)),
                             fill=eye_color, width=3)
    elif pose == "hurt":
        for sgn in (-1, 1):
            ex = cx + sgn * eye_dx
            canvas.draw.line(((ex - eye_r, eye_y),
                              (ex + eye_r, eye_y)),
                             fill=eye_color, width=3)
    elif pose == "attack":
        for sgn in (-1, 1):
            ex = cx + sgn * eye_dx
            canvas.draw.polygon(
                [
                    (ex - eye_r - 2, eye_y),
                    (ex + eye_r + 2, eye_y - eye_r),
                    (ex + eye_r + 2, eye_y + eye_r // 2),
                    (ex - eye_r - 2, eye_y + eye_r),
                ],
                fill=eye_color,
            )
    else:
        for sgn in (-1, 1):
            ex = cx + sgn * eye_dx
            canvas.draw.ellipse(
                (ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r),
                fill=eye_color,
            )
            canvas.draw.ellipse(
                (ex - 2, eye_y - eye_r + 1,
                 ex + 2, eye_y - eye_r + 5),
                fill=hex_to_rgb(0xFFFFFF),
            )

    mouth = str(tweak.get("mouth") or "flat")
    # Species-level mouth override (e.g. cat ":3") wins for idle moods.
    # Battle poses (down / hurt / attack) keep their gameplay-driven
    # mouth so KO + recoil still read correctly.
    if mouth_override and pose not in ("down", "hurt", "attack"):
        mouth = str(mouth_override)
    mx, my = cx, eye_y + mouth_dy
    if mouth == "cat3":
        # Cat ":3" mouth -- two small arcs meeting under the nose,
        # forming a soft ω / w shape. Iconic cute-cat look.
        hw = max(6, int(size * 0.045))
        ht = max(4, int(size * 0.030))
        # Left "smile" half (east -> south -> west arc opens upward).
        canvas.draw.arc(
            (mx - 2 * hw, my - ht, mx, my + ht),
            start=0, end=180, fill=eye_color, width=3,
        )
        # Right "smile" half -- mirrored on the other side of centre.
        canvas.draw.arc(
            (mx, my - ht, mx + 2 * hw, my + ht),
            start=0, end=180, fill=eye_color, width=3,
        )
        return
    if mouth == "smile":
        canvas.draw.arc((mx - 18, my - 8, mx + 18, my + 16),
                        start=0, end=180, fill=eye_color, width=3)
    elif mouth == "frown":
        canvas.draw.arc((mx - 18, my, mx + 18, my + 24),
                        start=180, end=360, fill=eye_color, width=3)
    elif mouth == "flat":
        canvas.draw.line(((mx - 12, my + 4), (mx + 12, my + 4)),
                         fill=eye_color, width=3)
    elif mouth == "drool":
        canvas.draw.line(((mx - 12, my + 4), (mx + 6, my + 4)),
                         fill=eye_color, width=3)
        canvas.draw.ellipse((mx + 6, my + 6, mx + 14, my + 22),
                            fill=hex_to_rgb(0x4FC3F7))
    elif mouth == "open":
        canvas.draw.ellipse((mx - 12, my, mx + 12, my + 16),
                            fill=eye_color)
    elif mouth == "snarl":
        canvas.draw.polygon(
            [
                (mx - 16, my), (mx + 16, my), (mx + 12, my + 12),
                (mx + 4, my + 6), (mx - 4, my + 6), (mx - 12, my + 12),
            ],
            fill=eye_color,
        )
    elif mouth == "ouch":
        canvas.draw.line(((mx - 8, my + 6), (mx + 8, my + 6)),
                         fill=eye_color, width=3)
        canvas.draw.line(((mx - 4, my + 10), (mx + 4, my + 2)),
                         fill=eye_color, width=3)
    elif mouth == "x":
        canvas.draw.line(((mx - 8, my - 2), (mx + 8, my + 12)),
                         fill=eye_color, width=3)
        canvas.draw.line(((mx - 8, my + 12), (mx + 8, my - 2)),
                         fill=eye_color, width=3)


def _belly_color(style: dict) -> tuple[int, int, int]:
    return hex_to_rgb(style.get("belly") or 0xFFFFFF)


# ── Per-species silhouettes ───────────────────────────────────────────
#
# Each function paints ONE buddy silhouette centered roughly in the
# bottom 2/3 of the canvas. They receive ``pose_shift_y`` so hurt /
# down poses slump downward without re-deriving every coordinate.
#
# Coordinate convention: cx = size // 2. All other measurements are
# fractions of ``size`` so the same draw works at 320 (battle) and
# 480 (standalone) without distortion.

def _draw_fox(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Front-facing fox: head centred, snout pointing DOWN (not sideways).

    Old version had a 3/4 head with the snout sticking out to the right
    and the nose dangling off the side -- the "nose on the side of the
    head" anatomy bug the players called out. This redraw plants the
    head centre on the vertical axis, with the snout/nose triangle
    centred below the eyes and the bushy tail visible curling up
    behind the right shoulder.
    """
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2

    # Bushy tail curling up behind the right shoulder.
    tail_pts = [
        (cx + int(size * 0.18), int(size * 0.74) + dy),
        (cx + int(size * 0.36), int(size * 0.60) + dy),
        (cx + int(size * 0.44), int(size * 0.44) + dy),
        (cx + int(size * 0.32), int(size * 0.38) + dy),
        (cx + int(size * 0.22), int(size * 0.50) + dy),
        (cx + int(size * 0.14), int(size * 0.66) + dy),
    ]
    canvas.draw.polygon(tail_pts, fill=body, outline=accent)
    # Cream tail tip.
    canvas.draw.ellipse(
        (cx + int(size * 0.36), int(size * 0.38) + dy,
         cx + int(size * 0.48), int(size * 0.50) + dy),
        fill=hex_to_rgb(0xFFFFFF), outline=accent, width=2,
    )

    # Body -- rounded torso. Top intentionally overlaps the head
    # ellipse so the silhouette reads as one continuous critter
    # (the head ellipse paints AFTER the body, hiding the overlap).
    # Without this overlap the head appeared to float above the body.
    body_left   = cx - int(size * 0.24)
    body_right  = cx + int(size * 0.24)
    body_top    = int(size * 0.46) + dy
    body_bottom = int(size * 0.88) + dy
    canvas.draw.ellipse(
        (body_left, body_top, body_right, body_bottom),
        fill=body, outline=accent, width=3,
    )
    # Cream belly along the centre.
    canvas.draw.ellipse(
        (cx - int(size * 0.14), body_top + int(size * 0.10),
         cx + int(size * 0.14), body_bottom - 6),
        fill=belly,
    )
    # Four small paws at the base.
    for sgn_x in (-1, 1):
        for off in (0.05, 0.15):
            px = cx + sgn_x * int(size * off)
            canvas.draw.ellipse(
                (px - int(size * 0.035), body_bottom - 4,
                 px + int(size * 0.035), body_bottom + int(size * 0.04)),
                fill=accent,
            )

    # Head -- round, sitting on the body, centred on cx.
    head_cx = cx
    head_cy = int(size * 0.34) + dy
    head_r  = int(size * 0.17)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )
    # Cream cheek/jaw markings on either side.
    for sgn in (-1, 1):
        canvas.draw.ellipse(
            (head_cx + sgn * int(size * 0.06) - int(size * 0.06),
             head_cy + int(size * 0.02),
             head_cx + sgn * int(size * 0.06) + int(size * 0.06),
             head_cy + int(size * 0.13)),
            fill=belly,
        )

    # Tall triangular ears with cream inner -- attached to top of head.
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.11)
        base_y = head_cy - int(size * 0.10)
        tip_y  = head_cy - int(size * 0.28)
        canvas.draw.polygon(
            [
                (ex - int(size * 0.045), base_y),
                (ex + int(size * 0.045), base_y),
                (ex, tip_y),
            ],
            fill=body, outline=accent,
        )
        canvas.draw.polygon(
            [
                (ex - int(size * 0.02), base_y),
                (ex + int(size * 0.02), base_y),
                (ex, tip_y + int(size * 0.05)),
            ],
            fill=belly,
        )

    # Small nose dot at the bottom-centre of the face. Removed the
    # cream-coloured snout polygon -- it read as a translucent
    # trapezoid layered over the mouth, which looked wrong.
    nose_y = head_cy + int(size * 0.09)
    nose_rx = max(3, int(size * 0.028))
    nose_ry = max(2, int(size * 0.020))
    canvas.draw.ellipse(
        (head_cx - nose_rx, nose_y - nose_ry,
         head_cx + nose_rx, nose_y + nose_ry),
        fill=accent,
    )

    # Eyes + mouth -- ALWAYS centred on the head's vertical axis.
    # Mouth sits below the nose; eyes above it.
    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.03),
                  eye_dx=int(size * 0.08), eye_r=5,
                  mouth_dy=int(size * 0.13), size=size)


def _draw_cat(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Front-facing cat -- big head, tall pointed ears, whiskers, curl tail.

    Cute-as-fuck silhouette: oversized round head dominates the frame
    (small body underneath cat-loaf style), pointed inner-pink ears
    perched on top, three symmetric whiskers per side, a pink button
    nose, and a tail curling behind. Eyes use the species' light blue
    eye_color so the face really pops.
    """
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2

    # Tail curling up behind the right shoulder. Drawn first so the
    # body covers its base.
    tail_pts = [
        (cx + int(size * 0.18), int(size * 0.78) + dy),
        (cx + int(size * 0.34), int(size * 0.62) + dy),
        (cx + int(size * 0.40), int(size * 0.46) + dy),
        (cx + int(size * 0.32), int(size * 0.36) + dy),
        (cx + int(size * 0.22), int(size * 0.40) + dy),
        (cx + int(size * 0.20), int(size * 0.56) + dy),
        (cx + int(size * 0.14), int(size * 0.72) + dy),
    ]
    canvas.draw.polygon(tail_pts, fill=body, outline=accent)
    # Light tail-tip stripe so it reads as a tabby flick.
    canvas.draw.ellipse(
        (cx + int(size * 0.30), int(size * 0.36) + dy,
         cx + int(size * 0.42), int(size * 0.46) + dy),
        fill=tuple(min(255, c + 30) for c in body[:3]),
        outline=accent, width=2,
    )

    # Cat-loaf body -- compact rounded square that OVERLAPS the head
    # so the silhouette reads as one continuous critter, not a
    # floating head sitting above a separate body. Drawn before the
    # head so the head ellipse paints on top of the body's top edge.
    body_left   = cx - int(size * 0.24)
    body_right  = cx + int(size * 0.24)
    body_top    = int(size * 0.48) + dy
    body_bottom = int(size * 0.88) + dy
    canvas.draw.rounded_rectangle(
        (body_left, body_top, body_right, body_bottom),
        radius=int(size * 0.16),
        fill=body, outline=accent, width=3,
    )
    # Pink belly oval.
    canvas.draw.ellipse(
        (cx - int(size * 0.13), body_top + int(size * 0.04),
         cx + int(size * 0.13), body_bottom - 4),
        fill=belly,
    )
    # Two front paws peeking out the bottom.
    for sgn_x in (-1, 1):
        px = cx + sgn_x * int(size * 0.10)
        canvas.draw.ellipse(
            (px - int(size * 0.045), body_bottom - 6,
             px + int(size * 0.045), body_bottom + int(size * 0.045)),
            fill=body, outline=accent, width=2,
        )

    # Big round head -- the star of the show.
    head_cx = cx
    head_cy = int(size * 0.34) + dy
    head_r  = int(size * 0.21)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )
    # Soft cheek tufts.
    for sgn in (-1, 1):
        canvas.draw.ellipse(
            (head_cx + sgn * int(size * 0.12) - int(size * 0.05),
             head_cy + int(size * 0.06),
             head_cx + sgn * int(size * 0.12) + int(size * 0.05),
             head_cy + int(size * 0.16)),
            fill=tuple(min(255, c + 18) for c in body[:3]),
        )

    # Tall pointed ears with pink inner triangle.
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.13)
        base_l = ex - int(size * 0.05)
        base_r = ex + int(size * 0.05)
        base_y = head_cy - int(size * 0.13)
        tip_y  = head_cy - int(size * 0.32)
        canvas.draw.polygon(
            [(base_l, base_y), (base_r, base_y), (ex, tip_y)],
            fill=body, outline=accent,
        )
        # Inner pink fill.
        canvas.draw.polygon(
            [
                (base_l + int(size * 0.012), base_y - 2),
                (base_r - int(size * 0.012), base_y - 2),
                (ex, tip_y + int(size * 0.05)),
            ],
            fill=hex_to_rgb(0xF8BBD0),
        )

    # Pink heart-shaped nose, centred just below the eye line.
    nose_y = head_cy + int(size * 0.05)
    canvas.draw.polygon(
        [
            (head_cx - int(size * 0.035), nose_y - 2),
            (head_cx + int(size * 0.035), nose_y - 2),
            (head_cx, nose_y + int(size * 0.04)),
        ],
        fill=hex_to_rgb(0xE91E63),
        outline=accent,
    )

    # Three symmetric whiskers per side, fanning from the cheeks.
    for sgn in (-1, 1):
        cheek_x = head_cx + sgn * int(size * 0.07)
        cheek_y = nose_y + int(size * 0.02)
        for dy_off in (-int(size * 0.025), 0, int(size * 0.025)):
            tip_x = head_cx + sgn * int(size * 0.24)
            tip_y = cheek_y + dy_off
            canvas.draw.line(
                ((cheek_x, cheek_y), (tip_x, tip_y)),
                fill=accent, width=2,
            )

    # Eyes + ":3" mouth -- ALWAYS centred on the head's vertical axis.
    # Cat-specific mouth override (two small upward arcs meeting under
    # the nose) gives the iconic cute-cat look. Battle poses (down,
    # hurt, attack) keep their gameplay-driven mouths.
    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.03),
                  eye_dx=int(size * 0.09), eye_r=6,
                  mouth_dy=int(size * 0.14), size=size,
                  mouth_override="cat3")


def _draw_wolf(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Front-facing wolf: bigger, broader, with fangs framing the muzzle."""
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2

    # Bushy tail curling up behind the right shoulder.
    tail_pts = [
        (cx + int(size * 0.20), int(size * 0.72) + dy),
        (cx + int(size * 0.40), int(size * 0.56) + dy),
        (cx + int(size * 0.46), int(size * 0.40) + dy),
        (cx + int(size * 0.34), int(size * 0.38) + dy),
        (cx + int(size * 0.24), int(size * 0.50) + dy),
        (cx + int(size * 0.16), int(size * 0.66) + dy),
    ]
    canvas.draw.polygon(tail_pts, fill=body, outline=accent)

    # Body -- broad, low stance.
    body_left   = cx - int(size * 0.28)
    body_right  = cx + int(size * 0.28)
    body_top    = int(size * 0.50) + dy
    body_bottom = int(size * 0.86) + dy
    canvas.draw.ellipse(
        (body_left, body_top, body_right, body_bottom),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (cx - int(size * 0.16), body_top + int(size * 0.10),
         cx + int(size * 0.16), body_bottom - 6),
        fill=belly,
    )
    # Four paws at the base.
    for sgn_x in (-1, 1):
        for off in (0.06, 0.18):
            px = cx + sgn_x * int(size * off)
            canvas.draw.ellipse(
                (px - int(size * 0.04), body_bottom - 4,
                 px + int(size * 0.04), body_bottom + int(size * 0.04)),
                fill=accent,
            )

    # Head -- broad, centred.
    head_cx = cx
    head_cy = int(size * 0.32) + dy
    head_r  = int(size * 0.19)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )

    # Upright triangular ears -- symmetric on the head top.
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.12)
        base_y = head_cy - int(size * 0.12)
        tip_y  = head_cy - int(size * 0.30)
        canvas.draw.polygon(
            [
                (ex - int(size * 0.04), base_y),
                (ex + int(size * 0.04), base_y),
                (ex, tip_y),
            ],
            fill=body, outline=accent,
        )
        canvas.draw.polygon(
            [
                (ex - int(size * 0.02), base_y),
                (ex + int(size * 0.02), base_y),
                (ex, tip_y + int(size * 0.05)),
            ],
            fill=accent,
        )

    # Nose at the bottom-centre of the face. The cream muzzle trapezoid
    # was dropped -- it looked like a translucent shape pasted over the
    # mouth. Two small fangs frame the mouth instead, which is enough
    # to keep the wolf reading as predator-toothed.
    nose_y = head_cy + int(size * 0.07)
    canvas.draw.ellipse(
        (head_cx - int(size * 0.03), nose_y - int(size * 0.025),
         head_cx + int(size * 0.03), nose_y + int(size * 0.02)),
        fill=accent,
    )
    fang_y = head_cy + int(size * 0.15)
    for sgn in (-1, 1):
        fx = head_cx + sgn * int(size * 0.05)
        canvas.draw.polygon(
            [
                (fx - 3, fang_y - 2),
                (fx + 3, fang_y - 2),
                (fx, fang_y + int(size * 0.04)),
            ],
            fill=hex_to_rgb(0xFFFFFF),
            outline=hex_to_rgb(0x546E7A),
        )

    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.03),
                  eye_dx=int(size * 0.09), eye_r=5,
                  mouth_dy=int(size * 0.13), size=size)


def _draw_shrimp(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Curved C-shaped segmented body, head at top-right, tail bottom-left.
    arc_cx, arc_cy = cx + int(size * 0.05), int(size * 0.55) + dy
    seg_r = int(size * 0.26)
    # Outer body arc as several overlapping ellipses (creates segments).
    for i, (ang_deg, w_scale) in enumerate([
        (210, 1.00), (240, 0.95), (270, 0.90), (300, 0.85), (330, 0.78),
    ]):
        ang = math.radians(ang_deg)
        sx = arc_cx + int(seg_r * math.cos(ang))
        sy = arc_cy + int(seg_r * math.sin(ang))
        sw = int(size * 0.08 * w_scale)
        canvas.draw.ellipse(
            (sx - sw, sy - sw, sx + sw, sy + sw),
            fill=body, outline=accent, width=2,
        )
    # Fan tail at the bottom-left end of the curve.
    tail_x, tail_y = arc_cx + int(seg_r * math.cos(math.radians(200))), \
                     arc_cy + int(seg_r * math.sin(math.radians(200)))
    for ang_deg in (170, 190, 210, 230):
        ang = math.radians(ang_deg)
        tip = (tail_x + int(size * 0.14 * math.cos(ang)),
               tail_y + int(size * 0.14 * math.sin(ang)))
        canvas.draw.polygon(
            [
                (tail_x, tail_y - 6),
                (tail_x, tail_y + 6),
                tip,
            ],
            fill=body, outline=accent,
        )
    # Head -- a larger blob at the top-right of the arc.
    head_cx = arc_cx + int(seg_r * math.cos(math.radians(335)))
    head_cy = arc_cy + int(seg_r * math.sin(math.radians(335)))
    head_r = int(size * 0.13)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )
    # Two long antennae trailing up.
    for sgn in (-1, 1):
        canvas.draw.line(
            (
                (head_cx + sgn * int(size * 0.04), head_cy - int(size * 0.04)),
                (head_cx + sgn * int(size * 0.12), head_cy - int(size * 0.32)),
            ),
            fill=accent, width=3,
        )
        canvas.draw.ellipse(
            (head_cx + sgn * int(size * 0.12) - 4,
             head_cy - int(size * 0.32) - 4,
             head_cx + sgn * int(size * 0.12) + 4,
             head_cy - int(size * 0.32) + 4),
            fill=accent,
        )
    # Tiny legs along the underside of the curve.
    for i, ang_deg in enumerate((250, 275, 300)):
        ang = math.radians(ang_deg)
        lx = arc_cx + int((seg_r + size * 0.04) * math.cos(ang))
        ly = arc_cy + int((seg_r + size * 0.04) * math.sin(ang))
        canvas.draw.line(
            ((lx, ly), (lx, ly + int(size * 0.06))),
            fill=accent, width=2,
        )
    _draw_face_at(canvas, style, pose, head_cx, head_cy,
                  eye_dx=int(size * 0.04), eye_r=4,
                  mouth_dy=int(size * 0.06), size=size)


def _draw_lobster(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Segmented vertical tail running down the canvas.
    for i, h in enumerate([0.50, 0.58, 0.66, 0.74]):
        seg_y = int(size * h) + dy
        seg_w = int(size * (0.20 - i * 0.015))
        canvas.draw.ellipse(
            (cx - seg_w, seg_y, cx + seg_w, seg_y + int(size * 0.10)),
            fill=body, outline=accent, width=2,
        )
    # Fan tail at the bottom.
    fan_y = int(size * 0.84) + dy
    for ang_deg in (200, 230, 270, 310, 340):
        ang = math.radians(ang_deg)
        tip = (cx + int(size * 0.16 * math.cos(ang)),
               fan_y + int(size * 0.10 * math.sin(ang)) + int(size * 0.08))
        canvas.draw.polygon(
            [(cx - 8, fan_y), (cx + 8, fan_y), tip],
            fill=body, outline=accent,
        )
    # Head/body capsule at top.
    head_cx, head_cy = cx, int(size * 0.38) + dy
    head_w, head_h = int(size * 0.18), int(size * 0.14)
    canvas.draw.ellipse(
        (head_cx - head_w, head_cy - head_h,
         head_cx + head_w, head_cy + head_h),
        fill=body, outline=accent, width=3,
    )
    # Two long front claws.
    for sgn in (-1, 1):
        claw_base = (head_cx + sgn * int(size * 0.10),
                     head_cy + int(size * 0.04))
        claw_end = (head_cx + sgn * int(size * 0.32),
                    head_cy + int(size * 0.06))
        canvas.draw.line((claw_base, claw_end), fill=body, width=int(size * 0.04))
        canvas.draw.ellipse(
            (claw_end[0] - int(size * 0.06), claw_end[1] - int(size * 0.06),
             claw_end[0] + int(size * 0.06), claw_end[1] + int(size * 0.06)),
            fill=body, outline=accent, width=2,
        )
        # Pincer slit
        canvas.draw.line(
            ((claw_end[0] - int(size * 0.04), claw_end[1]),
             (claw_end[0] + int(size * 0.04), claw_end[1])),
            fill=accent, width=2,
        )
    # Antennae.
    for sgn in (-1, 1):
        canvas.draw.line(
            ((head_cx + sgn * int(size * 0.06), head_cy - int(size * 0.10)),
             (head_cx + sgn * int(size * 0.10), head_cy - int(size * 0.30))),
            fill=accent, width=2,
        )
    _draw_face_at(canvas, style, pose, head_cx, head_cy,
                  eye_dx=int(size * 0.06), eye_r=5,
                  mouth_dy=int(size * 0.07), size=size)


def _draw_crab(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Wide, flat body.
    body_cx, body_cy = cx, int(size * 0.58) + dy
    body_w, body_h = int(size * 0.30), int(size * 0.16)
    canvas.draw.ellipse(
        (body_cx - body_w, body_cy - body_h,
         body_cx + body_w, body_cy + body_h),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (body_cx - int(body_w * 0.7), body_cy - int(body_h * 0.4),
         body_cx + int(body_w * 0.7), body_cy + int(body_h * 0.9)),
        fill=belly,
    )
    # Eye stalks rising from the top of the shell.
    for sgn in (-1, 1):
        stalk_base_x = body_cx + sgn * int(size * 0.08)
        stalk_top_y = body_cy - int(size * 0.24)
        canvas.draw.line(
            ((stalk_base_x, body_cy - body_h),
             (stalk_base_x, stalk_top_y)),
            fill=body, width=int(size * 0.025),
        )
        canvas.draw.ellipse(
            (stalk_base_x - int(size * 0.04), stalk_top_y - int(size * 0.04),
             stalk_base_x + int(size * 0.04), stalk_top_y + int(size * 0.04)),
            fill=hex_to_rgb(0xFFFFFF), outline=accent, width=2,
        )
        canvas.draw.ellipse(
            (stalk_base_x - 3, stalk_top_y - 3,
             stalk_base_x + 3, stalk_top_y + 3),
            fill=hex_to_rgb(style["eye_color"]),
        )
    # Two big claws on either side.
    for sgn in (-1, 1):
        claw_x = body_cx + sgn * int(size * 0.36)
        claw_y = body_cy
        canvas.draw.line(
            ((body_cx + sgn * body_w, body_cy),
             (claw_x, claw_y)),
            fill=body, width=int(size * 0.04),
        )
        # Pincer -- two triangles meeting.
        canvas.draw.polygon(
            [
                (claw_x - sgn * int(size * 0.02), claw_y - int(size * 0.05)),
                (claw_x + sgn * int(size * 0.08), claw_y - int(size * 0.02)),
                (claw_x + sgn * int(size * 0.05), claw_y + int(size * 0.04)),
            ],
            fill=body, outline=accent,
        )
        canvas.draw.polygon(
            [
                (claw_x - sgn * int(size * 0.02), claw_y + int(size * 0.05)),
                (claw_x + sgn * int(size * 0.08), claw_y + int(size * 0.02)),
                (claw_x + sgn * int(size * 0.05), claw_y - int(size * 0.02)),
            ],
            fill=body, outline=accent,
        )
    # Walking legs underneath.
    for sgn in (-1, 1):
        for i in range(3):
            leg_x = body_cx + sgn * (int(size * 0.10) + i * int(size * 0.07))
            canvas.draw.line(
                ((leg_x, body_cy + body_h - 2),
                 (leg_x + sgn * int(size * 0.04),
                  body_cy + body_h + int(size * 0.08))),
                fill=accent, width=3,
            )
    # Mouth only -- eyes are on the stalks, drawn above.
    _draw_face_at(canvas, style, pose, body_cx, body_cy + int(size * 0.04),
                  eye_dx=0, eye_r=0,
                  mouth_dy=0, size=size)


def _draw_octopus(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    head_cx, head_cy = cx, int(size * 0.40) + dy
    head_r = int(size * 0.22)
    # Bulbous head -- taller than wide.
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - int(head_r * 1.05),
         head_cx + head_r, head_cy + int(head_r * 0.95)),
        fill=body, outline=accent, width=3,
    )
    # Eight tentacles fanning out from below the head.
    base_y = head_cy + int(head_r * 0.7)
    for i in range(8):
        t = (i - 3.5) / 3.5  # -1..1
        start_x = head_cx + int(head_r * 0.85 * t)
        end_x = head_cx + int(size * 0.36 * t)
        mid_x = (start_x + end_x) // 2
        mid_y = base_y + int(size * 0.20)
        end_y = base_y + int(size * 0.34)
        # Quadratic-ish curve via two line segments.
        canvas.draw.line(
            ((start_x, base_y), (mid_x, mid_y)),
            fill=body, width=int(size * 0.04),
        )
        canvas.draw.line(
            ((mid_x, mid_y), (end_x, end_y)),
            fill=body, width=int(size * 0.035),
        )
        # Suction cup at the tip.
        canvas.draw.ellipse(
            (end_x - 4, end_y - 4, end_x + 4, end_y + 4),
            fill=belly, outline=accent,
        )
    # Highlight on head.
    canvas.draw.ellipse(
        (head_cx - int(head_r * 0.5), head_cy - int(head_r * 0.8),
         head_cx + int(head_r * 0.5), head_cy - int(head_r * 0.2)),
        fill=tuple(min(255, c + 25) for c in body[:3]),
    )
    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.04),
                  eye_dx=int(size * 0.09), eye_r=7,
                  mouth_dy=int(size * 0.10), size=size)


def _draw_zenny(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Round perched body.
    body_cx, body_cy = cx, int(size * 0.56) + dy
    body_r = int(size * 0.22)
    canvas.draw.ellipse(
        (body_cx - body_r, body_cy - body_r,
         body_cx + body_r, body_cy + body_r),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (body_cx - int(body_r * 0.55), body_cy - int(body_r * 0.1),
         body_cx + int(body_r * 0.55), body_cy + int(body_r * 0.75)),
        fill=belly,
    )
    # Head -- smaller circle on top.
    head_cx, head_cy = cx, int(size * 0.32) + dy
    head_r = int(size * 0.14)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )
    # Centred duckling beak -- soft trapezoid hanging below the eyes.
    beak_top_y = head_cy + int(size * 0.05)
    beak_bot_y = head_cy + int(size * 0.13)
    canvas.draw.polygon(
        [
            (head_cx - int(size * 0.06), beak_top_y),
            (head_cx + int(size * 0.06), beak_top_y),
            (head_cx + int(size * 0.05), beak_bot_y),
            (head_cx - int(size * 0.05), beak_bot_y),
        ],
        fill=hex_to_rgb(0xF57F17), outline=accent,
    )
    canvas.draw.line(
        ((head_cx - int(size * 0.05), int((beak_top_y + beak_bot_y) / 2)),
         (head_cx + int(size * 0.05), int((beak_top_y + beak_bot_y) / 2))),
        fill=accent, width=1,
    )
    # Folded wings on BOTH sides so anatomy reads symmetrically.
    wing_fill = tuple(max(0, c - 30) for c in body[:3])
    for sgn in (-1, 1):
        canvas.draw.polygon(
            [
                (body_cx + sgn * int(body_r * 0.55), body_cy - int(body_r * 0.20)),
                (body_cx + sgn * int(body_r * 1.05), body_cy + int(body_r * 0.20)),
                (body_cx + sgn * int(body_r * 0.40), body_cy + int(body_r * 0.50)),
            ],
            fill=wing_fill,
            outline=accent,
        )
    # Tail feather plume below.
    canvas.draw.polygon(
        [
            (body_cx, body_cy + body_r - 4),
            (body_cx - int(size * 0.04), body_cy + body_r + int(size * 0.16)),
            (body_cx + int(size * 0.04), body_cy + body_r + int(size * 0.16)),
        ],
        fill=accent,
    )
    # Two skinny perch legs.
    for sgn in (-1, 1):
        lx = body_cx + sgn * int(body_r * 0.4)
        canvas.draw.line(
            ((lx, body_cy + body_r - 4),
             (lx, body_cy + body_r + int(size * 0.08))),
            fill=accent, width=3,
        )
    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.02),
                  eye_dx=int(size * 0.06), eye_r=5,
                  mouth_dy=int(size * 0.08), size=size)


def _draw_pyper(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Coiled S-shape body via overlapping ellipses.
    coils = [
        (cx,                      int(size * 0.78) + dy, int(size * 0.18)),
        (cx + int(size * 0.10),  int(size * 0.66) + dy, int(size * 0.14)),
        (cx - int(size * 0.10),  int(size * 0.54) + dy, int(size * 0.12)),
        (cx + int(size * 0.06),  int(size * 0.42) + dy, int(size * 0.11)),
    ]
    for cox, coy, r in coils:
        canvas.draw.ellipse(
            (cox - r, coy - int(r * 0.8),
             cox + r, coy + int(r * 0.8)),
            fill=body, outline=accent, width=2,
        )
    # Scaly belly stripe down the coils.
    for cox, coy, r in coils:
        canvas.draw.ellipse(
            (cox - int(r * 0.6), coy - int(r * 0.3),
             cox + int(r * 0.6), coy + int(r * 0.5)),
            fill=belly,
        )
    # Head -- final coil topped with a flatter wider ellipse.
    head_cx, head_cy = cx + int(size * 0.06), int(size * 0.34) + dy
    head_w, head_h = int(size * 0.13), int(size * 0.08)
    canvas.draw.ellipse(
        (head_cx - head_w, head_cy - head_h,
         head_cx + head_w, head_cy + head_h),
        fill=body, outline=accent, width=3,
    )
    # Forked tongue if attacking / victorious.
    if pose in ("attack", "victory", "snarl"):
        canvas.draw.polygon(
            [
                (head_cx, head_cy + int(size * 0.02)),
                (head_cx + int(size * 0.16), head_cy + int(size * 0.06)),
                (head_cx + int(size * 0.12), head_cy + int(size * 0.08)),
                (head_cx + int(size * 0.18), head_cy + int(size * 0.10)),
                (head_cx + int(size * 0.10), head_cy + int(size * 0.10)),
            ],
            fill=hex_to_rgb(0xD32F2F),
        )
    _draw_face_at(canvas, style, pose, head_cx - int(size * 0.02), head_cy - 2,
                  eye_dx=int(size * 0.05), eye_r=4,
                  mouth_dy=int(size * 0.05), size=size)


def _draw_cobble(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Cobble -- a single rounded boulder with a face carved into it.

    Rewritten from a pile-of-circles + floating head + ears (which read
    as a kindergarten doodle) into one clean boulder silhouette with
    eyes / mouth carved in the upper third. Arms are two short stubby
    rock-paws anchored at the body shoulders; legs are two short
    rock-feet at the base. Anatomy is consistent regardless of mood.
    """
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2

    # Single boulder body, slightly wider than tall, with a flat base.
    body_top    = int(size * 0.30) + dy
    body_bottom = int(size * 0.82) + dy
    body_left   = cx - int(size * 0.30)
    body_right  = cx + int(size * 0.30)
    canvas.draw.rounded_rectangle(
        (body_left, body_top, body_right, body_bottom),
        radius=int(size * 0.18),
        fill=body, outline=accent, width=3,
    )

    # Light highlight along the upper-left to give it volume.
    canvas.draw.chord(
        (body_left + 6, body_top + 6, body_right - 6, body_bottom - 6),
        start=180, end=270,
        fill=tuple(min(255, c + 22) for c in body[:3]),
    )

    # Two surface cracks suggesting weathering.
    canvas.draw.line(
        ((cx - int(size * 0.16), int(size * 0.66) + dy),
         (cx - int(size * 0.06), int(size * 0.74) + dy)),
        fill=accent, width=2,
    )
    canvas.draw.line(
        ((cx + int(size * 0.04), int(size * 0.50) + dy),
         (cx + int(size * 0.18), int(size * 0.46) + dy)),
        fill=accent, width=2,
    )

    # Stubby arms anchored at the shoulders.
    arm_y = int(size * 0.50) + dy
    for sgn, x in ((-1, body_left), (1, body_right)):
        canvas.draw.rounded_rectangle(
            (x - int(size * 0.06) if sgn < 0 else x - int(size * 0.02),
             arm_y - int(size * 0.05),
             x + int(size * 0.02) if sgn < 0 else x + int(size * 0.06),
             arm_y + int(size * 0.06)),
            radius=int(size * 0.04),
            fill=body, outline=accent, width=2,
        )

    # Two flat feet at the base.
    for sgn in (-1, 1):
        foot_cx = cx + sgn * int(size * 0.14)
        canvas.draw.rounded_rectangle(
            (foot_cx - int(size * 0.07), body_bottom - 4,
             foot_cx + int(size * 0.07), body_bottom + int(size * 0.05)),
            radius=int(size * 0.03),
            fill=tuple(max(0, c - 18) for c in body[:3]),
            outline=accent, width=2,
        )

    # Face is carved directly into the upper third of the boulder.
    face_cx = cx
    face_cy = body_top + int(size * 0.10)
    _draw_face_at(canvas, style, pose, face_cx, face_cy,
                  eye_dx=int(size * 0.07), eye_r=4,
                  mouth_dy=int(size * 0.08), size=size)


def _draw_wecco(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Front-facing duck -- egg body, round head, flat orange bill, paddle feet.

    Drawn order is back-to-front: paddle feet, body, folded wings on
    each side, neck wedge, head, bill, then the face. The bill is the
    single feature that makes this read as a duck rather than another
    pastel-blue critter, so it lands as two stacked horizontal
    ellipses (upper + slightly darker lower bill) parked on the lower
    third of the head.
    """
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    bill_top = hex_to_rgb(0xFFB300)
    bill_bot = hex_to_rgb(0xE6A100)
    cx = size // 2

    # Paddle feet first so the body covers their inner edge.
    foot_y = int(size * 0.84) + dy
    foot_w = int(size * 0.075)
    foot_h = int(size * 0.045)
    for sgn in (-1, 1):
        fx = cx + sgn * int(size * 0.08)
        canvas.draw.ellipse(
            (fx - foot_w, foot_y - foot_h,
             fx + foot_w, foot_y + foot_h),
            fill=bill_top, outline=accent, width=2,
        )

    # Egg-shaped body, sitting low. Slightly taller than wide so the
    # silhouette reads as a duck rather than a sphere.
    body_left   = cx - int(size * 0.21)
    body_right  = cx + int(size * 0.21)
    body_top    = int(size * 0.50) + dy
    body_bottom = int(size * 0.84) + dy
    canvas.draw.ellipse(
        (body_left, body_top, body_right, body_bottom),
        fill=body, outline=accent, width=3,
    )
    # Cream/white belly along the centre.
    canvas.draw.ellipse(
        (cx - int(size * 0.12), body_top + int(size * 0.10),
         cx + int(size * 0.12), body_bottom - 6),
        fill=belly,
    )

    # Folded wings tucked along each side of the body. Each is a
    # crescent: outer arc body-coloured + accent outline, inner feather
    # tick to suggest plumage. Kept symmetric so the right-hand flip
    # in the battle scene still looks correct.
    for sgn in (-1, 1):
        wing_cx = cx + sgn * int(size * 0.16)
        wing_cy = int(size * 0.66) + dy
        wing_rx = int(size * 0.10)
        wing_ry = int(size * 0.14)
        canvas.draw.ellipse(
            (wing_cx - wing_rx, wing_cy - wing_ry,
             wing_cx + wing_rx, wing_cy + wing_ry),
            fill=body, outline=accent, width=2,
        )
        # Feather tick -- short accent line near the bottom of the wing.
        canvas.draw.line(
            ((wing_cx - int(size * 0.04), wing_cy + int(size * 0.06)),
             (wing_cx + int(size * 0.04), wing_cy + int(size * 0.09))),
            fill=accent, width=2,
        )

    # Short neck wedge bridging body to head -- a stubby trapezoid in
    # the body colour so the head doesn't visually float.
    neck_top    = int(size * 0.42) + dy
    neck_bottom = int(size * 0.54) + dy
    canvas.draw.polygon(
        [
            (cx - int(size * 0.10), neck_bottom),
            (cx + int(size * 0.10), neck_bottom),
            (cx + int(size * 0.085), neck_top),
            (cx - int(size * 0.085), neck_top),
        ],
        fill=body, outline=accent,
    )

    # Round head on top of the neck.
    head_cx = cx
    head_cy = int(size * 0.32) + dy
    head_r  = int(size * 0.15)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )

    # Flat duck bill -- two stacked horizontal ellipses below the eyes.
    bill_cy = head_cy + int(size * 0.10)
    bill_rx = int(size * 0.11)
    bill_ry = int(size * 0.040)
    canvas.draw.ellipse(
        (head_cx - bill_rx, bill_cy - bill_ry,
         head_cx + bill_rx, bill_cy + bill_ry),
        fill=bill_top, outline=accent, width=2,
    )
    canvas.draw.ellipse(
        (head_cx - int(bill_rx * 0.85),
         bill_cy + int(bill_ry * 0.2),
         head_cx + int(bill_rx * 0.85),
         bill_cy + int(bill_ry * 1.6)),
        fill=bill_bot, outline=accent, width=2,
    )
    # Thin nostril dot on the upper bill.
    canvas.draw.ellipse(
        (head_cx - 2, bill_cy - 2, head_cx + 2, bill_cy + 1),
        fill=accent,
    )

    # Tail tuft at the rear of the body. Small triangle just so the
    # rump reads as a duck butt, not a sphere.
    canvas.draw.polygon(
        [
            (cx - int(size * 0.04), body_bottom - int(size * 0.04)),
            (cx + int(size * 0.04), body_bottom - int(size * 0.04)),
            (cx, body_bottom + int(size * 0.04)),
        ],
        fill=body, outline=accent,
    )

    # Eyes -- the bill draws *below* the face, so we keep the standard
    # eye placement above the bill. Mouth dy lands inside/just below
    # the upper bill so a smile/frown reads as bill curvature.
    _draw_face_at(canvas, style, pose, head_cx, head_cy - int(size * 0.02),
                  eye_dx=int(size * 0.06), eye_r=4,
                  mouth_dy=int(size * 0.09), size=size)


def _draw_nimbus(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    # Cloud silhouette -- bigger and more elongated than wecco.
    base_cx, base_cy = cx, int(size * 0.52) + dy
    for ox, oy, r in [
        (-0.26, 0.04, 0.14),
        ( 0.26, 0.04, 0.14),
        (-0.10,-0.10, 0.18),
        ( 0.10,-0.10, 0.18),
        (-0.20, 0.14, 0.16),
        ( 0.20, 0.14, 0.16),
        ( 0.00, 0.02, 0.22),
    ]:
        rx = base_cx + int(size * ox)
        ry = base_cy + int(size * oy)
        rr = int(size * r)
        canvas.draw.ellipse(
            (rx - rr, ry - rr, rx + rr, ry + rr),
            fill=body, outline=accent, width=2,
        )
    # Raindrops underneath.
    for ox in (-0.16, -0.04, 0.10, 0.22):
        rx = base_cx + int(size * ox)
        ry = base_cy + int(size * 0.30)
        canvas.draw.polygon(
            [(rx, ry), (rx - 6, ry + 14), (rx + 6, ry + 14)],
            fill=hex_to_rgb(0x4FC3F7),
        )
    # Tiny lightning bolt to give it personality.
    canvas.draw.polygon(
        [
            (cx + int(size * 0.06), int(size * 0.76) + dy),
            (cx - int(size * 0.02), int(size * 0.82) + dy),
            (cx + int(size * 0.04), int(size * 0.82) + dy),
            (cx - int(size * 0.04), int(size * 0.90) + dy),
        ],
        fill=hex_to_rgb(0xFFEB3B),
    )
    # Face goes on the central puff.
    _draw_face_at(canvas, style, pose, cx, int(size * 0.52) + dy,
                  eye_dx=int(size * 0.06), eye_r=5,
                  mouth_dy=int(size * 0.07), size=size)


def _draw_blazer(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    flame_y = int(size * 0.18) + dy
    flame_color = hex_to_rgb(0xFF5722)
    flame_inner = hex_to_rgb(0xFFD54F)
    # Flame plume rising from the head.
    canvas.draw.polygon(
        [
            (cx - int(size * 0.18), int(size * 0.34) + dy),
            (cx - int(size * 0.10), flame_y - int(size * 0.04)),
            (cx, flame_y - int(size * 0.10)),
            (cx + int(size * 0.10), flame_y - int(size * 0.04)),
            (cx + int(size * 0.18), int(size * 0.34) + dy),
        ],
        fill=flame_color, outline=accent,
    )
    canvas.draw.polygon(
        [
            (cx - int(size * 0.10), int(size * 0.32) + dy),
            (cx, flame_y),
            (cx + int(size * 0.10), int(size * 0.32) + dy),
        ],
        fill=flame_inner,
    )
    # Body -- tall, like a fox/wolf but slimmer.
    canvas.draw.ellipse(
        (cx - int(size * 0.20), int(size * 0.46) + dy,
         cx + int(size * 0.20), int(size * 0.86) + dy),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (cx - int(size * 0.12), int(size * 0.60) + dy,
         cx + int(size * 0.12), int(size * 0.82) + dy),
        fill=belly,
    )
    # Triangular ears.
    head_cx, head_cy = cx, int(size * 0.42) + dy
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.10)
        canvas.draw.polygon(
            [
                (ex - int(size * 0.03), int(size * 0.40) + dy),
                (ex + int(size * 0.03), int(size * 0.40) + dy),
                (ex, int(size * 0.30) + dy),
            ],
            fill=body, outline=accent,
        )
    # Side flame tufts trailing along body.
    for sgn in (-1, 1):
        canvas.draw.polygon(
            [
                (cx + sgn * int(size * 0.20), int(size * 0.58) + dy),
                (cx + sgn * int(size * 0.30), int(size * 0.66) + dy),
                (cx + sgn * int(size * 0.20), int(size * 0.74) + dy),
            ],
            fill=flame_color,
        )
    _draw_face_at(canvas, style, pose, head_cx, head_cy + int(size * 0.04),
                  eye_dx=int(size * 0.06), eye_r=5,
                  mouth_dy=int(size * 0.08), size=size)


def _draw_thornling(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    base_cx, base_cy = cx, int(size * 0.58) + dy
    body_r = int(size * 0.22)
    # Round leafy body.
    canvas.draw.ellipse(
        (base_cx - body_r, base_cy - body_r,
         base_cx + body_r, base_cy + body_r),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (base_cx - int(body_r * 0.55), base_cy - int(body_r * 0.1),
         base_cx + int(body_r * 0.55), base_cy + int(body_r * 0.7)),
        fill=belly,
    )
    # Thorny spike crown on top.
    for sgn, offset_x in ((-1, -0.10), (0, 0), (1, 0.10)):
        tip_x = base_cx + int(size * offset_x)
        canvas.draw.polygon(
            [
                (tip_x - int(size * 0.03), int(size * 0.40) + dy),
                (tip_x + int(size * 0.03), int(size * 0.40) + dy),
                (tip_x, int(size * 0.26) + dy),
            ],
            fill=accent,
        )
    # Leaves on either side.
    for sgn in (-1, 1):
        canvas.draw.polygon(
            [
                (base_cx + sgn * int(body_r * 0.9), base_cy - int(body_r * 0.2)),
                (base_cx + sgn * int(body_r * 1.6), base_cy - int(body_r * 0.5)),
                (base_cx + sgn * int(body_r * 1.4), base_cy + int(body_r * 0.1)),
                (base_cx + sgn * int(body_r * 0.9), base_cy + int(body_r * 0.05)),
            ],
            fill=body, outline=accent,
        )
        canvas.draw.line(
            (
                (base_cx + sgn * int(body_r * 0.9), base_cy - int(body_r * 0.05)),
                (base_cx + sgn * int(body_r * 1.4), base_cy - int(body_r * 0.2)),
            ),
            fill=accent, width=2,
        )
    # Root-legs: two stubby trunks emerge from the base of the body
    # and split into three toe-roots each, planted on the floor.
    leg_top_y    = base_cy + body_r - 4
    leg_bot_y    = base_cy + body_r + int(size * 0.10)
    for sgn in (-1, 1):
        leg_cx = base_cx + sgn * int(size * 0.10)
        # Upper trunk.
        canvas.draw.rounded_rectangle(
            (leg_cx - int(size * 0.04), leg_top_y,
             leg_cx + int(size * 0.04), leg_bot_y),
            radius=int(size * 0.03),
            fill=body, outline=accent, width=2,
        )
        # Three toe-roots flaring outward at the base.
        for toe_dx in (-0.05, 0.0, 0.05):
            tx = leg_cx + int(size * toe_dx)
            ty = leg_bot_y + int(size * 0.04)
            canvas.draw.polygon(
                [
                    (leg_cx - int(size * 0.03), leg_bot_y - 2),
                    (leg_cx + int(size * 0.03), leg_bot_y - 2),
                    (tx, ty),
                ],
                fill=accent,
            )
    _draw_face_at(canvas, style, pose, base_cx, base_cy - int(size * 0.02),
                  eye_dx=int(size * 0.07), eye_r=5,
                  mouth_dy=int(size * 0.08), size=size)


def _draw_draclet(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    body_cx = cx
    body_top = int(size * 0.50) + dy
    body_bottom = int(size * 0.86) + dy
    # Body first so wings can clearly attach to it (drawn next, on top).
    canvas.draw.ellipse(
        (body_cx - int(size * 0.16), body_top,
         body_cx + int(size * 0.16), body_bottom),
        fill=body, outline=accent, width=3,
    )
    # Wings -- inner anchor sits INSIDE the body silhouette so there is
    # no visible gap. Each wing has a scalloped outer edge so it reads
    # as a bat rather than a butterfly.
    wing_anchor_y = int(size * 0.60) + dy
    for sgn in (-1, 1):
        anchor_top = (body_cx + sgn * int(size * 0.08), int(size * 0.54) + dy)
        anchor_bot = (body_cx + sgn * int(size * 0.10), int(size * 0.72) + dy)
        tip_top    = (body_cx + sgn * int(size * 0.46), int(size * 0.32) + dy)
        tip_mid_a  = (body_cx + sgn * int(size * 0.40), int(size * 0.48) + dy)
        tip_mid_b  = (body_cx + sgn * int(size * 0.44), int(size * 0.60) + dy)
        tip_bot    = (body_cx + sgn * int(size * 0.34), int(size * 0.72) + dy)
        canvas.draw.polygon(
            [anchor_top, tip_top, tip_mid_a, tip_mid_b, tip_bot, anchor_bot],
            fill=tuple(max(0, c - 30) for c in body[:3]),
            outline=accent, width=2,
        )
        # Membrane veins fanning out from the body anchor.
        for f in (0.30, 0.55, 0.80):
            canvas.draw.line(
                (
                    (body_cx + sgn * int(size * 0.10), wing_anchor_y),
                    (body_cx + sgn * int(size * (0.10 + f * 0.34)),
                     int(size * (0.34 + f * 0.30)) + dy),
                ),
                fill=accent, width=1,
            )
    # Belly highlight painted on top of the wings so it reads as part
    # of the body, not the wings.
    canvas.draw.ellipse(
        (body_cx - int(size * 0.10), int(size * 0.62) + dy,
         body_cx + int(size * 0.10), int(size * 0.82) + dy),
        fill=belly,
    )
    # Head + snout.
    head_cx, head_cy = cx, int(size * 0.38) + dy
    head_r = int(size * 0.14)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.polygon(
        [
            (head_cx + int(size * 0.04), head_cy + int(size * 0.02)),
            (head_cx + int(size * 0.18), head_cy + int(size * 0.04)),
            (head_cx + int(size * 0.04), head_cy + int(size * 0.10)),
        ],
        fill=body, outline=accent,
    )
    # Two horns angled back from head.
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.06)
        canvas.draw.polygon(
            [
                (ex - int(size * 0.02), head_cy - int(size * 0.10)),
                (ex + int(size * 0.02), head_cy - int(size * 0.10)),
                (ex + sgn * int(size * 0.06), head_cy - int(size * 0.24)),
            ],
            fill=accent,
        )
    # Tail with diamond tip.
    canvas.draw.line(
        ((cx, int(size * 0.86) + dy),
         (cx + int(size * 0.18), int(size * 0.96) + dy)),
        fill=body, width=int(size * 0.04),
    )
    canvas.draw.polygon(
        [
            (cx + int(size * 0.18), int(size * 0.92) + dy),
            (cx + int(size * 0.24), int(size * 0.96) + dy),
            (cx + int(size * 0.18), int(size * 1.00) + dy),
            (cx + int(size * 0.12), int(size * 0.96) + dy),
        ],
        fill=accent,
    )
    _draw_face_at(canvas, style, pose, head_cx - int(size * 0.02), head_cy,
                  eye_dx=int(size * 0.05), eye_r=4,
                  mouth_dy=int(size * 0.08), size=size)


def _draw_glitch(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    base_cx, base_cy = cx, int(size * 0.56) + dy
    body_r = int(size * 0.22)
    # Body as a chunky pixelated square ring instead of a clean circle.
    px = int(size * 0.04)
    for ix in range(-5, 6):
        for iy in range(-5, 6):
            rx = base_cx + ix * px
            ry = base_cy + iy * px
            d2 = ix * ix + iy * iy
            if d2 > 30:
                continue
            # Outer ring tinted accent, inner body color.
            color = body if d2 < 18 else accent
            # Occasional glitch hole.
            if (ix + iy + (level_glitch_seed(size))) % 7 == 0 and d2 > 5:
                continue
            canvas.draw.rectangle(
                (rx - px // 2, ry - px // 2,
                 rx + px // 2, ry + px // 2),
                fill=color,
            )
    # Scan-line artifact bars.
    for y_frac in (0.30, 0.62, 0.78):
        y = int(size * y_frac) + dy
        canvas.draw.rectangle(
            (base_cx - body_r - 8, y, base_cx + body_r + 8, y + 3),
            fill=hex_to_rgb(0xFFF59D),
        )
    # Glitch echo offsets -- ghost outline at +6/-6.
    canvas.draw.ellipse(
        (base_cx - body_r + 6, base_cy - body_r,
         base_cx + body_r + 6, base_cy + body_r),
        outline=hex_to_rgb(0xFF4081), width=1,
    )
    canvas.draw.ellipse(
        (base_cx - body_r - 6, base_cy - body_r,
         base_cx + body_r - 6, base_cy + body_r),
        outline=hex_to_rgb(0x4FC3F7), width=1,
    )
    _draw_face_at(canvas, style, pose, base_cx, base_cy - int(size * 0.04),
                  eye_dx=int(size * 0.08), eye_r=6,
                  mouth_dy=int(size * 0.10), size=size)


def level_glitch_seed(size: int) -> int:
    """Tiny deterministic offset based on size so two different-size
    glitch portraits don't show identical pixel holes."""
    return (size * 2654435761) & 0xFF


def _draw_default(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Generic fallback silhouette for unknown species: round critter."""
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    base_cx, base_cy = cx, int(size * 0.58) + dy
    body_r = int(size * 0.22)
    canvas.draw.ellipse(
        (base_cx - body_r, base_cy - body_r,
         base_cx + body_r, base_cy + body_r),
        fill=body, outline=accent, width=3,
    )
    canvas.draw.ellipse(
        (base_cx - int(body_r * 0.55), base_cy - int(body_r * 0.1),
         base_cx + int(body_r * 0.55), base_cy + int(body_r * 0.7)),
        fill=belly,
    )
    head_cx, head_cy = cx, int(size * 0.40) + dy
    head_r = int(size * 0.14)
    canvas.draw.ellipse(
        (head_cx - head_r, head_cy - head_r,
         head_cx + head_r, head_cy + head_r),
        fill=body, outline=accent, width=2,
    )
    _draw_face_at(canvas, style, pose, head_cx, head_cy,
                  eye_dx=int(size * 0.05), eye_r=5,
                  mouth_dy=int(size * 0.07), size=size)


def _draw_spiderlenny(canvas: RenderCanvas, style: dict, pose: str, size: int, dy: int) -> None:
    """Front-facing Spiderlenny -- bulbous abdomen, separate cephalothorax,
    eight legs (four per side, two pairs each), eight eyes, and the
    species' signature ( ͡o ͜ʖ ͡o) raised-eyebrow Lenny face.

    Drawn order: legs first (so the body covers their inner stubs),
    then abdomen, cephalothorax, fangs, eyes, eyebrows, Lenny mouth.
    """
    body, accent, belly = (
        hex_to_rgb(style["body_color"]),
        hex_to_rgb(style["accent"]),
        _belly_color(style),
    )
    cx = size // 2
    abd_cx, abd_cy = cx, int(size * 0.62) + dy
    abd_rx, abd_ry = int(size * 0.22), int(size * 0.20)
    head_cx, head_cy = cx, int(size * 0.36) + dy
    head_rx, head_ry = int(size * 0.16), int(size * 0.13)

    # Eight legs -- four per side, two angled up + two angled down. Each
    # leg has a "joint" segment so it reads as a real spider leg, not
    # a stick. Drawn first so the body covers the inner attachment.
    leg_origin_y = int(size * 0.50) + dy
    leg_width = max(2, int(size * 0.020))
    for sgn in (-1, 1):
        for i, (angle_up, length) in enumerate((
            (0.40, 0.36),   # upper-front
            (0.15, 0.42),   # mid-front
            (-0.12, 0.40),  # mid-back
            (-0.32, 0.32),  # rear
        )):
            origin = (cx + sgn * int(size * 0.06), leg_origin_y + int(i * size * 0.025))
            # Joint -- 60% along the leg, kinked toward the ground.
            joint = (
                cx + sgn * int(size * length * 0.55),
                leg_origin_y + int(size * (length * 0.55 * -angle_up + 0.12)),
            )
            tip = (
                cx + sgn * int(size * length),
                leg_origin_y + int(size * (length * -angle_up + 0.28)),
            )
            canvas.draw.line((origin, joint), fill=accent, width=leg_width)
            canvas.draw.line((joint, tip), fill=accent, width=leg_width)
            # Foot tick so each leg ends in a clear point.
            canvas.draw.ellipse(
                (tip[0] - 2, tip[1] - 2, tip[0] + 2, tip[1] + 2),
                fill=accent,
            )

    # Abdomen -- bulbous rear segment, body colour.
    canvas.draw.ellipse(
        (abd_cx - abd_rx, abd_cy - abd_ry,
         abd_cx + abd_rx, abd_cy + abd_ry),
        fill=body, outline=accent, width=3,
    )
    # Soft belly highlight on the abdomen.
    canvas.draw.ellipse(
        (abd_cx - int(abd_rx * 0.55), abd_cy - int(abd_ry * 0.10),
         abd_cx + int(abd_rx * 0.55), abd_cy + int(abd_ry * 0.75)),
        fill=belly,
    )

    # Cephalothorax (smaller head segment) sitting on top.
    canvas.draw.ellipse(
        (head_cx - head_rx, head_cy - head_ry,
         head_cx + head_rx, head_cy + head_ry),
        fill=body, outline=accent, width=3,
    )

    # Two short fangs hanging below the head.
    for sgn in (-1, 1):
        fx = head_cx + sgn * int(size * 0.05)
        canvas.draw.polygon(
            [
                (fx - 2, head_cy + int(size * 0.08)),
                (fx + 2, head_cy + int(size * 0.08)),
                (fx, head_cy + int(size * 0.16)),
            ],
            fill=hex_to_rgb(0xFFFFFF), outline=accent, width=1,
        )

    # Eight eyes -- two big main eyes plus six small ones above/below.
    # The two big ones form the Lenny ( ͡o ͜ʖ ͡o) gaze; the six smaller
    # cluster sells that this is a spider, not just a cute critter.
    eye_color = hex_to_rgb(style["eye_color"])
    big_y = head_cy - int(size * 0.01)
    for sgn in (-1, 1):
        ex = head_cx + sgn * int(size * 0.06)
        # Big eye -- white with dark pupil.
        canvas.draw.ellipse(
            (ex - 6, big_y - 6, ex + 6, big_y + 6),
            fill=hex_to_rgb(0xFFFFFF), outline=accent, width=1,
        )
        canvas.draw.ellipse(
            (ex - 3, big_y - 3, ex + 3, big_y + 3),
            fill=eye_color,
        )
        # Raised eyebrow ( ͡° ) -- short arc above each big eye.
        canvas.draw.arc(
            (ex - 9, big_y - 14, ex + 9, big_y - 4),
            start=200, end=340, fill=eye_color, width=2,
        )
        # Cluster of three small eyes -- one above the eyebrow arc,
        # two flanking the main eye.
        for ox, oy in ((0, -int(size * 0.07)),
                       (sgn * int(size * 0.10), -int(size * 0.02)),
                       (sgn * int(size * 0.04), int(size * 0.05))):
            sx = ex + ox
            sy = big_y + oy
            canvas.draw.ellipse((sx - 2, sy - 2, sx + 2, sy + 2), fill=eye_color)

    # Lenny mouth ( ͜ʖ ) -- centred between the two big eyes. Tilted
    # smug grin built from an arc + two small ticks.
    mouth_cx = head_cx
    mouth_cy = head_cy + int(size * 0.04)
    canvas.draw.arc(
        (mouth_cx - 9, mouth_cy - 4, mouth_cx + 9, mouth_cy + 8),
        start=10, end=170, fill=eye_color, width=2,
    )
    canvas.draw.line(
        ((mouth_cx - 9, mouth_cy + 1), (mouth_cx - 12, mouth_cy + 4)),
        fill=eye_color, width=2,
    )
    canvas.draw.line(
        ((mouth_cx + 9, mouth_cy + 1), (mouth_cx + 12, mouth_cy + 4)),
        fill=eye_color, width=2,
    )

    # Optional web strand dangling above the head -- one thin line so
    # the portrait reads as "spider on web" rather than just "bug".
    canvas.draw.line(
        ((cx, dy + int(size * 0.05)),
         (cx, head_cy - head_ry)),
        fill=accent, width=1,
    )


_SPECIES_DRAW: dict = {
    "fox":         _draw_fox,
    "cat":         _draw_cat,
    "wolf":        _draw_wolf,
    "shrimp":      _draw_shrimp,
    "lobster":     _draw_lobster,
    "crab":        _draw_crab,
    "octopus":     _draw_octopus,
    "zenny":       _draw_zenny,
    "pyper":       _draw_pyper,
    "cobble":      _draw_cobble,
    "wecco":       _draw_wecco,
    "nimbus":      _draw_nimbus,
    "blazer":      _draw_blazer,
    "thornling":   _draw_thornling,
    "draclet":     _draw_draclet,
    "glitch":      _draw_glitch,
    "spiderlenny": _draw_spiderlenny,
}


# ── Boss overlays ─────────────────────────────────────────────────────
#
# Painted on top of the species silhouette when row.boss_zone_id is set.
# Each routine assumes the species' head is roughly centred horizontally
# at cx = size // 2 and sits in the upper third of the canvas (the
# rebuilt species renderers all anchor heads on the vertical centre).
# Decorations land on top so they're visible from any pose.


def _draw_boss_overlay(
    canvas: RenderCanvas, boss_zone_id: str, size: int, dy: int,
) -> None:
    """Paint the boss-variant decoration for ``boss_zone_id``."""
    try:
        from configs.buddies_config import boss_variant as _bv
        variant = _bv(boss_zone_id)
    except Exception:
        return
    overlay = str(variant.get("overlay") or "")
    tint    = int(variant.get("accent_tint") or 0xFFD54F)
    cx = size // 2

    if overlay == "crown":
        # Five-point gold crown sitting above the head.
        crown_y = int(size * 0.13) + dy
        spike_h = int(size * 0.10)
        base_l  = cx - int(size * 0.13)
        base_r  = cx + int(size * 0.13)
        canvas.draw.polygon(
            [
                (base_l, crown_y + spike_h),
                (base_l + int(size * 0.05), crown_y),
                (base_l + int(size * 0.10), crown_y + int(spike_h * 0.5)),
                (cx,                         crown_y - int(size * 0.02)),
                (base_r - int(size * 0.10), crown_y + int(spike_h * 0.5)),
                (base_r - int(size * 0.05), crown_y),
                (base_r,                     crown_y + spike_h),
            ],
            fill=hex_to_rgb(tint),
            outline=hex_to_rgb(0xB28704), width=2,
        )
        # Three jewels.
        for sgn in (-1, 0, 1):
            jx = cx + sgn * int(size * 0.08)
            canvas.draw.ellipse(
                (jx - 4, crown_y + int(spike_h * 0.55) - 4,
                 jx + 4, crown_y + int(spike_h * 0.55) + 4),
                fill=hex_to_rgb(0xE53935 if sgn == 0 else 0x1E88E5),
                outline=hex_to_rgb(0xFFFFFF), width=1,
            )

    elif overlay == "helm":
        # Iron helm with twin horns on either side of the head.
        helm_top    = int(size * 0.12) + dy
        helm_bot    = int(size * 0.30) + dy
        helm_left   = cx - int(size * 0.16)
        helm_right  = cx + int(size * 0.16)
        canvas.draw.chord(
            (helm_left, helm_top, helm_right, helm_bot + int(size * 0.04)),
            start=180, end=360,
            fill=hex_to_rgb(tint),
            outline=hex_to_rgb(0x37474F), width=3,
        )
        # Visor slit.
        canvas.draw.rectangle(
            (cx - int(size * 0.10), helm_bot - int(size * 0.03),
             cx + int(size * 0.10), helm_bot - 2),
            fill=hex_to_rgb(0x1A1F2A),
        )
        # Horns curving outward.
        for sgn in (-1, 1):
            base_x = helm_left if sgn < 0 else helm_right
            tip_x  = base_x + sgn * int(size * 0.10)
            canvas.draw.polygon(
                [
                    (base_x,                helm_top + int(size * 0.04)),
                    (base_x,                helm_top + int(size * 0.10)),
                    (tip_x,                 helm_top - int(size * 0.02)),
                ],
                fill=hex_to_rgb(0xECEFF1),
                outline=hex_to_rgb(0x546E7A), width=2,
            )

    elif overlay == "trident_crown":
        # Coral crown -- three curving prongs above the head.
        base_y = int(size * 0.14) + dy
        for sgn in (-1, 0, 1):
            cx_off = cx + sgn * int(size * 0.10)
            tip_y  = base_y - int(size * 0.10) - (0 if sgn == 0 else int(size * 0.02))
            canvas.draw.polygon(
                [
                    (cx_off - int(size * 0.03), base_y),
                    (cx_off + int(size * 0.03), base_y),
                    (cx_off, tip_y),
                ],
                fill=hex_to_rgb(tint),
                outline=hex_to_rgb(0x006064), width=2,
            )
        # Connector ring.
        canvas.draw.rectangle(
            (cx - int(size * 0.16), base_y - 2,
             cx + int(size * 0.16), base_y + int(size * 0.03)),
            fill=hex_to_rgb(tint),
            outline=hex_to_rgb(0x006064),
        )
        # Pearl in the centre.
        canvas.draw.ellipse(
            (cx - 5, base_y + int(size * 0.005) - 1,
             cx + 5, base_y + int(size * 0.005) + 9),
            fill=hex_to_rgb(0xFFFFFF),
            outline=hex_to_rgb(0x80DEEA), width=1,
        )

    elif overlay == "antlers":
        # Pair of branching antlers sweeping up + outward.
        base_y = int(size * 0.18) + dy
        for sgn in (-1, 1):
            root_x = cx + sgn * int(size * 0.06)
            mid_x  = cx + sgn * int(size * 0.16)
            tip_x  = cx + sgn * int(size * 0.22)
            canvas.draw.line(
                ((root_x, base_y), (mid_x, base_y - int(size * 0.10))),
                fill=hex_to_rgb(tint), width=int(size * 0.025),
            )
            canvas.draw.line(
                ((mid_x, base_y - int(size * 0.10)),
                 (tip_x, base_y - int(size * 0.20))),
                fill=hex_to_rgb(tint), width=int(size * 0.022),
            )
            # Side branch.
            branch_mid = (mid_x, base_y - int(size * 0.10))
            branch_tip = (mid_x + sgn * int(size * 0.08),
                          base_y - int(size * 0.16))
            canvas.draw.line(
                (branch_mid, branch_tip),
                fill=hex_to_rgb(tint), width=int(size * 0.018),
            )
        # Leaf accents on the antler tips.
        for sgn in (-1, 1):
            lx = cx + sgn * int(size * 0.22)
            ly = base_y - int(size * 0.20)
            canvas.draw.ellipse(
                (lx - 5, ly - 5, lx + 5, ly + 5),
                fill=hex_to_rgb(0x66BB6A),
            )

    elif overlay == "flame_mane":
        # Three flickering flames rising from the head.
        base_y = int(size * 0.20) + dy
        for sgn, scale in ((-1, 0.8), (0, 1.0), (1, 0.8)):
            fx = cx + sgn * int(size * 0.10)
            tip_y = base_y - int(size * 0.22 * scale)
            # Outer red flame.
            canvas.draw.polygon(
                [
                    (fx - int(size * 0.05), base_y),
                    (fx + int(size * 0.05), base_y),
                    (fx + int(size * 0.02), tip_y + int(size * 0.04)),
                    (fx, tip_y),
                ],
                fill=hex_to_rgb(0xFF5722),
            )
            # Inner orange flame.
            canvas.draw.polygon(
                [
                    (fx - int(size * 0.03), base_y - int(size * 0.02)),
                    (fx + int(size * 0.03), base_y - int(size * 0.02)),
                    (fx, tip_y + int(size * 0.04)),
                ],
                fill=hex_to_rgb(tint),
            )
            # Yellow core.
            canvas.draw.polygon(
                [
                    (fx - int(size * 0.015), base_y - int(size * 0.04)),
                    (fx + int(size * 0.015), base_y - int(size * 0.04)),
                    (fx, tip_y + int(size * 0.06)),
                ],
                fill=hex_to_rgb(0xFFEB3B),
            )

    # Always paint a small star badge in the bottom-right corner so any
    # boss-tamed buddy is recognisable at a glance even if the overlay
    # key is unknown.
    star_cx = int(size * 0.88)
    star_cy = int(size * 0.88)
    pts: list[tuple[int, int]] = []
    r_out = int(size * 0.05)
    r_in  = int(r_out * 0.45)
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5
        rr = r_out if i % 2 == 0 else r_in
        pts.append((star_cx + int(rr * math.cos(ang)),
                    star_cy - int(rr * math.sin(ang))))
    canvas.draw.polygon(pts, fill=hex_to_rgb(tint),
                        outline=hex_to_rgb(0x1A1F2A))


# ── Action overlays ───────────────────────────────────────────────────

def _draw_action_overlay(canvas: RenderCanvas, pose: str, size: int) -> None:
    """Pose-specific overlay: motion lines, sparkles, impact stars."""
    if pose == "attack":
        for i in range(3):
            x0 = int(size * 0.78) + i * int(size * 0.04)
            y0 = int(size * 0.32 + i * size * 0.06)
            canvas.draw.line(
                ((x0, y0), (x0 + int(size * 0.10), y0 + 4)),
                fill=hex_to_rgb(0xFFEB3B), width=4,
            )
    elif pose == "victory":
        cx, cy = size // 2, int(size * 0.20)
        for ang in range(0, 360, 45):
            r = math.radians(ang)
            x1 = cx + int(size * 0.10 * math.cos(r))
            y1 = cy + int(size * 0.10 * math.sin(r))
            canvas.draw.line(((cx, cy), (x1, y1)),
                             fill=hex_to_rgb(0xFFEB3B), width=3)
        canvas.draw.ellipse(
            (cx - 10, cy - 10, cx + 10, cy + 10),
            fill=hex_to_rgb(0xFFF59D),
        )
    elif pose == "hurt":
        for sgn in (-1, 1):
            cx = size // 2 + sgn * int(size * 0.20)
            cy = int(size * 0.42)
            for ang in (0, 45, 90, 135):
                r = math.radians(ang)
                x1 = cx + int(20 * math.cos(r))
                y1 = cy + int(20 * math.sin(r))
                x2 = cx - int(20 * math.cos(r))
                y2 = cy - int(20 * math.sin(r))
                canvas.draw.line(((x1, y1), (x2, y2)),
                                 fill=hex_to_rgb(0xFF1744), width=3)
    elif pose == "using_item":
        sx = int(size * 0.70)
        sy = int(size * 0.30)
        canvas.draw.polygon(
            [
                (sx, sy - 20), (sx + 8, sy - 4), (sx + 24, sy),
                (sx + 8, sy + 4), (sx, sy + 20), (sx - 8, sy + 4),
                (sx - 24, sy), (sx - 8, sy - 4),
            ],
            fill=hex_to_rgb(0xFFD700), outline=hex_to_rgb(0xBF6F00),
        )


def _apply_burst_overlay(
    base_bytes: bytes, frame_idx: int, total_frames: int, size: int,
) -> bytes:
    """Layer a per-frame burst effect on top of the base portrait bytes."""
    import io as _io
    img = Image.open(_io.BytesIO(base_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = size // 2, size // 2

    if frame_idx <= 1:
        arc_r = int(size * 0.30 + frame_idx * 30)
        draw.arc(
            (cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r),
            start=200, end=340,
            fill=(255, 235, 59, 220), width=8,
        )
    elif frame_idx == 2:
        for r in range(int(size * 0.40), int(size * 0.55), 3):
            alpha = max(40, 200 - (r - int(size * 0.40)) * 8)
            draw.ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                outline=(255, 255, 255, alpha), width=2,
            )
    elif frame_idx == 3:
        for sgn in (-1, 1):
            x = cx + sgn * int(size * 0.20)
            draw.line(((x, cy - 60), (x, cy + 60)),
                      fill=(255, 23, 68, 200), width=3)
    elif frame_idx == 4:
        for offset_x in (-1, 0, 1):
            px = cx + offset_x * int(size * 0.18)
            py = int(size * 0.80)
            draw.ellipse((px - 14, py - 8, px + 14, py + 8),
                         fill=(200, 200, 200, 160))

    combined = Image.alpha_composite(img, overlay)
    if frame_idx <= 2:
        glow = ImageFilter.GaussianBlur(radius=8)
        combined = combined.filter(glow).resize(combined.size)
        combined = Image.alpha_composite(img, combined)
    return to_png_bytes(combined)
