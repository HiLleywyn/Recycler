"""services/delve_battle_render.py -- ASCII battle frame for delve mob fights.

The buddy battle PNG renderer (services.buddy_battle_scene) is reserved
for wild buddy encounters inside the delve and for buddy-arena fights.
Plain mob combat (goblins, skeletons, slimes, bosses) is rendered here
as a multi-line ASCII scene wrapped in an embed code block. This keeps
the procedural buddy art out of mob fights, which were never meant to
share that surface.

Public surface:

    render_mob_battle_frame(*, ...) -> str

The output is a fixed-width text block suitable for ``card(...)``
``description`` rendering inside a triple-backtick fence. The renderer
defends against Discord's 4096-char description / 1024-char field
limits by capping each line at ``MAX_LINE`` and the full block at
``MAX_BLOCK`` characters.

Per CLAUDE.md the file uses plain ASCII only -- no em/en dashes, no
unicode minus signs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from core.framework.ui import FormatKit

import configs.dungeon_config as dc

log = logging.getLogger(__name__)


MAX_LINE: int = 46          # fits Discord mobile code blocks without wrapping
MAX_BLOCK: int = 1900       # leave headroom inside the 2k embed description
HP_BAR_WIDTH: int = 12      # narrower so the bar fits inside one column

# ASCII art per class. Each is a 3-line ``tuple[str, str, str]``. Lines
# are kept short (under 14 chars including weapon glyphs) so two
# fighters fit side-by-side at ``MAX_LINE`` width.
_CLASS_ART: dict[str, tuple[str, str, str]] = {
    "warrior": (r" \O/ ", r" /|\=[]", r" / \ "),
    "rogue":   (r"  o  ", r" /|>>", r" / \ "),
    "mage":    (r" (*) ", r" /|=~~", r" / \ "),
    "archer":  (r"  o  ", r" /|--<", r" / \ "),
    "druid":   (r" (~) ", r" /|=*", r" / \ "),
}
_CLASS_ART_DEFAULT: tuple[str, str, str] = (r"  O  ", r" /|\ ", r" / \ ")

# ASCII art per mob key. Each is up to 4 lines. Keep each line at most
# 16 chars. Unmapped mobs fall through to ``_FALLBACK_BY_TIER``.
_MOB_ART: dict[str, tuple[str, ...]] = {
    # Tier 1
    "goblin":      (r" ,--.  ", r"( >o< )", r" |^^|  ", r" /__\  "),
    "kobold":      (r"  /\   ", r" (oo)  ", r"/|--|\ ", r"  ||   "),
    "giant_rat":   (r"        ", r"  __    ", r"<(o_>)__", r"  ~~ww  "),
    "shroom_imp":  (r"  (@)  ", r" (.. ) ", r"  |  |  ", r"  /\   "),
    # Tier 2
    "skeleton":    (r"  _.._ ", r" /o  o\\", r" \\__/ ", r"  /||\\ "),
    "bat":         (r"        ", r" /\^^/\ ", r"<( oo )>", r"  ~~~~  "),
    "slime":       (r"        ", r"  ,--.  ", r" ( oo ) ", r"  '~~'  "),
    "wisp":        (r"   *    ", r" * o *  ", r"  *_*   ", r"   *    "),
    # Tier 3
    "ghoul":       (r"  ,-,   ", r" ( ^^ ) ", r" |XX|/  ", r" /  \\  "),
    "spider":      (r"  ..    ", r" /MM\\   ", r" (oo)   ", r"//||\\\\ "),
    "kobold_shaman": (r"  /^\\  ", r" (oo)   ", r"/| t |\\ ", r"  /\\   "),
    "minotaur":    (r" \\\\__//", r" (o..o)", r"  ||X|| ", r"  /  \\ "),
    # Tier 4
    "wraith":      (r"  ,-,   ", r" (~~~~) ", r"  ~__~  ", r"  ~  ~  "),
    "troll":       (r" .---.  ", r" |o o|  ", r" |\\_/| ", r" /===\\ "),
    "basilisk":    (r"  ,_,   ", r" /v v\\  ", r" =====  ", r"  ~~~~  "),
    "demon":       (r"  /^^\\  ", r" (>o<)  ", r"  |X|   ", r" / | \\ "),
    "lich_acolyte": (r"  /^\\   ", r" /o o\\  ", r" |X X|  ", r" /===\\ "),
    "drake":       (r"  ___   ", r" /^^^\\  ", r"<( oo )>", r" \\===/  "),
    "banshee":     (r"   ?    ", r" /~~~\\  ", r" |O O|  ", r" |~~~|  "),
    # Bosses (regular mob entries plus boss flag)
    "ogre_lord":   (r" /^^^^\\ ", r" |o  o| ", r" |XXXX| ", r" /====\\ "),
    "lich":        (r"  /==\\  ", r" |o  o| ", r" |//\\\\| ", r" /XXXX\\ "),
    "dragon":      (r"   ___  ", r" /^^^^\\=", r"<(oXXo)>", r" \\====/ "),
    "ancient_one": (r"  /||\\  ", r" (O==O) ", r"  |XX|  ", r" /====\\ "),
    "abyssal_titan": (r" /====\\ ", r"(o O o O)", r" |XXXX| ", r" /====\\ "),
    "phoenix_lord": (r"  /\\^/\\ ", r" /=*o*=\\ ", r"  |XXX|  ", r"  /=\\   "),
    "void_warden": (r"  @==@  ", r" /====\\  ", r" |O  O|  ", r" \\====/  "),
    "the_archon":  (r"  /^^\\   ", r" /====\\  ", r"|O ww O|", r" \\====/  "),
    "world_serpent": (r"  __  __ ", r" /  \\/  \\", r"<( oo  )>", r"  ~~~~~~ "),
}

_FALLBACK_BY_TIER: dict[int, tuple[str, ...]] = {
    1: (r"  ,_,   ", r" (>.<)  ", r"  /|\\   ", r"  / \\   "),
    2: (r"  /^\\   ", r" ( oo ) ", r"  |||   ", r"  / \\   "),
    3: (r" /===\\  ", r"|o   o| ", r" \\===/  ", r"  / \\   "),
    4: (r" /====\\ ", r"|O   O| ", r" |XXXX| ", r" /====\\ "),
    5: (r" /====\\ ", r"|@   @| ", r" |WWWW| ", r" /====\\ "),
}

_FALLBACK_UNDEAD: tuple[str, ...] = (
    r"  ___   ", r" /o o\\  ", r" \\___/  ", r"  /|\\   ",
)


@dataclass
class PlayerView:
    """Snapshot of the player side used by the renderer."""
    name: str
    class_key: str
    class_name: str
    level: int
    hp: int
    max_hp: int
    atk: int
    defense: int
    spd: float
    stamina: int
    stamina_max: int
    skill_cd: int = 0
    weapon_kind: str = "melee"      # 'melee' | 'ranged' | 'spell'
    status: str = ""                # 'bleeding 2' / 'poise' / etc.


@dataclass
class MobView:
    """Snapshot of the mob side used by the renderer."""
    key: str
    name: str
    tier: int
    level: int
    hp: int
    max_hp: int
    atk: int
    defense: int
    spd: float
    is_boss: bool = False
    is_undead: bool = False
    status: str = ""


# Renderer ---------------------------------------------------------------

def _hp_bar(hp: int, max_hp: int) -> str:
    """ASCII HP bar with hash+dash glyphs. Plain ASCII only.

    Uses ``FormatKit.bar`` with explicit fill/empty chars instead of
    the default unicode blocks so the bar stays monospaced inside a
    Discord code fence even on narrow phone clients.
    """
    return FormatKit.bar(
        max(0, hp), max(1, max_hp),
        width=HP_BAR_WIDTH, fill="#", empty="-", show_pct=False,
    )


def _player_sprite(class_key: str, weapon_kind: str) -> tuple[str, str, str]:
    """Pick the 3-line class ASCII, swapping the weapon glyph on the
    middle line to match the equipped weapon kind."""
    base = _CLASS_ART.get(str(class_key or "").lower(), _CLASS_ART_DEFAULT)
    if weapon_kind == "ranged":
        return (base[0], r" /|--<", base[2])
    if weapon_kind in ("spell", "staff"):
        return (base[0], r" /|=~~", base[2])
    return base


def _mob_sprite(mob: MobView) -> tuple[str, ...]:
    art = _MOB_ART.get(mob.key)
    if art:
        return art
    if mob.is_undead:
        return _FALLBACK_UNDEAD
    return _FALLBACK_BY_TIER.get(max(1, min(5, int(mob.tier))),
                                  _FALLBACK_BY_TIER[1])


def _pad(s: str, w: int) -> str:
    if len(s) >= w:
        return s[:w]
    return s + " " * (w - len(s))


def _truncate_block(block: str) -> str:
    if len(block) <= MAX_BLOCK:
        return block
    return block[: MAX_BLOCK - 4] + "\n..."


def _trim(s: str, w: int) -> str:
    if len(s) <= w:
        return s
    if w <= 1:
        return s[:w]
    return s[: w - 1] + "."


def render_mob_battle_frame(
    *,
    player: PlayerView,
    mob: MobView,
    round_num: int,
    max_rounds: int,
    floor: int,
    action_banner: str = "",
    is_player_turn: bool = True,
) -> str:
    """Build a mobile-readable ASCII battle frame.

    Layout (each line padded to MAX_LINE columns and wrapped with a
    side border):

        +--------------------------------------------+
        |  F12  R 4/30           FIGHT!              |
        +--------------------------------------------+
        |  You (Knight L8)        Skeleton T3 L4     |
        |  HP [############--]    HP [###---------]  |
        |       142/200                 18/60        |
        |                                            |
        |    \\O/                       ,_._,         |
        |    /|\\=[]                   ( o o )       |
        |    / \\                      / |X|  \\       |
        |                                            |
        |  ATK 24 DEF 8 SPD .62  ATK 7 DEF 2 SPD .50 |
        |  Sta ###-- CD 1        Status: bleeding 2  |
        +--------------------------------------------+
    """
    inner = MAX_LINE - 4
    half = inner // 2
    right_w = inner - half - 1

    rounds_lbl = f"F{int(floor)}  R {int(round_num)}/{int(max_rounds)}"
    banner = (action_banner or ("YOUR TURN" if is_player_turn else "ENEMY TURN")).strip()
    banner = _trim(banner, inner - len(rounds_lbl) - 4)
    pad_n = max(1, inner - len(rounds_lbl) - len(banner))
    header_inner = rounds_lbl + " " * pad_n + banner

    left_name = _trim(f"You ({player.class_name} L{int(player.level)})", half)
    right_name_raw = f"{mob.name} T{int(mob.tier)} L{int(mob.level)}"
    if mob.is_boss:
        right_name_raw = "* " + right_name_raw
    right_name = _trim(right_name_raw, right_w)
    name_row = _pad(left_name, half) + " " + _pad(right_name, right_w)

    p_bar = _hp_bar(player.hp, player.max_hp)
    m_bar = _hp_bar(mob.hp, mob.max_hp)
    hp_row = (
        _pad(f"HP [{p_bar}]", half) + " " + _pad(f"HP [{m_bar}]", right_w)
    )
    hp_count = (
        _pad(f"   {int(player.hp)}/{int(player.max_hp)}", half)
        + " "
        + _pad(f"   {int(mob.hp)}/{int(mob.max_hp)}", right_w)
    )

    p_art = _player_sprite(player.class_key, player.weapon_kind)
    m_art = _mob_sprite(mob)
    art_rows = max(len(p_art), len(m_art))
    sprite_lines: list[str] = []
    for i in range(art_rows):
        left = p_art[i] if i < len(p_art) else " " * 5
        right = m_art[i] if i < len(m_art) else ""
        sprite_lines.append(_pad(_trim(left, half), half) + " "
                            + _pad(_trim(right, right_w), right_w))

    stats_left = _trim(
        f"ATK {int(player.atk)} DEF {int(player.defense)} SPD {player.spd:.2f}",
        half,
    )
    stats_right = _trim(
        f"ATK {int(mob.atk)} DEF {int(mob.defense)} SPD {mob.spd:.2f}",
        right_w,
    )
    stats_row = _pad(stats_left, half) + " " + _pad(stats_right, right_w)

    stam_bar = FormatKit.bar(
        max(0, player.stamina), max(1, player.stamina_max),
        width=5, fill="#", empty="-", show_pct=False,
    )
    cd_label = f"CD {int(player.skill_cd)}" if player.skill_cd > 0 else "CD-"
    status_left = _trim(f"Sta {stam_bar} {cd_label}", half)
    status_right = _trim(f"Status: {mob.status or '-'}", right_w)
    status_row = _pad(status_left, half) + " " + _pad(status_right, right_w)

    border = "+" + "-" * (inner + 2) + "+"
    body_lines: list[str] = [border, "| " + _pad(header_inner, inner) + " |", border]
    for row in [
        name_row,
        hp_row,
        hp_count,
        " " * inner,
        *sprite_lines,
        " " * inner,
        stats_row,
        status_row,
    ]:
        body_lines.append("| " + _pad(row, inner) + " |")
    body_lines.append(border)

    return _truncate_block("\n".join(body_lines))


# Player + mob view builders --------------------------------------------

def player_view_from_state(
    state: dict, *, class_key: str, weapon_kind: str | None = None,
) -> PlayerView:
    """Build a ``PlayerView`` from the dungeon state dict."""
    cmeta = dc.class_meta(class_key) or {}
    weapon = dc.weapon_meta(state.get("equipped_weapon") or "") or {}
    wk = str(weapon_kind or weapon.get("attack_kind") or "melee")
    buffs = dict(state.get("player_buffs") or {})
    status_bits: list[str] = []
    for k, payload in buffs.items():
        if not isinstance(payload, dict):
            continue
        if str(k).startswith("_ability_cd_"):
            continue
        dur = int(payload.get("duration") or 0)
        if dur > 0:
            status_bits.append(f"{k} {dur}")
    return PlayerView(
        name=str(state.get("display_name") or "You"),
        class_key=str(class_key or ""),
        class_name=str(cmeta.get("name") or class_key or "Adventurer"),
        level=int(state.get("level") or 1),
        hp=int(state.get("current_hp") or 0),
        max_hp=int(state.get("hp_max") or 1),
        atk=int(state.get("atk") or cmeta.get("atk_base") or 5),
        defense=int(state.get("defense") or cmeta.get("def_base") or 2),
        spd=float(state.get("spd") or cmeta.get("spd_base") or 0.5),
        stamina=int(state.get("stamina") or 0),
        stamina_max=int(state.get("stamina_max") or 5),
        skill_cd=int(state.get("skill_cd_remaining") or 0),
        weapon_kind=wk,
        status=", ".join(status_bits)[:24],
    )


def mob_view_from_state(mob_state: dict) -> MobView:
    """Build a ``MobView`` from the dungeon mob_state dict."""
    key = str(mob_state.get("key") or "")
    meta = dc.mob_meta(key) or {}
    tags = tuple(meta.get("tags") or ())
    return MobView(
        key=key,
        name=str(meta.get("name") or key.title() or "Mob"),
        tier=int(mob_state.get("tier") or meta.get("tier") or 1),
        level=int(mob_state.get("level") or meta.get("level") or 1),
        hp=int(mob_state.get("hp") or 0),
        max_hp=int(mob_state.get("max_hp") or 1),
        atk=int(mob_state.get("atk") or meta.get("atk_base") or 1),
        defense=int(mob_state.get("def") or meta.get("def_base") or 0),
        spd=float(mob_state.get("spd") or meta.get("spd_base") or 0.5),
        is_boss=bool(meta.get("boss") or mob_state.get("boss")),
        is_undead="undead" in tags,
        status=str(mob_state.get("status") or ""),
    )


__all__ = [
    "PlayerView",
    "MobView",
    "render_mob_battle_frame",
    "player_view_from_state",
    "mob_view_from_state",
]
