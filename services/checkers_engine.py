"""services/checkers_engine.py  -  American/English checkers rules + AI.

Self-contained engine for the chess cog's sister minigame. No external
dependency (python-chess covers chess; checkers has no widely-used
package). Covers:

  * 8x8 board, pieces ``r`` / ``R`` (red, kings uppercase) and
    ``b`` / ``B`` (black). Red moves "up" the board (rank ascending);
    black moves "down". Men move 1 diagonal forward; kings move 1
    diagonal any direction (American rules -- no flying king).
  * Jumps: single + multi. Forced-capture rule: if any capture exists
    for the side to move, only capture moves are legal.
  * King-me on reaching the far rank.
  * Game-over detection: opponent has no pieces or no legal moves.

Move notation:

    "a3-b4"           non-capture (single step)
    "c3xe5"           single jump
    "c3xe5xg7"        multi-jump

Coordinates: files a-h (0-7), ranks 1-8 (0-7). Red home rows = 1-3,
black home rows = 6-8 (English start position).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

# ── Board representation ────────────────────────────────────────────────

EMPTY: str = "."
INITIAL_BOARD_STR: str = (
    # Rank 1 .. 8 stacked low-to-high. Stored as 64 chars in file-major
    # ordering (a1, b1, ..., h1, a2, b2, ...). Red on dark squares of
    # ranks 1-3, black on dark squares of ranks 6-8. Light squares stay
    # empty.
    ".r.r.r.r"  # rank 1
    "r.r.r.r."  # rank 2
    ".r.r.r.r"  # rank 3
    "........"  # rank 4
    "........"  # rank 5
    "b.b.b.b."  # rank 6
    ".b.b.b.b"  # rank 7
    "b.b.b.b."  # rank 8
)


def sq_index(file: int, rank: int) -> int:
    """File 0-7, rank 0-7 -> 0-63 string index."""
    return rank * 8 + file


def parse_square(text: str) -> Optional[tuple[int, int]]:
    """``"a3"`` -> ``(0, 2)``. Returns None on bad input."""
    s = (text or "").strip().lower()
    if len(s) != 2 or s[0] not in "abcdefgh" or s[1] not in "12345678":
        return None
    return ord(s[0]) - ord("a"), int(s[1]) - 1


def square_str(file: int, rank: int) -> str:
    return f"{'abcdefgh'[file]}{rank + 1}"


def is_dark_square(file: int, rank: int) -> bool:
    """Pieces only ever live on dark squares (where (file + rank) is odd)."""
    return (file + rank) % 2 == 1


# ── Move objects ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Move:
    """A single move: list of squares visited.

    For a non-capture move the path is two squares (from, to). For a jump
    chain the path includes every landing square. ``captured`` lists the
    squares of the opponent pieces removed (empty for non-captures).
    """
    path: tuple[tuple[int, int], ...]
    captured: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    @property
    def is_jump(self) -> bool:
        return len(self.captured) > 0

    @property
    def from_sq(self) -> tuple[int, int]:
        return self.path[0]

    @property
    def to_sq(self) -> tuple[int, int]:
        return self.path[-1]

    def notation(self) -> str:
        sep = "x" if self.is_jump else "-"
        return sep.join(square_str(*p) for p in self.path)


# ── Board class ─────────────────────────────────────────────────────────

@dataclass
class Board:
    """8x8 checkers position. Mutable; ``apply`` returns a new Board copy."""
    cells: list[str] = field(default_factory=lambda: list(INITIAL_BOARD_STR))
    turn: str = "r"  # "r" or "b"

    def copy(self) -> "Board":
        return Board(cells=list(self.cells), turn=self.turn)

    def serialise(self) -> str:
        return "".join(self.cells)

    @classmethod
    def from_str(cls, board_str: str, turn: str) -> "Board":
        if len(board_str) != 64:
            raise ValueError("Board string must be 64 chars.")
        return cls(cells=list(board_str), turn=turn)

    def at(self, file: int, rank: int) -> str:
        if 0 <= file < 8 and 0 <= rank < 8:
            return self.cells[sq_index(file, rank)]
        return "#"  # off-board sentinel

    def set(self, file: int, rank: int, piece: str) -> None:
        self.cells[sq_index(file, rank)] = piece

    # ── piece helpers ────────────────────────────────────────────────

    @staticmethod
    def is_red(p: str) -> bool:
        return p == "r" or p == "R"

    @staticmethod
    def is_black(p: str) -> bool:
        return p == "b" or p == "B"

    @staticmethod
    def is_king(p: str) -> bool:
        return p in ("R", "B")

    @staticmethod
    def owner(p: str) -> Optional[str]:
        if p in ("r", "R"):
            return "r"
        if p in ("b", "B"):
            return "b"
        return None

    # ── move generation ──────────────────────────────────────────────

    def _piece_directions(self, piece: str) -> tuple[tuple[int, int], ...]:
        """Allowed diagonal directions for a piece on the standard board."""
        if piece == "r":
            return ((-1, 1), (1, 1))            # red men move up
        if piece == "b":
            return ((-1, -1), (1, -1))          # black men move down
        if piece in ("R", "B"):
            return ((-1, 1), (1, 1), (-1, -1), (1, -1))
        return ()

    def _jumps_from(
        self, file: int, rank: int, piece: str,
    ) -> list[Move]:
        """Return every chain of jumps starting at (file, rank).

        Recursive: each jump leads to a new search from the landing
        square, with the captured square included in ``captured`` so the
        same piece isn't jumped twice. King promotion mid-chain stops
        the chain (American rules).
        """
        results: list[Move] = []

        def search(
            cur_file: int, cur_rank: int, cur_piece: str,
            path: list[tuple[int, int]],
            captured: list[tuple[int, int]],
            board_state: list[str],
        ) -> None:
            extended = False
            for df, dr in self._piece_directions(cur_piece):
                mid_f, mid_r = cur_file + df, cur_rank + dr
                land_f, land_r = cur_file + 2 * df, cur_rank + 2 * dr
                if not (0 <= land_f < 8 and 0 <= land_r < 8):
                    continue
                mid_idx = sq_index(mid_f, mid_r)
                land_idx = sq_index(land_f, land_r)
                mid_piece = board_state[mid_idx]
                land_piece = board_state[land_idx]
                # Jumped square must hold an enemy piece, landing must
                # be empty, and we mustn't re-jump the same enemy.
                if (mid_f, mid_r) in captured:
                    continue
                if mid_piece == EMPTY:
                    continue
                if self.owner(mid_piece) == self.owner(cur_piece):
                    continue
                if land_piece != EMPTY:
                    continue
                # Make the jump on a copy of the board so chained search
                # sees the in-progress position.
                new_state = list(board_state)
                new_state[sq_index(cur_file, cur_rank)] = EMPTY
                new_state[mid_idx] = EMPTY
                # Promote on the chain step that reaches the far rank,
                # but stop the chain after promotion (American rule).
                landed_piece = cur_piece
                promoted = False
                if cur_piece == "r" and land_r == 7:
                    landed_piece = "R"
                    promoted = True
                elif cur_piece == "b" and land_r == 0:
                    landed_piece = "B"
                    promoted = True
                new_state[land_idx] = landed_piece
                new_path = path + [(land_f, land_r)]
                new_captured = captured + [(mid_f, mid_r)]
                if promoted:
                    results.append(
                        Move(tuple(new_path), tuple(new_captured)),
                    )
                    extended = True
                    continue
                # Recurse for further jumps.
                before = len(results)
                search(
                    land_f, land_r, landed_piece,
                    new_path, new_captured, new_state,
                )
                if len(results) == before:
                    # No further jump from here -- record this chain.
                    results.append(
                        Move(tuple(new_path), tuple(new_captured)),
                    )
                extended = True
            # If no extension was possible from this node and we were
            # mid-chain, the parent caller records the move. Roots with
            # no jumps return nothing.
            if not extended and len(captured) == 0:
                return

        search(file, rank, piece, [(file, rank)], [], list(self.cells))
        return results

    def _steps_from(
        self, file: int, rank: int, piece: str,
    ) -> list[Move]:
        out: list[Move] = []
        for df, dr in self._piece_directions(piece):
            nf, nr = file + df, rank + dr
            if not (0 <= nf < 8 and 0 <= nr < 8):
                continue
            if self.cells[sq_index(nf, nr)] != EMPTY:
                continue
            out.append(Move(((file, rank), (nf, nr))))
        return out

    def legal_moves(self) -> list[Move]:
        """All legal moves for the side to move. Forced captures honoured."""
        jumps: list[Move] = []
        steps: list[Move] = []
        for rank in range(8):
            for file in range(8):
                p = self.cells[sq_index(file, rank)]
                if self.owner(p) != self.turn:
                    continue
                jumps.extend(self._jumps_from(file, rank, p))
                steps.extend(self._steps_from(file, rank, p))
        return jumps if jumps else steps

    def is_terminal(self) -> tuple[bool, Optional[str]]:
        """Return ``(over, winner)``. Winner is 'r' / 'b' / None for ongoing."""
        if not self.legal_moves():
            # Side to move has no legal moves -- they lose.
            return True, ("b" if self.turn == "r" else "r")
        # Also handle "no pieces" explicitly in case of crowding edge cases.
        red = sum(1 for c in self.cells if c in ("r", "R"))
        black = sum(1 for c in self.cells if c in ("b", "B"))
        if red == 0:
            return True, "b"
        if black == 0:
            return True, "r"
        return False, None

    # ── apply move ───────────────────────────────────────────────────

    def apply(self, move: Move) -> "Board":
        """Return a new board with ``move`` applied. Doesn't mutate self."""
        nb = self.copy()
        from_f, from_r = move.from_sq
        piece = nb.cells[sq_index(from_f, from_r)]
        nb.set(from_f, from_r, EMPTY)
        for cf, cr in move.captured:
            nb.set(cf, cr, EMPTY)
        to_f, to_r = move.to_sq
        # Promote if reached far rank.
        if piece == "r" and to_r == 7:
            piece = "R"
        elif piece == "b" and to_r == 0:
            piece = "B"
        nb.set(to_f, to_r, piece)
        nb.turn = "b" if nb.turn == "r" else "r"
        return nb


