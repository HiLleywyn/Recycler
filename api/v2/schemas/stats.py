"""Stats and leaderboard Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ServerStats(BaseModel):
    """Server-wide statistics."""

    total_users: int = Field(0, description="Total registered users.")
    total_tokens: int = Field(0, description="Number of listed tokens.")
    total_pools: int = Field(0, description="Number of liquidity pools.")
    total_trades: int = Field(0, description="Total trades executed.")
    total_volume_usd: float = Field(0.0, description="Total trading volume in USD.")
    total_market_cap: float = Field(0.0, description="Combined market capitalization.")
    treasury_balance: float = Field(0.0, description="Treasury balance.")
    active_loans: int = Field(0, description="Number of active loans.")
    active_stakes: int = Field(0, description="Number of active stake positions.")
    mining_hashrate: float = Field(0.0, description="Total mining hashrate.")


class ReserveStats(BaseModel):
    """Treasury and gas fee reserve statistics."""

    treasury_balance: float = Field(0.0, description="Current guild treasury balance.")
    total_gas_collected: float = Field(
        0.0, description="Sum of gas collected across all confirmed validator blocks."
    )
    total_distributed_to_validators: float = Field(
        0.0, description="Sum of validator rewards across all confirmed blocks."
    )
    total_burned: float = Field(
        0.0,
        description=(
            "Approximate total tokens removed from circulating supply via slashing. "
            "Derived as sum of (initial_supply - current_supply) per token where "
            "initial_supply = max_supply * 0.5 (protocol seed value)."
        ),
    )


class LeaderboardEntry(BaseModel):
    """A single leaderboard entry."""

    rank: int = Field(..., description="Leaderboard rank.")
    user_id: str = Field(..., description="User ID.")
    username: str | None = Field(None, description="Display name.")
    value: float = Field(0.0, description="Metric value (net worth, profit, etc.).")
    detail: str | None = Field(None, description="Additional detail text.")
