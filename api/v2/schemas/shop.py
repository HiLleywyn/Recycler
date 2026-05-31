"""Shop Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ShopItemLeaderEntry(BaseModel):
    """Leaderboard entry for a shop item."""

    user_id: str = Field(..., description="User ID.")
    level: int = Field(1, description="Item level.")
    xp: float = Field(0.0, description="Item XP.")


class ShopItem(BaseModel):
    """A shop item with leaderboard data."""

    key: str = Field(..., description="Unique item key (hashstone, lockstone, vaultstone, liqstone).")
    name: str = Field(..., description="Display name.")
    description: str = Field("", description="Item description.")
    price: float = Field(0.0, description="Price in stablecoin (USD-pegged).")
    category: str = Field("item", description="Item category.")
    currency: str = Field("DSD", description="Default currency for the price (any stablecoin accepted).")
    top_users: list[ShopItemLeaderEntry] = Field(default_factory=list, description="Top leveled users.")


class ShopItemDetail(BaseModel):
    """Detailed shop item info."""

    key: str = Field(..., description="Unique item key.")
    name: str = Field(..., description="Display name.")
    description: str = Field("", description="Item description.")
    price: float = Field(0.0, description="Price in stablecoin (USD-pegged).")
    category: str = Field("item", description="Item category.")
    currency: str = Field("DSD", description="Default currency for the price (any stablecoin accepted).")
    mechanics: dict[str, Any] = Field(default_factory=dict, description="Item mechanics details.")


class InventoryItem(BaseModel):
    """User's owned item."""

    key: str = Field(..., description="Item key.")
    name: str = Field(..., description="Item name.")
    level: int = Field(1, description="Current level.")
    xp: float = Field(0.0, description="Current XP.")
    staked_amount: float = Field(0.0, description="Stablecoin amount staked into item.")
    acquired_at: str | None = Field(None, description="When item was acquired.")


class BuyRequest(BaseModel):
    """Request to purchase a shop item."""

    item_key: str = Field(..., description="Item key to purchase.")
    quantity: int = Field(1, ge=1, description="Quantity to buy.")


class BuyResult(BaseModel):
    """Result of a shop purchase."""

    success: bool = Field(True)
    message: str = Field(..., description="Human-readable result.")
    item_key: str = Field(..., description="Item purchased.")
    cost: float = Field(0.0, description="Total cost in stablecoin.")
    currency: str = Field("DSD", description="Currency used.")
    new_balance: float = Field(0.0, description="Updated stablecoin balance.")


class SellRequest(BaseModel):
    """Request to sell back a shop item."""

    item_key: str = Field(..., description="Item key to sell.")
    quantity: int = Field(1, ge=1, description="Quantity to sell.")


class SellResult(BaseModel):
    """Result of a shop sell-back."""

    success: bool = Field(True)
    message: str = Field(..., description="Human-readable result.")
    item_key: str = Field(..., description="Item sold.")
    revenue: float = Field(0.0, description="Stablecoin received (staked amount minus sell fee).")
    currency: str = Field("DSD", description="Currency received.")
    new_balance: float = Field(0.0, description="Updated stablecoin balance.")
