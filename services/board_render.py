"""PNG rendering for chess + checkers boards.

Rendered server-side with Pillow so every Discord client (desktop,
tablet, mobile) sees the same anti-aliased, chess.com-style board.
The output is a ``bytes`` object that the cog wraps in ``discord.File``
and references via ``embed.image("attachment://<name>.png")``.

Two public entry points:

    render_chess_png(board, flip=False) -> bytes
    render_checkers_png(board, flip=False, last_squares=None) -> bytes

Fonts: bundled DejaVu Sans Bold from ``assets/fonts/`` so the renderer
doesn't depend on system fonts being installed in the container.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


# ── Layout constants ────────────────────────────────────────────────────
_SQ: int = 88                   # square size in px
_GUTTER: int = 36               # coordinate gutter on the left + bottom
_BOARD: int = _SQ * 8           # 704
_W: int = _GUTTER + _BOARD + _GUTTER // 2   # add a small right margin
_H: int = _BOARD + _GUTTER + _GUTTER // 2

# ── Chess.com-inspired palette ─────────────────────────────────────────
_BG: tuple[int, int, int]            = (38, 36, 33)      # outer frame
_LIGHT_CHESS: tuple[int, int, int]   = (235, 236, 208)   # cream
_DARK_CHESS: tuple[int, int, int]    = (115, 149, 82)    # sage green
_HL_FROM: tuple[int, int, int, int]  = (246, 246, 105, 180)  # yellow tint
_HL_TO: tuple[int, int, int, int]    = (246, 246, 105, 220)
_GUTTER_FG: tuple[int, int, int]     = (200, 200, 195)

# Checkers: warm wood tones with cream squares
_LIGHT_CKR: tuple[int, int, int]     = (240, 217, 181)   # warm cream
_DARK_CKR: tuple[int, int, int]      = (110, 76, 47)     # walnut
_HL_CKR: tuple[int, int, int, int]   = (255, 215, 90, 170)
_RED_FILL: tuple[int, int, int]      = (196, 38, 46)
_RED_HL: tuple[int, int, int]        = (255, 120, 120)
_RED_RIM: tuple[int, int, int]       = (110, 18, 22)
_BLACK_FILL: tuple[int, int, int]    = (30, 30, 30)
_BLACK_HL: tuple[int, int, int]      = (105, 105, 110)
_BLACK_RIM: tuple[int, int, int]     = (5, 5, 5)
_GOLD: tuple[int, int, int]          = (240, 196, 75)


# ── Font loading ───────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"
_FONT_BOLD_PATH = _FONT_DIR / "DejaVuSans-Bold.ttf"
_FONT_REG_PATH = _FONT_DIR / "DejaVuSans.ttf"


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD_PATH if bold else _FONT_REG_PATH
    return ImageFont.truetype(str(path), size=size)


# ── Chess piece glyphs ─────────────────────────────────────────────────
# All pieces use the filled Unicode glyphs (♚♛♜♝♞♟) so they read the
# same regardless of foreground colour. Stroke width gives white pieces
# a black outline and black pieces a thin cream outline -- the chess.com
# look on either square colour.
_CHESS_GLYPH: dict[str, str] = {
    "K": "♚", "Q": "♛", "R": "♜",
    "B": "♝", "N": "♞", "P": "♟",
    "k": "♚", "q": "♛", "r": "♜",
    "b": "♝", "n": "♞", "p": "♟",
}


def _square_origin(file: int, rank: int, flip: bool) -> tuple[int, int]:
    """Top-left pixel of the (file, rank) square. file 0..7 = a..h, rank 0..7 = 1..8."""
    if flip:
        col = 7 - file
        row = rank
    else:
        col = file
        row = 7 - rank
    x = _GUTTER + col * _SQ
    y = row * _SQ
    return x, y


def _draw_board_squares(
    draw: ImageDraw.ImageDraw, light: tuple, dark: tuple,
) -> None:
    for rank in range(8):
        for file in range(8):
            x = _GUTTER + file * _SQ
            y = (7 - rank) * _SQ
            colour = light if (file + rank) % 2 == 1 else dark
            draw.rectangle((x, y, x + _SQ, y + _SQ), fill=colour)


def _draw_gutter(draw: ImageDraw.ImageDraw, flip: bool) -> None:
    """Render rank digits in the left gutter and file letters along the bottom.

    Labels live in the dedicated frame area outside the playable board so
    they're easy to read at a glance regardless of square colour. When
    ``flip`` is set, the order is reversed so coordinates always match
    the perspective of the side at the bottom of the board.
    """
    rank_font = _font(24, bold=True)
    file_font = _font(24, bold=True)
    files = "abcdefgh" if not flip else "hgfedcba"
    ranks = list(range(8, 0, -1)) if not flip else list(range(1, 9))

    # Rank digits in the left gutter, vertically centred per rank row.
    for i, r in enumerate(ranks):
        text = str(r)
        bbox = rank_font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        cx = _GUTTER // 2
        cy = i * _SQ + _SQ // 2
        draw.text(
            (cx - tw / 2, cy - th / 2 - 2),
            text, fill=_GUTTER_FG, font=rank_font,
        )

    # File letters in the bottom gutter, horizontally centred per file.
    bottom_y = _BOARD + (_H - _BOARD) // 2 - 2
    for i, f in enumerate(files):
        bbox = file_font.getbbox(f)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        cx = _GUTTER + i * _SQ + _SQ // 2
        draw.text(
            (cx - tw / 2, bottom_y - th / 2),
            f, fill=_GUTTER_FG, font=file_font,
        )


def _highlight_square(
    img: Image.Image, x: int, y: int, rgba: tuple[int, int, int, int],
) -> None:
    """Blend a translucent highlight over a single square."""
    overlay = Image.new("RGBA", (_SQ, _SQ), rgba)
    img.alpha_composite(overlay, (x, y))


# ── Public: chess ──────────────────────────────────────────────────────

def render_chess_png(board, flip: bool = False) -> bytes:
    """Render a ``chess.Board`` to a chess.com-style PNG.

    ``flip`` mirrors so the player viewing as Black sees their pieces
    at the bottom. The from / to squares of the most recent move are
    overlaid with a yellow tint identical to chess.com's "last move"
    cue.
    """
    img = Image.new("RGBA", (_W, _H), _BG)
    draw = ImageDraw.Draw(img)

    _draw_board_squares(draw, _LIGHT_CHESS, _DARK_CHESS)

    # Last-move highlight.
    last_squares: set[int] = set()
    move_stack = getattr(board, "move_stack", None)
    if move_stack:
        try:
            mv = board.peek()
            last_squares = {mv.from_square, mv.to_square}
        except Exception:
            last_squares = set()
    for sq in last_squares:
        f = sq % 8
        r = sq // 8
        x, y = _square_origin(f, r, flip)
        _highlight_square(img, x, y, _HL_FROM)

    _draw_gutter(draw, flip)

    # Pieces. Use the filled glyph for both colours; stroke width gives
    # white pieces a thick black outline and black pieces a thin cream
    # rim so they read on either square colour.
    piece_font = _font(int(_SQ * 0.78), bold=True)
    for sq in range(64):
        piece = board.piece_at(sq)
        if piece is None:
            continue
        sym = piece.symbol()
        glyph = _CHESS_GLYPH.get(sym)
        if not glyph:
            continue
        f = sq % 8
        r = sq // 8
        x, y = _square_origin(f, r, flip)
        is_white = sym.isupper()
        fill = (250, 250, 248) if is_white else (25, 25, 25)
        stroke = (15, 15, 15) if is_white else (250, 250, 248)
        # Centre the glyph in the square. The Unicode chess glyphs have
        # generous internal padding so anchor "mm" looks slightly low;
        # nudge up a few pixels.
        cx = x + _SQ // 2
        cy = y + _SQ // 2 - 2
        draw.text(
            (cx, cy), glyph,
            fill=fill, font=piece_font,
            anchor="mm",
            stroke_width=3, stroke_fill=stroke,
        )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Public: checkers ───────────────────────────────────────────────────

def _draw_checker(
    draw: ImageDraw.ImageDraw, x: int, y: int,
    fill: tuple, hl: tuple, rim: tuple, *, king: bool,
) -> None:
    """Draw a single checker on the square at top-left ``(x, y)``."""
    pad = int(_SQ * 0.10)
    box = (x + pad, y + pad, x + _SQ - pad, y + _SQ - pad)
    # Outer rim (slightly larger ellipse behind the piece for depth).
    rim_box = (
        x + pad - 2, y + pad - 1,
        x + _SQ - pad + 2, y + _SQ - pad + 3,
    )
    draw.ellipse(rim_box, fill=rim)
    # Main body.
    draw.ellipse(box, fill=fill, outline=rim, width=2)
    # Inner ring (chess.com / official-checkers style notched edge).
    inner_pad = int(_SQ * 0.18)
    inner_box = (
        x + inner_pad, y + inner_pad,
        x + _SQ - inner_pad, y + _SQ - inner_pad,
    )
    draw.ellipse(inner_box, outline=rim, width=2)
    # Highlight blob in the upper-left for a glossy 3D feel.
    hl_box = (
        x + int(_SQ * 0.22), y + int(_SQ * 0.20),
        x + int(_SQ * 0.46), y + int(_SQ * 0.36),
    )
    draw.ellipse(hl_box, fill=hl)
    if king:
        # Crown glyph on top.
        crown_font = _font(int(_SQ * 0.42), bold=True)
        cx = x + _SQ // 2
        cy = y + _SQ // 2 + 2
        draw.text(
            (cx, cy), "♕",  # ♕ -- reads as a crown at this scale
            fill=_GOLD, font=crown_font,
            anchor="mm",
            stroke_width=2, stroke_fill=(60, 40, 0),
        )


def render_checkers_png(
    board,
    flip: bool = False,
    *,
    last_squares: Optional[Iterable[tuple[int, int]]] = None,
) -> bytes:
    """Render a ``services.checkers_engine.Board`` to a wood-board PNG."""
    img = Image.new("RGBA", (_W, _H), _BG)
    draw = ImageDraw.Draw(img)

    _draw_board_squares(draw, _LIGHT_CKR, _DARK_CKR)

    last_set = set(last_squares or ())
    for (f, r) in last_set:
        if 0 <= f < 8 and 0 <= r < 8:
            x, y = _square_origin(f, r, flip)
            _highlight_square(img, x, y, _HL_CKR)

    _draw_gutter(draw, flip)

    for rank in range(8):
        for file in range(8):
            piece = board.at(file, rank)
            if piece in (".", " ", ""):
                continue
            x, y = _square_origin(file, rank, flip)
            if piece in ("r", "R"):
                _draw_checker(
                    draw, x, y,
                    fill=_RED_FILL, hl=_RED_HL, rim=_RED_RIM,
                    king=(piece == "R"),
                )
            elif piece in ("b", "B"):
                _draw_checker(
                    draw, x, y,
                    fill=_BLACK_FILL, hl=_BLACK_HL, rim=_BLACK_RIM,
                    king=(piece == "B"),
                )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Blackjack ──────────────────────────────────────────────────────────

_CARD_W: int = 130
_CARD_H: int = 184
_CARD_GAP: int = 16          # horizontal gap between cards (no overlap)
_CARD_FAN_OFFSET: int = _CARD_W + _CARD_GAP
_BJ_BG: tuple[int, int, int] = (24, 60, 38)        # casino felt green
_BJ_BG_HL: tuple[int, int, int] = (32, 78, 50)
_CARD_FACE: tuple[int, int, int] = (250, 250, 245)
_CARD_BORDER: tuple[int, int, int] = (12, 12, 14)
_CARD_BACK: tuple[int, int, int] = (146, 28, 38)   # deep red back
_CARD_BACK_PATTERN: tuple[int, int, int] = (110, 18, 26)
_SUIT_RED: tuple[int, int, int] = (200, 30, 40)
_SUIT_BLACK: tuple[int, int, int] = (15, 15, 20)

# Suit assignment by hand position is deterministic and cosmetic only
# (blackjack never uses suit for game logic). Cycling through all four
# suits keeps a hand of any length looking like a real deal.
_SUIT_CYCLE: tuple[str, ...] = ("♠", "♥", "♦", "♣")


def _card_rank_label(rank: int) -> str:
    return {1: "A", 11: "J", 12: "Q", 13: "K"}.get(rank, str(rank))


def _draw_card_face(
    img: Image.Image, draw: ImageDraw.ImageDraw,
    x: int, y: int, rank: int, suit: str,
) -> None:
    rect = (x, y, x + _CARD_W, y + _CARD_H)
    # Soft drop shadow under the card.
    shadow = Image.new("RGBA", (_CARD_W + 16, _CARD_H + 16), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (4, 6, _CARD_W + 12, _CARD_H + 14),
        radius=14, fill=(0, 0, 0, 110),
    )
    img.alpha_composite(shadow, (x - 8, y - 4))
    # Card face.
    draw.rounded_rectangle(
        rect, radius=14, fill=_CARD_FACE,
        outline=_CARD_BORDER, width=2,
    )
    is_red = suit in ("♥", "♦")
    fg = _SUIT_RED if is_red else _SUIT_BLACK
    rank_label = _card_rank_label(rank)
    rank_font = _font(34, bold=True)
    suit_small_font = _font(22, bold=True)
    suit_big_font = _font(int(_CARD_H * 0.42), bold=True)
    # Top-left rank + suit (and bottom-right rotated mirror).
    draw.text((x + 10, y + 8), rank_label, fill=fg, font=rank_font)
    draw.text((x + 12, y + 44), suit, fill=fg, font=suit_small_font)
    # Big centre suit.
    draw.text(
        (x + _CARD_W // 2, y + _CARD_H // 2 + 4), suit,
        fill=fg, font=suit_big_font, anchor="mm",
    )
    # Bottom-right rank/suit -- drawn rotated 180 by mirroring.
    bbox_r = rank_font.getbbox(rank_label)
    rw = bbox_r[2] - bbox_r[0]
    rh = bbox_r[3] - bbox_r[1]
    rotated = Image.new(
        "RGBA", (rw + 16, rh + 56), (0, 0, 0, 0),
    )
    rd = ImageDraw.Draw(rotated)
    rd.text((0, 0), rank_label, fill=fg, font=rank_font)
    rd.text((2, rh + 6), suit, fill=fg, font=suit_small_font)
    rotated = rotated.rotate(180, expand=True)
    img.alpha_composite(
        rotated,
        (x + _CARD_W - rotated.width - 8, y + _CARD_H - rotated.height - 8),
    )


def _draw_card_back(
    img: Image.Image, draw: ImageDraw.ImageDraw,
    x: int, y: int,
) -> None:
    rect = (x, y, x + _CARD_W, y + _CARD_H)
    shadow = Image.new("RGBA", (_CARD_W + 16, _CARD_H + 16), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (4, 6, _CARD_W + 12, _CARD_H + 14),
        radius=14, fill=(0, 0, 0, 110),
    )
    img.alpha_composite(shadow, (x - 8, y - 4))
    draw.rounded_rectangle(
        rect, radius=14, fill=_CARD_BACK,
        outline=(255, 220, 110), width=3,
    )
    # Diamond lattice pattern.
    pad = 12
    spacing = 16
    for px in range(x + pad, x + _CARD_W - pad, spacing):
        for py in range(y + pad, y + _CARD_H - pad, spacing):
            draw.line(
                (px, py + 6, px + 6, py),
                fill=_CARD_BACK_PATTERN, width=2,
            )
            draw.line(
                (px, py + 6, px + 6, py + 12),
                fill=_CARD_BACK_PATTERN, width=2,
            )
    # Centre logo: gold diamond.
    cx = x + _CARD_W // 2
    cy = y + _CARD_H // 2
    diamond = [
        (cx, cy - 22), (cx + 18, cy), (cx, cy + 22), (cx - 18, cy),
    ]
    draw.polygon(diamond, fill=(240, 198, 80), outline=(120, 86, 20))


def render_blackjack_png(
    *,
    player_cards: list[int],
    dealer_cards: list[int],
    player_value: int,
    dealer_value: Optional[int],
    reveal: bool,
    result: Optional[str] = None,
) -> bytes:
    """Render a blackjack table to a PNG.

    During play (``reveal=False``) the dealer's hole card is rendered as
    a card back and ``dealer_value`` is ignored. On reveal both hands
    are face-up and ``result`` is one of ``win`` / ``lose`` / ``push``
    / ``blackjack`` / ``bust``.
    """
    n_max = max(len(player_cards), len(dealer_cards), 2)
    width = (
        80 + _CARD_W + (n_max - 1) * _CARD_FAN_OFFSET + 80
    )
    width = max(width, 640)
    height = 80 + _CARD_H + 60 + _CARD_H + 60

    img = Image.new("RGBA", (width, height), _BJ_BG)
    draw = ImageDraw.Draw(img)

    # Felt vignette: softer green halo.
    halo = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.ellipse(
        (-width // 4, -height // 6, width + width // 4, height + height // 6),
        fill=_BJ_BG_HL,
    )
    halo.putalpha(80)
    img.alpha_composite(halo)

    label_font = _font(22, bold=True)
    value_font = _font(30, bold=True)

    def _draw_row(
        cards: list[int], top_y: int, label: str, value: Optional[int],
        hide_second: bool = False,
    ) -> None:
        draw.text(
            (40, top_y - 6), label, fill=(240, 240, 230), font=label_font,
        )
        if value is not None:
            v_text = f"{value}"
            bbox = value_font.getbbox(v_text)
            vw = bbox[2] - bbox[0]
            v_color = (255, 215, 90) if value <= 21 else (255, 110, 110)
            draw.text(
                (width - 40 - vw, top_y - 8), v_text,
                fill=v_color, font=value_font,
            )
        x = 80
        y = top_y + 26
        for i, card in enumerate(cards):
            if hide_second and i == 1:
                _draw_card_back(img, draw, x, y)
            else:
                suit = _SUIT_CYCLE[i % len(_SUIT_CYCLE)]
                _draw_card_face(img, draw, x, y, card, suit)
            x += _CARD_FAN_OFFSET

    # Dealer on top, player below -- standard table orientation.
    _draw_row(
        dealer_cards, 50, "DEALER",
        dealer_value if reveal else None, hide_second=not reveal,
    )
    player_top = 50 + _CARD_H + 80
    _draw_row(player_cards, player_top, "YOU", player_value)

    # Result banner overlay (only when revealed and we have a result).
    if reveal and result:
        banners = {
            "blackjack": ("BLACKJACK", (255, 215, 90), (90, 60, 0)),
            "win":       ("YOU WIN",  (110, 230, 140), (10, 60, 30)),
            "lose":      ("DEALER WINS", (240, 110, 110), (70, 14, 18)),
            "bust":      ("BUST", (240, 110, 110), (70, 14, 18)),
            "push":      ("PUSH", (220, 220, 220), (40, 40, 40)),
        }
        text, fg, shadow = banners.get(
            result, (result.upper(), (240, 240, 240), (20, 20, 20)),
        )
        banner_font = _font(46, bold=True)
        bbox = banner_font.getbbox(text)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        bx = (width - bw) // 2
        by = (height - bh) // 2 - 10
        # Banner backdrop.
        pad = 24
        backdrop = Image.new(
            "RGBA", (bw + pad * 2, bh + pad), (0, 0, 0, 170),
        )
        bd = ImageDraw.Draw(backdrop)
        bd.rounded_rectangle(
            (0, 0, bw + pad * 2, bh + pad), radius=14,
            fill=(0, 0, 0, 200), outline=fg, width=3,
        )
        img.alpha_composite(backdrop, (bx - pad, by - pad // 2))
        draw.text(
            (bx, by), text, fill=fg, font=banner_font,
            stroke_width=3, stroke_fill=shadow,
        )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Roulette ───────────────────────────────────────────────────────────

# European single-zero wheel order.
_ROULETTE_WHEEL_ORDER: tuple[int, ...] = (
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10,
    5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
)
_ROULETTE_RED: frozenset[int] = frozenset({
    1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36,
})
_ROULETTE_W: int = 720
_ROULETTE_H: int = 760
_R_OUTER: int = 270
_R_INNER: int = 168
_R_HUB: int = 90

_R_GREEN: tuple[int, int, int] = (16, 110, 60)
_R_RED: tuple[int, int, int] = (170, 28, 36)
_R_BLACK: tuple[int, int, int] = (22, 22, 26)
_R_RIM: tuple[int, int, int] = (180, 134, 60)
_R_RIM_DARK: tuple[int, int, int] = (110, 78, 30)
_R_HUB_FILL: tuple[int, int, int] = (35, 30, 38)
_R_HUB_RIM: tuple[int, int, int] = (220, 180, 90)


def _wedge_color(n: int) -> tuple[int, int, int]:
    if n == 0:
        return _R_GREEN
    return _R_RED if n in _ROULETTE_RED else _R_BLACK


def render_roulette_png(
    *,
    spin: int,
    bet_label: str,
    won: bool,
    payout_mult: float,
) -> bytes:
    """Render the roulette wheel with the result wedge highlighted."""
    img = Image.new("RGBA", (_ROULETTE_W, _ROULETTE_H), (18, 14, 22))
    draw = ImageDraw.Draw(img)
    cx, cy = _ROULETTE_W // 2, 320

    # Outer rim (gold band).
    draw.ellipse(
        (cx - _R_OUTER - 18, cy - _R_OUTER - 18,
         cx + _R_OUTER + 18, cy + _R_OUTER + 18),
        fill=_R_RIM_DARK,
    )
    draw.ellipse(
        (cx - _R_OUTER - 8, cy - _R_OUTER - 8,
         cx + _R_OUTER + 8, cy + _R_OUTER + 8),
        fill=_R_RIM,
    )

    # 37 wedges around the wheel. Each wedge spans 360/37 degrees.
    n = len(_ROULETTE_WHEEL_ORDER)
    sweep = 360.0 / n
    # Start so the result sits at the TOP (12 o'clock = -90 deg).
    spin_idx = _ROULETTE_WHEEL_ORDER.index(spin)
    start_angle = -90.0 - sweep / 2 - spin_idx * sweep
    label_font = _font(20, bold=True)
    for i, num in enumerate(_ROULETTE_WHEEL_ORDER):
        a0 = start_angle + i * sweep
        a1 = a0 + sweep
        colour = _wedge_color(num)
        draw.pieslice(
            (cx - _R_OUTER, cy - _R_OUTER, cx + _R_OUTER, cy + _R_OUTER),
            start=a0, end=a1, fill=colour, outline=_R_RIM_DARK, width=2,
        )

    # Inner ring (cosmetic divider).
    draw.ellipse(
        (cx - _R_INNER, cy - _R_INNER, cx + _R_INNER, cy + _R_INNER),
        outline=_R_RIM, width=4,
    )

    # Number labels: drawn upright at each wedge centre.
    import math
    label_radius = (_R_OUTER + _R_INNER) // 2 + 8
    for i, num in enumerate(_ROULETTE_WHEEL_ORDER):
        ang_deg = start_angle + i * sweep + sweep / 2
        ang_rad = math.radians(ang_deg)
        lx = cx + label_radius * math.cos(ang_rad)
        ly = cy + label_radius * math.sin(ang_rad)
        text = str(num)
        bbox = label_font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        is_win_wedge = (num == spin)
        fg = (255, 240, 180) if is_win_wedge else (240, 240, 240)
        draw.text(
            (lx - tw / 2, ly - th / 2 - 2), text,
            fill=fg, font=label_font,
            stroke_width=2 if is_win_wedge else 1,
            stroke_fill=(0, 0, 0),
        )

    # Hub.
    draw.ellipse(
        (cx - _R_HUB, cy - _R_HUB, cx + _R_HUB, cy + _R_HUB),
        fill=_R_HUB_FILL, outline=_R_HUB_RIM, width=4,
    )
    # Hub spokes (decorative cross).
    for ang in (0, 60, 120, 180, 240, 300):
        ar = math.radians(ang)
        x1 = cx + 18 * math.cos(ar)
        y1 = cy + 18 * math.sin(ar)
        x2 = cx + (_R_HUB - 12) * math.cos(ar)
        y2 = cy + (_R_HUB - 12) * math.sin(ar)
        draw.line((x1, y1, x2, y2), fill=_R_HUB_RIM, width=4)

    # Ball: white circle at the top of the wheel, just inside the rim.
    ball_r = 14
    bx = cx
    by = cy - _R_OUTER + 22
    draw.ellipse(
        (bx - ball_r, by - ball_r, bx + ball_r, by + ball_r),
        fill=(245, 245, 240), outline=(60, 60, 60), width=2,
    )
    # Specular highlight on ball.
    draw.ellipse(
        (bx - ball_r + 3, by - ball_r + 3, bx - 3, by - 3),
        fill=(255, 255, 255),
    )

    # Result strip at the bottom of the image.
    strip_h = 90
    strip_top = _ROULETTE_H - strip_h - 20
    strip = Image.new("RGBA", (_ROULETTE_W - 40, strip_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(strip)
    banner_color = (110, 230, 140) if won else (240, 110, 110)
    sd.rounded_rectangle(
        (0, 0, _ROULETTE_W - 40, strip_h), radius=18,
        fill=(0, 0, 0, 180), outline=banner_color, width=3,
    )
    img.alpha_composite(strip, (20, strip_top))

    big_font = _font(54, bold=True)
    sub_font = _font(22, bold=True)
    spin_color_label = (
        "GREEN" if spin == 0
        else ("RED" if spin in _ROULETTE_RED else "BLACK")
    )
    big_text = f"●  {spin}  ·  {spin_color_label}"
    bbox = big_font.getbbox(big_text)
    bw = bbox[2] - bbox[0]
    draw.text(
        ((_ROULETTE_W - bw) // 2, strip_top + 8),
        big_text, fill=banner_color, font=big_font,
        stroke_width=2, stroke_fill=(0, 0, 0),
    )
    sub = (
        f"BET {bet_label.upper()}  ·  "
        f"{'WIN ' + str(int(payout_mult)) + 'x' if won else 'LOSS'}"
    )
    bbox = sub_font.getbbox(sub)
    sw = bbox[2] - bbox[0]
    draw.text(
        ((_ROULETTE_W - sw) // 2, strip_top + 56),
        sub, fill=(220, 220, 220), font=sub_font,
    )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = [
    "render_chess_png",
    "render_checkers_png",
    "render_blackjack_png",
    "render_roulette_png",
]
