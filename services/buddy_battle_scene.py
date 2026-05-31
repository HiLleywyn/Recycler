"""services/buddy_battle_scene.py -- battle scene PNG renderer.

Renders the 1200x620 battle stage used by the new zone / tournament
battle view. Two buddy portraits face each other across a themed arena
background, with HP bars, names, level pills, status indicators, the
current-round indicator, and an optional action banner.

Two entry points:

    render_battle_frame(state) -> bytes
        Static per-round render. Use when the round resolves or the
        player opens the battle view.

    render_attack_burst(state, frame_idx, total_frames=6) -> bytes
        One frame of a multi-frame attack burst. Called by the cog's
        FPS edit loop (services.buddy_portrait shares the same idea
        for the standalone portrait).

State is a plain dict so the cog can pass synthetic / preview data:

    {
        "p1_row":  cc_buddies dict for player,
        "p1_hp":   current_hp,        "p1_max_hp": max_hp,
        "p1_status_icons": ["poison", "rage", ...],
        "p2_row":  cc_buddies dict for AI / opponent,
        "p2_hp":   ..., "p2_max_hp": ...,
        "p2_status_icons": [...],
        "round":          1..N,
        "max_rounds":     30,
        "zone_id":        "plains_gate" or "" for default arena,
        "action_banner":  "Quick Berry!" / "CRIT!" / "" if idle,
        "is_player_turn": True/False,   # show 'pick a move' hint
    }
"""
from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageDraw

from configs.buddies_config import ARENA_ZONES
from core.framework.render import RenderCanvas
from core.framework.render_primitives import (
    font, hex_to_rgb, rgba, text_with_outline, to_png_bytes,
)
from services.buddy_portrait import render_buddy_portrait

log = logging.getLogger(__name__)


_SCENE_W: int = 1200
_SCENE_H: int = 620


async def play_battle_action_burst(
    view,
    player,
    enemy,
    *,
    actor_side: str,        # "p1" or "p2"
    action: str,            # "strike" / "special" / "risky" / "brace"
    round_num: int,
    max_rounds: int,
    ability_name: str = "",
    zone_id: str = "",
) -> None:
    """Play the four-frame per-move attack burst on any battle view.

    Single source of truth for the animation overlays used by every
    interactive buddy-battle surface (PvP, wild buddy, arena map,
    fishing, delve, farming). The view only needs:

      * ``view.message`` -- the Discord message to edit
      * ``view._burst_count`` -- mutable int the helper increments

    Each action gets a distinct overlay style:
        strike  -- yellow slash arcs + impact flash
        special -- blue energy ring + radial burst on target
        risky   -- orange motion blur + 8-point red impact star
        brace   -- green shield ripple on the bracer (no target hit)
    """
    import asyncio as _aio
    import io as _io

    import discord  # noqa: F401  -- imported here to avoid a top-level cycle

    from configs.buddies_config import (
        BATTLE_BURST_FRAMES as _FRAMES,
        BATTLE_FRAME_INTERVAL_S as _INTERVAL,
        BATTLE_MAX_BURSTS_PER_BATTLE as _MAX,
    )

    msg = getattr(view, "message", None)
    if msg is None:
        return
    used = int(getattr(view, "_burst_count", 0) or 0)
    if used >= _MAX:
        return
    view._burst_count = used + 1

    banner_map = {
        "strike":  "STRIKE",
        "special": (ability_name or "SPECIAL").upper()[:18],
        "brace":   "BRACE",
        "risky":   "RISKY",
    }
    banner = banner_map.get(action, "ATTACK")
    is_brace = action == "brace"
    hit_side = ("p2" if actor_side == "p1" else "p1") if not is_brace else None

    for i in range(_FRAMES):
        state = fighters_to_scene_state(
            player, enemy,
            round_num=round_num,
            max_rounds=max_rounds,
            action_banner=banner,
            is_player_turn=False,
            zone_id=zone_id,
        )
        state["acting_side"] = actor_side
        if hit_side:
            state["hit_side"] = hit_side
        try:
            png = render_attack_burst(state, i, total_frames=_FRAMES)
            f = discord.File(_io.BytesIO(png), filename="battle.png")
            await msg.edit(attachments=[f])
        except Exception as exc:
            log.debug("play_battle_action_burst: edit failed: %s", exc)
            return
        await _aio.sleep(_INTERVAL)


