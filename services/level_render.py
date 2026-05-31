"""V3: Pillow-rendered ``,level`` card.

    render_level_card(*, user_name, avatar_bytes, level, rank_name,
                      total_xp, level_floor_xp, level_next_xp,
                      messages, streak_days, position,
                      equipped) -> bytes

Composes against ``core/framework/render.py`` so it inherits the project
palette and font cache. The equipped cosmetics (banner / frame / sigil
/ title) carry over from the profile card so the player's chat-level
view feels of-a-piece with their identity.
"""
from __future__ import annotations

from typing import Mapping, Optional

from constants.ui import (
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUCCESS,
)
from configs.cosmetics_config import BANNERS, FRAMES, SIGILS, TITLES
from core.framework.frame_art import draw_frame
from core.framework.render import RenderCanvas
from core.framework.sigil_art import draw_sigil


def render_level_card(
    *,
    user_name: str,
    avatar_bytes: Optional[bytes] = None,
    level: int = 0,
    rank_name: str | None = None,
    total_xp: int = 0,
    level_floor_xp: int = 0,
    level_next_xp: int = 0,
    messages: int = 0,
    streak_days: int = 0,
    position: int | None = None,
    equipped: Mapping[str, str] | None = None,
) -> bytes:
    """1200x440 chat-level card with cosmetic-aware accents."""
    eq = dict(equipped or {})
    # System defaults: black ("obsidian") banner, neutral frame, star
    # sigil. Title is left UNSET so the card shows the player's rank
    # rather than a generic "Novice" if nothing custom is equipped.
    banner = BANNERS.get(eq.get("banner") or "obsidian", BANNERS["obsidian"])
    frame = FRAMES.get(eq.get("frame") or "simple", FRAMES["simple"])
    sigil = SIGILS.get(eq.get("sigil") or "star", SIGILS["star"])
    _title_key = eq.get("title") or ""
    title = TITLES.get(_title_key) if _title_key else None
    _title_label = (title["label"] if title else rank_name) or "Player"

    canvas = RenderCanvas(
        1200, 440,
        bg=int(banner["color"]),
        gradient_to=int(banner.get("accent", banner["color"])),
    )
    accent = int(banner.get("accent", C_GOLD))

    # Header bar
    canvas.halo((0, 0, 1200, 70), accent, radius=24, alpha=160)
    canvas.title(
        f"Chat Level  -  {user_name}",
        subtitle=_title_label,
        color=accent,
    )

    # Avatar with framed ring + per-frame decorator (claw / halo /
    # chain / pips / facets) from core/framework/frame_art.
    _frame_id = eq.get("frame") or "simple"
    draw_frame(
        canvas, _frame_id, (60, 110),
        size=160,
        avatar_bytes=avatar_bytes,
        color=int(frame["color"]),
        accent=accent,
        ring_width=int(frame.get("ring_width", 4)),
        fallback_color=C_NAVY,
    )

    # Sigil corner stamp -- procedural silhouette when themed,
    # legacy glyph-in-disc otherwise.
    _sigil_id = eq.get("sigil") or "star"
    _sigil_xy = (1110, 30)
    _sigil_d = 52
    if not draw_sigil(
        canvas, _sigil_id, _sigil_xy,
        diameter=_sigil_d,
        color=int(sigil["color"]),
        accent=accent,
        bg=int(banner["color"]),
    ):
        canvas.glyph_token(
            _sigil_xy, sigil.get("glyph", "?")[:1],
            color=int(sigil["color"]),
            diameter=_sigil_d, font_size=22,
        )

    # Title epithet line, if any -- small italic-style flavour quote.
    if title is not None and title.get("epithet"):
        canvas.text(
            (270, 92), f"“{str(title['epithet'])[:80]}”",
            color=0xBFC7D5, size=13, bold=False,
        )

    # Level + rank stat blocks (right of avatar)
    canvas.stat_block(
        (270, 110), label="LEVEL",
        value=str(int(level)),
        color=C_GOLD, size=(180, 76),
    )
    canvas.stat_block(
        (470, 110), label="RANK",
        value=(rank_name or "-")[:16],
        color=C_PURPLE, size=(280, 76),
    )
    canvas.stat_block(
        (770, 110), label="LEADERBOARD",
        value=(f"#{position}" if position is not None else "Unranked"),
        color=C_INFO, size=(220, 76),
    )

    # Progress bar
    into = max(0, int(total_xp) - int(level_floor_xp))
    needed = max(1, int(level_next_xp) - int(level_floor_xp))
    frac = max(0.0, min(1.0, into / needed))
    canvas.text(
        (270, 200), "PROGRESS TO NEXT LEVEL",
        color=accent, size=12, bold=True,
    )
    canvas.progress_bar(
        (270, 220, 1140, 252), frac, color=C_SUCCESS,
        label=f"{into:,} / {needed:,} XP  ({frac * 100:.1f}%)",
    )

    # Bottom stat strip
    canvas.divider(290)
    canvas.stat_block(
        (60, 310), label="TOTAL XP",
        value=f"{int(total_xp):,}",
        color=C_GOLD, size=(260, 76),
    )
    canvas.stat_block(
        (340, 310), label="MESSAGES",
        value=f"{int(messages):,}",
        color=C_INFO, size=(260, 76),
    )
    canvas.stat_block(
        (620, 310), label="STREAK",
        value=f"{int(streak_days)}d",
        color=C_PURPLE, size=(260, 76),
    )
    canvas.footer("Chat Level")
    return canvas.to_png_bytes()
