"""Savings Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SavingsPool(BaseModel):
    """Savings pool statistics."""

    symbol: str = Field(..., description="Asset symbol (USD or SUN).")
    total_deposits: float = Field(0.0, description="Total deposits in the pool.")
    total_borrowed: float = Field(0.0, description="Total amount borrowed from pool.")
    utilization_pct: float = Field(0.0, description="Pool utilization percentage.")
    deposit_apy: float = Field(0.0, description="Current deposit APY.")
    borrow_apy: float = Field(0.0, description="Current borrow APY.")


class ReserveBalance(BaseModel):
    """Community reserve balance."""

    symbol: str = Field(..., description="Asset symbol.")
    balance: float = Field(0.0, description="Reserve balance.")


class SavingsDepositRequest(BaseModel):
    """Request to deposit into savings."""

    amount: float = Field(..., gt=0, description="Amount to deposit.")
    asset: Literal["usd", "sun"] = Field(..., description="Asset to deposit: usd or sun.")


class SavingsWithdrawRequest(BaseModel):
    """Request to withdraw from savings."""

    amount: float = Field(..., gt=0, description="Amount to withdraw.")
    asset: Literal["usd", "sun"] = Field(..., description="Asset to withdraw: usd or sun.")


class SavingsPosition(BaseModel):
    """User's savings position."""

    symbol: str = Field(..., description="Asset symbol.")
    amount: float = Field(0.0, description="Current deposited amount.")
    interest_earned: float = Field(0.0, description="Total interest earned.")
    apy: float = Field(0.0, description="Current APY.")
    last_interest: str | None = Field(None, description="Last interest accrual timestamp.")
    created_at: str | None = Field(None, description="When position was opened.")


class SavingsActionResult(BaseModel):
    """Result of a savings deposit or withdrawal."""

    success: bool = Field(True)
    message: str = Field(..., description="Human-readable result.")
    symbol: str = Field(..., description="Asset symbol.")
    amount: float = Field(0.0, description="Amount deposited or withdrawn.")
    new_balance: float = Field(0.0, description="Updated savings balance.")