def fighters_to_scene_state(
    player,
    enemy,
    *,
    round_num: int,
    max_rounds: int,
    action_banner: str = "",
    is_player_turn: bool = True,
    zone_id: str = "",
) -> dict[str, Any]:
    """Build the dict shape ``render_battle_frame`` expects from two
    Fighter-like objects.

    This is the single adapter every interactive battle view uses so
    fishing wild-buddy fights, delve wild-buddy fights, arena map,
    PvP, and farming pest fights all share the same scene PNG +
    animation overlays. Any object with ``hp / max_hp / level / tier
    / species / name`` attrs works (Fighter, _DelveFighter,
    _FishingFighter, etc.) -- access is by getattr with safe defaults
    so an incomplete row doesn't crash the renderer.
    """
    def _row(f, default_species: str) -> dict:
        return {
            "id":           int(getattr(f, "id", 0) or 0),
            "species":      str(getattr(f, "species", default_species) or default_species),
            "name":         str(getattr(f, "name", "Buddy") or "Buddy"),
            "level":        int(getattr(f, "level", 1) or 1),
            "rarity_tier":  int(getattr(f, "tier", 1) or 1),
            "hunger":       80, "happiness": 80, "energy": 80,
            "gear":         {},
            "boss_zone_id": str(getattr(f, "boss_zone_id", "") or ""),
        }
    return {
        "p1_row":          _row(player, "default"),
        "p2_row":          _row(enemy, "default"),
        "p1_hp":           int(getattr(player, "hp", 0) or 0),
        "p1_max_hp":       max(1, int(getattr(player, "max_hp", 1) or 1)),
        "p2_hp":           int(getattr(enemy, "hp", 0) or 0),
        "p2_max_hp":       max(1, int(getattr(enemy, "max_hp", 1) or 1)),
        "p1_status_icons": [],
        "p2_status_icons": [],
        "round":           int(round_num),
        "max_rounds":      int(max_rounds),
        "zone_id":         str(zone_id),
        "action_banner":   str(action_banner),
        "is_player_turn":  bool(is_player_turn),
    }


def render_battle_frame(state: dict[str, Any]) -> bytes:
    """Static battle scene PNG for the current round."""
    zone_id = str(state.get("zone_id") or "")
    z = ARENA_ZONES.get(zone_id, {})
    bg = z.get("bg_gradient") or (0x6e2e2e, 0x1a0a0a)
    if not isinstance(bg, (tuple, list)) or len(bg) != 2:
        bg = (0x6e2e2e, 0x1a0a0a)

    canvas = RenderCanvas(_SCENE_W, _SCENE_H, bg=int(bg[1]), gradient_to=int(bg[0]))

    _draw_arena_floor(canvas, zone_id)
    _draw_player_side(canvas, state, side="p1", x_anchor=140)
    _draw_player_side(canvas, state, side="p2", x_anchor=_SCENE_W - 140 - 320, mirror=True)
    _draw_center_band(canvas, state)
    _draw_round_indicator(canvas, state)

    if state.get("is_player_turn"):
        _draw_turn_hint(canvas)

    return canvas.to_png_bytes()


def render_attack_burst(state: dict[str, Any], frame_idx: int, *, total_frames: int = 6) -> bytes:
    """One frame of an attack burst sequence.

    The base scene is rendered, then a per-frame overlay is composed on
    top (motion lines, impact flash, recoil tint). Bounded to 6 frames
    by default; total_frames is exposed so future effects can take
    longer (e.g. ultimate moves).
    """
    base = render_battle_frame(state)
    return _apply_scene_overlay(base, frame_idx, total_frames, state)


def _draw_arena_floor(canvas: RenderCanvas, zone_id: str) -> None:
    """Painted floor band at the bottom of the scene."""
    floor_y = int(_SCENE_H * 0.72)
    canvas.draw.rectangle(
        (0, floor_y, _SCENE_W, _SCENE_H),
        fill=rgba(0x000000, 80),
    )
    # Horizon line
    canvas.draw.line(
        ((0, floor_y), (_SCENE_W, floor_y)),
        fill=hex_to_rgb(0x000000), width=2,
    )
    # Zone-themed silhouette in the back (procedural mountains / waves)
    region = ARENA_ZONES.get(zone_id, {}).get("region", "neutral")
    if region == "plains":
        for i in range(6):
            base_x = i * 220
            canvas.draw.polygon(
                [
                    (base_x, floor_y),
                    (base_x + 110, floor_y - 120),
                    (base_x + 220, floor_y),
                ],
                fill=rgba(0x4a6b3a, 200),
            )
    elif region == "stone":
        for i in range(5):
            base_x = i * 260
            canvas.draw.polygon(
                [
                    (base_x, floor_y),
                    (base_x + 130, floor_y - 200),
                    (base_x + 260, floor_y),
                ],
                fill=rgba(0x5a4a3a, 220),
            )
    elif region == "tide":
        for y in range(floor_y - 80, floor_y, 14):
            canvas.draw.line(
                ((0, y), (_SCENE_W, y + 6)),
                fill=rgba(0x29b6f6, 60), width=2,
            )
    elif region == "tournament":
        # Columns
        for x in (100, 1100):
            canvas.draw.rectangle(
                (x - 30, floor_y - 220, x + 30, floor_y),
                fill=rgba(0xf1c40f, 90),
            )


