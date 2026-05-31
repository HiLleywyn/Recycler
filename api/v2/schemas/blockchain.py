"""Blockchain Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BlockInfo(BaseModel):
    """Information about a blockchain block."""

    block_num: int = Field(..., description="Block number.")
    network: str = Field("", description="Network name.")
    status: str = Field("pending", description="Block status (pending, confirmed).")
    tx_count: int = Field(0, description="Number of transactions in block.")
    block_hash: str = Field("", description="Block hash.")
    miner_id: int | None = Field(None, description="Miner/validator user ID (if PoW).")
    ts: datetime | None = Field(None, description="Block timestamp.")


class TransactionInfo(BaseModel):
    """Information about a blockchain transaction."""

    tx_hash: str = Field(..., description="Transaction hash.")
    tx_type: str = Field("", description="Transaction type (buy, sell, swap, transfer, stake, etc.).")
    user_id: int | None = Field(None, description="User ID who initiated.")
    username: str = Field("", description="Username.")
    symbol_in: str | None = Field(None, description="Input token symbol.")
    amount_in: float | None = Field(None, description="Input amount.")
    symbol_out: str | None = Field(None, description="Output token symbol.")
    amount_out: float | None = Field(None, description="Output amount.")
    fee: float = Field(0.0, description="Transaction fee.")
    gas_fee: float = Field(0.0, description="Gas fee paid.")
    ts: datetime | None = Field(None, description="Transaction timestamp.")
    block_num: int | None = Field(None, description="Block number (if confirmed).")


class MempoolEntry(BaseModel):
    """A pending transaction in the mempool."""

    id: int = Field(..., description="Mempool entry ID.")
    tx_type: str = Field("", description="Action type.")
    user_id: int = Field(0, description="User who submitted.")
    network: str = Field("", description="Network name.")
    symbol: str = Field("", description="Primary token involved.")
    amount: float = Field(0.0, description="Primary amount.")
    gas_fee: float = Field(0.0, description="Gas fee.")
    gas_price: str = Field("medium", description="Gas price tier.")
    status: str = Field("pending", description="Status.")
    ts: datetime | None = Field(None, description="Submission timestamp.")


class ExplorerSummary(BaseModel):
    """Overview stats for the blockchain explorer."""

    total_blocks: int = Field(0, description="Total blocks across all networks.")
    total_transactions: int = Field(0, description="Total transactions.")
    total_addresses: int = Field(0, description="Total unique addresses/users.")
    networks: list[str] = Field(default_factory=list, description="Active network names.")
    mempool_size: int = Field(0, description="Current mempool size.")
