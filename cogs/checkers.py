"""cogs/checkers.py  -  Gamba Network checkers: PvE + PvP + leaderboard.

Surface mirrors ``cogs/chess.py``:

    ,checkers play [bet] [token]               vs AI
    ,checkers challenge @user [bet] [token]    PvP
    ,checkers move <notation>                  e.g. ``a3-b4``, ``c3xe5xg7``
    ,checkers board                            redraw active match
    ,checkers resign                           forfeit
    ,checkers leaderboard                      ELO top 10
    ,checkers stats [@user]                    personal record

Engine + AI live in ``services/checkers_engine.py`` (American rules,
forced-capture, men-only forward, kings 1-step, multi-jump).

Wins mint **CROWN** via ``services.gamba.award_game_token`` and bump
ELO using the same K=32 formula as chess. Bets settle in USD or GBC.
"""
from __future__ import annotations

import asyncio
import io
import json as _json
import logging
import random
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from services.board_render import render_checkers_png
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, fmt_token, fmt_usd,
)
from services import checkers_engine as ckr
from services import gamba as gamba_svc

log = logging.getLogger(__name__)


_AI_USER_ID: int = 0
_AI_DISPLAY: str = "Discoin AI"

# Per-difficulty AI search depth. 'normal' preserves the legacy
# depth=4 behaviour. 'easy' plays a much shallower search so casual
# players can win; 'hard' looks two plies further. Random tiebreaks
# already live in the engine, so the easy tier still varies turn to
# turn even at the lowest depth.
_AI_DIFFICULTIES: tuple[str, ...] = ("easy", "normal", "hard")
_AI_DEPTH: dict[str, int] = {"easy": 2, "normal": 4, "hard": 6}
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


def _elo_update(winner_elo: int, loser_elo: int, draw: bool = False) -> tuple[int, int]:
    K = 32
    expected_w = 1.0 / (1.0 + 10 ** ((loser_elo - winner_elo) / 400.0))
    expected_l = 1.0 - expected_w
    if draw:
        return (
            max(100, winner_elo + int(round(K * (0.5 - expected_w)))),
            max(100, loser_elo + int(round(K * (0.5 - expected_l)))),
        )
    return (
        max(100, winner_elo + int(round(K * (1.0 - expected_w)))),
        max(100, loser_elo + int(round(K * (0.0 - expected_l)))),
    )


