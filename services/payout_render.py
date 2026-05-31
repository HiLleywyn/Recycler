"""V3: Pillow-rendered payout receipts shown by ,daily / ,work / ,fish
catch / ,farm harvest / similar high-traffic commands.

Each function returns ``bytes`` (a PNG) that the cog wraps in
``discord.File`` + ``embed.image("attachment://name.png")``. The
visuals reuse ``core/framework/render.py`` so the palette and font stack
match every other V3 card.
"""
from __future__ import annotations

from typing import Optional, Sequence

from constants.ui import (
    C_CHART_BG,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUBTLE,
    C_SUCCESS,
)
from core.framework.frame_art import draw_frame
from core.framework.render import RenderCanvas
from core.framework.render_primitives import font
from core.framework.sigil_art import draw_sigil


def _cosmetic_theme(
    equipped: Optional[dict],
    fallback_bg: int,
    fallback_accent: int,
) -> dict:
    """Pull banner color / frame ring / sigil glyph + title label from
    the player's equipped cosmetic record so payout receipts share the
    same look as ,level and ,profile.

    Returns ``{bg, accent, frame_color, frame_width, sigil_glyph,
    sigil_color, title_label}``. Any missing slot falls back to the
    caller's defaults.
    """
    from configs.cosmetics_config import BANNERS, FRAMES, SIGILS, TITLES
    eq = dict(equipped or {})
    banner = BANNERS.get(eq.get("banner") or "")
    frame_id = eq.get("frame") or ""
    frame = FRAMES.get(frame_id)
    sigil_id = eq.get("sigil") or ""
    sigil = SIGILS.get(sigil_id)
    title = TITLES.get(eq.get("title") or "")
    return {
        "bg": int(banner["color"]) if banner else fallback_bg,
        "accent": (
            int(banner["accent"])
            if banner and "accent" in banner
            else fallback_accent
        ),
        "frame_id": frame_id,
        "frame_color": int(frame["color"]) if frame else fallback_accent,
        "frame_width": int(frame.get("ring_width", 4)) if frame else 4,
        "sigil_id": sigil_id,
        "sigil_glyph": str(sigil["glyph"])[:1] if sigil else "",
        "sigil_color": int(sigil["color"]) if sigil else fallback_accent,
        "title_label": str(title["label"]) if title else "",
        "title_epithet": str(title["epithet"]) if (title and title.get("epithet")) else "",
    }


def _right_text_x(text: str, right_edge: int, *, size: int, bold: bool) -> int:
    """Measure ``text`` and return the x where it should start so its
    right edge lands at ``right_edge``."""
    f = font(size, bold=bold)
    try:
        bbox = f.getbbox(text)
        w = bbox[2] - bbox[0]
    except Exception:
        w = int(size * 0.6 * len(text))
    return right_edge - w


