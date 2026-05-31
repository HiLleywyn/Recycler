"""Pool (liquidity) Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PoolInfo(BaseModel):
    """Information about a liquidity pool."""

    pool_id: str = Field(..., description="Unique pool identifier (e.g. 'ARC-USDC').")
    token_a: str = Field(..., description="First token symbol.")
    token_b: str = Field(..., description="Second token symbol.")
    reserve_a: float = Field(0.0, description="Reserve of token A.")
    reserve_b: float = Field(0.0, description="Reserve of token B.")
    total_lp: float = Field(0.0, description="Total LP shares outstanding.")
    tvl: float = Field(0.0, description="Total value locked in USD.")
    apy: float = Field(0.0, description="Estimated annual percentage yield.")
    fee_rate: float = Field(0.003, description="Swap fee rate (e.g. 0.003 = 0.3%).")
    volume_24h: float = Field(0.0, description="24-hour trading volume in USD.")


class AddLiquidityRequest(BaseModel):
    """Request to add liquidity to a pool."""

    pool_id: str = Field(..., description="Pool to add liquidity to.")
    amount_a: float = Field(..., gt=0, description="Amount of token A to add.")
    amount_b: float = Field(..., gt=0, description="Amount of token B to add.")


class RemoveLiquidityRequest(BaseModel):
    """Request to remove liquidity from a pool."""

    pool_id: str = Field(..., description="Pool to remove liquidity from.")
    lp_shares: float = Field(..., gt=0, description="Number of LP shares to redeem.")


class LPPosition(BaseModel):
    """A user's liquidity provider position."""

    pool_id: str = Field(..., description="Pool identifier.")
    token_a: str = Field("", description="First token symbol.")
    token_b: str = Field("", description="Second token symbol.")
    lp_shares: float = Field(0.0, description="LP shares held.")
    value_usd: float = Field(0.0, description="Approximate USD value of position.")
    share_pct: float = Field(0.0, description="Percentage share of the pool.")
    added_at: datetime | None = Field(None, description="When liquidity was first added.")