@dataclass
class _MatchRow:
    match_id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    red_user_id: int
    black_user_id: Optional[int]
    ai_side: Optional[str]   # "r" or "b" or None
    bet_token: str
    bet_amount_raw: int
    board_str: str
    turn: str
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
                mh = _json.loads(mh)
            except Exception:
                mh = []
        return cls(
            match_id=int(row["match_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row.get("message_id") else None,
            red_user_id=int(row["red_user_id"]),
            black_user_id=(
                int(row["black_user_id"]) if row.get("black_user_id") else None
            ),
            ai_side=row.get("ai_side"),
            bet_token=str(row.get("bet_token") or "USD"),
            bet_amount_raw=int(row.get("bet_amount_raw") or 0),
            board_str=str(row["board"]),
            turn=str(row["turn"]),
            move_history=list(mh),
            status=str(row["status"]),
            turn_user_id=int(row["turn_user_id"]),
            auto_bump=bool(row.get("auto_bump") or False),
            ai_difficulty=str(
                row.get("ai_difficulty") or _AI_DEFAULT_DIFFICULTY
            ),
        )

    def board(self) -> ckr.Board:
        return ckr.Board.from_str(self.board_str, self.turn)

    def player_side(self, uid: int) -> Optional[str]:
        if self.red_user_id == uid:
            return "r"
        if self.black_user_id == uid:
            return "b"
        return None


class _IxCtx:
    """Lightweight DiscoContext-shaped adapter for interaction-driven flows.

    The match cog has many helper methods written against a real
    DiscoContext (`ctx.db`, `ctx.reply(...)`, etc). When a player
    clicks a button instead of typing a command we don't have a Context
    object -- only an Interaction. This proxy exposes the subset of the
    interface those helpers actually use, so the same code paths cover
    both surfaces.
    """

    def __init__(self, interaction: discord.Interaction, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.guild = interaction.guild
        self.guild_id = interaction.guild_id
        self.channel = interaction.channel
        self.author = interaction.user
        self.message = interaction.message
        self._interaction = interaction
        # Best-effort prefix; checkers' embeds use it only for footer text.
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
        # mention_author is a Context-only kwarg; strip it before
        # forwarding to channel.send.
        kwargs.pop("mention_author", None)
        return await self._interaction.channel.send(*args, **kwargs)

    async def reply_error(self, msg: str) -> None:
        await self._interaction.channel.send(
            embed=card("Error", color=C_ERROR).description(msg).build(),
        )


class _ChallengeView(discord.ui.View):
    """Accept/decline buttons on a PvP challenge."""

    def __init__(
        self, *, target_id: int, on_accept,
    ) -> None:
        super().__init__(timeout=120)
        self.target_id = target_id
        self._on_accept = on_accept

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


class _CheckersGameView(discord.ui.View):
    """Interactive checkers view -- two Selects (From / To) + action buttons.

    The view is rebuilt after every move so each turn shows fresh legal
    moves. Both PvP players share the same message; ``interaction_check``
    funnels to the side whose turn it is.

    State:
      ``from_sq`` -- the (file, rank) the player picked from the From
        select. ``None`` until they've chosen, at which point the To
        select unlocks with the legal destinations from that square.
    """

    def __init__(
        self, cog: "CheckersCog", match_id: int,
        auto_bump: bool = False,
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = int(match_id)
        self.from_sq: Optional[tuple[int, int]] = None
        self.auto_bump = bool(auto_bump)
        self._lock = asyncio.Lock()
        self._build()

    # -- discord.py guard: only the side-to-move can interact ----------
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

    # -- component layout ----------------------------------------------
    def _build(self) -> None:
        """Rebuild children based on current ``from_sq`` selection."""
        self.clear_items()
        # We have to re-add the components fresh because Select state
        # is per-View; placeholders + options change with each turn.
        self.add_item(_FromSelect(self))
        self.add_item(_ToSelect(self))
        # Row 2: display controls (one-click, no game-state mutation).
        self.add_item(_BtnRefresh(self))
        self.add_item(_BtnBump(self))
        self.add_item(_BtnAutoBump(self, current=self.auto_bump))
        # Row 3: per-turn game actions.
        self.add_item(_BtnClearFrom(self))
        self.add_item(_BtnResign(self))

    async def reset_for_new_turn(self) -> None:
        """Discord requires a fresh View instance when options change drastically.
        This helper only exists as documentation of the rebuild flow.
        """
        self.from_sq = None
        self._build()


def _piece_name(piece: str) -> str:
    """Friendly piece name for the From-select labels."""
    return {
        "r": "Red", "R": "Red King",
        "b": "Black", "B": "Black King",
    }.get(piece, "?")


class _FromSelect(discord.ui.Select):
    """First dropdown: which of your pieces would you like to move."""

    def __init__(self, view: _CheckersGameView) -> None:
        self._game = view
        # Default placeholder; refreshed on each interaction by reading
        # the current match state.
        super().__init__(
            placeholder="Pick a piece to move...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="(loading)", value="_pending", default=False,
                ),
            ],
            row=0,
        )

    async def _refresh_options(self) -> None:
        m = await self._game.cog._fetch_match(self._game.match_id)
        if m is None:
            return
        board = m.board()
        legal = board.legal_moves()
        # Group by from-square -- one Select option per movable piece.
        by_from: dict[tuple[int, int], list[ckr.Move]] = {}
        for mv in legal:
            by_from.setdefault(mv.from_sq, []).append(mv)
        opts: list[discord.SelectOption] = []
        for (f, r), moves in sorted(
            by_from.items(), key=lambda kv: (kv[0][1], kv[0][0])
        ):
            piece = board.at(f, r)
            sq = ckr.square_str(f, r)
            jumps = sum(1 for mv in moves if mv.is_jump)
            steps = len(moves) - jumps
            tail = []
            if jumps:
                tail.append(f"{jumps} jump{'s' if jumps != 1 else ''}")
            if steps:
                tail.append(f"{steps} step{'s' if steps != 1 else ''}")
            label = f"{sq}  -  {_piece_name(piece)}"
            desc = " · ".join(tail)[:100] or "no moves"
            opts.append(
                discord.SelectOption(
                    label=label[:100], value=sq, description=desc,
                    default=(self._game.from_sq == (f, r)),
                ),
            )
            if len(opts) >= 25:
                break
        if not opts:
            opts = [discord.SelectOption(label="(no legal moves)", value="_none")]
            self.disabled = True
        else:
            self.disabled = False
        self.options = opts

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        if val.startswith("_"):
            await interaction.response.defer()
            return
        sq = ckr.parse_square(val)
        if sq is None:
            await interaction.response.send_message(
                "Bad square selection.", ephemeral=True,
            )
            return
        self._game.from_sq = sq
        # Rebuild the To select with destinations for the new pick.
        await self._game.cog._refresh_view_message(
            self._game, interaction=interaction,
        )


class _ToSelect(discord.ui.Select):
    """Second dropdown: legal destinations from the picked from-square."""

    def __init__(self, view: _CheckersGameView) -> None:
        self._game = view
        super().__init__(
            placeholder="Pick a destination... (choose from-square first)",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="(pick from-square first)", value="_pending"),
            ],
            disabled=True,
            row=1,
        )

    async def _refresh_options(self) -> None:
        if self._game.from_sq is None:
            self.options = [
                discord.SelectOption(
                    label="(pick from-square first)", value="_pending",
                ),
            ]
            self.disabled = True
            self.placeholder = "Pick a destination... (choose from-square first)"
            return
        m = await self._game.cog._fetch_match(self._game.match_id)
        if m is None:
            return
        board = m.board()
        legal = [mv for mv in board.legal_moves() if mv.from_sq == self._game.from_sq]
        opts: list[discord.SelectOption] = []
        for mv in legal:
            tag = " (jump)" if mv.is_jump else ""
            label = mv.notation()
            desc_parts = [f"{len(mv.captured)} capture{'s' if len(mv.captured) != 1 else ''}"] if mv.is_jump else []
            opts.append(
                discord.SelectOption(
                    label=label[:100],
                    value=mv.notation(),
                    description=" · ".join(desc_parts)[:100] or "single step",
                ),
            )
            if len(opts) >= 25:
                break
        if not opts:
            opts = [discord.SelectOption(label="(no destinations)", value="_none")]
            self.disabled = True
        else:
            self.disabled = False
        from_sq_str = ckr.square_str(*self._game.from_sq)
        self.placeholder = f"From {from_sq_str} -- pick destination..."
        self.options = opts

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        if val.startswith("_"):
            await interaction.response.defer()
            return
        await self._game.cog._apply_view_move(
            self._game, interaction, notation=val,
        )


