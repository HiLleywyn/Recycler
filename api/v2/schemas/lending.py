"""Lending Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class LendingStats(BaseModel):
    """Protocol-wide lending statistics."""

    total_borrowed: float = Field(0.0, description="Total USD borrowed.")
    total_collateral: float = Field(0.0, description="Total collateral locked.")
    active_loans: int = Field(0, description="Number of active USD loans.")
    avg_collateral_ratio: float = Field(0.0, description="Average collateral ratio.")


class LoanPublic(BaseModel):
    """Public-facing loan data."""

    user_id: str = Field(..., description="Borrower user ID.")
    principal: float = Field(0.0, description="Original principal.")
    outstanding: float = Field(0.0, description="Current outstanding balance.")
    collateral: float = Field(0.0, description="Collateral locked.")
    collateral_ratio: float = Field(0.0, description="Current collateral ratio.")
    created_at: str | None = Field(None, description="When loan was created.")


class BorrowRequest(BaseModel):
    """Request to borrow USD."""

    amount: float = Field(..., gt=0, description="USD amount to borrow.")
    collateral: float = Field(..., gt=0, description="Collateral to lock.")


class RepayRequest(BaseModel):
    """Request to repay a loan."""

    amount: float = Field(..., gt=0, description="Amount to repay.")


class AddCollateralRequest(BaseModel):
    """Request to add collateral to an existing loan."""

    amount: float = Field(..., gt=0, description="Additional collateral amount.")


class LoanActionResult(BaseModel):
    """Result of a loan action."""

    success: bool = Field(True)
    message: str = Field(..., description="Human-readable result.")
    outstanding: float = Field(0.0, description="Updated outstanding balance.")
    collateral: float = Field(0.0, description="Updated collateral.")


class MyLoan(BaseModel):
    """User's active loan details."""

    loan_type: str = Field("usd", description="Loan type: usd or sun.")
    principal: float = Field(0.0, description="Original principal.")
    outstanding: float = Field(0.0, description="Current outstanding.")
    collateral: float = Field(0.0, description="Collateral locked.")
    collateral_ratio: float = Field(0.0, description="Current collateral ratio.")
    last_interest: str | None = Field(None, description="Last interest accrual.")
    created_at: str | None = Field(None, description="Loan creation time.")