def render_payout_card(
    *,
    user_name: str,
    avatar_bytes: Optional[bytes] = None,
    title: str = "Payout",
    subtitle: str = "",
    badge_text: str = "",
    badge_color: int = C_INFO,
    accent_color: int = C_GOLD,
    reward_usd: float = 0.0,
    gross_usd: float = 0.0,
    tax_usd: float = 0.0,
    bonus_usd: float = 0.0,
    bonuses: Optional[Sequence[tuple[str, str]]] = None,
    new_wallet_usd: float = 0.0,
    footer: str = "V3 Payout",
    extra_footer: Optional[str] = None,
    equipped: Optional[dict] = None,
) -> bytes:
    """1200x500 payout receipt -- usable by ,daily / ,work / ,ape / ,beg.

    The card has the same shape for every command so players see a
    consistent layout: avatar + badge stamp on the left, gross / tax /
    bonus stat blocks across the top, big net-reward pill in the middle,
    list of applied multipliers, and a wallet headline at the bottom.

    When ``equipped`` is provided, the card themes off the player's
    cosmetic loadout (banner color / accent / frame ring / sigil corner
    / title under name) -- so the daily / work / ape / beg receipts
    visually match the player's ,level and ,profile cards.
    """
    theme = _cosmetic_theme(equipped, fallback_bg=C_NAVY, fallback_accent=accent_color)
    bg = theme["bg"]
    accent = theme["accent"]
    canvas = RenderCanvas(1200, 500, bg=bg, gradient_to=C_CHART_BG)
    canvas.halo((0, 0, 1200, 80), accent, radius=24, alpha=150)
    # Title carries the equipped title (e.g. "Cat Lord", "High Roller")
    # so the player's identity rides every payout receipt.
    _sub = subtitle
    if theme["title_label"]:
        if _sub:
            _sub = f"{theme['title_label']}  -  {_sub}"
        else:
            _sub = theme["title_label"]
    canvas.title(
        f"{title}  -  {user_name}",
        subtitle=_sub,
        color=accent,
    )
    # Avatar shrunk from 140 -> 100 so the net-reward pill (which
    # spans the full card width starting at y=220) doesn't slice
    # through the avatar's bottom edge.
    draw_frame(
        canvas, theme["frame_id"], (60, 110),
        size=100,
        avatar_bytes=avatar_bytes,
        color=theme["frame_color"],
        accent=accent,
        ring_width=theme["frame_width"],
        fallback_color=C_NAVY,
    )
    # Sigil corner-stamp (top right). Procedural silhouette when the
    # sigil id has themed art; legacy glyph-in-disc for unthemed sigils.
    if theme["sigil_id"]:
        _sigil_xy = (1110, 30)
        _sigil_d = 52
        if not draw_sigil(
            canvas, theme["sigil_id"], _sigil_xy,
            diameter=_sigil_d,
            color=theme["sigil_color"],
            accent=accent,
            bg=bg,
        ):
            canvas.glyph_token(
                _sigil_xy, theme["sigil_glyph"] or "?",
                color=theme["sigil_color"], diameter=_sigil_d, font_size=22,
            )
    if badge_text:
        # Slightly smaller badge (44 -> 36) tucked just past the
        # avatar's right edge AND well clear of decorated frame
        # ornaments (which extend ~15 px past the disc on tabby /
        # cards / dice / etc). The previous (200, 110) sat directly
        # on the ring decoration.
        canvas.glyph_token(
            (180, 142), badge_text[:6],
            color=badge_color, diameter=36, font_size=12,
        )
    # Epithet intentionally NOT rendered on payout cards -- the title
    # label is already in the subtitle line and the small card surface
    # has no room for a second-row quote without overlapping either
    # the avatar disc or the stat-block strip. The full epithet stays
    # visible on the larger profile + level cards.

    # Stat blocks. Drop blocks with $0 values so an ,ape loss (which
    # has no gross / tax / bonus, only the wager forfeit) doesn't show
    # three columns of "$0.00" wasted real estate. Collected first so
    # the layout x-positions update with the visible block count.
    _stat_blocks: list[tuple[str, str, int]] = []
    if gross_usd > 0:
        _stat_blocks.append(("GROSS", f"${gross_usd:,.2f}", C_INFO))
    if tax_usd > 0:
        _stat_blocks.append(("-TAX", f"-${tax_usd:,.2f}", 0xe67e22))
    if bonus_usd > 0:
        _stat_blocks.append(("+BONUS", f"+${bonus_usd:,.2f}", C_GOLD))
    # Lay out evenly across the right two-thirds of the card. With 1
    # block fill the space; with 2 or 3 reduce width to fit.
    if _stat_blocks:
        _strip_x0 = 240
        _strip_x1 = 1000
        _strip_w = _strip_x1 - _strip_x0
        _gap = 20
        _n = len(_stat_blocks)
        _block_w = (_strip_w - _gap * (_n - 1)) // _n
        for _i, (_lbl, _val, _col) in enumerate(_stat_blocks):
            _bx = _strip_x0 + _i * (_block_w + _gap)
            canvas.stat_block(
                (_bx, 110), label=_lbl, value=_val,
                color=_col, size=(_block_w, 80),
            )

    # Net reward pill (centred, big)
    pill_color = C_SUCCESS if reward_usd >= 0 else 0xe74c3c
    canvas.rounded_panel(
        (60, 220, 1140, 290), color=pill_color, radius=14,
        outline=accent_color, outline_width=2,
    )
    canvas.text(
        (80, 232),
        "NET REWARD" if reward_usd >= 0 else "NET LOSS",
        color=0x0a2818 if reward_usd >= 0 else 0xFFFFFF,
        size=14, bold=True,
    )
    net_str = (
        f"+${reward_usd:,.2f}" if reward_usd >= 0
        else f"-${abs(reward_usd):,.2f}"
    )
    net_color = 0x0a2818 if reward_usd >= 0 else 0xFFFFFF
    net_x = _right_text_x(net_str, 1140 - 8, size=28, bold=True)
    canvas.text((net_x, 232), net_str, color=net_color, size=28, bold=True)

    # Applied multipliers list
    y = 310
    canvas.text(
        (60, y), "APPLIED MULTIPLIERS",
        color=C_SUBTLE, size=12, bold=True,
    )
    y += 22
    if bonuses:
        for label, value in list(bonuses)[:5]:
            canvas.text((80, y), f"-  {label}", color=0xBFC7D5, size=14)
            vx = _right_text_x(value, 1140 - 8, size=14, bold=False)
            canvas.text((vx, y), value, color=C_GOLD, size=14)
            y += 22
    else:
        canvas.text((80, y), "-  No multipliers applied", color=C_SUBTLE, size=12)
        y += 22

    # Wallet headline
    canvas.divider(440)
    canvas.text(
        (60, 455), "WALLET",
        color=C_SUBTLE, size=12, bold=True,
    )
    w_str = f"${new_wallet_usd:,.2f}"
    wx = _right_text_x(w_str, 1140 - 8, size=18, bold=True)
    canvas.text((wx, 450), w_str, color=C_INFO, size=18, bold=True)
    if extra_footer:
        canvas.text(
            (60, 478), extra_footer,
            color=C_PURPLE, size=10,
        )
    canvas.footer(footer)
    return canvas.to_png_bytes()


