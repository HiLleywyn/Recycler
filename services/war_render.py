"""V3 Pillar 3: Pillow war-map renderer.

    render_war_map(match, nodes, *, group_a_name, group_b_name,
                   time_remaining_sec) -> bytes

Returns a 1600x900 PNG with the 12-node board, scoreline at the top,
per-node fill bars, and a time-remaining banner.
"""
from __future__ import annotations

from typing import Sequence

from constants.ui import (
    C_AMBER,
    C_CHART_BG,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_SUCCESS,
)
from core.framework.render import RenderCanvas
from services.clan_wars import NODES


# Hand-laid node coordinates so the board feels like a map, not a list.
_NODE_COORDS: dict[str, tuple[int, int]] = {
    "mine":       (200, 240),
    "vault":      (450, 240),
    "bazaar":     (700, 240),
    "forge":      (950, 240),
    "lighthouse": (1200, 240),
    "reef":       (1400, 320),
    "grove":      (200, 500),
    "crypt":      (450, 500),
    "spire":      (700, 500),
    "orchard":    (950, 500),
    "forum":      (1200, 500),
    "apex":       (800, 700),
}


def render_war_map(
    match: dict,
    nodes: Sequence[dict],
    *,
    group_a_name: str = "Group A",
    group_b_name: str = "Group B",
    time_remaining_sec: int = 0,
) -> bytes:
    canvas = RenderCanvas(1600, 900, bg=C_NAVY, gradient_to=C_CHART_BG)
    # Header
    a_total = sum(int(n.get("a_score") or 0) for n in nodes)
    b_total = sum(int(n.get("b_score") or 0) for n in nodes)
    a_owns = sum(
        1 for n in nodes
        if int(n.get("a_score") or 0) > int(n.get("b_score") or 0)
    )
    b_owns = sum(
        1 for n in nodes
        if int(n.get("b_score") or 0) > int(n.get("a_score") or 0)
    )
    canvas.title(
        f"Apex Conflict  -  {group_a_name} vs {group_b_name}",
        subtitle=(
            f"{group_a_name}: {a_owns} nodes / {a_total:,} pts  -  "
            f"{group_b_name}: {b_owns} nodes / {b_total:,} pts"
        ),
        color=C_GOLD,
    )
    # Time-remaining strip
    canvas.rounded_panel((40, 100, 1560, 130), color=C_CHART_BG, radius=8)
    canvas.text(
        (60, 106),
        f"Time remaining: {_format_remaining(time_remaining_sec)}",
        color=C_AMBER, size=14, bold=True,
    )
    # Map panel
    canvas.rounded_panel((40, 150, 1560, 850), color=C_CHART_BG, radius=14)

    score_by_node = {str(n["node_id"]): n for n in nodes}
    for node in NODES:
        nid = node["id"]
        x, y = _NODE_COORDS.get(nid, (200, 200))
        score = score_by_node.get(nid, {"a_score": 0, "b_score": 0})
        a = int(score.get("a_score") or 0)
        b = int(score.get("b_score") or 0)
        # Owner ring color
        if a > b:
            ring = C_SUCCESS
        elif b > a:
            ring = C_ERROR
        else:
            ring = C_INFO
        # Node disc
        canvas.draw.ellipse(
            (x - 60, y - 60, x + 60, y + 60),
            fill=_to_rgb(C_NAVY), outline=_to_rgb(ring), width=4,
        )
        # Label inside
        canvas.text(
            (x - 36, y - 32), node["label"][:12],
            color=0xFFFFFF, size=14, bold=True,
        )
        # Scoreline
        canvas.text(
            (x - 36, y - 10),
            f"A {a:,}",
            color=C_SUCCESS, size=12, bold=True,
        )
        canvas.text(
            (x - 36, y + 8),
            f"B {b:,}",
            color=C_ERROR, size=12, bold=True,
        )
        # Weight emblem on Apex
        if node.get("weight", 1.0) > 1.0:
            canvas.pill_badge(
                (x + 28, y - 60), f"x{node['weight']:.1f}",
                color=C_GOLD, font_size=10, padding=(6, 3),
            )
    # Bottom legend
    canvas.pill_badge((60, 860), group_a_name[:18], color=C_SUCCESS)
    canvas.pill_badge((220, 860), group_b_name[:18], color=C_ERROR)
    canvas.pill_badge((380, 860), "Apex weight x3", color=C_GOLD)
    return canvas.to_png_bytes()


def _to_rgb(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _format_remaining(secs: int) -> str:
    if secs <= 0:
        return "ended"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"
