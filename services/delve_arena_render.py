"""services/delve_arena_render.py -- ASCII battle frames for the delve arena.

The arena frame is a 36-column card built for mobile Discord. Both
fighter cards read top-down with an identical layout (no mirroring, no
ASCII sprite art) so the eye can compare numbers row-by-row instead of
parsing a mirrored P2 stat block back-up-the-page.

Layout::

    +----------------------------------+
    | ARENA  R 03/25      Copper III   |   <- meta header
    |               1024 ELO           |
    | Action:  STRIKE  vs  BRACE       |   <- per-round action ribbon
    +--------------- P1 ----------------+
    | lleywyn              Mage  Lv 10 |   <- fighter identity
    | ATK 34   DEF 14   SPD 0.75       |   <- stats line
    | CD: STN2 FIR3                    |   <- cooldowns (omitted if -)
    | HP 68/68  [##############]  100% |   <- bar + numbers + percent
    +--------------- P2 ----------------+
    | Rival 1234           Mage  Lv  9 |
    | ATK 35   DEF 12   SPD 0.71       |
    | CD: -                            |
    | HP 15/65  [###-----------]   23% |
    +----------------------------------+
    |   *** WIN: lleywyn FLAWLESS *** |   <- only on terminal frames
    +----------------------------------+

Public surface:

    render_arena_frame(*, ...) -> str

Per CLAUDE.md the output is plain ASCII only -- no em/en dashes, no
unicode minus signs, no multi-byte block characters.
"""
from __future__ import annotations

from core.framework.ui import FormatKit

from services.delve_battle_render import _pad, _trim, _truncate_block
from services.delve_arena_battle import ArenaProfile


# Mobile-safe outer width. Discord iOS/Android renders codeblocks at
# roughly 36 monospace chars in portrait before wrapping kicks in --
# anything wider folds the border onto a second line.
ARENA_WIDTH: int = 36           # full outer width including borders
ARENA_HP_BAR_WIDTH: int = 14    # HP bar fill width inside the brackets

_RANK_LABEL: dict[str, str] = {
    "copper": "Copper", "silver": "Silver",
    "gold":   "Gold",   "rune":   "Rune",
}

_DIVISION_ROMAN: tuple[str, ...] = ("I", "II", "III", "IV", "V")


def _rank_pill(rank_key: str, division: int) -> str:
    label = _RANK_LABEL.get(str(rank_key).lower(), str(rank_key).title())
    roman = _DIVISION_ROMAN[max(0, min(4, int(division) - 1))]
    return f"{label} {roman}"


def _hp_bar(hp: int, max_hp: int, width: int = ARENA_HP_BAR_WIDTH) -> str:
    return FormatKit.bar(
        max(0, hp), max(1, max_hp),
        width=width, fill="#", empty="-", show_pct=False,
    )


def _hp_pct(hp: int, max_hp: int) -> int:
    if max_hp <= 0:
        return 0
    return max(0, min(100, int(round(hp * 100 / max_hp))))


def _fmt_cds(cds: dict | None, stunned: int) -> str:
    """Compact cooldown summary: ``STN2 FIR3 STA1`` or ``-``.

    Single-word ability keys take their first three letters (``fireball``
    -> ``FIR``); multi-word keys take initials (``stun_lock`` -> ``SL``).
    """
    bits: list[str] = []
    if stunned > 0:
        bits.append(f"STN{int(stunned)}")
    for k, v in (cds or {}).items():
        if not v or int(v) <= 0:
            continue
        parts = [p for p in str(k).split("_") if p]
        if len(parts) >= 2:
            short = "".join(p[0] for p in parts)[:3].upper() or "AB"
        else:
            short = (parts[0] if parts else "AB")[:3].upper()
        bits.append(f"{short}{int(v)}")
    return " ".join(bits) if bits else "-"


def _centered(s: str, w: int) -> str:
    s = s[:w]
    pad = max(0, w - len(s))
    left = pad // 2
    return " " * left + s + " " * (pad - left)


def _split_inline(left_text: str, right_text: str, w: int) -> str:
    """Place ``left_text`` left-aligned and ``right_text`` right-aligned
    inside the same ``w``-wide line. Trims the left side if they would
    collide."""
    rt = right_text[:w]
    lt = left_text[: max(0, w - len(rt) - 1)]
    pad = max(1, w - len(lt) - len(rt))
    return lt + " " * pad + rt


def _border(w: int) -> str:
    return "+" + "-" * (w - 2) + "+"


def _labeled_border(label: str, w: int) -> str:
    """Border with ``label`` centred inside the dashes: ``+--- P1 ---+``."""
    inner = w - 2
    tag = f" {label} " if label else ""
    if len(tag) >= inner:
        return _border(w)
    pad = inner - len(tag)
    left = pad // 2
    right = pad - left
    return "+" + "-" * left + tag + "-" * right + "+"


def _wrap_line(content: str, w: int) -> str:
    return "| " + _pad(content, w - 4) + " |"


def _classify_banner(banner: str) -> tuple[str, str]:
    """Return ``('action' | 'final' | 'intro', clean_text)``.

    Round-by-round actions and intros live in the top action ribbon;
    terminal win / draw / KO banners get a callout at the bottom of
    the frame.
    """
    b = (banner or "").strip()
    if not b:
        return "action", ""
    up = b.upper()
    if any(tok in up for tok in ("WIN:", "DRAW", "DEFEAT", "KO!", "FORFEIT")):
        return "final", b
    if up == "FIGHT!":
        return "intro", b
    return "action", b


