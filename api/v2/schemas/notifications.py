"""Notifications Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Notification(BaseModel):
    """A single notification entry."""

    id: int = Field(..., description="Notification ID.")
    type: str = Field(..., description="Notification type.")
    title: str = Field(..., description="Notification title.")
    body: str | None = Field(None, description="Notification body.")
    data: dict[str, Any] | None = Field(None, description="Structured payload.")
    is_read: bool = Field(False, description="Whether it has been read.")
    created_at: str = Field(..., description="Timestamp (ISO 8601).")


class MarkReadRequest(BaseModel):
    """Request to mark specific notifications as read."""

    ids: list[int] = Field(..., min_length=1, description="List of notification IDs to mark read.")


class NotificationPreferences(BaseModel):
    """User notification preferences (DM settings)."""

    dm_mining: bool = Field(True, description="DM on mining events.")
    dm_transfer: bool = Field(True, description="DM on transfers.")
    dm_validator: bool = Field(True, description="DM on validator events.")
    dm_staking: bool = Field(True, description="DM on staking events.")
    dm_2fa: bool = Field(True, description="DM on 2FA events.")


class NotificationPreferencesUpdate(BaseModel):
    """Partial update for notification preferences."""

    dm_mining: bool | None = Field(None, description="DM on mining events.")
    dm_transfer: bool | None = Field(None, description="DM on transfers.")
    dm_validator: bool | None = Field(None, description="DM on validator events.")
    dm_staking: bool | None = Field(None, description="DM on staking events.")
    dm_2fa: bool | None = Field(None, description="DM on 2FA events.")
