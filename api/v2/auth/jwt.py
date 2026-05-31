from __future__ import annotations

import hashlib
import secrets
import time
from uuid import uuid4

import jwt as pyjwt

from api.v2.config import get_settings


def create_access_token(
    user_id: str,
    guild_id: str,
    username: str,
    avatar: str | None,
    is_admin: bool = False,
    is_owner: bool = False,
) -> str:
    """Create a full-access JWT (HS256, 15 min expiry by default).

    The ``jti`` (JWT ID) is a UUID4 that can be used for token revocation.
    ``is_owner`` is True when the user is the bot developer (REPORT_TARGET_USER_ID)
     -  this grants full exemption from security enforcement across the bot, API,
    and dashboard.  Guild ownership confers no special privileges.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "guild_id": guild_id,
        "username": username,
        "avatar": avatar,
        "is_admin": is_admin,
        "is_owner": is_owner,
        "iat": now,
        "exp": now + settings.JWT_EXPIRE_SECONDS,
        "jti": str(uuid4()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def create_partial_token(
    user_id: str,
    username: str,
    avatar: str | None,
) -> str:
    """Create a short-lived partial JWT used before guild selection.

    This token has ``partial=True`` and a 5-minute expiry.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "avatar": avatar,
        "partial": True,
        "iat": now,
        "exp": now + 300,  # 5 minutes
        "jti": str(uuid4()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def create_tfa_pending_token(
    user_id: str,
    guild_id: str,
    username: str,
    avatar: str | None,
) -> str:
    """Create a short-lived JWT that indicates 2FA verification is pending.

    This token has ``tfa_pending=True`` and a 5-minute expiry.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "guild_id": guild_id,
        "username": username,
        "avatar": avatar,
        "tfa_pending": True,
        "iat": now,
        "exp": now + 300,  # 5 minutes
        "jti": str(uuid4()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    """Verify and decode a JWT.

    Raises ``pyjwt.ExpiredSignatureError`` or ``pyjwt.InvalidTokenError``
    on failure.
    """
    settings = get_settings()
    return pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])


def generate_refresh_token() -> tuple[str, str]:
    """Generate a cryptographically random refresh token.

    Returns:
        A tuple of ``(raw_token_hex, sha256_hash)`` -- the raw token is sent
        to the client, and only the hash is stored server-side.
    """
    raw = secrets.token_hex(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed
