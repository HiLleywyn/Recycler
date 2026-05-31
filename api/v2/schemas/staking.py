"""Staking Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ValidatorInfo(BaseModel):
    """Information about a staking validator."""

    validator_id: str = Field(..., description="Unique validator identifier.")
    name: str = Field(..., description="Validator display name.")
    network: str = Field("", description="Network the validator belongs to.")
    emoji: str = Field("", description="Validator emoji icon.")
    reward_rate: float = Field(0.0, description="Annual reward rate (as decimal, e.g. 0.05 = 5%).")
    uptime: float = Field(0.0, description="Uptime rate (0-1).")
    slash_rate: float = Field(0.0, description="Slash rate (0-1).")
    total_staked: float = Field(0.0, description="Total tokens staked with this validator.")
    staker_count: int = Field(0, description="Number of unique stakers.")


class StakeRequest(BaseModel):
    """Request to stake tokens with a validator."""

    validator_id: str = Field(..., description="Validator to stake with.")
    symbol: str = Field(..., description="Token symbol to stake.")
    amount: float = Field(..., gt=0, description="Amount to stake.")


class UnstakeRequest(BaseModel):
    """Request to unstake tokens from a validator."""

    validator_id: str = Field(..., description="Validator to unstake from.")
    symbol: str = Field(..., description="Token symbol to unstake.")
    amount: float = Field(..., gt=0, description="Amount to unstake.")


class DelegateRequest(BaseModel):
    """Request to delegate to a player-run PoS validator."""

    validator_user_id: int = Field(..., description="User ID of the validator operator.")
    network: str = Field(..., description="Network name.")
    amount: float = Field(..., gt=0, description="Amount to delegate.")


class UndelegateRequest(BaseModel):
    """Request to undelegate from a player-run PoS validator."""

    validator_user_id: int = Field(..., description="User ID of the validator operator.")
    network: str = Field(..., description="Network name.")
    amount: float = Field(..., gt=0, description="Amount to undelegate.")


class StakeInfo(BaseModel):
    """A user's active stake position."""

    user_id: int = Field(..., description="User ID.")
    validator_id: str = Field(..., description="Validator ID.")
    validator_name: str = Field("", description="Validator display name.")
    symbol: str = Field(..., description="Staked token symbol.")
    amount: float = Field(0.0, description="Amount staked.")
    value_usd: float = Field(0.0, description="Approximate USD value.")
    reward_rate: float = Field(0.0, description="Validator reward rate.")
    staked_at: datetime | None = Field(None, description="When the stake was created.")


class PosValidatorInfo(BaseModel):
    """Information about a player-run PoS validator."""

    user_id: int = Field(..., description="Validator operator user ID.")
    network: str = Field(..., description="Network name.")
    stake_token: str = Field(..., description="Token required for staking.")
    stake_amount: float = Field(0.0, description="Validator's self-stake amount.")
    is_active: bool = Field(True, description="Whether the validator is active.")
    total_blocks_validated: int = Field(0, description="Total blocks validated.")
    total_rewards_earned: float = Field(0.0, description="Total rewards earned.")
    slash_count: int = Field(0, description="Number of times slashed.")
    delegation_count: int = Field(0, description="Number of delegators.")
    total_delegated: float = Field(0.0, description="Total amount delegated.")


class DelegationInfo(BaseModel):
    """A user's delegation to a PoS validator."""

    id: int = Field(..., description="Delegation record ID.")
    delegator_id: int = Field(..., description="Delegator user ID.")
    validator_user_id: int = Field(..., description="Validator operator user ID.")
    network: str = Field(..., description="Network name.")
    token: str = Field(..., description="Delegated token symbol.")
    amount: float = Field(0.0, description="Amount delegated.")
    total_earned: float = Field(0.0, description="Total rewards earned from delegation.")
    locked_until: datetime | None = Field(None, description="When the delegation lock expires.")
    delegated_at: datetime | None = Field(None, description="When the delegation was created.")