# Backwards-compatible wrapper kept for the daily flow that called this
# name directly. Same behaviour as render_payout_card with daily defaults.
def render_daily_card(
    *,
    user_name: str,
    avatar_bytes: Optional[bytes] = None,
    streak_days: int = 0,
    reward_usd: float = 0.0,
    gross_usd: float = 0.0,
    tax_usd: float = 0.0,
    bonus_usd: float = 0.0,
    bonuses: Optional[Sequence[tuple[str, str]]] = None,
    new_wallet_usd: float = 0.0,
    next_milestone: Optional[tuple[int, str]] = None,
    equipped: Optional[dict] = None,
) -> bytes:
    badge_color = (
        C_GOLD if streak_days >= 30 else
        (C_PURPLE if streak_days >= 7 else C_INFO)
    )
    extra = None
    if next_milestone is not None:
        days_to, label = next_milestone
        extra = (
            f"Next milestone: {label} "
            f"(in {days_to} day{'s' if days_to != 1 else ''})"
        )
    return render_payout_card(
        user_name=user_name,
        avatar_bytes=avatar_bytes,
        title="Daily Claim",
        subtitle=f"Streak: {streak_days} day{'s' if streak_days != 1 else ''}",
        badge_text=str(min(streak_days, 999)),
        badge_color=badge_color,
        accent_color=C_GOLD,
        reward_usd=reward_usd,
        gross_usd=gross_usd,
        tax_usd=tax_usd,
        bonus_usd=bonus_usd,
        bonuses=bonuses,
        new_wallet_usd=new_wallet_usd,
        footer="V3 Daily Claim",
        extra_footer=extra,
        equipped=equipped,
    )
