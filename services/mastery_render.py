"""V3 Pillar 2 -- Pillow renderer for the Apex Mastery board.

Two public entry points:

    render_mastery_board(summary, *, display_name) -> bytes
        Full 1200x900 board showing all nine track bars + the node tree.
    render_track_levelup(track, summary) -> bytes
        Tight 800x300 card celebrating a track level-up. Used by inbox
        and DM notifications.

The node tree uses a hand-laid coordinate map keyed by branch so the
art lives in code instead of an external sprite. Branches are coloured
distinctly so a player skimming the board sees the four categories at
a glance.
"""
from __future__ import annotations


from constants.ui import (
    C_CHART_BG,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUCCESS,
    C_TEAL,
)
from core.framework.render import RenderCanvas
from configs.mastery_config import NODES, TRACKS


_BRANCH_COLOR = {
    "economy": C_GOLD,
    "combat":  C_ERROR,
    "luck":    C_PURPLE,
    "utility": C_TEAL,
}


def render_mastery_board(summary, *, display_name: str = "Player") -> bytes:
    """Full mastery board PNG.

    Canvas is sized 1200x1280 since the Buddy Battles expansion bumped
    the node tree from 20 to 38 nodes (utility = 11 nodes in the
    tallest column). Card heights and per-card description line counts
    were tightened so the tree still scans cleanly at a glance.
    """
    canvas = RenderCanvas(1200, 1280, bg=C_NAVY, gradient_to=C_CHART_BG)
    canvas.title(
        f"Apex Mastery  -  {display_name}",
        subtitle=(
            f"{summary.points_available} pts available  -  "
            f"{summary.points_spent} spent  -  "
            f"{len(summary.unlocked)} / {len(NODES)} nodes unlocked"
        ),
        color=C_GOLD,
    )

    # Branch totals legend row immediately under the subtitle
    _render_branch_totals(canvas, summary)

    # Left column: track bars (50-460 x 130-x).
    _render_track_bars(canvas, summary)

    # Right column: node grid (490-1160 x 130-x).
    _render_node_tree(canvas, summary)

    canvas.footer("Apex Mastery")
    return canvas.to_png_bytes()


def _render_branch_totals(canvas: RenderCanvas, summary) -> None:
    """Tiny 'Economy 6/9  Combat 3/10  ...' summary row under the title."""
    y = 86
    x = 50
    parts: list[tuple[str, int, int, int]] = []
    for branch in ("economy", "combat", "luck", "utility"):
        nodes = [n for n in NODES if n["branch"] == branch]
        owned = sum(1 for n in nodes if n["id"] in summary.unlocked)
        parts.append((branch.title(), owned, len(nodes), _BRANCH_COLOR[branch]))
    for label, owned, total, color in parts:
        rect = canvas.pill_badge(
            (x, y), f"{label}  {owned}/{total}",
            color=color, text_color=0x1A1A1A,
            padding=(10, 4), font_size=12,
        )
        x = rect[2] + 10


def _render_track_bars(canvas: RenderCanvas, summary) -> None:
    track_keys = list(TRACKS.keys())
    row_h = 76
    x0 = 50
    y0 = 156
    canvas.text((x0, y0 - 22), "TRACKS", color=C_INFO, size=14, bold=True)
    for i, key in enumerate(track_keys):
        meta = TRACKS[key]
        info = summary.tracks.get(key, {"level": 1, "progress": 0.0, "xp": 0})
        y = y0 + i * row_h
        # Card background
        canvas.rounded_panel((x0, y, x0 + 410, y + row_h - 8),
                             color=C_CHART_BG, radius=10)
        # Emoji + name
        canvas.text((x0 + 14, y + 8), f"{meta['emoji']}  {meta['label']}",
                    color=0xDDE2EB, size=16, bold=True)
        # Level pill
        canvas.pill_badge(
            (x0 + 320, y + 8),
            f"L{info['level']}",
            color=_level_color(info["level"]),
        )
        # Progress bar to next level
        canvas.progress_bar(
            (x0 + 14, y + 42, x0 + 396, y + 56),
            float(info.get("progress", 0.0)),
            color=C_SUCCESS,
        )


