"""services/buddy_arena_render.py -- arena map PNG renderer.

Renders a Pokemon-Stadium-style world map: nodes for each of the 14
zones laid out by region across a 1200x900 canvas, edges for the
neighbours graph from buddies_config.ARENA_ZONES, current node
highlighted with a pulsing halo, cleared zones stamped with a trophy.

Public:
    render_arena_map(progress, *, focus_zone=None) -> bytes
    render_tournament_bracket(progress, *, current_round=1) -> bytes
"""
from __future__ import annotations

import logging
import math
from typing import Iterable

from PIL import ImageDraw

from configs.buddies_config import (
    ARENA_REGIONS,
    ARENA_ZONES,
    TOURNAMENT_BRACKET,
)
from core.framework.render import RenderCanvas
from core.framework.render_primitives import (
    font, hex_to_rgb, text_with_outline,
)

log = logging.getLogger(__name__)


_MAP_W: int = 1600
_MAP_H: int = 1080

# Layout grid. Regions get non-overlapping rectangular bands; zones get
# hand-placed positions so each node sits inside its region's band and
# inter-region edges (Forest->Stone, Volcano->Tide, Tide->Tournament,
# etc.) read as clean connectors rather than crossing diagonals.
#
# Vertical stacking (top-to-bottom):
#   Forest band  (left)  +  Volcano band (right)  +  Tournament (top-far-right)
#   Plains       (left)  +  Stone        (middle) +  Tide (right)
_ZONE_POS: dict[str, tuple[int, int]] = {
    # Plains region (lower-left)
    "plains_gate":       (140, 760),
    "grassy_meadow":     (270, 620),
    "windmill_lane":     (270, 880),
    "ember_grove":       (410, 950),    # side
    "plains_arena":      (400, 740),

    # Stone region (lower-middle)
    "stone_pass":        (560, 620),
    "quarry_pit":        (560, 860),
    "obsidian_ridge":    (700, 740),
    "moonlit_pool":      (700, 950),    # side
    "stone_colosseum":   (840, 740),

    # Tide region (lower-right)
    "tide_shore":        (960, 620),
    "coral_cove":        (960, 860),
    "lighthouse_hop":    (1100, 740),
    "tide_amphitheatre": (1240, 620),

    # Forest region (upper-left, new)
    "whisper_path":      (560, 400),
    "fern_hollow":       (440, 300),
    "thorn_thicket":     (600, 280),
    "druid_circle":      (700, 380),

    # Volcano region (upper-right, new) -- band is x 820-1280
    "volcano_gate":      (870, 380),
    "ember_steppes":     (1000, 320),
    "lava_tube":         (1120, 400),
    "magma_caldera":     (1230, 380),

    # Specials -- placed inside their parent region band, near the
    # neighbour zones in the graph.
    "caravan_clearing":  (320, 380),     # Forest -- caravan connects to
                                          # whisper_path + druid_circle
    "mossy_market":      (240, 260),     # Forest -- connects to fern_hollow
    "ash_springs":       (870, 260),     # Volcano -- connects to volcano_gate
    "smith_camp":        (1230, 260),    # Volcano -- connects to lava_tube

    # Tournament hub (top-far-right, own panel) -- panel is x 1300-1570
    "champion_hall":     (1435, 310),
}


