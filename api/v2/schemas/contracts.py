"""Contracts Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ContractSummary(BaseModel):
    """Summary of a deployed smart contract."""

    address: str = Field(..., description="Contract address.")
    name: str = Field(..., description="Contract name.")
    network: str = Field(..., description="Network deployed on.")
    type: str = Field("custom", description="Contract type.")
    owner_id: str = Field(..., description="Owner user ID.")
    is_paused: bool = Field(False, description="Whether contract is paused.")
    call_count: int = Field(0, description="Number of calls made.")
    deployed_at: str | None = Field(None, description="Deployment timestamp.")
    description: str = Field("", description="Contract description.")


class ContractEvent(BaseModel):
    """A single contract event log entry."""

    id: int = Field(..., description="Event ID.")
    event: str = Field(..., description="Event name.")
    data: dict[str, Any] = Field(default_factory=dict, description="Event data.")
    block_id: int | None = Field(None, description="Block ID.")
    ts: str = Field(..., description="Event timestamp.")


class ContractDetail(BaseModel):
    """Full contract details including state."""

    address: str = Field(..., description="Contract address.")
    name: str = Field(..., description="Contract name.")
    network: str = Field(..., description="Network deployed on.")
    type: str = Field("custom", description="Contract type.")
    owner_id: str = Field(..., description="Owner user ID.")
    is_paused: bool = Field(False, description="Whether contract is paused.")
    call_count: int = Field(0, description="Number of calls made.")
    deployed_at: str | None = Field(None, description="Deployment timestamp.")
    description: str = Field("", description="Contract description.")
    definition: dict[str, Any] = Field(default_factory=dict, description="Contract definition.")
    state: dict[str, Any] = Field(default_factory=dict, description="Current state.")
    recent_events: list[ContractEvent] = Field(default_factory=list, description="Recent events.")


class TokenContractInfo(BaseModel):
    """Token contract parameters (fees, burns, etc.)."""

    symbol: str = Field(..., description="Token symbol.")
    params: dict[str, Any] = Field(default_factory=dict, description="Contract parameters.")
    created_at: str | None = Field(None, description="When contract was created.")
    updated_at: str | None = Field(None, description="Last update time.")