# ── Move parsing ────────────────────────────────────────────────────────

def parse_move(board: Board, text: str) -> Optional[Move]:
    """Parse a notation string against the legal-move list. None if illegal."""
    s = (text or "").strip().lower().replace(" ", "")
    if not s:
        return None
    sep = "x" if "x" in s else ("-" if "-" in s else None)
    if sep is None:
        # Try single-step shorthand "a3b4".
        if len(s) == 4:
            a = parse_square(s[:2])
            b = parse_square(s[2:])
            if a and b:
                target = Move((a, b))
                for mv in board.legal_moves():
                    if mv.from_sq == a and mv.to_sq == b and not mv.captured:
                        return mv
                # Maybe a single-jump abbreviation -- check captures.
                for mv in board.legal_moves():
                    if (
                        mv.from_sq == a and mv.to_sq == b
                        and len(mv.path) == 2 and mv.captured
                    ):
                        return mv
        return None
    parts = s.split(sep)
    sq_path = []
    for part in parts:
        sq = parse_square(part)
        if sq is None:
            return None
        sq_path.append(sq)
    for mv in board.legal_moves():
        path_tuple = tuple(mv.path)
        if path_tuple == tuple(sq_path):
            return mv
    return None


# ── AI ──────────────────────────────────────────────────────────────────

