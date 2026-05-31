"""Trading Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class BuyRequest(BaseModel):
    """Request body for buying tokens with USD."""

    symbol: str = Field(..., description="Token symbol to buy (e.g. ARC).")
    amount: float | None = Field(None, gt=0, description="Quantity of tokens to buy.")
    amount_usd: float | None = Field(None, gt=0, description="USD amount to spend.")

    @model_validator(mode="after")
    def check_amount_or_usd(self) -> "BuyRequest":
        if self.amount is None and self.amount_usd is None:
            raise ValueError("Provide either 'amount' (token qty) or 'amount_usd' (USD to spend).")
        if self.amount is not None and self.amount_usd is not None:
            raise ValueError("Provide only one of 'amount' or 'amount_usd', not both.")
        return self


class SellRequest(BaseModel):
    """Request body for selling tokens for USD."""

    symbol: str = Field(..., description="Token symbol to sell (e.g. ARC).")
    amount: float | None = Field(None, gt=0, description="Quantity of tokens to sell.")
    amount_usd: float | None = Field(None, gt=0, description="USD value to sell.")

    @model_validator(mode="after")
    def check_amount_or_usd(self) -> "SellRequest":
        if self.amount is None and self.amount_usd is None:
            raise ValueError("Provide either 'amount' (token qty) or 'amount_usd' (USD target).")
        if self.amount is not None and self.amount_usd is not None:
            raise ValueError("Provide only one of 'amount' or 'amount_usd', not both.")
        return self


class SwapQuoteRequest(BaseModel):
    """Request body for a swap price quote."""

    token_in: str = Field(..., description="Symbol of token to send.")
    token_out: str = Field(..., description="Symbol of token to receive.")
    amount_in: float = Field(..., gt=0, description="Quantity of token_in to swap.")


class SwapExecuteRequest(BaseModel):
    """Request body for executing a swap."""

    token_in: str = Field(..., description="Symbol of token to send.")
    token_out: str = Field(..., description="Symbol of token to receive.")
    amount_in: float = Field(..., gt=0, description="Quantity of token_in to swap.")
    min_amount_out: float = Field(0.0, ge=0, description="Minimum acceptable output (slippage protection).")
    slippage_pct: float = Field(1.0, ge=0, le=50, description="Max slippage tolerance in percent.")
    quote_id: str | None = Field(None, description="Optional quote ID from /swap/quote for binding.")


class TransferRequest(BaseModel):
    """Request body for sending USD to another user."""

    to_user_id: int = Field(..., description="Recipient user ID.")
    amount: float = Field(..., gt=0, description="USD amount to transfer.")


class CefiDefiTransferRequest(BaseModel):
    """Request body for transferring tokens between CeFi and DeFi."""

    symbol: str = Field(..., description="Token symbol to transfer (e.g. ARC).")
    amount: float = Field(..., gt=0, description="Quantity of tokens to transfer.")
    network: str = Field(..., description="DeFi network short name (e.g. 'arcadia').")


class TradeResult(BaseModel):
    """Result of a buy or sell operation."""

    success: bool = Field(..., description="Whether the trade succeeded.")
    tx_hash: str = Field("", description="Transaction hash.")
    symbol: str = Field("", description="Token symbol traded.")
    amount: float = Field(0.0, description="Quantity of tokens traded.")
    cost: float = Field(0.0, description="USD cost or revenue.")
    fee: float = Field(0.0, description="Platform fee charged.")
    new_price: float = Field(0.0, description="New price after trade impact.")
    new_balance: float = Field(0.0, description="Updated USD wallet balance.")
    error: str | None = Field(None, description="Error message if trade failed.")


class SwapQuote(BaseModel):
    """Quote returned by the swap/quote endpoint."""

    token_in: str = Field(..., description="Input token symbol.")
    token_out: str = Field(..., description="Output token symbol.")
    amount_in: float = Field(..., description="Input amount.")
    amount_out: float = Field(..., description="Estimated output amount.")
    price_impact_pct: float = Field(0.0, description="Estimated price impact as a percentage.")
    fee: float = Field(0.0, description="Swap fee amount.")
    route: str = Field("", description="Routing path description (e.g. 'ARC -> USDC via pool').")
    quote_id: str = Field("", description="Unique quote identifier for binding to execute.")
    expires_at: float = Field(0.0, description="Unix timestamp when this quote expires.")
    pool_state_hash: str = Field("", description="Hash of pool state at quote time.")


class SwapResult(BaseModel):
    """Result of an executed swap."""

    success: bool = Field(..., description="Whether the swap succeeded.")
    tx_hash: str = Field("", description="Transaction hash.")
    token_in: str = Field("", description="Input token symbol.")
    token_out: str = Field("", description="Output token symbol.")
    amount_in: float = Field(0.0, description="Input amount.")
    amount_out: float = Field(0.0, description="Output amount received.")
    price_impact_pct: float = Field(0.0, description="Actual price impact.")
    fee: float = Field(0.0, description="Swap fee charged.")
    error: str | None = Field(None, description="Error message if swap failed.")
