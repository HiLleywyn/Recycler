"""Portfolio and user Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Badges (defined before UserProfile to avoid forward reference issues)
# ---------------------------------------------------------------------------

class Badge(BaseModel):
    """Achievement badge."""

    badge_id: str = Field(..., description="Badge identifier.")
    name: str = Field(..., description="Badge name.")
    description: str | None = Field(None, description="Badge description.")
    icon: str | None = Field(None, description="Badge icon.")
    category: str | None = Field(None, description="Badge category.")
    earned_at: str | None = Field(None, description="When earned.")


# ---------------------------------------------------------------------------
# User profile (public)
# ---------------------------------------------------------------------------

class UserProfile(BaseModel):
    """Public trading profile."""

    user_id: str = Field(..., description="User ID.")
    username: str | None = Field(None, description="Display username.")
    avatar: str | None = Field(None, description="Avatar URL.")
    total_trades: int = Field(0, description="Total trades executed.")
    total_trade_volume: float = Field(0.0, description="Total trade volume in USD.")
    realized_pnl: float = Field(0.0, description="Realized PnL.")
    best_trade_pnl: float = Field(0.0, description="Best single trade PnL.")
    worst_trade_pnl: float = Field(0.0, description="Worst single trade PnL.")
    win_count: int = Field(0, description="Winning trades.")
    loss_count: int = Field(0, description="Losing trades.")
    win_rate: float = Field(0.0, description="Win rate percentage.")
    total_games: int = Field(0, description="Total games played.")
    total_wagered: float = Field(0.0, description="Total amount wagered.")
    total_game_profit: float = Field(0.0, description="Net game profit.")
    badges: list[Badge] = Field(default_factory=list, description="Earned badges.")
    member_since: str | None = Field(None, description="Account creation date.")


class PnLSnapshot(BaseModel):
    """Historical PnL data point."""

    net_worth: float = Field(0.0, description="Net worth at snapshot time.")
    ts: str = Field(..., description="Snapshot timestamp.")


class GameStats(BaseModel):
    """Gambling stats by game type."""

    game_type: str = Field(..., description="Game type (coinflip, dice, etc.).")
    games_played: int = Field(0, description="Total games played.")
    total_wagered: float = Field(0.0, description="Total amount wagered.")
    total_profit: float = Field(0.0, description="Net profit from this game.")
    best_win: float = Field(0.0, description="Largest single win.")
    avg_bet: float = Field(0.0, description="Average bet size.")


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

class UserSettings(BaseModel):
    """User display/preference settings."""

    theme: str = Field("dark", description="UI theme (dark/light).")
    currency_format: str = Field("usd", description="Preferred currency display format.")
    price_precision: int = Field(2, description="Decimal places for prices.")
    default_chart_tf: str = Field("1h", description="Default chart timeframe.")
    auto_levelup: bool = Field(False, description="Automatically level up items when XP threshold is met.")


class UserSettingsUpdate(BaseModel):
    """Partial update for user settings."""

    theme: str | None = Field(None, description="UI theme.")
    currency_format: str | None = Field(None, description="Currency format.")
    price_precision: int | None = Field(None, ge=0, le=8, description="Price precision.")
    default_chart_tf: str | None = Field(None, description="Default chart timeframe.")
    auto_levelup: bool | None = Field(None, description="Automatically level up items when XP threshold is met.")


class UserSearchResult(BaseModel):
    """User search result entry."""

    user_id: str = Field(..., description="User ID.")
    username: str | None = Field(None, description="Display name.")
    avatar: str | None = Field(None, description="Avatar URL.")
    net_worth: float = Field(0.0, description="Net worth.")


# ---------------------------------------------------------------------------
# Portfolio overview
# ---------------------------------------------------------------------------

class PortfolioOverview(BaseModel):
    """High-level portfolio summary."""

    wallet: float = Field(0.0, description="USD wallet balance.")
    bank: float = Field(0.0, description="USD bank balance.")
    net_worth: float = Field(0.0, description="Total net worth in USD.")
    net_worth_change_24h: float = Field(0.0, description="Net worth change in the last 24 hours (USD).")
    holdings_count: int = Field(0, description="Number of distinct token holdings.")
    stakes_count: int = Field(0, description="Number of active stake positions.")
    lp_count: int = Field(0, description="Number of LP positions.")


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

class HoldingItem(BaseModel):
    """Single token holding."""

    symbol: str = Field(..., description="Token symbol.")
    amount: float = Field(0.0, description="Quantity held.")
    value_usd: float = Field(0.0, description="Current USD value.")
    avg_cost_basis: float | None = Field(None, description="Average cost basis per token, if available.")
    unrealized_pnl: float | None = Field(None, description="Unrealized profit/loss in USD.")
    network: str | None = Field(None, description="Network the token belongs to.")
    holding_type: Literal["cefi", "defi"] = Field("cefi", description="Whether holding is CeFi or DeFi.")


# ---------------------------------------------------------------------------
# Staking
# ---------------------------------------------------------------------------

class StakeItem(BaseModel):
    """Active stake position."""

    validator_id: str = Field(..., description="Validator identifier.")
    validator_name: str = Field("", description="Human-readable validator name.")
    symbol: str = Field(..., description="Staked token symbol.")
    amount: float = Field(0.0, description="Amount staked.")
    value_usd: float = Field(0.0, description="Current USD value of stake.")
    apy: float = Field(0.0, description="Annual percentage yield.")
    staked_at: str | None = Field(None, description="Timestamp when staked (ISO 8601).")


# ---------------------------------------------------------------------------
# LP Positions
# ---------------------------------------------------------------------------

class LPPositionItem(BaseModel):
    """Liquidity pool position."""

    pool_id: str = Field(..., description="Pool identifier.")
    token_a: str = Field(..., description="First token in the pair.")
    token_b: str = Field(..., description="Second token in the pair.")
    lp_shares: float = Field(0.0, description="LP share tokens held.")
    value_usd: float = Field(0.0, description="Current USD value of the position.")
    share_pct: float = Field(0.0, description="Percentage of pool owned.")
    added_at: str | None = Field(None, description="When position was added (ISO 8601).")


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------

class SavingsItem(BaseModel):
    """Savings deposit."""

    asset: str = Field(..., description="Asset symbol (e.g. USD, SUN).")
    amount: float = Field(0.0, description="Amount deposited.")
    interest_earned: float = Field(0.0, description="Cumulative interest earned.")
    apy: float = Field(0.0, description="Current annual percentage yield.")
    deposited_at: str | None = Field(None, description="When deposit was made (ISO 8601).")


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

class LoanItem(BaseModel):
    """Active loan position."""

    loan_id: str = Field("", description="Loan identifier.")
    principal: float = Field(0.0, description="Original principal amount.")
    outstanding: float = Field(0.0, description="Current outstanding balance.")
    collateral: float = Field(0.0, description="Collateral locked.")
    interest_rate: float = Field(0.0, description="Annual interest rate.")
    created_at: str | None = Field(None, description="When loan was created (ISO 8601).")
    loan_type: Literal["usd", "sun"] = Field("usd", description="Loan type: USD or SUN-backed.")


# ---------------------------------------------------------------------------
# Net Worth Breakdown
# ---------------------------------------------------------------------------

class NetWorthBreakdown(BaseModel):
    """Detailed net worth composition."""

    cefi: float = Field(0.0, description="CeFi crypto value.")
    defi: float = Field(0.0, description="DeFi wallet value.")
    staking: float = Field(0.0, description="NPC yield-farm stake value.")
    pos: float = Field(0.0, description="PoS validator own-stake value.")
    lp: float = Field(0.0, description="Liquidity pool position value.")
    mining: float = Field(0.0, description="Mining rig book value.")
    delegations: float = Field(0.0, description="Delegation value.")
    savings: float = Field(0.0, description="Savings deposit value.")
    items: float = Field(0.0, description="Item (stone) staked value.")
    lunar_mint: float = Field(0.0, description="Lunar Mint staked group tokens value (USD, 24h TWAP).")
    moon_pool: float = Field(0.0, description="Moon Pool staked MOON value (USD at spot).")
    total: float = Field(0.0, description="Total net worth.")


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------

class PnLData(BaseModel):
    """Realized and unrealized profit/loss."""

    realized_pnl: float = Field(0.0, description="Total realized PnL.")
    unrealized_pnl: float = Field(0.0, description="Total unrealized PnL.")
    total_pnl: float = Field(0.0, description="Sum of realized + unrealized PnL.")
    pnl_history: list[dict] = Field(default_factory=list, description="Historical PnL data points.")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class TransactionItem(BaseModel):
    """Single transaction record."""

    tx_hash: str = Field(..., description="Transaction hash.")
    tx_type: str = Field(..., description="Transaction type (BUY, SELL, SWAP, TRANSFER, etc.).")
    symbol_in: str | None = Field(None, description="Input token symbol.")
    amount_in: float | None = Field(None, description="Input amount.")
    symbol_out: str | None = Field(None, description="Output token symbol.")
    amount_out: float | None = Field(None, description="Output amount.")
    fee: float = Field(0.0, description="Fee charged.")
    gas_fee: float = Field(0.0, description="Gas fee, if applicable.")
    ts: str = Field(..., description="Transaction timestamp (ISO 8601).")
