"""cogs/chess.py  -  Gamba Network chess: PvE + PvP + leaderboard.

Surface:

    ,chess play [bet] [token]              start a vs-AI match
    ,chess challenge @user [bet] [token]   propose a PvP match
    ,chess move <uci>                      e.g. ``e2e4`` / ``g1f3`` / ``e7e8q``
    ,chess board                           re-render your active match
    ,chess resign                          forfeit your active match
    ,chess leaderboard                     ELO leaderboard (top 10)
    ,chess stats [@user]                   personal record card

Engine: ``python-chess`` (added in pyproject.toml). The cog stays pure
Python -- legal-move generation, check / mate / stalemate detection,
castling, en-passant, and promotion all live in the library so every
position is provably legal. Persistence is FEN + UCI move history in
``gamba_chess_matches``; the position survives bot restarts.

Bets settle in **USD** (default) or **GBC**. Winners take the pot;
on a draw the bet is refunded. Every win also mints **GAMBIT** via
``services.gamba.award_game_token`` and bumps the player's ELO using
the standard rating formula (K=32, lower for high-rated players).

Boards render as Unicode squares + chess piece glyphs in a ``code``
block so the layout aligns with monospace Discord rendering. AI is a
shallow minimax with material + mobility eval -- not a grandmaster,
but it plays a credible casual-strength opponent.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands

try:
    import chess as _chess
except Exception as _exc:  # pragma: no cover - hard fail if dep missing
    _chess = None
    _chess_import_error = _exc
else:
    _chess_import_error = None

from core.config import Config
from core.framework.bot import Discoin
from services.board_render import render_chess_png
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, fmt_token, fmt_usd,
)
from services import gamba as gamba_svc

log = logging.getLogger(__name__)


_AI_USER_ID: int = 0  # sentinel; AI takes the unfilled seat
_AI_DISPLAY: str = "Discoin AI"

# Material values for the AI eval (centipawns, king omitted -- the
# library detects mate so eval never needs to score the king).
_MATERIAL: dict[int, int] = {1: 100, 2: 320, 3: 330, 4: 500, 5: 900}

# Per-difficulty AI search depth and tiebreak randomness. 'normal'
# preserves the legacy depth=2 / randint(0, 5) behaviour so existing
# matches play exactly the same as before; 'easy' blunders frequently
# (depth=1 + heavy noise), 'hard' looks one full ply further.
_AI_DIFFICULTIES: tuple[str, ...] = ("easy", "normal", "hard")
_AI_DEPTH: dict[str, int] = {"easy": 1, "normal": 2, "hard": 3}
_AI_NOISE: dict[str, int] = {"easy": 60, "normal": 5, "hard": 0}
_AI_DEFAULT_DIFFICULTY: str = "normal"


def _normalise_difficulty(raw: Optional[str]) -> Optional[str]:
    """Map user input to a known difficulty key, or None if unrecognised."""
    if raw is None:
        return _AI_DEFAULT_DIFFICULTY
    val = raw.strip().lower()
    if val in _AI_DIFFICULTIES:
        return val
    return None


def _bet_color(token: str) -> int:
    return C_GOLD if token == "GBC" else C_NAVY


def _ai_pick_move(
    board: "_chess.Board", depth: int = 2, noise: int = 5,
) -> "_chess.Move":
    """Pick a move via shallow negamax with material + mobility eval.

    ``depth`` controls how many plies the negamax searches; ``noise`` is
    the maximum random tiebreak added to each move's score (centipawns).
    'easy' difficulty raises noise so the AI walks into hanging pieces;
    'hard' zeroes noise and bumps depth so the AI plays its top line.
    """
    def evaluate(b: "_chess.Board") -> int:
        if b.is_checkmate():
            return -100_000 if b.turn == _chess.WHITE else 100_000
        if b.is_stalemate() or b.is_insufficient_material():
            return 0
        score = 0
        for piece_type, val in _MATERIAL.items():
            score += val * len(b.pieces(piece_type, _chess.WHITE))
            score -= val * len(b.pieces(piece_type, _chess.BLACK))
        score += len(list(b.legal_moves)) * (1 if b.turn == _chess.WHITE else -1)
        return score

    def negamax(b: "_chess.Board", d: int, color: int) -> int:
        if d == 0 or b.is_game_over():
            return color * evaluate(b)
        best = -10**9
        for mv in b.legal_moves:
            b.push(mv)
            val = -negamax(b, d - 1, -color)
            b.pop()
            if val > best:
                best = val
        return best

    color = 1 if board.turn == _chess.WHITE else -1
    moves = list(board.legal_moves)
    random.shuffle(moves)
    best_score = -10**9
    best_move = moves[0]
    for mv in moves:
        board.push(mv)
        s = -negamax(board, depth - 1, -color)
        board.pop()
        if noise > 0:
            s += random.randint(0, noise)
        if s > best_score:
            best_score = s
            best_move = mv
    return best_move


# ELO update with K=32 (chess.com casual default).
def _elo_update(winner_elo: int, loser_elo: int, draw: bool = False) -> tuple[int, int]:
    K = 32
    expected_w = 1.0 / (1.0 + 10 ** ((loser_elo - winner_elo) / 400.0))
    expected_l = 1.0 - expected_w
    if draw:
        new_w = winner_elo + int(round(K * (0.5 - expected_w)))
        new_l = loser_elo + int(round(K * (0.5 - expected_l)))
    else:
        new_w = winner_elo + int(round(K * (1.0 - expected_w)))
        new_l = loser_elo + int(round(K * (0.0 - expected_l)))
    return max(100, new_w), max(100, new_l)


@dataclass
class _MatchRow:
    match_id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    white_user_id: int
    black_user_id: Optional[int]
    ai_side: Optional[str]
    bet_token: str
    bet_amount_raw: int
    fen: str
    move_history: list[str]
    status: str
    turn_user_id: int
    auto_bump: bool = False
    ai_difficulty: str = _AI_DEFAULT_DIFFICULTY

    @classmethod
    def from_dict(cls, row: dict) -> "_MatchRow":
        mh = row.get("move_history") or []
        if isinstance(mh, str):
            try:
                import json as _j
                mh = _j.loads(mh)
            except Exception:
                mh = []
        return cls(
            match_id=int(row["match_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row.get("message_id") else None,
            white_user_id=int(row["white_user_id"]),
            black_user_id=(
                int(row["black_user_id"]) if row.get("black_user_id") else None
            ),
            ai_side=row.get("ai_side"),
            bet_token=str(row.get("bet_token") or "USD"),
            bet_amount_raw=int(row.get("bet_amount_raw") or 0),
            fen=str(row["fen"]),
            move_history=list(mh),
            status=str(row["status"]),
            turn_user_id=int(row["turn_user_id"]),
            auto_bump=bool(row.get("auto_bump") or False),
            ai_difficulty=str(
                row.get("ai_difficulty") or _AI_DEFAULT_DIFFICULTY
            ),
        )

    def board(self) -> "_chess.Board":
        return _chess.Board(self.fen)

    def opponent_id(self, uid: int) -> Optional[int]:
        if self.white_user_id == uid:
            return self.black_user_id
        if self.black_user_id == uid:
            return self.white_user_id
        return None

    def player_side(self, uid: int) -> Optional[str]:
        if self.white_user_id == uid:
            return "white"
        if self.black_user_id == uid:
            return "black"
        return None


class _IxCtx:
    """DiscoContext-shaped adapter for interaction-driven flows."""

    def __init__(self, interaction: discord.Interaction, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.guild = interaction.guild
        self.guild_id = interaction.guild_id
        self.channel = interaction.channel
        self.author = interaction.user
        self.message = interaction.message
        self._interaction = interaction
        try:
            cp = bot.command_prefix
            self.prefix = cp(bot, None) if callable(cp) else (
                cp[0] if isinstance(cp, (list, tuple)) else str(cp)
            )
        except Exception:
            self.prefix = ","

    async def send(self, *args, **kwargs):
        return await self._interaction.channel.send(*args, **kwargs)

    async def reply(self, *args, **kwargs):
        kwargs.pop("mention_author", None)
        return await self._interaction.channel.send(*args, **kwargs)

    async def reply_error(self, msg: str) -> None:
        await self._interaction.channel.send(
            embed=card("Error", color=C_ERROR).description(msg).build(),
        )


_PIECE_NAME: dict[str, str] = {
    "K": "King", "Q": "Queen", "R": "Rook",
    "B": "Bishop", "N": "Knight", "P": "Pawn",
}


def _from_select_options(board: "_chess.Board") -> list[discord.SelectOption]:
    """One option per from-square that has at least one legal move."""
    by_from: dict[int, list["_chess.Move"]] = {}
    for mv in board.legal_moves:
        by_from.setdefault(mv.from_square, []).append(mv)
    out: list[discord.SelectOption] = []
    for sq in sorted(by_from.keys()):
        piece = board.piece_at(sq)
        if piece is None:
            continue
        sq_name = _chess.square_name(sq)
        name = _PIECE_NAME.get(piece.symbol().upper(), "?")
        n = len(by_from[sq])
        out.append(
            discord.SelectOption(
                label=f"{sq_name}  -  {name}",
                value=sq_name,
                description=f"{n} legal move{'s' if n != 1 else ''}",
            ),
        )
        if len(out) >= 25:
            break
    return out


def _to_select_options(
    board: "_chess.Board", from_sq_name: str,
) -> list[discord.SelectOption]:
    """Legal destinations for ``from_sq_name`` as Select options."""
    try:
        from_sq = _chess.parse_square(from_sq_name)
    except ValueError:
        return []
    out: list[discord.SelectOption] = []
    for mv in board.legal_moves:
        if mv.from_square != from_sq:
            continue
        san = board.san(mv)  # e.g. "Nf3", "exd5", "O-O", "e8=Q"
        uci = mv.uci()        # canonical, used as the value
        captured = board.is_capture(mv)
        check_or_mate = ""
        # Quick test push to detect check/mate without leaving residue.
        board.push(mv)
        if board.is_checkmate():
            check_or_mate = " (mate)"
        elif board.is_check():
            check_or_mate = " (check)"
        board.pop()
        desc = "capture" if captured else ""
        if check_or_mate:
            desc = (desc + check_or_mate).strip(" -·")
        if mv.promotion:
            desc = (desc + " promotion").strip()
        out.append(
            discord.SelectOption(
                label=f"{san}{check_or_mate}"[:100],
                value=uci,
                description=(desc[:100] or "step"),
            ),
        )
        if len(out) >= 25:
            break
    return out


class _ChessGameView(discord.ui.View):
    """Interactive chess view: From / To Selects + action buttons."""

    def __init__(
        self, cog: "ChessCog", match_id: int,
        auto_bump: bool = False,
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = int(match_id)
        self.from_sq: Optional[str] = None  # e.g. "e2"
        self.auto_bump = bool(auto_bump)
        self._lock = asyncio.Lock()
        self._build()

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        m = await self.cog._fetch_match(self.match_id)
        if m is None or m.status != "active":
            await interaction.response.send_message(
                "This match is no longer active.", ephemeral=True,
            )
            return False
        if int(interaction.user.id) != int(m.turn_user_id):
            await interaction.response.send_message(
                "Not your turn.", ephemeral=True,
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        self.add_item(_ChessFromSelect(self))
        self.add_item(_ChessToSelect(self))
        # Row 2: display controls.
        self.add_item(_ChessBtnRefresh(self))
        self.add_item(_ChessBtnBump(self))
        self.add_item(_ChessBtnAutoBump(self, current=self.auto_bump))
        # Row 3: per-turn game actions.
        self.add_item(_ChessBtnClearFrom(self))
        self.add_item(_ChessBtnResign(self))


class _ChessFromSelect(discord.ui.Select):
    def __init__(self, view: _ChessGameView) -> None:
        self._game = view
        super().__init__(
            placeholder="Pick a piece to move...",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label="(loading)", value="_pending")],
            row=0,
        )

    async def _refresh_options(self) -> None:
        m = await self._game.cog._fetch_match(self._game.match_id)
        if m is None:
            return
        opts = _from_select_options(m.board())
        if not opts:
            opts = [discord.SelectOption(label="(no legal moves)", value="_none")]
            self.disabled = True
        else:
            self.disabled = False
        # Mark the player's current pick as default if still legal.
        if self._game.from_sq:
            for o in opts:
                if o.value == self._game.from_sq:
                    o.default = True
                    break
        self.options = opts

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        if val.startswith("_"):
            await interaction.response.defer()
            return
        self._game.from_sq = val
        await self._game.cog._refresh_view_message(
            self._game, interaction=interaction,
        )


class _ChessToSelect(discord.ui.Select):
    def __init__(self, view: _ChessGameView) -> None:
        self._game = view
        super().__init__(
            placeholder="Pick a destination...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="(pick from-square first)", value="_pending"),
            ],
            disabled=True,
            row=1,
        )

    async def _refresh_options(self) -> None:
        if not self._game.from_sq:
            self.options = [
                discord.SelectOption(label="(pick from-square first)", value="_pending"),
            ]
            self.disabled = True
            self.placeholder = "Pick a destination... (choose from-square first)"
            return
        m = await self._game.cog._fetch_match(self._game.match_id)
        if m is None:
            return
        opts = _to_select_options(m.board(), self._game.from_sq)
        if not opts:
            opts = [discord.SelectOption(label="(no destinations)", value="_none")]
            self.disabled = True
        else:
            self.disabled = False
        self.placeholder = f"From {self._game.from_sq} -- pick destination..."
        self.options = opts

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        if val.startswith("_"):
            await interaction.response.defer()
            return
        await self._game.cog._apply_view_move(
            self._game, interaction, uci=val,
        )


class _ChessBtnRefresh(discord.ui.Button):
    def __init__(self, view: _ChessGameView) -> None:
        super().__init__(
            label="Refresh", style=discord.ButtonStyle.secondary,
            emoji="\U0001F501", row=2,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._refresh_view_message(
            self._game, interaction=interaction,
        )


class _ChessBtnClearFrom(discord.ui.Button):
    def __init__(self, view: _ChessGameView) -> None:
        super().__init__(
            label="Clear pick", style=discord.ButtonStyle.secondary,
            emoji="\U000021A9", row=3,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self._game.from_sq = None
        await self._game.cog._refresh_view_message(
            self._game, interaction=interaction,
        )


class _ChessBtnBump(discord.ui.Button):
    """Re-post the chess match panel at the bottom of the channel."""

    def __init__(self, view: _ChessGameView) -> None:
        super().__init__(
            label="Bump", style=discord.ButtonStyle.secondary,
            emoji="\U0001F53C", row=2,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._bump_via_view(
            self._game, interaction,
        )


class _ChessBtnAutoBump(discord.ui.Button):
    """Toggle auto-bump on/off for this match."""

    def __init__(self, view: _ChessGameView, current: bool = False) -> None:
        style = (
            discord.ButtonStyle.success if current
            else discord.ButtonStyle.secondary
        )
        label = "Auto-bump: ON" if current else "Auto-bump: OFF"
        super().__init__(
            label=label, style=style,
            emoji="\U0001F501", row=2,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._toggle_auto_bump(
            self._game, interaction,
        )


class _ChessBtnResign(discord.ui.Button):
    def __init__(self, view: _ChessGameView) -> None:
        super().__init__(
            label="Resign", style=discord.ButtonStyle.danger,
            emoji="\U0001F3F3", row=3,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._resign_via_view(self._game, interaction)


class _ChallengeView(discord.ui.View):
    """Accept/decline buttons on a PvP challenge."""

    def __init__(
        self, *, challenger_id: int, target_id: int, on_accept,
        bet_token: str, bet_raw: int,
    ) -> None:
        super().__init__(timeout=120)
        self.challenger_id = challenger_id
        self.target_id = target_id
        self._on_accept = on_accept
        self.bet_token = bet_token
        self.bet_raw = bet_raw
        self.accepted: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "This challenge isn't for you.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.accepted = True
        for c in self.children:
            c.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self._on_accept(interaction)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            embed=card("Challenge Declined", color=C_NEUTRAL).description(
                "The challenge was declined."
            ).build(),
        )
        self.stop()


class ChessCog(commands.Cog):
    """Gamba Network chess: PvE + PvP + leaderboard + ELO."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        if _chess is None:
            log.error(
                "python-chess is not installed; chess cog will refuse all "
                "commands. Install via requirements.txt: %s",
                _chess_import_error,
            )

    # ── helpers ──────────────────────────────────────────────────────────

    async def _get_active_match(
        self, ctx: DiscoContext, uid: int,
    ) -> Optional[_MatchRow]:
        row = await ctx.db.fetch_one(
            """
            SELECT * FROM gamba_chess_matches
             WHERE guild_id=$1 AND status='active'
               AND (white_user_id=$2 OR black_user_id=$2)
             ORDER BY started_at DESC LIMIT 1
            """,
            ctx.guild_id, uid,
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    async def _get_match(
        self, ctx: DiscoContext, match_id: int,
    ) -> Optional[_MatchRow]:
        row = await ctx.db.fetch_one(
            "SELECT * FROM gamba_chess_matches WHERE match_id=$1",
            match_id,
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    async def _fetch_match(self, match_id: int) -> Optional[_MatchRow]:
        """ctx-free version used by the interactive view."""
        row = await self.bot.db.fetch_one(
            "SELECT * FROM gamba_chess_matches WHERE match_id=$1",
            int(match_id),
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    def _ensure_engine(self, ctx: DiscoContext) -> bool:
        if _chess is not None:
            return True
        # Fire-and-forget reply -- the import error is already logged.
        asyncio.create_task(ctx.reply_error(
            "Chess engine is not installed on this deployment. "
            "Re-deploy after `pip install python-chess`."
        ))
        return False

    async def _validate_bet(
        self, ctx: DiscoContext, token: str, amt_h: float,
    ) -> tuple[bool, int, str]:
        """Confirm the user has enough of ``token`` to cover ``amt_h``.

        Returns ``(ok, raw_amount, error)`` -- when ok is False, error is
        a player-readable string the caller should pass to ``reply_error``.
        """
        if amt_h <= 0:
            return False, 0, "Bet must be positive."
        token = token.upper()
        if token not in Config.GAMBA_BET_TOKENS:
            return False, 0, (
                f"Bets must be one of: "
                f"{', '.join(sorted(Config.GAMBA_BET_TOKENS))}."
            )
        amt_raw = int(to_raw(amt_h))
        if token == "USD":
            row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            bal_raw = int(row["wallet"]) if row else 0
        else:
            # GAMBA_BET_TOKENS narrows non-USD to GBC, which lives in
            # wallet_holdings on the gam-network short.
            h = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id,
                Config.GAMBA_NETWORK_SHORT, token,
            )
            bal_raw = int(h["amount"]) if h else 0
        if bal_raw < amt_raw:
            return False, 0, (
                f"Insufficient balance. You have "
                f"{to_human(bal_raw):,.4f} {token}."
            )
        return True, amt_raw, ""

    async def _escrow_take(
        self, ctx: DiscoContext, uid: int, token: str, amt_raw: int,
    ) -> None:
        """Burn ``amt_raw`` from ``uid`` -- the bot holds the escrow notionally."""
        if amt_raw <= 0:
            return
        if token == "USD":
            await ctx.db.update_wallet(uid, ctx.guild_id, -int(amt_raw))
        else:
            await ctx.db.update_wallet_holding(
                uid, ctx.guild_id,
                Config.GAMBA_NETWORK_SHORT, token, -int(amt_raw),
            )

    async def _escrow_pay(
        self, ctx: DiscoContext, uid: int, token: str, amt_raw: int,
    ) -> None:
        if amt_raw <= 0:
            return
        if token == "USD":
            await ctx.db.update_wallet(uid, ctx.guild_id, int(amt_raw))
        else:
            await ctx.db.update_wallet_holding(
                uid, ctx.guild_id,
                Config.GAMBA_NETWORK_SHORT, token, int(amt_raw),
            )

    async def _save_position(
        self, ctx: DiscoContext, m: _MatchRow, board: "_chess.Board",
    ) -> None:
        import json as _j
        m.fen = board.fen()
        m.move_history = [mv.uci() for mv in board.move_stack]
        next_uid = (
            m.white_user_id if board.turn == _chess.WHITE else m.black_user_id
        )
        # AI seat shows up as next-turn = AI sentinel so the renderer can hide
        # the prompt; resolved on the next ai-move tick.
        m.turn_user_id = next_uid if next_uid else _AI_USER_ID
        await ctx.db.execute(
            """
            UPDATE gamba_chess_matches
               SET fen=$2, move_history=$3::jsonb,
                   turn_user_id=$4, last_move_at=NOW()
             WHERE match_id=$1
            """,
            m.match_id, m.fen, _j.dumps(m.move_history), int(m.turn_user_id),
        )

    async def _bump_stats(
        self, ctx: DiscoContext, uid: int,
        wins: int = 0, losses: int = 0, draws: int = 0,
        vs_ai: bool = False,
        wagered_raw: int = 0, won_raw: int = 0,
        elo_delta: int = 0,
    ) -> int:
        """Upsert per-user stats. Returns new ELO."""
        await ctx.db.execute(
            """
            INSERT INTO gamba_chess_stats (user_id, guild_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, guild_id) DO NOTHING
            """,
            uid, ctx.guild_id,
        )
        new_elo = await ctx.db.fetch_val(
            """
            UPDATE gamba_chess_stats
               SET wins             = wins             + $3,
                   losses           = losses           + $4,
                   draws            = draws            + $5,
                   vs_ai_wins       = vs_ai_wins       + $6,
                   vs_ai_losses     = vs_ai_losses     + $7,
                   vs_ai_draws      = vs_ai_draws      + $8,
                   total_wagered_raw = total_wagered_raw + $9::numeric,
                   total_won_raw     = total_won_raw     + $10::numeric,
                   elo_rating       = GREATEST(100, elo_rating + $11),
                   last_played      = NOW()
             WHERE user_id=$1 AND guild_id=$2
            RETURNING elo_rating
            """,
            uid, ctx.guild_id,
            int(wins), int(losses), int(draws),
            int(wins if vs_ai else 0),
            int(losses if vs_ai else 0),
            int(draws if vs_ai else 0),
            int(wagered_raw), int(won_raw),
            int(elo_delta),
        )
        return int(new_elo or 1200)

    async def _get_elo(self, ctx: DiscoContext, uid: int) -> int:
        row = await ctx.db.fetch_one(
            "SELECT elo_rating FROM gamba_chess_stats WHERE user_id=$1 AND guild_id=$2",
            uid, ctx.guild_id,
        )
        return int(row["elo_rating"]) if row else 1200

    # ── render helpers ────────────────────────────────────────────────────

    _BOARD_FILENAME: str = "chess_board.png"

    def _build_board_file(
        self, m: _MatchRow, *, viewer_id: Optional[int] = None,
    ) -> discord.File:
        """Render the position to a PNG and wrap it in a ``discord.File``.

        The board is oriented so the side to move sits at the bottom --
        the view flips between turns so the active player always reads
        their own pieces in the front rows. ``viewer_id`` is accepted
        for caller compatibility but no longer changes orientation.
        """
        del viewer_id
        board = m.board()
        flip = board.turn == _chess.BLACK
        png = render_chess_png(board, flip=flip)
        return discord.File(io.BytesIO(png), filename=self._BOARD_FILENAME)

    def _build_match_payload(
        self, ctx, m: _MatchRow, *,
        result: Optional[str] = None, extra_lines: list[str] | None = None,
        viewer_id: Optional[int] = None,
    ) -> tuple[discord.Embed, discord.File]:
        """Return ``(embed, file)`` for a chess match message.

        Callers pass ``file`` as the message attachment (``file=`` on a
        fresh send, ``attachments=[file]`` on an edit) so the embed's
        ``attachment://`` image reference resolves.
        """
        embed = self._build_match_embed(
            ctx, m, result=result, extra_lines=extra_lines,
            viewer_id=viewer_id,
        )
        file = self._build_board_file(m, viewer_id=viewer_id)
        return embed, file

    def _build_match_embed(
        self, ctx, m: _MatchRow, *,
        result: Optional[str] = None, extra_lines: list[str] | None = None,
        viewer_id: Optional[int] = None,
    ) -> discord.Embed:
        """Render the live match. ``ctx`` is a DiscoContext or _IxCtx.

        The board itself is attached as a PNG via ``_build_board_file``;
        this embed only references it by ``attachment://`` URL. Callers
        that need both at once should use ``_build_match_payload``.

        ``viewer_id`` is accepted for caller compatibility but no longer
        affects orientation -- the board image flips automatically with
        the side to move.
        """
        del viewer_id
        board = m.board()
        white_label = self._user_label(ctx, m.white_user_id, m.ai_side == "white")
        black_label = self._user_label(ctx, m.black_user_id, m.ai_side == "black")
        bet_label = (
            fmt_usd(to_human(m.bet_amount_raw))
            if m.bet_token == "USD"
            else fmt_token(to_human(m.bet_amount_raw), m.bet_token)
        )

        if result:
            title = "♞ Chess  ·  Match Finished"
        else:
            title = (
                f"♞ Chess  ·  ♔ {white_label}  vs  ♚ {black_label}"
            )

        next_to_move = "White" if board.turn == _chess.WHITE else "Black"
        next_uid = m.white_user_id if board.turn == _chess.WHITE else m.black_user_id
        is_ai_turn = (
            (board.turn == _chess.WHITE and m.ai_side == "white") or
            (board.turn == _chess.BLACK and m.ai_side == "black")
        )
        history_str = self._format_history_san(m)

        # Build the description top-down: result banner -> board -> meta.
        sections: list[str] = []
        if result:
            if extra_lines:
                sections.append("\n".join(extra_lines))
        else:
            mover = (
                _AI_DISPLAY if is_ai_turn else (
                    f"<@{next_uid}>" if next_uid else "?"
                )
            )
            check_str = "  ·  **Check!**" if board.is_check() else ""
            sections.append(
                f"**{next_to_move}** to move  ·  {mover}{check_str}"
            )

        meta_bits: list[str] = [f"Bet **{bet_label}**"]
        if m.ai_side:
            meta_bits.append(f"AI **{m.ai_difficulty}**")
        if result:
            meta_bits.append(f"Total moves `{board.fullmove_number}`")
        else:
            meta_bits.append(f"Move `{board.fullmove_number}`")
        if history_str:
            meta_bits.append(f"`{history_str}`")
        sections.append("  ·  ".join(meta_bits))

        if result and not extra_lines:
            # Defensive -- a finished embed without a result banner is
            # unusual, but we still want a closing line.
            sections.append("Match finished.")

        builder = card(title, color=_bet_color(m.bet_token)).description(
            "\n".join(sections)
        ).image(f"attachment://{self._BOARD_FILENAME}")
        if not result:
            builder = builder.footer(
                "Pick a piece -> destination, or type "
                f"{getattr(ctx, 'prefix', ',')}chess move <uci>"
            )
        return builder.build()

    def _format_history_san(self, m: _MatchRow) -> str:
        """Replay the move history through python-chess and return SAN pairs."""
        if not m.move_history:
            return ""
        try:
            replay = _chess.Board()
            sans: list[str] = []
            for uci in m.move_history:
                try:
                    mv = _chess.Move.from_uci(uci)
                except ValueError:
                    sans.append(uci)
                    continue
                if mv not in replay.legal_moves:
                    sans.append(uci)
                    continue
                sans.append(replay.san(mv))
                replay.push(mv)
        except Exception:
            return " ".join(m.move_history[-12:])
        # Pair into "1. e4 e5  2. Nf3 Nc6 ..." -- show last 6 full-moves.
        pairs: list[str] = []
        for i in range(0, len(sans), 2):
            num = i // 2 + 1
            white = sans[i] if i < len(sans) else ""
            black = sans[i + 1] if i + 1 < len(sans) else ""
            pairs.append(f"{num}.{white} {black}".strip())
        return "  ".join(pairs[-6:])

    def _user_label(
        self, ctx, uid: Optional[int], is_ai: bool,
    ) -> str:
        if is_ai or not uid:
            return f"\U0001F916 {_AI_DISPLAY}"
        guild = getattr(ctx, "guild", None)
        member = guild.get_member(uid) if guild else None
        return member.display_name if member else f"<@{uid}>"

    # ── view-driven helpers (called from interactive selects/buttons) ──

    async def _send_match_with_view(
        self, ctx, m: _MatchRow,
    ) -> Optional[discord.Message]:
        view = _ChessGameView(self, m.match_id, auto_bump=m.auto_bump)
        for child in view.children:
            if isinstance(child, _ChessFromSelect):
                await child._refresh_options()
            elif isinstance(child, _ChessToSelect):
                await child._refresh_options()
        embed, file = self._build_match_payload(
            ctx, m, viewer_id=int(getattr(ctx.author, "id", 0)),
        )
        msg = await ctx.send(embed=embed, file=file, view=view)
        try:
            await ctx.db.execute(
                "UPDATE gamba_chess_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(msg.id),
            )
        except Exception:
            log.debug("chess: storing message_id failed", exc_info=True)
        return msg

    async def _refresh_view_message(
        self, view: "_ChessGameView",
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        m = await self._fetch_match(view.match_id)
        if m is None:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.defer()
            return
        # Sync auto-bump state + rebuild children so the AutoBump
        # button label / style reflects current state.
        view.auto_bump = bool(m.auto_bump)
        view._build()
        for child in view.children:
            if isinstance(child, _ChessFromSelect):
                await child._refresh_options()
            elif isinstance(child, _ChessToSelect):
                await child._refresh_options()
        viewer = (
            int(interaction.user.id) if interaction is not None
            else int(m.turn_user_id)
        )
        ctx_proxy = (
            _IxCtx(interaction, self.bot) if interaction is not None
            else None
        )
        if ctx_proxy is None:
            class _Mini:
                guild = None
                prefix = ","
                class author: id = 0
            ctx_proxy = _Mini()
        embed, file = self._build_match_payload(ctx_proxy, m, viewer_id=viewer)
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.message.edit(
                        embed=embed, attachments=[file], view=view,
                    )
                else:
                    await interaction.response.edit_message(
                        embed=embed, attachments=[file], view=view,
                    )
            except discord.HTTPException:
                log.debug("chess refresh_view_message edit failed", exc_info=True)

    async def _bump_view_message(
        self, view: "_ChessGameView",
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        """Delete the current chess match message + re-post fresh at bottom."""
        m = await self._fetch_match(view.match_id)
        if m is None:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.defer()
            return
        view.auto_bump = bool(m.auto_bump)
        view._build()
        for child in view.children:
            if isinstance(child, _ChessFromSelect):
                await child._refresh_options()
            elif isinstance(child, _ChessToSelect):
                await child._refresh_options()
        viewer = (
            int(interaction.user.id) if interaction is not None
            else int(m.turn_user_id)
        )
        ctx_proxy = (
            _IxCtx(interaction, self.bot) if interaction is not None
            else None
        )
        if ctx_proxy is None:
            class _Mini:
                guild = None
                prefix = ","
                class author: id = 0
            ctx_proxy = _Mini()
        embed, file = self._build_match_payload(ctx_proxy, m, viewer_id=viewer)
        channel = (interaction.channel if interaction is not None else None)
        if channel is None:
            channel = self.bot.get_channel(int(m.channel_id))
        if channel is None:
            return
        if interaction is not None and not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        if m.message_id:
            try:
                old_msg = await channel.fetch_message(int(m.message_id))
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.debug("chess bump: source delete failed", exc_info=True)
        try:
            sent = await channel.send(embed=embed, file=file, view=view)
        except discord.HTTPException:
            log.debug("chess bump: re-post failed", exc_info=True)
            return
        try:
            view.message = sent  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            await self.bot.db.execute(
                "UPDATE gamba_chess_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(sent.id),
            )
        except Exception:
            log.debug("chess bump: message_id update failed", exc_info=True)

    async def _bump_via_view(
        self, view: "_ChessGameView", interaction: discord.Interaction,
    ) -> None:
        await self._bump_view_message(view, interaction=interaction)

    async def _toggle_auto_bump(
        self, view: "_ChessGameView", interaction: discord.Interaction,
    ) -> None:
        m = await self._fetch_match(view.match_id)
        if m is None or m.status != "active":
            await interaction.response.send_message(
                "Match is no longer active.", ephemeral=True,
            )
            return
        new = not bool(m.auto_bump)
        await self.bot.db.execute(
            "UPDATE gamba_chess_matches SET auto_bump=$2 WHERE match_id=$1",
            m.match_id, new,
        )
        await self._refresh_view_message(view, interaction=interaction)

    async def _apply_view_move(
        self, view: "_ChessGameView",
        interaction: discord.Interaction, *, uci: str,
    ) -> None:
        if view._lock.locked():
            await interaction.response.send_message(
                "Move already in progress -- one sec.", ephemeral=True,
            )
            return
        async with view._lock:
            ctx = _IxCtx(interaction, self.bot)
            m = await self._fetch_match(view.match_id)
            if m is None or m.status != "active":
                await interaction.response.send_message(
                    "Match is no longer active.", ephemeral=True,
                )
                return
            if int(interaction.user.id) != int(m.turn_user_id):
                await interaction.response.send_message(
                    "Not your turn.", ephemeral=True,
                )
                return
            board = m.board()
            try:
                mv = _chess.Move.from_uci(uci)
            except ValueError:
                await interaction.response.send_message(
                    f"`{uci}` is invalid UCI.", ephemeral=True,
                )
                return
            if mv not in board.legal_moves:
                await interaction.response.send_message(
                    f"`{uci}` is no longer legal -- click Refresh.",
                    ephemeral=True,
                )
                return
            board.push(mv)
            await self._save_position(ctx, m, board)
            view.from_sq = None
            if board.is_game_over():
                if not interaction.response.is_done():
                    await interaction.response.defer()
                for child in view.children:
                    child.disabled = True  # type: ignore[attr-defined]
                view.stop()
                try:
                    await interaction.message.edit(view=view)
                except discord.HTTPException:
                    pass
                await self._finalise(ctx, m, board)
                return
            m_post = await self._fetch_match(view.match_id) or m
            ai_to_move = (
                (board.turn == _chess.WHITE and m_post.ai_side == "white") or
                (board.turn == _chess.BLACK and m_post.ai_side == "black")
            )
            if ai_to_move:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                await self._run_ai_turn(ctx, m_post)
                m_after = await self._fetch_match(view.match_id) or m_post
                board_after = m_after.board()
                if board_after.is_game_over():
                    for child in view.children:
                        child.disabled = True  # type: ignore[attr-defined]
                    view.stop()
                    try:
                        await interaction.message.edit(view=view)
                    except discord.HTTPException:
                        pass
                    await self._finalise(ctx, m_after, board_after)
                    return
                # Auto-bump if enabled, otherwise edit in place.
                m_now = await self._fetch_match(view.match_id) or m_after
                if m_now.auto_bump:
                    await self._bump_view_message(view, interaction=interaction)
                else:
                    await self._refresh_view_message(view, interaction=interaction)
                return
            # PvP path: opponent's turn now.
            m_now = await self._fetch_match(view.match_id) or m
            if m_now.auto_bump:
                await self._bump_view_message(view, interaction=interaction)
            else:
                await self._refresh_view_message(view, interaction=interaction)

    async def _resign_via_view(
        self, view: "_ChessGameView", interaction: discord.Interaction,
    ) -> None:
        ctx = _IxCtx(interaction, self.bot)
        m = await self._fetch_match(view.match_id)
        if m is None or m.status != "active":
            await interaction.response.send_message(
                "Match is no longer active.", ephemeral=True,
            )
            return
        side = m.player_side(int(interaction.user.id))
        if not side:
            await interaction.response.send_message(
                "You're not in this match.", ephemeral=True,
            )
            return
        winner_side = "white" if side == "black" else "black"
        await ctx.db.execute(
            "UPDATE gamba_chess_matches SET status='resigned', ended_at=NOW() WHERE match_id=$1",
            m.match_id,
        )
        if not interaction.response.is_done():
            await interaction.response.defer()
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        view.stop()
        try:
            await interaction.message.edit(view=view)
        except discord.HTTPException:
            pass
        await self._settle_payouts(
            ctx, m, winner_side=winner_side, draw=False, by_resign=True,
        )

    async def _refresh_or_resend(self, ctx, m: _MatchRow) -> None:
        """Edit the live match message in place; fall back to sending fresh."""
        if m.message_id and ctx.channel:
            try:
                msg = await ctx.channel.fetch_message(int(m.message_id))
                view = _ChessGameView(self, m.match_id)
                for child in view.children:
                    if isinstance(child, _ChessFromSelect):
                        await child._refresh_options()
                    elif isinstance(child, _ChessToSelect):
                        await child._refresh_options()
                viewer = int(getattr(ctx.author, "id", 0))
                embed, file = self._build_match_payload(
                    ctx, m, viewer_id=viewer,
                )
                await msg.edit(
                    embed=embed, attachments=[file], view=view,
                )
                return
            except (discord.HTTPException, discord.NotFound):
                pass
        await self._send_match_with_view(ctx, m)

    # ── ,chess group ─────────────────────────────────────────────────────

    @commands.group(
        name="chess", aliases=["chs"], invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def chess(self, ctx: DiscoContext) -> None:
        """Chess hub. ``,chess play`` to start vs AI, ``,chess help`` for more."""
        await self.chess_help(ctx)

    @chess.command(name="help")
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_help(self, ctx: DiscoContext) -> None:
        embed = card(
            "♞ Chess Commands", color=C_INFO,
        ).description(
            "Play chess against the AI or another player. Wins mint **GAMBIT** "
            "and bump your ELO; lifetime bets settle in USD or GBC."
        ).field(
            "Start a match",
            f"`{ctx.prefix}chess play [bet] [token] [easy|normal|hard]` -- vs AI\n"
            f"`{ctx.prefix}chess challenge @user [bet] [token]` -- vs player",
            False,
        ).field(
            "During a match",
            f"`{ctx.prefix}chess move <uci>` -- e.g. `e2e4`, `g1f3`, `e7e8q`\n"
            f"`{ctx.prefix}chess board` -- redraw the position\n"
            f"`{ctx.prefix}chess resign` -- forfeit the match",
            False,
        ).field(
            "Records",
            f"`{ctx.prefix}chess leaderboard` -- top ELO\n"
            f"`{ctx.prefix}chess stats [@user]` -- personal record",
            False,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @chess.command(name="play", aliases=["start", "ai"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_play(
        self, ctx: DiscoContext,
        bet: Optional[str] = None, token: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> None:
        """Start a match vs the Discoin AI. Optional bet (refunded on a draw).

        Difficulty: ``easy`` / ``normal`` / ``hard``. Defaults to ``normal``.
        Easy blunders often, hard searches deeper.
        """
        if not self._ensure_engine(ctx):
            return
        existing = await self._get_active_match(ctx, ctx.author.id)
        if existing:
            await ctx.reply_error(
                f"You already have an active chess match (#{existing.match_id}). "
                f"Finish it or `{ctx.prefix}chess resign`."
            )
            return
        diff = _normalise_difficulty(difficulty)
        if diff is None:
            await ctx.reply_error(
                "Difficulty must be `easy`, `normal`, or `hard`."
            )
            return
        bet_h = float(bet) if bet else 0.0
        token = (token or "USD").upper()
        amt_raw = 0
        if bet_h > 0:
            ok, amt_raw, err = await self._validate_bet(ctx, token, bet_h)
            if not ok:
                await ctx.reply_error(err)
                return
            await self._escrow_take(ctx, ctx.author.id, token, amt_raw)
        # Player flips for white. ai_side records which colour the AI took.
        player_white = bool(random.getrandbits(1))
        white_uid = ctx.author.id if player_white else _AI_USER_ID
        black_uid = _AI_USER_ID if player_white else ctx.author.id
        ai_side = "black" if player_white else "white"
        turn_uid = white_uid

        row = await ctx.db.fetch_one(
            """
            INSERT INTO gamba_chess_matches
              (guild_id, channel_id, white_user_id, black_user_id,
               ai_side, bet_token, bet_amount_raw, turn_user_id,
               ai_difficulty)
            VALUES ($1, $2, $3, $4, $5, $6, $7::numeric, $8, $9)
            RETURNING *
            """,
            ctx.guild_id, ctx.channel.id,
            white_uid if white_uid != _AI_USER_ID else 0,
            black_uid if black_uid != _AI_USER_ID else None,
            ai_side, token, int(amt_raw), int(turn_uid), diff,
        )
        # AI takes white -> we need to insert AI in white_user_id=0 above
        # but the schema has NOT NULL on white_user_id; treat 0 as the AI
        # sentinel. The renderer/AI loop checks ai_side, not the id.
        if not row:
            await ctx.reply_error("Could not start match -- try again.")
            if amt_raw > 0:
                await self._escrow_pay(ctx, ctx.author.id, token, amt_raw)
            return
        m = _MatchRow.from_dict(dict(row))
        # If the AI took white it moves first.
        if ai_side == "white":
            await self._run_ai_turn(ctx, m)
            m = await self._get_match(ctx, m.match_id) or m
        await self._send_match_with_view(ctx, m)

    @chess.command(name="challenge", aliases=["vs", "duel"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_challenge(
        self, ctx: DiscoContext, target: discord.Member,
        bet: Optional[str] = None, token: Optional[str] = None,
    ) -> None:
        """Propose a PvP chess match. Both players must cover the bet."""
        if not self._ensure_engine(ctx):
            return
        if target.bot or target.id == ctx.author.id:
            await ctx.reply_error("Pick a real, different opponent.")
            return
        if await self._get_active_match(ctx, ctx.author.id):
            await ctx.reply_error(
                f"You already have an active match. Resign or finish it first."
            )
            return
        if await self._get_active_match(ctx, target.id):
            await ctx.reply_error(
                f"{target.display_name} already has an active match."
            )
            return
        bet_h = float(bet) if bet else 0.0
        token = (token or "USD").upper()
        amt_raw = 0
        if bet_h > 0:
            ok, amt_raw, err = await self._validate_bet(ctx, token, bet_h)
            if not ok:
                await ctx.reply_error(err)
                return
            # Verify target also has the funds before proposing.
            if token == "USD":
                trow = await ctx.db.get_user(target.id, ctx.guild_id)
                tbal = int(trow["wallet"]) if trow else 0
            else:
                th = await ctx.db.get_wallet_holding(
                    target.id, ctx.guild_id,
                    Config.GAMBA_NETWORK_SHORT, token,
                )
                tbal = int(th["amount"]) if th else 0
            if tbal < amt_raw:
                await ctx.reply_error(
                    f"{target.display_name} doesn't have enough {token} "
                    f"to match the bet."
                )
                return

        bet_label = (
            fmt_usd(bet_h) if token == "USD"
            else fmt_token(bet_h, token)
        ) if amt_raw > 0 else "no stakes"

        async def _on_accept(interaction: discord.Interaction) -> None:
            # Re-validate funds at accept time -- balances move while waiting.
            if amt_raw > 0:
                ok2, _, err2 = await self._validate_bet(ctx, token, bet_h)
                if not ok2:
                    await interaction.followup.send(
                        embed=card(
                            "Bet no longer affordable", color=C_ERROR,
                        ).description(err2).build(),
                    )
                    return
                if token == "USD":
                    trow = await ctx.db.get_user(target.id, ctx.guild_id)
                    tbal = int(trow["wallet"]) if trow else 0
                else:
                    th = await ctx.db.get_wallet_holding(
                        target.id, ctx.guild_id,
                        Config.GAMBA_NETWORK_SHORT, token,
                    )
                    tbal = int(th["amount"]) if th else 0
                if tbal < amt_raw:
                    await interaction.followup.send(
                        embed=card(
                            "Bet no longer affordable", color=C_ERROR,
                        ).description(
                            f"{target.display_name} no longer has the bet."
                        ).build(),
                    )
                    return
                await self._escrow_take(ctx, ctx.author.id, token, amt_raw)
                await self._escrow_take(ctx, target.id, token, amt_raw)
            white_uid, black_uid = (
                (ctx.author.id, target.id)
                if random.getrandbits(1)
                else (target.id, ctx.author.id)
            )
            row = await ctx.db.fetch_one(
                """
                INSERT INTO gamba_chess_matches
                  (guild_id, channel_id, white_user_id, black_user_id,
                   bet_token, bet_amount_raw, turn_user_id)
                VALUES ($1, $2, $3, $4, $5, $6::numeric, $7)
                RETURNING *
                """,
                ctx.guild_id, ctx.channel.id,
                int(white_uid), int(black_uid),
                token, int(amt_raw), int(white_uid),
            )
            m = _MatchRow.from_dict(dict(row))
            game_view = _ChessGameView(self, m.match_id)
            for child in game_view.children:
                if isinstance(child, _ChessFromSelect):
                    await child._refresh_options()
                elif isinstance(child, _ChessToSelect):
                    await child._refresh_options()
            embed, file = self._build_match_payload(
                ctx, m, viewer_id=int(white_uid),
            )
            msg = await interaction.followup.send(
                embed=embed, file=file, view=game_view,
            )
            await ctx.db.execute(
                "UPDATE gamba_chess_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(msg.id),
            )

        view = _ChallengeView(
            challenger_id=ctx.author.id, target_id=target.id,
            on_accept=_on_accept, bet_token=token, bet_raw=int(amt_raw),
        )
        embed = card(
            "♞ Chess Challenge", color=_bet_color(token),
        ).description(
            f"{ctx.author.mention} challenges {target.mention} to chess.\n"
            f"Bet: **{bet_label}** each.\n\n"
            f"{target.display_name}: accept or decline below within 2 minutes."
        ).build()
        await ctx.reply(embed=embed, view=view, mention_author=False)

    @chess.command(name="move", aliases=["m", "mv", "play_move"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_move(self, ctx: DiscoContext, *, uci: str) -> None:
        """Make a move in your active match. UCI notation: ``e2e4``, ``g1f3``, ``e7e8q``."""
        if not self._ensure_engine(ctx):
            return
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("You don't have an active chess match.")
            return
        side = m.player_side(ctx.author.id)
        if not side:
            await ctx.reply_error("You're not in this match.")
            return
        board = m.board()
        whose_turn = "white" if board.turn == _chess.WHITE else "black"
        if side != whose_turn:
            await ctx.reply_error(f"It's {whose_turn}'s turn -- not yours.")
            return
        try:
            move = _chess.Move.from_uci(uci.strip())
        except ValueError:
            await ctx.reply_error(
                f"`{uci}` is not valid UCI. Try `e2e4`, `g1f3`, `e7e8q` (promotion)."
            )
            return
        if move not in board.legal_moves:
            # Try SAN as a friendly fallback so casual players can use ``Nf3``.
            try:
                move = board.parse_san(uci.strip())
            except Exception:
                await ctx.reply_error(
                    f"`{uci}` is illegal in this position. "
                    f"`{ctx.prefix}chess board` to re-check."
                )
                return
        board.push(move)
        await self._save_position(ctx, m, board)
        if board.is_game_over():
            await self._finalise(ctx, m, board)
            return
        # Refresh the match row, then run AI if its turn.
        m = await self._get_match(ctx, m.match_id) or m
        ai_to_move = (
            (board.turn == _chess.WHITE and m.ai_side == "white") or
            (board.turn == _chess.BLACK and m.ai_side == "black")
        )
        if ai_to_move:
            await self._run_ai_turn(ctx, m)
            m = await self._get_match(ctx, m.match_id) or m
            board = m.board()
            if board.is_game_over():
                await self._finalise(ctx, m, board)
                return
        await self._refresh_or_resend(ctx, m)

    async def _run_ai_turn(self, ctx: DiscoContext, m: _MatchRow) -> None:
        board = m.board()
        if board.is_game_over():
            return
        depth = _AI_DEPTH.get(m.ai_difficulty, _AI_DEPTH[_AI_DEFAULT_DIFFICULTY])
        noise = _AI_NOISE.get(m.ai_difficulty, _AI_NOISE[_AI_DEFAULT_DIFFICULTY])
        ai_move = _ai_pick_move(board, depth=depth, noise=noise)
        board.push(ai_move)
        await self._save_position(ctx, m, board)

    @chess.command(name="board", aliases=["b", "show", "view"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_board(self, ctx: DiscoContext) -> None:
        """Re-render your active chess match."""
        if not self._ensure_engine(ctx):
            return
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("No active chess match.")
            return
        await self._refresh_or_resend(ctx, m)

    @chess.command(name="resign", aliases=["forfeit", "ff"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_resign(self, ctx: DiscoContext) -> None:
        """Forfeit your active match. Bet goes to the opponent (or burns vs AI)."""
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("No active chess match.")
            return
        side = m.player_side(ctx.author.id)
        if not side:
            await ctx.reply_error("You're not in this match.")
            return
        winner_side = "white" if side == "black" else "black"
        await ctx.db.execute(
            """
            UPDATE gamba_chess_matches
               SET status='resigned', ended_at=NOW()
             WHERE match_id=$1
            """,
            m.match_id,
        )
        await self._settle_payouts(
            ctx, m, winner_side=winner_side, draw=False, by_resign=True,
        )

    # ── finalise ─────────────────────────────────────────────────────────

    async def _finalise(
        self, ctx: DiscoContext, m: _MatchRow, board: "_chess.Board",
    ) -> None:
        """Resolve a finished match: status, payouts, ELO, token mint."""
        outcome = board.outcome()
        winner_side: Optional[str] = None
        draw = False
        status = "draw"
        if outcome and outcome.winner is True:
            winner_side = "white"
            status = "white_won"
        elif outcome and outcome.winner is False:
            winner_side = "black"
            status = "black_won"
        else:
            draw = True
        await ctx.db.execute(
            """
            UPDATE gamba_chess_matches
               SET status=$2, ended_at=NOW()
             WHERE match_id=$1
            """,
            m.match_id, status,
        )
        await self._settle_payouts(
            ctx, m, winner_side=winner_side, draw=draw, by_resign=False,
        )

    async def _settle_payouts(
        self, ctx: DiscoContext, m: _MatchRow,
        *, winner_side: Optional[str], draw: bool, by_resign: bool,
    ) -> None:
        from configs.items_config import SHOP_ITEMS as _SI
        board = m.board()
        is_ai_match = m.ai_side is not None
        bet_h = to_human(m.bet_amount_raw)
        bet_token = m.bet_token
        winner_uid: Optional[int] = None
        loser_uid: Optional[int] = None
        if not draw:
            winner_uid = (
                m.white_user_id if winner_side == "white" else m.black_user_id
            )
            loser_uid = (
                m.black_user_id if winner_side == "white" else m.white_user_id
            )

        # Pot accounting:
        #   PvE (vs AI): on win the player keeps escrow + matching mint from
        #     the house; on loss the escrow burns. On draw the escrow refunds.
        #   PvP: pot is 2x escrow; winner takes all; on draw both refund.
        result_lines: list[str] = []
        if draw:
            if m.bet_amount_raw > 0:
                if m.white_user_id and m.white_user_id != _AI_USER_ID:
                    await self._escrow_pay(
                        ctx, m.white_user_id, bet_token, m.bet_amount_raw,
                    )
                if m.black_user_id and m.black_user_id != _AI_USER_ID:
                    await self._escrow_pay(
                        ctx, m.black_user_id, bet_token, m.bet_amount_raw,
                    )
            result_lines.append("\U0001F91D **Draw** -- bets refunded.")
            for uid in (m.white_user_id, m.black_user_id):
                if not uid or uid == _AI_USER_ID:
                    continue
                await self._bump_stats(
                    ctx, uid, draws=1, vs_ai=is_ai_match,
                    wagered_raw=m.bet_amount_raw,
                )
        else:
            payout_raw = m.bet_amount_raw * (1 if is_ai_match else 2)
            if winner_uid and winner_uid != _AI_USER_ID:
                if payout_raw > 0:
                    await self._escrow_pay(ctx, winner_uid, bet_token, payout_raw)
                # Token mint: profit_usd = bet * 1 (PvE) or bet * 1 (PvP, since
                # the loser's bet is profit). Use bet value as the profit.
                profit_h = bet_h
                profit_usd = await self._convert_to_usd(
                    ctx, bet_token, profit_h,
                )
                # Side Bet Slip doubles the mint.
                doubled = await gamba_svc.consume_if_present(
                    ctx.db, ctx.guild_id, winner_uid, "side_bet_slip",
                )
                # Lucky Chip adds +5% to the USD payout (player gets a
                # bonus credit straight to their wallet -- not the bet).
                lucky_bonus_raw = 0
                if await gamba_svc.consume_if_present(
                    ctx.db, ctx.guild_id, winner_uid, "lucky_chip",
                ):
                    bonus_h = bet_h * float(
                        _SI.get("lucky_chip", {}).get("stats", {}).get(
                            "gamba_win_bonus", 0.0,
                        ) or 0.05,
                    )
                    if bet_token == "USD" and bonus_h > 0:
                        lucky_bonus_raw = int(to_raw(bonus_h))
                        await ctx.db.update_wallet(
                            winner_uid, ctx.guild_id, lucky_bonus_raw,
                        )
                        result_lines.append(
                            f"\U0001F340 Lucky Chip: **+{fmt_usd(bonus_h)}**"
                        )
                if profit_usd > 0:
                    minted_sym, minted_raw = await gamba_svc.award_game_token(
                        ctx.db, ctx.guild_id, winner_uid,
                        "chess", profit_usd, side_bet_double=doubled,
                    )
                    if minted_raw > 0:
                        prefix = "\U0001F3AB Side Bet 2x  " if doubled else ""
                        result_lines.append(
                            f"{prefix}♞ Minted: **"
                            f"{fmt_token(to_human(minted_raw), minted_sym)}**"
                        )
                # ELO + stats update.
                if loser_uid and loser_uid != _AI_USER_ID:
                    w_elo = await self._get_elo(ctx, winner_uid)
                    l_elo = await self._get_elo(ctx, loser_uid)
                    new_w, new_l = _elo_update(w_elo, l_elo)
                    await self._bump_stats(
                        ctx, winner_uid, wins=1, vs_ai=False,
                        wagered_raw=m.bet_amount_raw,
                        won_raw=payout_raw - m.bet_amount_raw,
                        elo_delta=new_w - w_elo,
                    )
                    await self._bump_stats(
                        ctx, loser_uid, losses=1, vs_ai=False,
                        wagered_raw=m.bet_amount_raw,
                        elo_delta=new_l - l_elo,
                    )
                else:
                    await self._bump_stats(
                        ctx, winner_uid, wins=1, vs_ai=True,
                        wagered_raw=m.bet_amount_raw,
                        won_raw=m.bet_amount_raw,
                        elo_delta=10,
                    )
            elif loser_uid and loser_uid != _AI_USER_ID:
                # Player lost vs AI; check House Marker for partial refund.
                if m.bet_amount_raw > 0:
                    refund_pct = float(
                        _SI.get("house_marker", {}).get("stats", {}).get(
                            "gamba_loss_refund", 0.0,
                        ) or 0.25,
                    )
                    if refund_pct > 0 and await gamba_svc.consume_if_present(
                        ctx.db, ctx.guild_id, loser_uid, "house_marker",
                    ):
                        refund_raw = int(m.bet_amount_raw * refund_pct)
                        if refund_raw > 0:
                            await self._escrow_pay(
                                ctx, loser_uid, bet_token, refund_raw,
                            )
                            result_lines.append(
                                f"\U0001F3F4 House Marker: refunded "
                                f"`{to_human(refund_raw):,.4f} {bet_token}`"
                            )
                await self._bump_stats(
                    ctx, loser_uid, losses=1, vs_ai=is_ai_match,
                    wagered_raw=m.bet_amount_raw, elo_delta=-8,
                )

            verdict = (
                "wins by resignation" if by_resign else "checkmates"
            )
            winner_label = self._user_label(
                ctx, winner_uid,
                is_ai_match and (
                    (winner_side == "white" and m.ai_side == "white")
                    or (winner_side == "black" and m.ai_side == "black")
                ),
            )
            result_lines.insert(
                0, f"\U0001F451 **{winner_label}** {verdict}!"
            )
            if m.bet_amount_raw > 0 and winner_uid and winner_uid != _AI_USER_ID:
                pay_label = (
                    fmt_usd(to_human(m.bet_amount_raw * (1 if is_ai_match else 2)))
                    if bet_token == "USD"
                    else fmt_token(
                        to_human(m.bet_amount_raw * (1 if is_ai_match else 2)),
                        bet_token,
                    )
                )
                result_lines.append(f"\U0001F4B0 Payout: **{pay_label}**")

        embed, file = self._build_match_payload(
            ctx, m, result="finished", extra_lines=result_lines,
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    async def _convert_to_usd(
        self, ctx: DiscoContext, token: str, amount_h: float,
    ) -> float:
        if token == "USD":
            return float(amount_h)
        try:
            row = await ctx.db.get_price(token, ctx.guild_id)
        except Exception:
            return 0.0
        if not row:
            return 0.0
        return float(amount_h) * float(row["price"])

    # ── leaderboard / stats ──────────────────────────────────────────────

    @chess.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_leaderboard(self, ctx: DiscoContext) -> None:
        rows = await ctx.db.fetch_all(
            """
            SELECT user_id, elo_rating, wins, losses, draws
              FROM gamba_chess_stats
             WHERE guild_id=$1 AND (wins + losses + draws) > 0
             ORDER BY elo_rating DESC, wins DESC
             LIMIT 10
            """,
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "♞ Chess Leaderboard", color=C_NEUTRAL,
                ).description(
                    f"No matches played yet. `{ctx.prefix}chess play` to start."
                ).build(),
                mention_author=False,
            )
            return
        lines = []
        medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"`#{i+1:>2}`"
            uid = int(r["user_id"])
            mem = ctx.guild.get_member(uid) if ctx.guild else None
            name = mem.display_name if mem else f"<@{uid}>"
            lines.append(
                f"{medal} **{name}** -- ELO `{int(r['elo_rating'])}` "
                f"({int(r['wins'])}W / {int(r['losses'])}L / {int(r['draws'])}D)"
            )
        await ctx.reply(
            embed=card(
                "♞ Chess Leaderboard  ·  Top 10", color=C_GOLD,
            ).description("\n".join(lines)).build(),
            mention_author=False,
        )

    @chess.command(name="stats", aliases=["record"])
    @guild_only
    @no_bots
    @ensure_registered
    async def chess_stats(
        self, ctx: DiscoContext, member: Optional[discord.Member] = None,
    ) -> None:
        target = member or ctx.author
        row = await ctx.db.fetch_one(
            """
            SELECT * FROM gamba_chess_stats
             WHERE user_id=$1 AND guild_id=$2
            """,
            target.id, ctx.guild_id,
        )
        if not row:
            await ctx.reply(
                embed=card(
                    f"♞ {target.display_name}'s Chess Record",
                    color=C_NEUTRAL,
                ).description("No matches played yet.").build(),
                mention_author=False,
            )
            return
        wins = int(row["wins"])
        losses = int(row["losses"])
        draws = int(row["draws"])
        total = wins + losses + draws
        wr = (wins / total * 100.0) if total else 0.0
        wager_h = to_human(int(row["total_wagered_raw"]))
        won_h = to_human(int(row["total_won_raw"]))
        embed = card(
            f"♞ {target.display_name}'s Chess Record", color=C_PURPLE,
        ).field(
            "ELO", f"`{int(row['elo_rating'])}`", True,
        ).field(
            "W / L / D", f"`{wins} / {losses} / {draws}`", True,
        ).field(
            "Win rate", f"`{wr:.1f}%`", True,
        ).field(
            "vs AI",
            f"`{int(row['vs_ai_wins'])}W / "
            f"{int(row['vs_ai_losses'])}L / "
            f"{int(row['vs_ai_draws'])}D`",
            True,
        ).field(
            "Total wagered", f"`{wager_h:,.4f}`", True,
        ).field(
            "Total won (profit)", f"`{won_h:,.4f}`", True,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChessCog(bot))
