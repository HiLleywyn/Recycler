"""V3 Pillar 4: Pillow profile card (1200x600).

    render_profile_card(*, user_name, avatar_bytes, equipped,
                        net_worth_usd, mastery_summary,
                        season_rank=None, clan_war_scoreline=None,
                        badges=()) -> bytes

The profile card is the player's identity surface. It composites the
equipped Banner (background tint), Frame (avatar ring), Sigil (corner
emblem), and Title (suffix under name). Net worth, mastery sparkline,
and any season/war context surface as stat blocks on the right.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from constants.ui import (
    C_CHART_BG,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_SUBTLE,
    C_SUCCESS,
)
from configs.cosmetics_config import BANNERS, FRAMES, SIGILS, TITLES
from core.framework.frame_art import draw_frame
from core.framework.render import RenderCanvas
from core.framework.render_primitives import hex_to_rgb as _rgb
from core.framework.sigil_art import draw_sigil


def _fit_to_width(
    canvas: RenderCanvas,
    text: str,
    *,
    size: int,
    max_width: int,
    bold: bool = False,
) -> str:
    """Return ``text`` truncated with ``...`` if it would overflow ``max_width``
    at the rendered font size.

    Pixel-accurate via ``ImageDraw.textlength`` so a long display_name
    doesn't spill past panel edges regardless of glyph mix. Local helper
    so the render module doesn't depend on core.framework.sigil_art.
    """
    from core.framework.render_primitives import font as _font
    f = _font(size, bold=bold)
    if int(canvas.draw.textlength(text, font=f)) <= max_width:
        return text
    ellipsis = "..."
    cut = len(text)
    while cut > 0 and int(canvas.draw.textlength(text[:cut] + ellipsis, font=f)) > max_width:
        cut -= 1
    return (text[:cut].rstrip() + ellipsis) if cut > 0 else ellipsis


def render_profile_card(
    *,
    user_name: str,
    avatar_bytes: Optional[bytes] = None,
    equipped: Mapping[str, str] | None = None,
    net_worth_usd: float = 0.0,
    mastery_summary: dict | None = None,
    season_rank: int | None = None,
    clan_war_scoreline: str | None = None,
    badges: Iterable[str] = (),
    job_title: str | None = None,
    job_level: int | None = None,
    chat_level: int | None = None,
    chat_rank: str | None = None,
    streak_days: int | None = None,
    achievements_unlocked: int | None = None,
    days_since_join: int | None = None,
    top_mastery_tracks: Iterable[tuple[str, int]] = (),
    favorite_game: str | None = None,
) -> bytes:
    eq = dict(equipped or {})
    # System defaults: black ("obsidian") banner, neutral frame + star
    # sigil. Title is intentionally NOT defaulted -- when the player
    # hasn't equipped a custom title we fall through to their job +
    # level under the name instead of forcing a generic "Novice" label.
    banner = BANNERS.get(eq.get("banner") or "obsidian", BANNERS["obsidian"])
    frame = FRAMES.get(eq.get("frame") or "simple", FRAMES["simple"])
    sigil = SIGILS.get(eq.get("sigil") or "star", SIGILS["star"])
    _title_key = eq.get("title") or ""
    title = TITLES.get(_title_key) if _title_key else None

    canvas = RenderCanvas(
        1200, 720,
        bg=int(banner["color"]),
        gradient_to=int(banner.get("accent", banner["color"])),
    )
    # V3 polish: render the banner's pixel-art pattern (stars/moon/sun/
    # trees/waves/pirate_ship/cats/cards) onto the banner backdrop
    # before the rest of the card draws on top.
    try:
        from core.framework.banner_patterns import draw_pattern
        draw_pattern(
            canvas.draw,
            banner.get("pattern"),
            (0, 0, 1200, 720),
            int(banner.get("accent", banner["color"])),
        )
    except Exception:
        pass
    # Header strip with accent halo
    canvas.halo((0, 0, 1200, 80), int(banner.get("accent", banner["color"])),
                radius=24, alpha=160)

    # Avatar disc (left of card) with the framed ring + per-frame
    # decorator (claw marks, halo, chain links, etc.) from
    # core/framework/frame_art.py.
    _frame_id = eq.get("frame") or "simple"
    draw_frame(
        canvas,
        _frame_id,
        (60, 100),
        size=180,
        avatar_bytes=avatar_bytes,
        color=int(frame["color"]),
        accent=int(banner.get("accent", frame.get("color", C_GOLD))),
        ring_width=int(frame.get("ring_width", 4)),
        fallback_color=C_NAVY,
    )
    # Name + title block to the right of the avatar. Pixel-clamp the
    # rendered width so a long display_name doesn't run past the right
    # edge of the card (the previous ``[:40]`` slice was a char count,
    # not a width measurement, and a long-but-narrow-glyph name still
    # spilled past the stats panel).
    _name_text = _fit_to_width(
        canvas, user_name, size=42, bold=True, max_width=720,
    )
    canvas.text((280, 110), _name_text, color=0xFFFFFF, size=42, bold=True, outline=True)
    # Subtitle priority: custom equipped title > job + level > "Player"
    if title is not None and title.get("label"):
        _sub_label = str(title["label"])[:60]
    elif job_title is not None and job_level is not None:
        _sub_label = f"{str(job_title)[:40]}  -  Level {int(job_level)}"
    elif job_title is not None:
        _sub_label = str(job_title)[:60]
    else:
        _sub_label = "Player"
    canvas.text(
        (280, 170), _sub_label,
        color=int(banner.get("accent", C_GOLD)), size=20, bold=False,
    )
    # Title epithet -- a one-line flavour quote, only when an actual
    # title is equipped. Keeps a Job/Level subtitle clean for players
    # without a title set.
    if title is not None and title.get("epithet"):
        canvas.text(
            (280, 200), f"“{str(title['epithet'])[:90]}”",
            color=0xBFC7D5, size=15, bold=False,
        )

    # Sigil stamp in the top-right corner -- procedural silhouette
    # when the sigil has a themed drawing, otherwise fall through to
    # the legacy "letter in a disc" so unthemed sigils still render.
    _sigil_id = eq.get("sigil") or "star"
    _sigil_xy = (1100, 50)
    _sigil_d = 64
    _sigil_accent = int(banner.get("accent", 0xFFFFFF))
    if not draw_sigil(
        canvas, _sigil_id, _sigil_xy,
        diameter=_sigil_d,
        color=int(sigil["color"]),
        accent=_sigil_accent,
        bg=int(banner["color"]),
    ):
        canvas.glyph_token(
            _sigil_xy, sigil.get("glyph", "?")[:1],
            color=int(sigil["color"]),
            diameter=_sigil_d, font_size=24,
        )

    # Stats panel (right side)
    canvas.rounded_panel((680, 250, 1160, 540), color=C_CHART_BG, radius=14)
    canvas.text((700, 260), "PROFILE STATS", color=C_INFO, size=12, bold=True)
    canvas.stat_block(
        (700, 286), label="NET WORTH",
        value=f"${net_worth_usd:,.0f}",
        color=C_GOLD, size=(440, 76),
    )
    # Mastery summary line. Every track defaults to L1 in the DB, so
    # an untouched track that hasn't been seeded into the summary
    # ``tracks`` dict still counts as L1 -- otherwise a player with
    # one big-mastery track and the rest unseeded sees their sum as
    # just the top track's level instead of "top + 8 baseline L1s".
    if mastery_summary:
        from configs.mastery_config import TRACKS as _ALL_TRACKS
        _summary_tracks = mastery_summary.get("tracks", {}) or {}
        total_lvl = 0
        for _key in _ALL_TRACKS:
            _row = _summary_tracks.get(_key) or {}
            total_lvl += max(1, int(_row.get("level") or 1))
        nodes = mastery_summary.get("unlocked_count", 0)
        canvas.stat_block(
            (700, 374), label="MASTERY",
            value=f"L{total_lvl} sum  -  {nodes} nodes",
            color=C_SUCCESS, size=(440, 76),
        )

    # Bottom-left context: season + war
    info_y = 290
    if season_rank is not None:
        canvas.rounded_panel((60, info_y, 640, info_y + 70),
                             color=C_CHART_BG, radius=10)
        canvas.text((78, info_y + 10), "SEASON RANK",
                    color=C_INFO, size=12, bold=True)
        canvas.text((78, info_y + 30), f"#{season_rank}",
                    color=C_GOLD, size=26, bold=True)
        info_y += 86
    if clan_war_scoreline:
        canvas.rounded_panel((60, info_y, 640, info_y + 70),
                             color=C_CHART_BG, radius=10)
        canvas.text((78, info_y + 10), "CLAN WAR",
                    color=C_INFO, size=12, bold=True)
        canvas.text((78, info_y + 30), clan_war_scoreline[:50],
                    color=0xDDE2EB, size=18)

    # V3 polish: "Player details" strip across the bottom -- chat
    # level / rank, job & level, daily streak, achievements, days
    # joined, favorite minigame, top mastery tracks. Anything not
    # supplied is silently skipped so a brand-new player's card
    # doesn't render a wall of zeros.
    canvas.rounded_panel((60, 550, 1160, 680), color=C_CHART_BG, radius=14)
    canvas.text((78, 562), "PLAYER DETAILS",
                color=C_INFO, size=12, bold=True)
    detail_pairs: list[tuple[str, str]] = []
    if job_title is not None and job_level is not None:
        detail_pairs.append(("Job", f"{job_title} -- Lv {int(job_level)}"))
    if chat_level is not None:
        _rank = f" ({chat_rank})" if chat_rank else ""
        detail_pairs.append(("Chat Level", f"Lv {int(chat_level)}{_rank}"))
    if streak_days is not None:
        detail_pairs.append((
            "Daily Streak",
            f"{int(streak_days)} day{'s' if streak_days != 1 else ''}",
        ))
    if achievements_unlocked is not None:
        detail_pairs.append(("Achievements", f"{int(achievements_unlocked)}"))
    if days_since_join is not None:
        detail_pairs.append(("Days Played", f"{int(days_since_join)}"))
    if favorite_game:
        detail_pairs.append(("Favorite Game", favorite_game[:24]))
    top_tracks = list(top_mastery_tracks)[:3]
    if top_tracks:
        detail_pairs.append((
            "Top Mastery",
            ", ".join(f"{n} L{lvl}" for n, lvl in top_tracks),
        ))
    # Lay them out in a 2-column grid. Tightened row spacing 28 -> 23
    # so up to 8 pairs (4 rows) fit cleanly inside the 130 px panel
    # without clipping the bottom row against the panel edge.
    col_x = [78, 620]
    row_y = 586
    row_h = 23
    for i, (label, value) in enumerate(detail_pairs[:8]):
        x = col_x[i % 2]
        y = row_y + (i // 2) * row_h
        canvas.text((x, y), f"{label.upper()}:",
                    color=C_SUBTLE, size=11, bold=True)
        canvas.text((x + 132, y - 2), value[:42],
                    color=0xFFFFFF, size=14, bold=False)

    # Badge row above the details panel
    badge_x = 60
    badge_y = 510
    for badge in list(badges)[:8]:
        rect = canvas.pill_badge(
            (badge_x, badge_y), badge[:16],
            color=int(banner.get("accent", C_GOLD)),
            font_size=11, padding=(10, 5),
        )
        badge_x = rect[2] + 8

    canvas.footer("V3 Profile")
    return canvas.to_png_bytes()


def render_gallery(
    inventory: Mapping[str, list[str]],
    *,
    user_name: str = "Player",
) -> bytes:
    """Grid view of every owned cosmetic across all slots. 1000x800."""
    canvas = RenderCanvas(1000, 800, bg=C_NAVY, gradient_to=C_CHART_BG)
    canvas.title(
        f"Cosmetic Gallery  -  {user_name}",
        subtitle="Equip via `,profile equip <slot> <id>`",
        color=C_GOLD,
    )
    y = 130
    for slot, ids in inventory.items():
        canvas.text((40, y), slot.upper(), color=C_INFO, size=12, bold=True)
        x = 40
        for cid in ids:
            label_color = _slot_color(slot, cid)
            rect = canvas.pill_badge(
                (x, y + 22), cid[:20],
                color=label_color,
                font_size=12, padding=(12, 6),
            )
            x = rect[2] + 10
            if x > 920:
                y += 50
                x = 40
        y += 70
        if y > 720:
            break
    canvas.footer(f"{sum(len(v) for v in inventory.values())} owned across {len(inventory)} slots")
    return canvas.to_png_bytes()


def _slot_color(slot: str, item_id: str) -> int:
    """Pick a representative color for an inventory pill."""
    from configs.cosmetics_config import BANNERS, FRAMES, SIGILS
    if slot == "banner":
        return int(BANNERS.get(item_id, {}).get("accent", C_INFO))
    if slot == "frame":
        return int(FRAMES.get(item_id, {}).get("color", C_INFO))
    if slot == "sigil":
        return int(SIGILS.get(item_id, {}).get("color", C_INFO))
    return C_GOLD


_RARITY_COLOR = {
    "common":    0x95a5a6,   # gray
    "rare":      0x3498db,   # blue
    "epic":      0x9b59b6,   # purple
    "legendary": 0xf1c40f,   # gold
}


def _rarity_of(entry: dict) -> str:
    r = str(entry.get("rarity") or "").lower()
    if r in _RARITY_COLOR:
        return r
    price = float(entry.get("price_usd") or 0)
    if price >= 30000:
        return "legendary"
    if price >= 10000:
        return "epic"
    if price >= 3000:
        return "rare"
    return "common"


def shop_paginate(
    listings: list[dict],
    *,
    page: int = 1,
    per_page: int = 12,
) -> tuple[list[dict], int, int]:
    """Return (slice, page, total_pages). Page is 1-indexed."""
    total = max(1, (len(listings) + per_page - 1) // per_page)
    page = max(1, min(int(page), total))
    start = (page - 1) * per_page
    return listings[start:start + per_page], page, total


def render_shop(
    listings: list[dict],
    *,
    theme: str | None = None,
    owned: set[str] | None = None,
    wallet_usd: float = 0.0,
    page: int = 1,
    per_page: int = 12,
    total_pages: int | None = None,
) -> bytes:
    """Cosmetic shop catalogue card (1200x900).

    Groups items by theme, shows a price-tagged card per item. Owned
    items render with a check mark + dim accent so the player sees
    what they already have.
    """
    from configs.cosmetics_config import THEMES, BANNERS
    from core.framework.banner_patterns import draw_pattern as _draw_pattern
    owned = owned or set()
    canvas = RenderCanvas(1200, 900, bg=C_NAVY, gradient_to=C_CHART_BG)
    title = "Cosmetic Shop"
    if theme:
        title += f"  -  {THEMES.get(theme, {}).get('label', theme)}"
    _page_str = f"  -  Page {int(page)}/{int(total_pages or 1)}"
    canvas.title(
        title + _page_str,
        subtitle=(
            f"Wallet: ${wallet_usd:,.2f}  -  "
            f"Buy with `,profile buy <slot> <id>`  -  "
            "use the buttons below to flip pages"
        ),
        color=C_GOLD,
    )
    if not listings:
        canvas.text(
            (60, 200),
            "No items in this theme yet. Try `,profile shop`.",
            color=0xBFC7D5, size=18,
        )
        return canvas.to_png_bytes()
    # Group by theme for layout.
    grouped: dict[str, list[dict]] = {}
    for entry in listings:
        grouped.setdefault(entry.get("theme") or "general", []).append(entry)
    y = 110
    for theme_id, items in grouped.items():
        theme_meta = THEMES.get(theme_id, {"label": theme_id.title(), "color": C_INFO})
        canvas.text(
            (40, y), theme_meta["label"].upper(),
            color=int(theme_meta["color"]), size=14, bold=True,
        )
        y += 20
        x = 40
        for entry in items:
            slot = entry["slot"]
            cid = entry["id"]
            path = f"{slot}/{cid}"
            is_owned = path in owned
            rarity = _rarity_of(entry)
            rarity_color = _RARITY_COLOR[rarity]
            # Slot-coloured tile for items where the colour is the actual
            # thing being sold (banner / frame / sigil). Title cards
            # have their own coloured ribbon in the art well, so they
            # use a neutral dark backdrop instead of a saturated swatch.
            if is_owned:
                card_color = 0x2c3e50
            elif slot == "title":
                card_color = C_CHART_BG
            else:
                card_color = _slot_color(slot, cid)
            border = rarity_color if not is_owned else 0x4a5260
            border_w = 4 if rarity == "legendary" else (3 if rarity == "epic" else 2)
            # Bigger card -- need room for the actual art preview, not
            # just a colour swatch. 270x150 fits a 96x96 art square on
            # the left and a label / price / rarity stack on the right.
            CARD_W = 270
            CARD_H = 150
            canvas.rounded_panel(
                (x, y, x + CARD_W, y + CARD_H),
                color=card_color, radius=10,
                outline=border, outline_width=border_w,
            )
            # Art well (left side of the card)
            art_x = x + 10
            art_y = y + 32
            art_size = 96
            art_rect = (art_x, art_y, art_x + art_size, art_y + art_size)
            if slot == "banner":
                banner_full = BANNERS.get(cid) or {}
                # Solid banner colour as base
                canvas.draw.rounded_rectangle(
                    art_rect, radius=8,
                    fill=_rgb(int(banner_full.get("color", card_color))),
                )
                if banner_full.get("pattern"):
                    try:
                        _draw_pattern(
                            canvas.draw,
                            banner_full["pattern"],
                            art_rect,
                            int(banner_full.get("accent", rarity_color)),
                        )
                    except Exception:
                        pass
            elif slot == "sigil":
                sigil_full = SIGILS.get(cid) or {}
                sig_color = int(sigil_full.get("color", rarity_color))
                # Backdrop tile so the sigil disc has contrast.
                canvas.draw.rounded_rectangle(
                    art_rect, radius=8, fill=_rgb(C_CHART_BG),
                )
                # The sigil renderer draws its own disc + ring; centre
                # it inside the art well.
                if not draw_sigil(
                    canvas, cid, (art_x + 8, art_y + 8),
                    diameter=art_size - 16,
                    color=sig_color,
                    accent=rarity_color,
                    bg=C_CHART_BG,
                ):
                    canvas.glyph_token(
                        (art_x + 16, art_y + 16),
                        sigil_full.get("glyph", "?")[:1],
                        color=sig_color,
                        diameter=art_size - 32, font_size=28,
                    )
            elif slot == "frame":
                frame_full = FRAMES.get(cid) or {}
                # Backdrop tile + a placeholder avatar disc with the
                # frame ring + decorator. Shows what the player will
                # actually see equipped, not a bare swatch.
                canvas.draw.rounded_rectangle(
                    art_rect, radius=8, fill=_rgb(C_CHART_BG),
                )
                inner = max(32, art_size - 24)
                draw_frame(
                    canvas, cid,
                    (art_x + (art_size - inner) // 2,
                     art_y + (art_size - inner) // 2),
                    size=inner,
                    avatar_bytes=None,
                    color=int(frame_full.get("color", rarity_color)),
                    accent=rarity_color,
                    ring_width=int(frame_full.get("ring_width", 4)),
                    fallback_color=int(frame_full.get("color", C_NAVY)),
                )
            elif slot == "title":
                # Title preview: a centred ribbon badge inside the art
                # well. Pixel-truncate the label so a long title can't
                # overflow the 88 px ribbon, and SKIP the epithet here --
                # the right-side info column will render the epithet in
                # the proper width-aware way.
                title_full = TITLES.get(cid) or {}
                canvas.draw.rounded_rectangle(
                    art_rect, radius=8, fill=_rgb(C_CHART_BG),
                )
                _ribbon_rect = (
                    art_x + 4, art_y + 32,
                    art_x + art_size - 4, art_y + 68,
                )
                canvas.rounded_panel(
                    _ribbon_rect, color=rarity_color, radius=8,
                )
                _label_full = str(title_full.get("label", cid))
                _label_fit = _fit_to_width(
                    canvas, _label_full,
                    size=13, bold=True,
                    max_width=_ribbon_rect[2] - _ribbon_rect[0] - 12,
                )
                canvas.text(
                    (_ribbon_rect[0] + 6, _ribbon_rect[1] + 6),
                    _label_fit,
                    color=0xFFFFFF, size=13, bold=True,
                )
            # Slot badge (top-right of card so it doesn't fight the art)
            slot_label = slot.upper()
            canvas.pill_badge(
                (x + CARD_W - 76, y + 8), slot_label,
                color=int(theme_meta["color"]),
                font_size=10, padding=(8, 4),
            )
            # Label + id (right of the art well). For title items the
            # epithet sits under the id so the player sees the flavour
            # quote on the same card as the price.
            info_x = art_x + art_size + 12
            info_w_text = max(8, x + CARD_W - 12 - info_x)
            label = _fit_to_width(
                canvas, entry.get("label", cid),
                size=15, bold=True, max_width=info_w_text,
            )
            canvas.text(
                (info_x, y + 32), label,
                color=0xFFFFFF if not is_owned else 0xBFC7D5,
                size=15, bold=True,
            )
            _id_text = _fit_to_width(
                canvas, f"id: {cid}",
                size=10, max_width=info_w_text,
            )
            canvas.text(
                (info_x, y + 54), _id_text,
                color=0xBFC7D5, size=10,
            )
            if slot == "title":
                _ep_full = str((TITLES.get(cid) or {}).get("epithet") or "")
                if _ep_full:
                    _ep_fit = _fit_to_width(
                        canvas, _ep_full,
                        size=10, max_width=info_w_text,
                    )
                    canvas.text(
                        (info_x, y + 70), _ep_fit,
                        color=0xBFC7D5, size=10,
                    )
            # Price + rarity stacked at the bottom-right
            price_text = (
                "OWNED" if is_owned else f"${entry['price_usd']:,.0f}"
            )
            canvas.pill_badge(
                (info_x, y + CARD_H - 56), price_text,
                color=C_SUCCESS if is_owned else C_GOLD,
                font_size=12, padding=(8, 4),
            )
            canvas.pill_badge(
                (info_x, y + CARD_H - 28), rarity.upper(),
                color=rarity_color, font_size=10, padding=(6, 3),
            )
            x += CARD_W + 15
            if x > 920:
                y += CARD_H + 15
                x = 40
        if x > 40:
            y += CARD_H + 15
        y += 16
        if y > 820:
            break
    canvas.footer(f"Page {int(page)}/{int(total_pages or 1)}  -  Cosmetic Shop")
    return canvas.to_png_bytes()
