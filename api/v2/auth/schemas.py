from __future__ import annotations

from pydantic import BaseModel, Field


class GuildSelectRequest(BaseModel):
    """Request body for selecting a guild after OAuth login."""

    guild_id: str = Field(..., description="The Discord guild (server) ID to log in to.")


class TwoFactorVerifyRequest(BaseModel):
    """Request body for verifying a TOTP code."""

    code: str = Field(
        ...,
        min_length=6,
        max_length=8,
        description="6-digit TOTP code or 8-character backup code.",
    )


class TokenResponse(BaseModel):
    """Response returned when issuing access tokens."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds.")


class UserResponse(BaseModel):
    """Current authenticated user information."""

    user_id: str
    username: str
    avatar: str | None = None
    guild_id: str | None = None
    is_admin: bool = False


class GuildInfo(BaseModel):
    """Minimal guild information returned to the client."""

    id: str
    name: str
    icon: str | None = None


class GuildsResponse(BaseModel):
    """List of mutual guilds."""

    guilds: list[GuildInfo]


class TwoFactorSetupResponse(BaseModel):
    """Response when initiating 2FA setup."""

    secret: str = Field(..., description="Base32-encoded TOTP secret.")
    uri: str = Field(..., description="otpauth:// URI for QR code generation.")
    backup_codes: list[str] = Field(..., description="One-time backup codes.")


class TwoFactorStatusResponse(BaseModel):
    """Response for 2FA status check."""

    enabled: bool