def _render_node_tree(canvas: RenderCanvas, summary) -> None:
    x0 = 490
    y0 = 156
    canvas.text((x0, y0 - 22), "SKILL NODES", color=C_INFO, size=14, bold=True)
    # Group by branch and lay out as 4 columns of cards. The expansion
    # bumped the largest column (utility) from 5 to 11 nodes so cards
    # are smaller; 2-line wrapped description fits without overflow.
    cols = ["economy", "combat", "luck", "utility"]
    col_w = 170
    col_gap = 6
    card_h = 90
    pad_x = 10
    title_size = 12
    desc_size = 10
    desc_line_h = 12
    max_desc_lines = 3
    for c, branch in enumerate(cols):
        col_x = x0 + c * (col_w + col_gap)
        canvas.text(
            (col_x, y0), branch.upper(),
            color=_BRANCH_COLOR[branch], size=12, bold=True,
        )
        nodes = [n for n in NODES if n["branch"] == branch]
        for r, node in enumerate(nodes):
            y = y0 + 22 + r * (card_h + 8)
            owned = node["id"] in summary.unlocked
            color = _BRANCH_COLOR[branch] if owned else C_CHART_BG
            ring = _BRANCH_COLOR[branch] if owned else 0x4a5260
            canvas.rounded_panel(
                (col_x, y, col_x + col_w, y + card_h),
                color=color if owned else C_CHART_BG, radius=10,
                outline=ring, outline_width=2,
            )
            # Cost pill (drawn first so we know its width and can clamp the name)
            cost_rect = canvas.pill_badge(
                (col_x + col_w - 36, y + 8),
                f"{node['cost']}",
                color=C_GOLD if owned else 0x4a5260,
                font_size=10,
            )
            # Name (truncate with ellipsis to whatever fits between left
            # padding and the cost pill).
            name_max_w = (cost_rect[0] - 6) - (col_x + pad_x)
            name_text = _fit_with_ellipsis(
                canvas, node["name"], size=title_size, bold=True,
                max_width=name_max_w,
            )
            canvas.text(
                (col_x + pad_x, y + 8), name_text,
                color=0xFFFFFF if owned else 0xBFC7D5,
                size=title_size, bold=True,
            )
            # Description -- word-wrap against the actual card width.
            desc_max_w = col_w - 2 * pad_x
            wrapped = _wrap_to_width(
                canvas, node["description"],
                size=desc_size, max_width=desc_max_w,
                max_lines=max_desc_lines,
            )
            desc_y = y + 30
            for line in wrapped:
                canvas.text(
                    (col_x + pad_x, desc_y), line,
                    color=0xDDE2EB if owned else 0x8E96A4,
                    size=desc_size,
                )
                desc_y += desc_line_h
            # Owned check mark
            if owned:
                canvas.text((col_x + pad_x, y + card_h - 14), "UNLOCKED",
                            color=C_SUCCESS, size=9, bold=True)


def _wrap_to_width(
    canvas: RenderCanvas,
    text: str,
    *,
    size: int,
    max_width: int,
    max_lines: int = 4,
    bold: bool = False,
) -> list[str]:
    """Greedy word-wrap against the actual pixel width of the rendered glyphs.

    Falls back to per-character wrapping for tokens that exceed ``max_width``
    on their own (rare, but happens with backtick-delimited command names like
    ``\\`,daily\\```). Truncates with an ellipsis once ``max_lines`` is hit so
    long descriptions don't bleed off the card.
    """
    from core.framework.render_primitives import font as _font

    if not text:
        return []
    f = _font(size, bold=bold)
    lines: list[str] = []
    cur = ""
    words = text.split()
    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        candidate = (cur + " " + word).strip() if cur else word
        if int(canvas.draw.textlength(candidate, font=f)) <= max_width:
            cur = candidate
            i += 1
            continue
        if cur:
            lines.append(cur)
            cur = ""
            continue
        # Word alone is wider than the line -- hard-split on chars.
        cut = len(word)
        while cut > 1 and int(canvas.draw.textlength(word[:cut], font=f)) > max_width:
            cut -= 1
        lines.append(word[:cut])
        words[i] = word[cut:]
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words) and lines:
        # We had leftovers -- ellipsis the last visible line.
        last = lines[-1]
        ellipsis = "..."
        while (
            last
            and int(canvas.draw.textlength(last + ellipsis, font=f)) > max_width
        ):
            last = last[:-1]
        lines[-1] = (last + ellipsis).rstrip()
    return lines


def _fit_with_ellipsis(
    canvas: RenderCanvas,
    text: str,
    *,
    size: int,
    max_width: int,
    bold: bool = False,
) -> str:
    """Return ``text`` truncated with ``...`` if it would overflow ``max_width``."""
    from core.framework.render_primitives import font as _font

    f = _font(size, bold=bold)
    if int(canvas.draw.textlength(text, font=f)) <= max_width:
        return text
    ellipsis = "..."
    cut = len(text)
    while cut > 0 and int(canvas.draw.textlength(text[:cut] + ellipsis, font=f)) > max_width:
        cut -= 1
    return (text[:cut] + ellipsis) if cut > 0 else ellipsis


def _level_color(level: int) -> int:
    if level >= 80:
        return C_GOLD
    if level >= 50:
        return C_PURPLE
    if level >= 25:
        return C_INFO
    return C_TEAL


def render_track_levelup(track: str, level: int, *, display_name: str) -> bytes:
    """Tight celebration card for a track level-up. 800x300."""
    canvas = RenderCanvas(800, 300, bg=C_NAVY, gradient_to=C_CHART_BG)
    meta = TRACKS.get(track, {"label": track, "emoji": "⭐"})
    canvas.title(
        f"{meta['emoji']}  Apex Mastery -- {meta['label']} L{level}",
        subtitle=f"Level up for {display_name}",
        color=C_GOLD,
    )
    canvas.rounded_panel((40, 110, 760, 270), color=C_CHART_BG, radius=14)
    canvas.text(
        (60, 140),
        f"{meta['label']} reached level {level}.",
        color=0xDDE2EB, size=20, bold=True,
    )
    canvas.text(
        (60, 180),
        f"+{level - 1} mastery point banked. "
        f"Unlock a node with `,mastery unlock <id>`.",
        color=0xBFC7D5, size=14,
    )
    return canvas.to_png_bytes()