def render_arena_map(
    progress: dict,
    *,
    focus_zone: str | None = None,
    can_use_hidden: Iterable[str] = (),
) -> bytes:
    """Render the player's current view of the arena map.

    ``progress`` is the cc_buddy_map_progress row. ``focus_zone``
    overrides the current_zone highlight (used when rendering a preview
    for travel confirmation). ``can_use_hidden`` lists hidden-zone ids
    the player has unlocked so they can be rendered solid instead of
    locked.
    """
    canvas = RenderCanvas(_MAP_W, _MAP_H, bg=0x0F1419, gradient_to=0x182536)

    # Header
    canvas.title(
        "Buddy Arena Map",
        subtitle="Travel zone-by-zone -- clear three region bosses to qualify for the Champion Tournament.",
    )

    cleared = set(progress.get("cleared_zones") or [])
    current = focus_zone or str(progress.get("current_zone_id") or "")
    unlocks = set(progress.get("region_unlocks") or [])
    hidden_avail = set(can_use_hidden or ())

    # Region backdrop panels -- non-overlapping rectangles. Lower row
    # holds the three original regions; upper row holds Forest +
    # Volcano + the Tournament hub. Top-row bands start at y=200 so
    # the horizontal legend (y=110-150) sits cleanly above them
    # without overlapping the Whispering Forest header label.
    _draw_region_band(canvas, "plains",  (60,   540, 470, 1020))
    _draw_region_band(canvas, "stone",   (490,  540, 890, 1020))
    _draw_region_band(canvas, "tide",    (910,  540, 1320, 1020))
    _draw_region_band(canvas, "forest",  (60,   200, 800, 510))
    _draw_region_band(canvas, "volcano", (820,  200, 1280, 510))
    _draw_region_panel(canvas, "tournament", (1300, 200, 1570, 410))

    # Edges (drawn before nodes so they sit underneath)
    _draw_edges(canvas, cleared, unlocks, current)

    # Nodes
    for zid, pos in _ZONE_POS.items():
        z = ARENA_ZONES.get(zid, {})
        if not z:
            continue
        is_current = (zid == current)
        is_cleared = (zid in cleared)
        is_neighbor = (zid in (ARENA_ZONES.get(current, {}).get("neighbors") or []))
        unlocked_region = (z.get("region") in unlocks
                           or z.get("region") in ("side", "tournament"))
        hidden_locked = bool(z.get("hidden")) and zid not in hidden_avail and not is_cleared
        _draw_node(
            canvas, zid, z, pos,
            is_current=is_current,
            is_cleared=is_cleared,
            is_neighbor=is_neighbor,
            unlocked_region=unlocked_region,
            hidden_locked=hidden_locked,
        )

    # Legend
    _draw_legend(canvas)

    # Tournament status pill
    state = str(progress.get("tournament_state") or "locked")
    champ_count = int(progress.get("champion_count") or 0)
    _draw_tournament_pill(canvas, state, champ_count)

    return canvas.to_png_bytes()


def render_tournament_bracket(
    progress: dict, *, current_round: int = 1,
) -> bytes:
    """Render the 4-round championship bracket with the current round lit."""
    canvas = RenderCanvas(_MAP_W, 600, bg=0x0F0A1E, gradient_to=0x2C1F00)
    canvas.title(
        "Champion Tournament",
        subtitle="Single elimination. Win four matches to take the crown.",
    )

    col_w = (_MAP_W - 200) // len(TOURNAMENT_BRACKET)
    base_y = 220
    for i, entry in enumerate(TOURNAMENT_BRACKET):
        rd = int(entry["round"])
        active = rd == int(current_round)
        x = 100 + i * col_w
        color = 0xF1C40F if active else 0x4E342E
        canvas.rounded_panel(
            (x, base_y, x + col_w - 20, base_y + 240),
            color=color if active else 0x2F2317,
            radius=14,
            outline=color, outline_width=3 if active else 1,
        )
        canvas.text(
            (x + 18, base_y + 16),
            f"Round {rd}",
            color=0xFFFFFF, size=14, bold=True,
        )
        canvas.text(
            (x + 18, base_y + 40),
            str(entry["label"]),
            color=0xFFE082, size=18, bold=True,
        )
        canvas.text(
            (x + 18, base_y + 76),
            f"+{int(entry['level_bonus'])} AI levels",
            color=0xCCCCCC, size=14,
        )
        canvas.text(
            (x + 18, base_y + 100),
            f"Reward: ${int(entry['reward_usd']):,}",
            color=0xB2FF59, size=14, bold=True,
        )
        canvas.text(
            (x + 18, base_y + 124),
            f"+ {entry['reward_item']}",
            color=0xCE93D8, size=13,
        )
        if active:
            canvas.glyph_token(
                (x + col_w - 60, base_y + 16),
                "NOW",
                color=0xF1C40F, text_color=0x1A1A1A,
                diameter=36, font_size=11,
            )

    canvas.footer("Lose a round and the bracket resets -- come back stronger.")
    return canvas.to_png_bytes()


# ── Helpers ────────────────────────────────────────────────────────────