def _format_action_ribbon(banner_kind: str, banner_text: str, inner: int) -> str:
    """Build the top-of-card action ribbon line."""
    if banner_kind == "intro":
        return _centered(">>>  FIGHT!  <<<", inner)
    if banner_kind == "final":
        # Terminal frames don't repeat the result up top; the bottom
        # banner carries it.
        return _centered("- match over -", inner)
    if not banner_text:
        return _centered("- pick an action -", inner)
    # Round actions arrive as "STRIKE vs BRACE". Normalise to a single
    # ASCII arrow so the action read is unambiguous.
    text = banner_text.replace(" vs ", " -> ").replace(" VS ", " -> ")
    return _centered(f"Action: {_trim(text, max(1, inner - 9))}", inner)


def _build_id_line(name: str, class_name: str, level: int, inner: int) -> str:
    cls_label = f"{class_name} Lv{int(level):>2}"
    avail = inner - len(cls_label) - 1
    nm = _trim(name, max(1, avail))
    return _split_inline(nm, cls_label, inner)


def _build_stats_line(atk: float, df: float, spd: float, inner: int) -> str:
    line = f"ATK {int(atk):<3}  DEF {int(df):<3}  SPD {float(spd):.2f}"
    return _trim(line, inner)


def _build_cd_line(cd_text: str, inner: int) -> str:
    return _trim(f"CD: {cd_text}", inner)


def _build_hp_line(hp: int, max_hp: int, inner: int) -> str:
    """``HP 68/68  [##############]  100%`` -- bar reserves room for big numbers
    and the closing bracket can't get eaten."""
    pct = _hp_pct(hp, max_hp)
    nums = f"{int(hp)}/{int(max_hp)}"
    pct_str = f"{pct:>3}%"
    bar = _hp_bar(hp, max_hp)
    bar_block = f"[{bar}]"
    left = f"HP {nums}"
    # Reserve right-side width for the percentage. Whatever's left in
    # the middle goes to the bar block.
    fixed = len(left) + 2 + len(bar_block) + 2 + len(pct_str)
    if fixed > inner:
        # Numbers + percent always survive; trim the bar if necessary.
        # Worst-case fallback drops the bar entirely.
        avail_for_bar = inner - (len(left) + 2 + 2 + len(pct_str))
        if avail_for_bar >= 4:
            new_bar_width = max(2, avail_for_bar - 2)
            bar = _hp_bar(hp, max_hp, width=new_bar_width)
            bar_block = f"[{bar}]"
        else:
            return _trim(f"HP {nums}  {pct_str}", inner)
    pad = max(1, inner - len(left) - len(bar_block) - len(pct_str) - 2)
    return left + " " * pad + bar_block + "  " + pct_str


def render_arena_frame(
    *,
    p1: ArenaProfile, p2: ArenaProfile,
    p1_hp: int, p2_hp: int,
    p1_cooldowns: dict | None = None,
    p2_cooldowns: dict | None = None,
    p1_stunned: int = 0, p2_stunned: int = 0,
    round_num: int = 1,
    max_rounds: int = 25,
    action_banner: str = "",
    rank_key: str = "copper",
    division: int = 1,
    elo: int = 0,
) -> str:
    """Build the arena ASCII frame.

    36-column vertical card with identical top-down P1 and P2 sections.
    """
    w = ARENA_WIDTH
    inner = w - 4

    # Header: round + rank pill + ELO + action ribbon -----------------
    rounds_lbl = f"R {int(round_num):>2}/{int(max_rounds):>2}"
    rank_lbl = _rank_pill(rank_key, division)
    header_left = f"ARENA  {rounds_lbl}"
    header_right = rank_lbl
    header_meta = _split_inline(header_left, header_right, inner)
    elo_line = _centered(f"{int(elo)} ELO", inner)

    banner_kind, banner_text = _classify_banner(action_banner)
    action_ribbon = _format_action_ribbon(banner_kind, banner_text, inner)

    # Per-fighter card body lines -------------------------------------
    def _fighter_lines(
        prof: ArenaProfile, hp: int,
        cooldowns: dict | None, stunned: int,
    ) -> list[str]:
        cd_text = _fmt_cds(cooldowns, stunned)
        rows = [
            _build_id_line(prof.name, prof.class_name, prof.level, inner),
            _build_stats_line(prof.atk, prof.defense, prof.spd, inner),
        ]
        if cd_text and cd_text != "-":
            rows.append(_build_cd_line(cd_text, inner))
        rows.append(_build_hp_line(int(hp), int(prof.hp_max), inner))
        return rows

    p1_lines = _fighter_lines(p1, p1_hp, p1_cooldowns, p1_stunned)
    p2_lines = _fighter_lines(p2, p2_hp, p2_cooldowns, p2_stunned)

    # Terminal banner --------------------------------------------------
    final_line = ""
    if banner_kind == "final" and banner_text:
        final_line = _centered(f"*** {_trim(banner_text, inner - 8)} ***", inner)

    # Assemble ---------------------------------------------------------
    body: list[str] = [
        _border(w),
        _wrap_line(header_meta, w),
        _wrap_line(elo_line, w),
        _wrap_line(action_ribbon, w),
        _labeled_border("P1", w),
    ]
    body.extend(_wrap_line(ln, w) for ln in p1_lines)
    body.append(_labeled_border("P2", w))
    body.extend(_wrap_line(ln, w) for ln in p2_lines)
    body.append(_border(w))
    if final_line:
        body.append(_wrap_line(final_line, w))
        body.append(_border(w))

    return _truncate_block("\n".join(body))


__all__ = ["render_arena_frame", "ARENA_WIDTH", "ARENA_HP_BAR_WIDTH"]