def _draw_player_side(
    canvas: RenderCanvas, state: dict, *,
    side: str, x_anchor: int, mirror: bool = False,
) -> None:
    """Draw one fighter's portrait + name + HP bar + status."""
    row = state.get(f"{side}_row") or {}
    hp = int(state.get(f"{side}_hp") or 0)
    max_hp = max(1, int(state.get(f"{side}_max_hp") or 1))
    status = list(state.get(f"{side}_status_icons") or [])

    # Compose the portrait inline via render_buddy_portrait
    pose = _pose_for_side(state, side)
    portrait_bytes = render_buddy_portrait(row, mood=pose, theme="battle", size=320)
    import io as _io
    portrait_img = Image.open(_io.BytesIO(portrait_bytes)).convert("RGBA")
    if mirror:
        portrait_img = portrait_img.transpose(Image.FLIP_LEFT_RIGHT)
    canvas.img.paste(portrait_img, (int(x_anchor), 140), mask=portrait_img)

    # Name + level pill above the portrait. Bumped up to y=72 to make
    # room for the rarity + species pill underneath without overlapping
    # the portrait disc (which paints from y=140).
    name = str(row.get("name") or "Buddy")
    level = int(row.get("level") or 1)
    canvas.pill_badge(
        (int(x_anchor) + 30, 72),
        f"{name} -- Lv {level}",
        color=0x1a1a1a, text_color=0xFFFFFF,
        padding=(12, 6), font_size=14,
    )

    # Rarity + type pill (under the name pill) -- surfaces what the
    # opponent IS so the player can read level / rarity / species at a
    # glance during every battle. Tier colour matches the standard
    # rarity ladder (Common gray -> Legendary gold).
    rarity_tier = max(1, min(5, int(row.get("rarity_tier") or 1)))
    _TIER_INFO = {
        1: ("Common",    0x9E9E9E),
        2: ("Uncommon",  0x66BB6A),
        3: ("Rare",      0x42A5F5),
        4: ("Epic",      0xAB47BC),
        5: ("Legendary", 0xFFB300),
    }
    rarity_label, rarity_color = _TIER_INFO.get(rarity_tier, _TIER_INFO[1])
    species_label = str(row.get("species") or "").strip().title() or "Buddy"
    canvas.pill_badge(
        (int(x_anchor) + 30, 106),
        f"{rarity_label} {species_label}",
        color=rarity_color, text_color=0x1A1A1A,
        padding=(10, 4), font_size=12,
    )

    # HP bar below the portrait
    bar_rect = (int(x_anchor) + 10, 470, int(x_anchor) + 310, 500)
    canvas.progress_bar(
        bar_rect,
        hp / max_hp,
        color=_hp_color(hp, max_hp),
        bg_color=0x2c2c2c,
        radius=8,
        label=f"{hp}/{max_hp}",
        label_color=0xFFFFFF,
    )

    # Status badge row
    if status:
        sx = int(x_anchor) + 10
        for icon in status[:5]:
            label, color = _status_meta(icon)
            rect = canvas.pill_badge(
                (sx, 514),
                label, color=color, text_color=0x1a1a1a,
                padding=(8, 3), font_size=11,
            )
            sx = rect[2] + 6