def _draw_region_band(canvas: RenderCanvas, region_key: str, rect: tuple[int,int,int,int]) -> None:
    r = ARENA_REGIONS.get(region_key, {})
    color = int(r.get("theme_color") or 0x607D8B)
    canvas.rounded_panel(rect, color=0x1B2A33, radius=18, outline=color, outline_width=2)
    canvas.text(
        (rect[0] + 18, rect[1] + 14),
        str(r.get("label") or region_key.title()),
        color=color, size=18, bold=True,
    )
    canvas.text(
        (rect[0] + 18, rect[1] + 40),
        str(r.get("tagline") or ""),
        color=0xB0BEC5, size=12,
    )


def _draw_region_panel(canvas: RenderCanvas, kind: str, rect: tuple[int,int,int,int]) -> None:
    canvas.rounded_panel(rect, color=0x2C1F00, radius=18, outline=0xF1C40F, outline_width=2)
    canvas.text(
        (rect[0] + 18, rect[1] + 14),
        "Champion Hall",
        color=0xF1C40F, size=16, bold=True,
    )
    canvas.text(
        (rect[0] + 18, rect[1] + 40),
        "Clear 3 region bosses",
        color=0xCCCCCC, size=12,
    )


def _draw_edges(
    canvas: RenderCanvas, cleared: set[str], unlocks: set[str], current: str,
) -> None:
    """Edges -- darker when the destination region isn't unlocked yet."""
    drawn: set[tuple[str, str]] = set()
    for src, z in ARENA_ZONES.items():
        if src not in _ZONE_POS:
            continue
        x0, y0 = _ZONE_POS[src]
        for dst in (z.get("neighbors") or []):
            key = tuple(sorted((src, dst)))
            if key in drawn or dst not in _ZONE_POS:
                continue
            drawn.add(key)
            x1, y1 = _ZONE_POS[dst]
            dst_region = ARENA_ZONES.get(dst, {}).get("region")
            ready = dst_region in unlocks or dst_region in ("side", "tournament")
            color = (0xF1C40F if src in cleared and dst in cleared
                     else (0x4FC3F7 if (src == current or dst == current)
                           else (0x546E7A if ready else 0x37474F)))
            canvas.draw.line(((x0, y0), (x1, y1)),
                             fill=hex_to_rgb(color), width=4)


_SPECIAL_KIND_GLYPHS: dict[str, tuple[str, int]] = {
    "shop":   ("$", 0x66BB6A),
    "spring": ("~", 0x4FC3F7),
    "dig":    ("*", 0xFFB300),
    "trader": ("?", 0xBA68C8),
}