_KING_BONUS: float = 1.5
_BACK_RANK_BONUS: float = 0.05
_CENTER_BONUS: float = 0.02


def _evaluate(board: Board) -> float:
    """Return positive scores for red, negative for black."""
    score = 0.0
    for rank in range(8):
        for file in range(8):
            p = board.cells[sq_index(file, rank)]
            if p == EMPTY:
                continue
            v = _KING_BONUS if board.is_king(p) else 1.0
            # Centre bonus -- middle 4x4 is more flexible.
            if 2 <= file <= 5 and 2 <= rank <= 5:
                v += _CENTER_BONUS
            # Back rank holders block opponent promotion -- keep a man home.
            if board.is_red(p) and rank == 0:
                v += _BACK_RANK_BONUS
            elif board.is_black(p) and rank == 7:
                v += _BACK_RANK_BONUS
            score += v if board.is_red(p) else -v
    return score


def ai_pick_move(board: Board, depth: int = 4) -> Optional[Move]:
    """Negamax with material + position eval. Random tiebreak."""
    moves = board.legal_moves()
    if not moves:
        return None
    color = 1 if board.turn == "r" else -1
    random.shuffle(moves)
    best_score = -1e18
    best_move: Move = moves[0]

    def negamax(b: Board, d: int, c: int, alpha: float, beta: float) -> float:
        over, winner = b.is_terminal()
        if over:
            if winner == "r":
                return c * 10_000.0
            if winner == "b":
                return c * -10_000.0
            return 0.0
        if d == 0:
            return c * _evaluate(b)
        best = -1e18
        for mv in b.legal_moves():
            val = -negamax(b.apply(mv), d - 1, -c, -beta, -alpha)
            if val > best:
                best = val
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    for mv in moves:
        s = -negamax(board.apply(mv), depth - 1, -color, -1e18, 1e18)
        if s > best_score:
            best_score = s
            best_move = mv
    return best_move


__all__ = [
    "EMPTY",
    "INITIAL_BOARD_STR",
    "Move",
    "Board",
    "parse_square",
    "square_str",
    "parse_move",
    "ai_pick_move",
]