def _draw_center_band(canvas: RenderCanvas, state: dict) -> None:
    """Action banner / VS / round number across the middle."""
    banner = str(state.get("action_banner") or "")
    if banner:
        canvas.halo((460, 280, 740, 380), color=0xFFEB3B, radius=18, alpha=180)
        f = font(28, bold=True)
        tw = int(canvas.draw.textlength(banner, font=f))
        text_with_outline(
            canvas.draw,
            ((_SCENE_W - tw) // 2, 304),
            banner,
            font_obj=f, fill=(255, 235, 59), outline=(0, 0, 0),
            outline_width=3,
        )
    else:
        f = font(34, bold=True)
        vs = "VS"
        tw = int(canvas.draw.textlength(vs, font=f))
        canvas.draw.text(
            ((_SCENE_W - tw) // 2, 300),
            vs, fill=hex_to_rgb(0xCCCCCC), font=f,
        )


def _draw_round_indicator(canvas: RenderCanvas, state: dict) -> None:
    rd = int(state.get("round") or 1)
    mx = int(state.get("max_rounds") or 30)
    canvas.pill_badge(
        (_SCENE_W // 2 - 60, 560),
        f"Round {rd} / {mx}",
        color=0x2c3e50, text_color=0xFFFFFF,
        padding=(14, 6), font_size=14,
    )


def _draw_turn_hint(canvas: RenderCanvas) -> None:
    """Bottom-right 'pick a move' hint when waiting on player input."""
    canvas.pill_badge(
        (_SCENE_W - 320, 560),
        "Pick an item or attack",
        color=0x4FC3F7, text_color=0x1a1a1a,
        padding=(12, 6), font_size=13,
    )


def _pose_for_side(state: dict, side: str) -> str:
    """Translate state into the portrait pose for this fighter."""
    if int(state.get(f"{side}_hp") or 0) <= 0:
        return "down"
    banner = str(state.get("action_banner") or "").lower()
    if "crit" in banner and state.get("acting_side") == side:
        return "victory"
    if state.get("acting_side") == side and banner:
        return "attack"
    if state.get("hit_side") == side:
        return "hurt"
    return "neutral"


def _hp_color(hp: int, max_hp: int) -> int:
    pct = hp / max(1, max_hp)
    if pct >= 0.6:
        return 0x66BB6A
    if pct >= 0.3:
        return 0xF1C40F
    return 0xE74C3C


def _status_meta(icon: str) -> tuple[str, int]:
    return {
        "poison":   ("POISON", 0xCE93D8),
        "rage":     ("RAGE",   0xEF5350),
        "iron":     ("IRON",   0x90A4AE),
        "swift":    ("SWIFT",  0xFFEE58),
        "stunned":  ("STUN",   0xFFA726),
        "regen":    ("REGEN",  0x66BB6A),
        "revive":   ("REVIVE", 0xFF8A65),
    }.get(str(icon).lower(), (str(icon).upper(), 0x546E7A))


def _move_kind_from_banner(banner: str) -> str:
    """Map the per-frame banner string back to a move kind so the
    overlay can swap visuals per action.

    Strike / Special / Risky / Brace use distinct effect families;
    item bursts (e.g. "Quick Berry!") read as ``"item"``.
    """
    b = str(banner or "").upper()
    if "BRACE" in b:
        return "brace"
    if "RISKY" in b:
        return "risky"
    if "STRIKE" in b or "K.O." in b or "VICTORY" in b:
        return "strike"
    # Anything else that arrives during _play_action_animation is the
    # player's named Special (ability_name uppercased). Treat as special.
    if state_action_is_special(b):
        return "special"
    if "BERRY" in b or "VIAL" in b or "DUST" in b or "BALM" in b or "BOLT" in b or "TEAR" in b:
        return "item"
    return "strike"


# Set of action banner tokens we know are SHIPPED non-special words. Any
# other ALL-CAPS banner during a player burst is treated as a Special.
_NON_SPECIAL_BANNER_TOKENS: frozenset[str] = frozenset({
    "FIGHT!", "STRIKE", "BRACE", "RISKY", "K.O.", "VICTORY!", "TAMED!",
})


def state_action_is_special(banner_upper: str) -> bool:
    """True when the banner is a Special move (any named ability)."""
    if not banner_upper:
        return False
    for tok in _NON_SPECIAL_BANNER_TOKENS:
        if tok in banner_upper:
            return False
    # Item names also matched in caller before this; reaching here means
    # the banner is a Special ability label.
    return True


def _apply_scene_overlay(
    base_bytes: bytes, frame_idx: int, total_frames: int, state: dict,
) -> bytes:
    """Layer per-frame burst effects on top of the base scene PNG.

    Per-move differentiation:
      - strike : yellow slash arcs, white impact flash
      - special: blue energy ring, radial burst, lingering aura
      - risky  : orange flash with diagonal motion blur, red recoil
      - brace  : green shield ripple on the BRACER (player), no enemy hit
      - item   : neutral white shimmer on the user
    """
    import io as _io
    img = Image.open(_io.BytesIO(base_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    attacker = str(state.get("acting_side") or "p1")
    cx_attacker = 300 if attacker == "p1" else _SCENE_W - 300
    cx_target = _SCENE_W - 300 if attacker == "p1" else 300
    cy = 300
    move = _move_kind_from_banner(str(state.get("action_banner") or ""))

    if move == "brace":
        # Defensive green shield ripple around the attacker (who braced).
        for r in range(40 + frame_idx * 18, 80 + frame_idx * 26, 8):
            alpha = max(40, 180 - (r - 40))
            draw.ellipse(
                (cx_attacker - r, cy - r, cx_attacker + r, cy + r),
                outline=(102, 187, 106, alpha), width=4,
            )
    elif move == "special":
        # Wind-up: blue glow at the attacker.
        if frame_idx <= 1:
            for r in range(50 + frame_idx * 20, 130, 8):
                draw.ellipse(
                    (cx_attacker - r, cy - r, cx_attacker + r, cy + r),
                    outline=(64, 156, 255, 220 - r), width=3,
                )
        elif frame_idx == 2:
            # Radial burst on target.
            for ang in range(0, 360, 22):
                import math as _math
                rad = _math.radians(ang)
                x0 = cx_target + int(60 * _math.cos(rad))
                y0 = cy + int(60 * _math.sin(rad))
                x1 = cx_target + int(170 * _math.cos(rad))
                y1 = cy + int(170 * _math.sin(rad))
                draw.line(((x0, y0), (x1, y1)),
                          fill=(64, 156, 255, 220), width=4)
        elif frame_idx == 3:
            # Blue impact wash on target column.
            col_x0 = cx_target - 200
            col_x1 = cx_target + 200
            draw.rectangle((col_x0, 110, col_x1, _SCENE_H - 60),
                           fill=(64, 156, 255, 80))
        elif frame_idx == 4:
            # Lingering aura ring on target.
            for r in range(120, 200, 6):
                draw.ellipse(
                    (cx_target - r, cy - r, cx_target + r, cy + r),
                    outline=(64, 156, 255, max(40, 200 - r)),
                    width=2,
                )
    elif move == "risky":
        # Orange whirl + diagonal motion blur on the attacker.
        if frame_idx <= 1:
            for i in range(4):
                draw.line(
                    ((cx_attacker - 60 + i * 8,
                      cy - 60 + i * 18),
                     (cx_attacker + 80 + i * 8,
                      cy + 60 + i * 18)),
                    fill=(255, 152, 0, 220 - i * 30), width=4,
                )
        elif frame_idx == 2:
            # Hard impact star (8-point) on target.
            import math as _math
            for ang in range(0, 360, 45):
                rad = _math.radians(ang)
                x = cx_target + int(120 * _math.cos(rad))
                y = cy + int(120 * _math.sin(rad))
                draw.line(((cx_target, cy), (x, y)),
                          fill=(255, 87, 34, 240), width=6)
        elif frame_idx == 3:
            # Bold red wash + screen-shake hint via diagonal bands.
            for x in range(0, _SCENE_W, 80):
                draw.line(((x, 0), (x + 60, _SCENE_H)),
                          fill=(255, 87, 34, 30), width=10)
    elif move == "item":
        # Neutral white shimmer near the user.
        for r in range(30 + frame_idx * 12, 90 + frame_idx * 18, 8):
            alpha = max(40, 180 - (r - 30))
            draw.ellipse(
                (cx_attacker - r, cy - r, cx_attacker + r, cy + r),
                outline=(255, 255, 255, alpha), width=2,
            )
    else:
        # Strike (default) -- the previous overlay shape.
        if frame_idx <= 1:
            for i in range(3):
                yoff = -30 + i * 30
                x0 = cx_attacker
                x1 = cx_attacker + int(
                    (cx_target - cx_attacker) * (0.3 + 0.2 * frame_idx)
                )
                draw.line(((x0, cy + yoff), (x1, cy + yoff)),
                          fill=(255, 235, 59, 220), width=5)
        elif frame_idx == 2:
            for r in range(60, 180, 12):
                alpha = max(60, 200 - (r - 60))
                draw.ellipse(
                    (cx_target - r, cy - r, cx_target + r, cy + r),
                    outline=(255, 255, 255, alpha), width=3,
                )
        elif frame_idx == 3:
            col_x0 = cx_target - 180
            col_x1 = cx_target + 180
            draw.rectangle((col_x0, 100, col_x1, _SCENE_H - 60),
                           fill=(255, 87, 34, 70))
        elif frame_idx == 4:
            for offset_x in (-1, 0, 1):
                px = cx_attacker + offset_x * 50
                py = int(_SCENE_H * 0.78)
                draw.ellipse((px - 20, py - 10, px + 20, py + 10),
                             fill=(220, 220, 220, 140))

    combined = Image.alpha_composite(img, overlay)
    return to_png_bytes(combined)