def _draw_node(
    canvas: RenderCanvas,
    zone_id: str,
    z: dict,
    pos: tuple[int, int],
    *,
    is_current: bool,
    is_cleared: bool,
    is_neighbor: bool,
    unlocked_region: bool,
    hidden_locked: bool,
) -> None:
    x, y = pos
    r = 34 if z.get("boss") else 28
    region_color = ARENA_REGIONS.get(z.get("region") or "", {}).get("theme_color", 0x607D8B)
    is_special = str(z.get("region") or "") == "special"

    if hidden_locked:
        # Dotted outline question mark for hidden zones
        canvas.draw.ellipse((x - r, y - r, x + r, y + r),
                            outline=hex_to_rgb(0x424242), width=2)
        canvas.text((x - 6, y - 12), "?", color=0x9E9E9E, size=20, bold=True)
        return

    if not unlocked_region and not is_cleared:
        # Region-locked node -- grey
        node_color = 0x37474F
        edge_color = 0x546E7A
    elif is_current:
        node_color = int(region_color)
        edge_color = 0xFFEB3B
        # Halo
        canvas.halo((x - r - 14, y - r - 14, x + r + 14, y + r + 14),
                    color=edge_color, radius=20, alpha=120)
    elif is_cleared:
        node_color = int(region_color)
        edge_color = 0xF1C40F
    elif is_neighbor:
        node_color = int(region_color)
        edge_color = 0x4FC3F7
    else:
        node_color = int(region_color)
        edge_color = 0x546E7A

    # Specials get a square-ish rounded shape to distinguish them from
    # combat nodes at a glance.
    if is_special:
        canvas.draw.rounded_rectangle(
            (x - r, y - r, x + r, y + r),
            radius=10,
            fill=hex_to_rgb(node_color),
            outline=hex_to_rgb(edge_color), width=4,
        )
        kind = str(z.get("kind") or "")
        glyph, glyph_color = _SPECIAL_KIND_GLYPHS.get(kind, ("?", 0xFFFFFF))
        f = font(22, bold=True)
        gw = int(canvas.draw.textlength(glyph, font=f))
        text_with_outline(
            canvas.draw,
            (x - gw // 2, y - 16),
            glyph, font_obj=f,
            fill=hex_to_rgb(glyph_color),
            outline=(0, 0, 0), outline_width=2,
        )
    else:
        canvas.draw.ellipse(
            (x - r, y - r, x + r, y + r),
            fill=hex_to_rgb(node_color), outline=hex_to_rgb(edge_color), width=4,
        )
    # Boss star
    if z.get("boss"):
        _draw_star(canvas.draw, (x, y - 4), 14, hex_to_rgb(0xFFEB3B))

    # Trophy badge on cleared zones (lower-right of node)
    if is_cleared:
        canvas.glyph_token(
            (x + 14, y + r - 12),
            "✓",  # check mark
            color=0xF1C40F, text_color=0x1A1A1A,
            diameter=20, font_size=12,
        )

    # Zone label below node
    label = str(z.get("name") or zone_id)
    f = font(13, bold=True)
    tw = int(canvas.draw.textlength(label, font=f))
    text_with_outline(
        canvas.draw,
        (x - tw // 2, y + r + 8),
        label, font_obj=f, fill=(255, 255, 255),
        outline=(0, 0, 0), outline_width=2,
    )
    # Tier hint
    tier_text = f"L{int(z.get('tier_min') or 1)}-{int(z.get('tier_max') or 5)}"
    tf = font(11)
    ttw = int(canvas.draw.textlength(tier_text, font=tf))
    canvas.draw.text(
        (x - ttw // 2, y + r + 26),
        tier_text, fill=(180, 200, 220), font=tf,
    )


def _draw_legend(canvas: RenderCanvas) -> None:
    """Two-row horizontal legend just below the title.

    Avoids the upper-left corner so the Whispering Forest header
    isn't covered. Items are spaced across the canvas width so the
    legend reads at a glance and never collides with a region band.
    """
    row1 = [
        ("● Current",        0xFFEB3B),
        ("● Travelable",     0x4FC3F7),
        ("● Cleared",        0xF1C40F),
        ("● Locked",         0x546E7A),
        ("● Hidden",         0x424242),
        ("★ Region boss",    0xFFEB3B),
    ]
    row2 = [
        ("$ Shop",     0x66BB6A),
        ("~ Spring",   0x4FC3F7),
        ("* Dig",      0xFFB300),
        ("? Trader",   0xBA68C8),
    ]
    # Place row1 across y=120; row2 across y=148. Start x=44.
    x = 44
    for txt, col in row1:
        canvas.text((x, 120), txt, color=col, size=13, bold=True)
        x += 150
    x = 44
    for txt, col in row2:
        canvas.text((x, 148), txt, color=col, size=13, bold=True)
        x += 140


def _draw_tournament_pill(canvas: RenderCanvas, state: str, champ_count: int) -> None:
    """Tournament state badge in the upper-right."""
    color = {
        "locked":      0x546E7A,
        "qualified":   0x4FC3F7,
        "in_progress": 0xFF7043,
        "champion":    0xF1C40F,
    }.get(state, 0x546E7A)
    label = {
        "locked":      "TOURNAMENT LOCKED",
        "qualified":   "TOURNAMENT READY",
        "in_progress": "BRACKET IN PROGRESS",
        "champion":    f"CHAMPION x{champ_count}" if champ_count else "CHAMPION",
    }.get(state, state.upper())
    canvas.pill_badge((1380, 90), label,
                      color=color, text_color=0x1A1A1A,
                      padding=(14, 8), font_size=14)


def _draw_star(draw: ImageDraw.ImageDraw, center: tuple[int, int], r: int, fill) -> None:
    cx, cy = center
    pts: list[tuple[int, int]] = []
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else int(r * 0.45)
        pts.append((cx + int(rr * math.cos(ang)),
                    cy - int(rr * math.sin(ang))))
    draw.polygon(pts, fill=fill)
