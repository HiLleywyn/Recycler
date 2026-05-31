"""Game Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Play requests & game-specific options
# ---------------------------------------------------------------------------

class GamePlayRequest(BaseModel):
    """Generic play request accepted by all stateless games."""

    bet_amount: float = Field(..., gt=0, description="Amount to wager.")
    options: dict[str, Any] = Field(default_factory=dict, description="Game-specific options.")


class CoinflipOptions(BaseModel):
    """Options for a coinflip game."""

    choice: Literal["heads", "tails"] = Field(..., description="Heads or tails.")


class DiceOptions(BaseModel):
    """Options for a dice game."""

    target: int = Field(..., ge=1, le=100, description="Target number (1-100).")
    over_under: Literal["over", "under"] = Field(..., description="Bet over or under target.")


class RouletteOptions(BaseModel):
    """Options for a roulette game."""

    bet_type: str = Field(
        ...,
        description="Type of bet: 'color', 'number', 'dozen', 'column', 'half', 'parity'.",
    )
    bet_value: str = Field(
        ...,
        description="Bet value: e.g. 'red', 'black', '0'-'36', '1st', '2nd', '3rd', 'low', 'high', 'even', 'odd'.",
    )



class BlackjackAction(BaseModel):
    """Action to take in an active blackjack session."""

    session_id: str = Field(..., description="Active blackjack session UUID.")
    action: Literal["hit", "stand", "double"] = Field(..., description="Blackjack action.")


class MinesReveal(BaseModel):
    """Reveal a tile in an active mines session."""

    session_id: str = Field(..., description="Active mines session UUID.")
    row: int = Field(..., ge=0, le=4, description="Row index (0-4).")
    col: int = Field(..., ge=0, le=4, description="Column index (0-4).")


class MinesCashout(BaseModel):
    """Cash out of an active mines session."""

    session_id: str = Field(..., description="Active mines session UUID.")


class MinesStartRequest(BaseModel):
    """Start a new mines game."""

    bet_amount: float = Field(..., gt=0, description="Amount to wager.")
    mine_count: int = Field(default=5, ge=1, le=24, description="Number of mines (1-24).")


class BlackjackStartRequest(BaseModel):
    """Start a new blackjack game."""

    bet_amount: float = Field(..., gt=0, description="Amount to wager.")


class CrashJoinRequest(BaseModel):
    """Join the current crash round."""

    bet_amount: float = Field(..., gt=0, description="Amount to wager.")


class CrashCashoutRequest(BaseModel):
    """Cash out of current crash round."""

    session_id: str = Field(..., description="Crash session UUID.")


class PlinkoOptions(BaseModel):
    """Options for a plinko game."""

    risk: Literal["low", "medium", "high"] = Field(default="medium", description="Risk level.")
    rows: int = Field(default=16, ge=8, le=16, description="Number of rows (8-16).")


class WheelOptions(BaseModel):
    """Options for the wheel game."""

    segments: int = Field(default=20, ge=10, le=50, description="Number of segments on wheel.")


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class GameResult(BaseModel):
    """Result of a completed game."""

    game_id: int = Field(..., description="Unique game result ID.")
    game_type: str = Field(..., description="Game type identifier.")
    bet_amount: float = Field(..., description="Amount wagered.")
    payout: float = Field(..., description="Amount paid out.")
    profit: float = Field(..., description="Net profit (payout - bet).")
    multiplier: float = Field(..., description="Payout multiplier.")
    result_data: dict[str, Any] = Field(default_factory=dict, description="Game-specific result details.")


class GameSession(BaseModel):
    """An active game session (blackjack, mines, crash)."""

    session_id: str = Field(..., description="Unique session UUID.")
    game_type: str = Field(..., description="Game type identifier.")
    bet_amount: float = Field(..., description="Amount wagered.")
    state: dict[str, Any] = Field(default_factory=dict, description="Current game state (visible to player).")
    status: str = Field(..., description="Session status: active, completed, expired, cancelled.")


class GameStats(BaseModel):
    """Server-wide statistics for a game type."""

    game_type: str = Field(..., description="Game type identifier.")
    total_played: int = Field(0, description="Total games played.")
    total_wagered: float = Field(0.0, description="Total amount wagered.")
    total_profit: float = Field(0.0, description="Total house profit.")
    house_edge: float = Field(0.0, description="Effective house edge percentage.")


class GameLeaderboardEntry(BaseModel):
    """A single entry on the game leaderboard."""

    user_id: int = Field(..., description="User ID.")
    username: str = Field("", description="Username.")
    total_wagered: float = Field(0.0, description="Total amount wagered.")
    total_profit: float = Field(0.0, description="Total profit.")
    win_count: int = Field(0, description="Number of wins.")
    loss_count: int = Field(0, description="Number of losses.")


class ProvablyFairData(BaseModel):
    """Provably fair verification data for a game."""

    game_id: int = Field(..., description="Game result ID.")
    server_seed: str = Field(..., description="Server seed (revealed after game).")
    client_seed: str = Field(..., description="Client seed.")
    nonce: int = Field(..., description="Nonce value.")
    server_seed_hash: str = Field(..., description="SHA-256 hash of server seed.")
    result: dict[str, Any] = Field(default_factory=dict, description="Computed result for verification.")


class CrashState(BaseModel):
    """Current state of the crash game round."""

    round_id: str = Field("", description="Current round identifier.")
    status: str = Field("waiting", description="Round status: waiting, running, crashed.")
    multiplier: float = Field(1.0, description="Current multiplier.")
    players: int = Field(0, description="Number of players in round.")
    crash_point: float | None = Field(None, description="Crash point (shown after crash).")
