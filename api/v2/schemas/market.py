"""Market data Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PriceData(BaseModel):
    """Full price data for a single token."""

    symbol: str = Field(..., description="Token symbol (e.g. ARC).")
    name: str = Field("", description="Human-readable token name.")
    price: float = Field(..., description="Current price in USD.")
    open_price: float = Field(..., description="Opening price for the current period.")
    high_24h: float = Field(..., description="24-hour high.")
    low_24h: float = Field(..., description="24-hour low.")
    change_24h_pct: float = Field(..., description="24-hour price change percentage.")
    market_cap: float = Field(0.0, description="Market capitalization (price * circulating supply).")
    circulating_supply: float = Field(0.0, description="Circulating supply of the token.")
    max_supply: float | None = Field(None, description="Maximum supply cap, if applicable.")
    volume_24h: float = Field(0.0, description="24-hour trading volume in USD.")
    network: str | None = Field(None, description="Network the token belongs to.")
    buyable_usd: bool = Field(False, description="Can be bought directly with USD.")
    swappable: bool = Field(False, description="Available on the swap exchange.")
    stakeable: bool = Field(False, description="Can be staked with validators.")


class CandleData(BaseModel):
    """OHLCV candle data point."""

    time: int = Field(..., description="Candle timestamp (Unix seconds).")
    open: float = Field(..., description="Open price.")
    high: float = Field(..., description="High price.")
    low: float = Field(..., description="Low price.")
    close: float = Field(..., description="Close price.")
    volume: float = Field(0.0, description="Trading volume.")


class TickerData(BaseModel):
    """Compact ticker entry."""

    symbol: str = Field(..., description="Token symbol.")
    price: float = Field(..., description="Current price in USD.")
    change_24h_pct: float = Field(..., description="24-hour price change percentage.")
    buyable_usd: bool = Field(False, description="Can be bought directly with USD.")
    swappable: bool = Field(False, description="Available on the swap exchange.")
    stakeable: bool = Field(False, description="Can be staked with validators.")


class TokenMetadata(BaseModel):
    """Static metadata about a token."""

    symbol: str = Field(..., description="Token symbol.")
    name: str = Field(..., description="Human-readable token name.")
    network: str | None = Field(None, description="Network name.")
    consensus: str = Field("PoS", description="Consensus mechanism.")
    max_supply: float | None = Field(None, description="Maximum supply, if applicable.")
    is_stablecoin: bool = Field(False, description="Whether this token is a stablecoin.")
    is_stakeable: bool = Field(False, description="Whether the token can be staked.")
    is_mineable: bool = Field(False, description="Whether the token can be mined.")


class MarketOverview(BaseModel):
    """Aggregate market statistics."""

    total_market_cap: float = Field(..., description="Sum of all token market caps.")
    total_volume_24h: float = Field(0.0, description="Sum of all 24h trading volumes.")
    total_tokens: int = Field(..., description="Number of tokens listed.")
    top_gainers: list[TickerData] = Field(default_factory=list, description="Top gaining tokens.")
    top_losers: list[TickerData] = Field(default_factory=list, description="Top losing tokens.")


class NetworkInfo(BaseModel):
    """Information about a blockchain network."""

    name: str = Field(..., description="Network name.")
    consensus: str = Field("PoS", description="Consensus mechanism.")
    block_time: float | None = Field(None, description="Average block time in seconds.")
    total_tokens: int = Field(0, description="Number of tokens on this network.")
    total_staked: float = Field(0.0, description="Total value staked on this network.")


class GainersLosers(BaseModel):
    """Top gainers and losers response."""

    gainers: list[TickerData] = Field(default_factory=list, description="Top 5 gainers.")
    losers: list[TickerData] = Field(default_factory=list, description="Top 5 losers.")
