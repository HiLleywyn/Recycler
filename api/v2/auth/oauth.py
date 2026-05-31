from __future__ import annotations

from urllib.parse import urlencode
from typing import Any

import httpx

from api.v2.config import get_settings

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"


def get_oauth_url(state: str) -> str:
    """Build the Discord OAuth2 authorization URL."""
    settings = get_settings()
    params = {
        "client_id": settings.DISCORD_CLIENT_ID,
        "redirect_uri": settings.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    }
    return f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}"


async def exchange_code(code: str) -> str:
    """Exchange an authorization code for a Discord access token.

    Returns the ``access_token`` string.
    """
    settings = get_settings()
    data = {
        "client_id": settings.DISCORD_CLIENT_ID,
        "client_secret": settings.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.DISCORD_REDIRECT_URI,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            DISCORD_OAUTH_TOKEN,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def get_discord_user(access_token: str) -> dict[str, Any]:
    """Fetch the authenticated user's Discord profile (``/users/@me``)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_user_guilds(access_token: str) -> list[dict[str, Any]]:
    """Fetch the authenticated user's guild list (``/users/@me/guilds``)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()