class _BtnRefresh(discord.ui.Button):
    def __init__(self, view: _CheckersGameView) -> None:
        super().__init__(
            label="Refresh", style=discord.ButtonStyle.secondary,
            emoji="\U0001F501", row=2,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._refresh_view_message(
            self._game, interaction=interaction,
        )


class _BtnClearFrom(discord.ui.Button):
    def __init__(self, view: _CheckersGameView) -> None:
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


class _BtnBump(discord.ui.Button):
    """Re-post the match panel at the bottom of the channel.

    Owner-locked via the view's interaction_check (only the
    side-to-move interacts here, but bump is OK for anyone in the
    match -- skip the turn check at the cog level).
    """

    def __init__(self, view: _CheckersGameView) -> None:
        super().__init__(
            label="Bump", style=discord.ButtonStyle.secondary,
            emoji="\U0001F53C", row=2,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._bump_via_view(
            self._game, interaction,
        )


class _BtnAutoBump(discord.ui.Button):
    """Toggle auto-bump on/off for this match.

    When ON, the cog re-posts the panel at the bottom after the AI's
    reply (PvE) or the opponent's move (PvP) so the side whose turn
    it now is can find the panel without scrolling.
    """

    def __init__(self, view: _CheckersGameView, current: bool = False) -> None:
        # Style flips to success when ON so the player can see state.
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


class _BtnResign(discord.ui.Button):
    def __init__(self, view: _CheckersGameView) -> None:
        super().__init__(
            label="Resign", style=discord.ButtonStyle.danger,
            emoji="\U0001F3F3", row=3,
        )
        self._game = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.cog._resign_via_view(
            self._game, interaction,
        )


class CheckersCog(commands.Cog):
    """Gamba Network checkers."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── DB helpers ───────────────────────────────────────────────────

    async def _get_active_match(
        self, ctx: DiscoContext, uid: int,
    ) -> Optional[_MatchRow]:
        row = await ctx.db.fetch_one(
            """
            SELECT * FROM gamba_checkers_matches
             WHERE guild_id=$1 AND status='active'
               AND (red_user_id=$2 OR black_user_id=$2)
             ORDER BY started_at DESC LIMIT 1
            """,
            ctx.guild_id, uid,
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    async def _get_match(
        self, ctx: DiscoContext, match_id: int,
    ) -> Optional[_MatchRow]:
        row = await ctx.db.fetch_one(
            "SELECT * FROM gamba_checkers_matches WHERE match_id=$1",
            match_id,
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    async def _fetch_match(self, match_id: int) -> Optional[_MatchRow]:
        """ctx-free version used by the interactive view."""
        row = await self.bot.db.fetch_one(
            "SELECT * FROM gamba_checkers_matches WHERE match_id=$1",
            int(match_id),
        )
        return _MatchRow.from_dict(dict(row)) if row else None

    async def _validate_bet(
        self, ctx: DiscoContext, token: str, amt_h: float,
    ) -> tuple[bool, int, str]:
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
        self, ctx: DiscoContext, m: _MatchRow, board: ckr.Board,
        move_notation: str,
    ) -> None:
        m.board_str = board.serialise()
        m.turn = board.turn
        m.move_history = list(m.move_history) + [move_notation]
        next_uid = (
            m.red_user_id if board.turn == "r" else m.black_user_id
        )
        m.turn_user_id = next_uid if next_uid else _AI_USER_ID
        await ctx.db.execute(
            """
            UPDATE gamba_checkers_matches
               SET board=$2, turn=$3, move_history=$4::jsonb,
                   turn_user_id=$5, last_move_at=NOW()
             WHERE match_id=$1
            """,
            m.match_id, m.board_str, m.turn,
            _json.dumps(m.move_history), int(m.turn_user_id),
        )

    async def _bump_stats(
        self, ctx: DiscoContext, uid: int,
        wins: int = 0, losses: int = 0, draws: int = 0,
        vs_ai: bool = False,
        wagered_raw: int = 0, won_raw: int = 0,
        elo_delta: int = 0,
    ) -> int:
        await ctx.db.execute(
            """
            INSERT INTO gamba_checkers_stats (user_id, guild_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, guild_id) DO NOTHING
            """,
            uid, ctx.guild_id,
        )
        new_elo = await ctx.db.fetch_val(
            """
            UPDATE gamba_checkers_stats
               SET wins              = wins + $3,
                   losses            = losses + $4,
                   draws             = draws + $5,
                   vs_ai_wins        = vs_ai_wins        + $6,
                   vs_ai_losses      = vs_ai_losses      + $7,
                   vs_ai_draws       = vs_ai_draws       + $8,
                   total_wagered_raw = total_wagered_raw + $9::numeric,
                   total_won_raw     = total_won_raw     + $10::numeric,
                   elo_rating        = GREATEST(100, elo_rating + $11),
                   last_played       = NOW()
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
            "SELECT elo_rating FROM gamba_checkers_stats WHERE user_id=$1 AND guild_id=$2",
            uid, ctx.guild_id,
        )
        return int(row["elo_rating"]) if row else 1200

    async def _convert_to_usd(
        self, ctx: DiscoContext, token: str, amount_h: float,
    ) -> float:
        if token == "USD":
            return float(amount_h)
        try:
            row = await ctx.db.get_price(token, ctx.guild_id)
        except Exception:
            return 0.0
        return float(amount_h) * float(row["price"]) if row else 0.0

    # ── render ────────────────────────────────────────────────────────

    def _user_label(
        self, ctx: DiscoContext, uid: Optional[int], is_ai: bool,
    ) -> str:
        if is_ai or not uid:
            return f"\U0001F916 {_AI_DISPLAY}"
        member = ctx.guild.get_member(uid) if ctx.guild else None
        return member.display_name if member else f"<@{uid}>"

    _BOARD_FILENAME: str = "checkers_board.png"

    def _last_squares(self, m: _MatchRow) -> list[tuple[int, int]]:
        """Parse the last move into the from / mid-jump square coordinates."""
        last_squares: list[tuple[int, int]] = []
        if m.move_history:
            last_note = m.move_history[-1]
            sep = "x" if "x" in last_note else "-"
            for part in last_note.split(sep):
                sq = ckr.parse_square(part)
                if sq is not None:
                    last_squares.append(sq)
        return last_squares

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
        flip = board.turn == "b"
        png = render_checkers_png(
            board, flip=flip, last_squares=self._last_squares(m),
        )
        return discord.File(io.BytesIO(png), filename=self._BOARD_FILENAME)

    def _build_match_payload(
        self, ctx, m: _MatchRow, *,
        result: Optional[str] = None, extra_lines: list[str] | None = None,
        viewer_id: Optional[int] = None,
    ) -> tuple[discord.Embed, discord.File]:
        """Return ``(embed, file)`` for a checkers match message.

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
        red_label = self._user_label(ctx, m.red_user_id, m.ai_side == "r")
        black_label = self._user_label(ctx, m.black_user_id, m.ai_side == "b")
        bet_label = (
            fmt_usd(to_human(m.bet_amount_raw)) if m.bet_token == "USD"
            else fmt_token(to_human(m.bet_amount_raw), m.bet_token)
        )

        if result:
            title = "\U0001F451 Checkers  ·  Match Finished"
        else:
            title = (
                "\U0001F451 Checkers  ·  "
                f"\U0001F534 {red_label}  vs  \U000026AB {black_label}"
            )

        is_ai_turn = (m.ai_side == m.turn)
        next_uid = m.red_user_id if board.turn == "r" else m.black_user_id
        next_color = "Red" if board.turn == "r" else "Black"

        # Last-move text label for the meta line; the actual square
        # highlighting on the board image is done by _build_board_file.
        last_note: Optional[str] = (
            m.move_history[-1] if m.move_history else None
        )

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
            sections.append(f"**{next_color}** to move  ·  {mover}")

        meta_bits: list[str] = [f"Bet **{bet_label}**"]
        if m.ai_side:
            meta_bits.append(f"AI **{m.ai_difficulty}**")
        if last_note:
            meta_bits.append(f"Last `{last_note}`")
        meta_bits.append(f"Moves `{len(m.move_history)}`")
        sections.append("  ·  ".join(meta_bits))

        if result and not extra_lines:
            sections.append("Match finished.")

        builder = card(title, color=_bet_color(m.bet_token)).description(
            "\n".join(sections)
        ).image(f"attachment://{self._BOARD_FILENAME}")
        if not result:
            builder = builder.footer(
                "Pick a piece -> destination, or type "
                f"{ctx.prefix}checkers move <a3-b4>"
            )
        return builder.build()

    # ── view-driven helpers (called from interactive selects/buttons) ──

    async def _send_match_with_view(
        self, ctx, m: _MatchRow,
    ) -> Optional[discord.Message]:
        """Send the match embed + an interactive view to the channel.

        Stores the message id on ``gamba_checkers_matches.message_id``
        so subsequent moves can edit the same message in place. Used
        for the initial ``,checkers play`` / ``,checkers challenge``
        accept response.
        """
        view = _CheckersGameView(self, m.match_id, auto_bump=m.auto_bump)
        # Pre-populate the From select so the first interaction has the
        # legal-moves menu ready instead of "(loading)".
        for child in view.children:
            if isinstance(child, _FromSelect):
                await child._refresh_options()
            elif isinstance(child, _ToSelect):
                await child._refresh_options()
        embed, file = self._build_match_payload(
            ctx, m, viewer_id=int(getattr(ctx.author, "id", 0)),
        )
        msg = await ctx.send(embed=embed, file=file, view=view)
        try:
            await ctx.db.execute(
                "UPDATE gamba_checkers_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(msg.id),
            )
        except Exception:
            log.debug("checkers: storing message_id failed", exc_info=True)
        return msg

    async def _refresh_view_message(
        self, view: "_CheckersGameView",
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        """Re-read the match and edit the message in-place with fresh selects."""
        m = await self._fetch_match(view.match_id)
        if m is None:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.defer()
            return
        # Sync auto-bump state from the row + rebuild children so the
        # AutoBump button label reflects current state. _build clears
        # children + re-adds them so the selects are fresh too.
        view.auto_bump = bool(m.auto_bump)
        view._build()
        # Refresh dropdown options based on current state.
        for child in view.children:
            if isinstance(child, _FromSelect):
                await child._refresh_options()
            elif isinstance(child, _ToSelect):
                await child._refresh_options()
        # The board image now flips with the side to move, but we still
        # pass the interacting user's id for downstream label/context use.
        viewer = (
            int(interaction.user.id) if interaction is not None
            else int(m.turn_user_id)
        )
        ctx_proxy = (
            _IxCtx(interaction, self.bot) if interaction is not None
            else None
        )
        # When ctx_proxy is None we fall back to a minimal namespace --
        # only used by _user_label/_build_match_embed which read .guild.
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
                log.debug("refresh_view_message edit failed", exc_info=True)

    async def _bump_view_message(
        self, view: "_CheckersGameView",
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        """Delete the current match message + re-post a fresh copy at
        the bottom of the channel.

        Updates ``gamba_checkers_matches.message_id`` to the new
        message id and rebinds ``view.message`` so subsequent button
        clicks edit the bumped copy. If the source message no longer
        exists (already deleted or wrong channel) we just send a fresh
        message and update the row -- the bump silently degrades to a
        plain re-post.
        """
        m = await self._fetch_match(view.match_id)
        if m is None:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.defer()
            return
        # Re-sync auto-bump + selects on the rebuilt view.
        view.auto_bump = bool(m.auto_bump)
        view._build()
        for child in view.children:
            if isinstance(child, _FromSelect):
                await child._refresh_options()
            elif isinstance(child, _ToSelect):
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
        # Resolve the channel: prefer the interaction channel; fall back
        # to the row's stored channel_id so a non-interaction caller
        # (e.g. opponent-move auto-bump in PvP) still works.
        channel = None
        if interaction is not None:
            channel = interaction.channel
        if channel is None:
            channel = self.bot.get_channel(int(m.channel_id))
        if channel is None:
            return
        # Defer the interaction up front so Discord doesn't 3s-timeout
        # while the delete + re-post completes.
        if interaction is not None and not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        # Delete the source message (silent on permission failure).
        if m.message_id:
            try:
                old_msg = await channel.fetch_message(int(m.message_id))
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.debug("checkers bump: source delete failed", exc_info=True)
        # Re-post the embed + view at the bottom of the channel.
        try:
            sent = await channel.send(embed=embed, file=file, view=view)
        except discord.HTTPException:
            log.debug("checkers bump: re-post failed", exc_info=True)
            return
        try:
            view.message = sent  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            await self.bot.db.execute(
                "UPDATE gamba_checkers_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(sent.id),
            )
        except Exception:
            log.debug("checkers bump: message_id update failed", exc_info=True)

    async def _bump_via_view(
        self, view: "_CheckersGameView", interaction: discord.Interaction,
    ) -> None:
        """Bump button click handler. Owner check delegated to view-level
        interaction_check (only the side-to-move can interact with the
        view at all)."""
        await self._bump_view_message(view, interaction=interaction)

    async def _toggle_auto_bump(
        self, view: "_CheckersGameView", interaction: discord.Interaction,
    ) -> None:
        """Flip the auto_bump column on this match + refresh the panel."""
        m = await self._fetch_match(view.match_id)
        if m is None or m.status != "active":
            await interaction.response.send_message(
                "Match is no longer active.", ephemeral=True,
            )
            return
        new = not bool(m.auto_bump)
        await self.bot.db.execute(
            "UPDATE gamba_checkers_matches SET auto_bump=$2 WHERE match_id=$1",
            m.match_id, new,
        )
        await self._refresh_view_message(view, interaction=interaction)

    async def _apply_view_move(
        self, view: "_CheckersGameView",
        interaction: discord.Interaction, *, notation: str,
    ) -> None:
        """Apply a move chosen via the To-select dropdown."""
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
            mv = ckr.parse_move(board, notation)
            if mv is None:
                await interaction.response.send_message(
                    f"`{notation}` is no longer legal -- click Refresh.",
                    ephemeral=True,
                )
                return
            new_board = board.apply(mv)
            await self._save_position(ctx, m, new_board, mv.notation())
            view.from_sq = None
            # Terminal?
            over, winner = new_board.is_terminal()
            if over:
                # Acknowledge the interaction first so we don't time out.
                if not interaction.response.is_done():
                    await interaction.response.defer()
                # Disable the view since the game is done.
                for child in view.children:
                    child.disabled = True  # type: ignore[attr-defined]
                view.stop()
                try:
                    await interaction.message.edit(view=view)
                except discord.HTTPException:
                    pass
                await self._finalise(ctx, m, winner)
                return
            # AI's turn?
            m_post = await self._fetch_match(view.match_id) or m
            ai_to_move = (m_post.ai_side == new_board.turn)
            if ai_to_move:
                # Acknowledge first; the AI tick + edit will follow.
                if not interaction.response.is_done():
                    await interaction.response.defer()
                await self._run_ai_turn(ctx, m_post)
                m_after = await self._fetch_match(view.match_id) or m_post
                board_after = m_after.board()
                over2, winner2 = board_after.is_terminal()
                if over2:
                    for child in view.children:
                        child.disabled = True  # type: ignore[attr-defined]
                    view.stop()
                    try:
                        await interaction.message.edit(view=view)
                    except discord.HTTPException:
                        pass
                    await self._finalise(ctx, m_after, winner2)
                    return
                # Refresh view + embed for the human's next turn. If the
                # player toggled auto-bump on, re-post the panel at the
                # bottom of the channel instead of editing in place.
                m_now = await self._fetch_match(view.match_id) or m_after
                if m_now.auto_bump:
                    await self._bump_view_message(view, interaction=interaction)
                else:
                    await self._refresh_view_message(view, interaction=interaction)
                return
            # Human-vs-human: refresh (or bump) so the opponent's next
            # turn is visible without scrolling.
            m_now = await self._fetch_match(view.match_id) or m
            if m_now.auto_bump:
                await self._bump_view_message(view, interaction=interaction)
            else:
                await self._refresh_view_message(view, interaction=interaction)

    async def _resign_via_view(
        self, view: "_CheckersGameView", interaction: discord.Interaction,
    ) -> None:
        ctx = _IxCtx(interaction, self.bot)
        m = await self._fetch_match(view.match_id)
        if m is None or m.status != "active":
            await interaction.response.send_message(
                "Match is no longer active.", ephemeral=True,
            )
            return
        if int(interaction.user.id) not in (
            int(m.red_user_id or 0), int(m.black_user_id or 0),
        ):
            await interaction.response.send_message(
                "You're not in this match.", ephemeral=True,
            )
            return
        side = "r" if int(interaction.user.id) == int(m.red_user_id) else "b"
        winner = "b" if side == "r" else "r"
        await ctx.db.execute(
            "UPDATE gamba_checkers_matches SET status='resigned', ended_at=NOW() WHERE match_id=$1",
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
        await self._settle_payouts(ctx, m, winner=winner, by_resign=True)

    # ── ,checkers group ──────────────────────────────────────────────

    @commands.group(
        name="checkers", aliases=["ck", "draughts"], invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers(self, ctx: DiscoContext) -> None:
        await self.checkers_help(ctx)

    @checkers.command(name="help")
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_help(self, ctx: DiscoContext) -> None:
        embed = card(
            "\U0001F451 Checkers Commands", color=C_INFO,
        ).description(
            "American/English rules: men move 1 diagonal forward, kings 1 "
            "diagonal any direction, captures are forced, multi-jumps allowed. "
            "Wins mint **CROWN** and bump your ELO."
        ).field(
            "Start a match",
            f"`{ctx.prefix}checkers play [bet] [token] [easy|normal|hard]`\n"
            f"`{ctx.prefix}checkers challenge @user [bet] [token]`",
            False,
        ).field(
            "During a match",
            f"`{ctx.prefix}checkers move <a3-b4>` -- step\n"
            f"`{ctx.prefix}checkers move <c3xe5xg7>` -- multi-jump\n"
            f"`{ctx.prefix}checkers board` -- redraw\n"
            f"`{ctx.prefix}checkers resign` -- forfeit",
            False,
        ).field(
            "Records",
            f"`{ctx.prefix}checkers leaderboard`\n"
            f"`{ctx.prefix}checkers stats [@user]`",
            False,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @checkers.command(name="play", aliases=["start", "ai"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_play(
        self, ctx: DiscoContext,
        bet: Optional[str] = None, token: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> None:
        """Start a match vs the AI.

        Difficulty: ``easy`` / ``normal`` / ``hard``. Defaults to ``normal``.
        Easy uses a shallow search so casual players can win; hard searches
        deeper.
        """
        if await self._get_active_match(ctx, ctx.author.id):
            await ctx.reply_error(
                f"You already have an active checkers match. Resign first."
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
        # Random side -- player flips for red.
        player_red = bool(random.getrandbits(1))
        red_uid = ctx.author.id if player_red else _AI_USER_ID
        black_uid = _AI_USER_ID if player_red else ctx.author.id
        ai_side = "b" if player_red else "r"
        turn_uid = red_uid if red_uid != _AI_USER_ID else _AI_USER_ID
        row = await ctx.db.fetch_one(
            """
            INSERT INTO gamba_checkers_matches
              (guild_id, channel_id, red_user_id, black_user_id,
               ai_side, bet_token, bet_amount_raw, board, turn_user_id,
               ai_difficulty)
            VALUES ($1, $2, $3, $4, $5, $6, $7::numeric, $8, $9, $10)
            RETURNING *
            """,
            ctx.guild_id, ctx.channel.id,
            red_uid if red_uid != _AI_USER_ID else 0,
            black_uid if black_uid != _AI_USER_ID else None,
            ai_side, token, int(amt_raw),
            ckr.INITIAL_BOARD_STR, int(turn_uid), diff,
        )
        if not row:
            await ctx.reply_error("Could not start match -- try again.")
            if amt_raw > 0:
                await self._escrow_pay(ctx, ctx.author.id, token, amt_raw)
            return
        m = _MatchRow.from_dict(dict(row))
        if ai_side == "r":
            await self._run_ai_turn(ctx, m)
            m = await self._get_match(ctx, m.match_id) or m
        await self._send_match_with_view(ctx, m)

    @checkers.command(name="challenge", aliases=["vs", "duel"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_challenge(
        self, ctx: DiscoContext, target: discord.Member,
        bet: Optional[str] = None, token: Optional[str] = None,
    ) -> None:
        if target.bot or target.id == ctx.author.id:
            await ctx.reply_error("Pick a real, different opponent.")
            return
        if await self._get_active_match(ctx, ctx.author.id):
            await ctx.reply_error("You already have an active match.")
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
                    f"{target.display_name} doesn't have enough {token}."
                )
                return

        bet_label = (
            (fmt_usd(bet_h) if token == "USD" else fmt_token(bet_h, token))
            if amt_raw > 0 else "no stakes"
        )

        async def _on_accept(interaction: discord.Interaction) -> None:
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
            red_uid, black_uid = (
                (ctx.author.id, target.id)
                if random.getrandbits(1)
                else (target.id, ctx.author.id)
            )
            row = await ctx.db.fetch_one(
                """
                INSERT INTO gamba_checkers_matches
                  (guild_id, channel_id, red_user_id, black_user_id,
                   bet_token, bet_amount_raw, board, turn_user_id)
                VALUES ($1, $2, $3, $4, $5, $6::numeric, $7, $8)
                RETURNING *
                """,
                ctx.guild_id, ctx.channel.id,
                int(red_uid), int(black_uid),
                token, int(amt_raw),
                ckr.INITIAL_BOARD_STR, int(red_uid),
            )
            m = _MatchRow.from_dict(dict(row))
            game_view = _CheckersGameView(self, m.match_id)
            for child in game_view.children:
                if isinstance(child, _FromSelect):
                    await child._refresh_options()
                elif isinstance(child, _ToSelect):
                    await child._refresh_options()
            embed, file = self._build_match_payload(
                ctx, m, viewer_id=int(red_uid),
            )
            msg = await interaction.followup.send(
                embed=embed, file=file, view=game_view,
            )
            await ctx.db.execute(
                "UPDATE gamba_checkers_matches SET message_id=$2 WHERE match_id=$1",
                m.match_id, int(msg.id),
            )

        view = _ChallengeView(target_id=target.id, on_accept=_on_accept)
        embed = card(
            "\U0001F451 Checkers Challenge", color=_bet_color(token),
        ).description(
            f"{ctx.author.mention} challenges {target.mention} to checkers.\n"
            f"Bet: **{bet_label}** each.\n\n"
            f"{target.display_name}: accept or decline below within 2 minutes."
        ).build()
        await ctx.reply(embed=embed, view=view, mention_author=False)

    @checkers.command(name="move", aliases=["m", "mv"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_move(self, ctx: DiscoContext, *, notation: str) -> None:
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("You don't have an active checkers match.")
            return
        side = m.player_side(ctx.author.id)
        if not side:
            await ctx.reply_error("You're not in this match.")
            return
        board = m.board()
        if board.turn != side:
            await ctx.reply_error(
                f"It's "
                f"{'red' if board.turn == 'r' else 'black'}"
                f"'s turn -- not yours."
            )
            return
        move = ckr.parse_move(board, notation)
        if move is None:
            await ctx.reply_error(
                f"`{notation}` is not legal here. "
                f"`{ctx.prefix}checkers board` to see legal moves."
            )
            return
        new_board = board.apply(move)
        await self._save_position(ctx, m, new_board, move.notation())
        # Check terminal.
        over, winner = new_board.is_terminal()
        if over:
            await self._finalise(ctx, m, winner)
            return
        m = await self._get_match(ctx, m.match_id) or m
        if m.ai_side == new_board.turn:
            await self._run_ai_turn(ctx, m)
            m = await self._get_match(ctx, m.match_id) or m
            board2 = m.board()
            over2, winner2 = board2.is_terminal()
            if over2:
                await self._finalise(ctx, m, winner2)
                return
        # Edit the existing match message in place if possible; otherwise
        # send a fresh one with the interactive view.
        await self._refresh_or_resend(ctx, m)

    async def _run_ai_turn(self, ctx, m: _MatchRow) -> None:
        board = m.board()
        if board.is_terminal()[0]:
            return
        depth = _AI_DEPTH.get(m.ai_difficulty, _AI_DEPTH[_AI_DEFAULT_DIFFICULTY])
        mv = ckr.ai_pick_move(board, depth=depth)
        if mv is None:
            return
        new_board = board.apply(mv)
        await self._save_position(ctx, m, new_board, mv.notation())

    async def _refresh_or_resend(self, ctx, m: _MatchRow) -> None:
        """After a text-command move: edit the live match message in place.

        Falls back to sending a fresh message + view if the original
        message is missing (deleted by Discord, channel cleared, etc).
        """
        if m.message_id and ctx.channel:
            try:
                msg = await ctx.channel.fetch_message(int(m.message_id))
                view = _CheckersGameView(self, m.match_id)
                for child in view.children:
                    if isinstance(child, _FromSelect):
                        await child._refresh_options()
                    elif isinstance(child, _ToSelect):
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

    @checkers.command(name="board", aliases=["b", "show", "view"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_board(self, ctx: DiscoContext) -> None:
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("No active checkers match.")
            return
        await self._refresh_or_resend(ctx, m)

    @checkers.command(name="resign", aliases=["forfeit", "ff"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_resign(self, ctx: DiscoContext) -> None:
        m = await self._get_active_match(ctx, ctx.author.id)
        if not m:
            await ctx.reply_error("No active checkers match.")
            return
        side = m.player_side(ctx.author.id)
        if not side:
            await ctx.reply_error("You're not in this match.")
            return
        winner = "b" if side == "r" else "r"
        await ctx.db.execute(
            "UPDATE gamba_checkers_matches SET status='resigned', ended_at=NOW() WHERE match_id=$1",
            m.match_id,
        )
        await self._settle_payouts(ctx, m, winner=winner, by_resign=True)

    # ── finalise ─────────────────────────────────────────────────────

    async def _finalise(
        self, ctx: DiscoContext, m: _MatchRow, winner: Optional[str],
    ) -> None:
        if winner == "r":
            status = "red_won"
        elif winner == "b":
            status = "black_won"
        else:
            status = "draw"
        await ctx.db.execute(
            """
            UPDATE gamba_checkers_matches
               SET status=$2, ended_at=NOW()
             WHERE match_id=$1
            """,
            m.match_id, status,
        )
        await self._settle_payouts(ctx, m, winner=winner, by_resign=False)

    async def _settle_payouts(
        self, ctx: DiscoContext, m: _MatchRow,
        *, winner: Optional[str], by_resign: bool,
    ) -> None:
        from configs.items_config import SHOP_ITEMS as _SI
        is_ai_match = m.ai_side is not None
        bet_h = to_human(m.bet_amount_raw)
        bet_token = m.bet_token
        winner_uid: Optional[int] = None
        loser_uid: Optional[int] = None
        if winner is not None:
            winner_uid = m.red_user_id if winner == "r" else m.black_user_id
            loser_uid = m.black_user_id if winner == "r" else m.red_user_id

        result_lines: list[str] = []
        if winner is None:
            # Draw -- refund both.
            if m.bet_amount_raw > 0:
                if m.red_user_id and m.red_user_id != _AI_USER_ID:
                    await self._escrow_pay(
                        ctx, m.red_user_id, bet_token, m.bet_amount_raw,
                    )
                if m.black_user_id and m.black_user_id != _AI_USER_ID:
                    await self._escrow_pay(
                        ctx, m.black_user_id, bet_token, m.bet_amount_raw,
                    )
            result_lines.append("\U0001F91D **Draw** -- bets refunded.")
            for uid in (m.red_user_id, m.black_user_id):
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
                    await self._escrow_pay(
                        ctx, winner_uid, bet_token, payout_raw,
                    )
                profit_h = bet_h
                profit_usd = await self._convert_to_usd(
                    ctx, bet_token, profit_h,
                )
                doubled = await gamba_svc.consume_if_present(
                    ctx.db, ctx.guild_id, winner_uid, "side_bet_slip",
                )
                if await gamba_svc.consume_if_present(
                    ctx.db, ctx.guild_id, winner_uid, "lucky_chip",
                ):
                    bonus_pct = float(
                        _SI.get("lucky_chip", {}).get("stats", {}).get(
                            "gamba_win_bonus", 0.0,
                        ) or 0.05,
                    )
                    bonus_h = bet_h * bonus_pct
                    if bet_token == "USD" and bonus_h > 0:
                        await ctx.db.update_wallet(
                            winner_uid, ctx.guild_id, int(to_raw(bonus_h)),
                        )
                        result_lines.append(
                            f"\U0001F340 Lucky Chip: **+{fmt_usd(bonus_h)}**"
                        )
                if profit_usd > 0:
                    minted_sym, minted_raw = await gamba_svc.award_game_token(
                        ctx.db, ctx.guild_id, winner_uid,
                        "checkers", profit_usd, side_bet_double=doubled,
                    )
                    if minted_raw > 0:
                        prefix = "\U0001F3AB Side Bet 2x  " if doubled else ""
                        result_lines.append(
                            f"{prefix}\U0001F451 Minted: **"
                            f"{fmt_token(to_human(minted_raw), minted_sym)}**"
                        )
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

            verdict = "wins by resignation" if by_resign else "wins"
            winner_label = self._user_label(
                ctx, winner_uid,
                is_ai_match and (
                    (winner == "r" and m.ai_side == "r")
                    or (winner == "b" and m.ai_side == "b")
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

    # ── leaderboard / stats ──────────────────────────────────────────

    @checkers.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_leaderboard(self, ctx: DiscoContext) -> None:
        rows = await ctx.db.fetch_all(
            """
            SELECT user_id, elo_rating, wins, losses, draws
              FROM gamba_checkers_stats
             WHERE guild_id=$1 AND (wins + losses + draws) > 0
             ORDER BY elo_rating DESC, wins DESC
             LIMIT 10
            """,
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "\U0001F451 Checkers Leaderboard", color=C_NEUTRAL,
                ).description(
                    f"No matches played yet. `{ctx.prefix}checkers play` to start."
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
                "\U0001F451 Checkers Leaderboard  ·  Top 10", color=C_GOLD,
            ).description("\n".join(lines)).build(),
            mention_author=False,
        )

    @checkers.command(name="stats", aliases=["record"])
    @guild_only
    @no_bots
    @ensure_registered
    async def checkers_stats(
        self, ctx: DiscoContext, member: Optional[discord.Member] = None,
    ) -> None:
        target = member or ctx.author
        row = await ctx.db.fetch_one(
            """
            SELECT * FROM gamba_checkers_stats
             WHERE user_id=$1 AND guild_id=$2
            """,
            target.id, ctx.guild_id,
        )
        if not row:
            await ctx.reply(
                embed=card(
                    f"\U0001F451 {target.display_name}'s Checkers Record",
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
        embed = (
            card(
                f"\U0001F451 {target.display_name}'s Checkers Record",
                color=C_PURPLE,
            )
            .field("ELO", f"`{int(row['elo_rating'])}`", True)
            .field("W / L / D", f"`{wins} / {losses} / {draws}`", True)
            .field("Win rate", f"`{wr:.1f}%`", True)
            .field(
                "vs AI",
                f"`{int(row['vs_ai_wins'])}W / "
                f"{int(row['vs_ai_losses'])}L / "
                f"{int(row['vs_ai_draws'])}D`",
                True,
            )
            .field("Total wagered", f"`{wager_h:,.4f}`", True)
            .field("Total won (profit)", f"`{won_h:,.4f}`", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(CheckersCog(bot))
