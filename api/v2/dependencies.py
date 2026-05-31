from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request

from api.v2.config import get_settings
from api.v2.exceptions import ForbiddenError, ModuleDisabledError, UnauthorizedError


async def get_db(request: Request):
    """Get database connection. Uses asyncpg pool if available, falls back to bot's DB pool."""
    app = request.app
    if hasattr(app.state, 'db_pool') and app.state.db_pool is not None:
        async with app.state.db_pool.acquire() as conn:
            yield conn
    elif hasattr(app.state, 'bot') and app.state.bot is not None:
        # Running inside bot process -- use bot's asyncpg pool directly
        bot_pool = getattr(app.state.bot.db, '_pool', None)
        if bot_pool is not None:
            async with bot_pool.acquire() as conn:
                yield conn
        else:
            raise HTTPException(status_code=503, detail="Bot database pool not initialized")
    else:
        raise HTTPException(status_code=503, detail="Database unavailable")


async def get_orm_db(request: Request):
    """Get the ORM Database instance (for services that need ORM methods like compute_net_worth)."""
    app = request.app
    if hasattr(app.state, 'bot') and app.state.bot is not None:
        db = getattr(app.state.bot, 'db', None)
        if db is not None:
            return db
    raise HTTPException(status_code=503, detail="ORM database unavailable")


async def get_redis(request: Request):
    """Return the shared Redis client stored in app state (may be None)."""
    return getattr(request.app.state, 'redis', None)


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Decode JWT from the Authorization header and return the user payload.

    Raises 401 if the token is missing, expired, or invalid.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token has expired.")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token.")

    # Check Redis jti blacklist (populated on explicit logout)
    jti = payload.get("jti")
    if jti:
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            try:
                if await redis.exists(f"discoin:jwt_blacklist:{jti}"):
                    raise UnauthorizedError("Token has been revoked.")
            except UnauthorizedError:
                raise
            except Exception:
                pass  # fail open on Redis errors  -  don't lock users out

    if payload.get("partial"):
        raise UnauthorizedError("Partial token cannot access this resource. Complete login first.")

    if payload.get("tfa_pending"):
        raise UnauthorizedError("Two-factor authentication required.")

    return {
        "user_id": payload["sub"],
        "guild_id": payload.get("guild_id"),
        "username": payload.get("username"),
        "avatar": payload.get("avatar"),
        "is_admin": payload.get("is_admin", False),
        "is_owner": payload.get("is_owner", False),
        "jti": payload.get("jti"),
    }


async def get_optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any] | None:
    """Same as get_current_user but returns None when no auth is provided."""
    if not authorization or not authorization.startswith("Bearer "):
        return None

    try:
        return await get_current_user(request, authorization)
    except UnauthorizedError:
        return None


async def require_bot_owner(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Bot-owner-only API gate.

    Used for premium subscription management endpoints where guild admins
    must NOT be allowed (since they could grant their own server premium
    for free). Resolves owner from the JWT's ``is_owner`` flag (set at
    OAuth login from Discord application ownership) and falls back to
    ``Config.BOT_OWNER_ID`` for users authenticated through other paths.
    """
    if user.get("is_owner"):
        return user
    try:
        from core.config import Config
        if Config.BOT_OWNER_ID and int(user.get("user_id", 0)) == int(Config.BOT_OWNER_ID):
            return user
    except Exception:
        pass
    raise ForbiddenError("Bot-owner access required.")


async def require_admin(
    user: dict[str, Any] = Depends(get_current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    """Verify admin access, re-checking the database on every request.

    ``is_owner`` (bot developer) is considered permanent and is not re-queried.
    ``is_admin`` is re-verified against the ``admin_users`` table so that
    revocations take effect immediately without waiting for token expiry.
    """
    if user.get("is_owner"):
        return user

    guild_id = user.get("guild_id")
    user_id = user.get("user_id")
    if guild_id and user_id:
        row = await conn.fetchrow(
            "SELECT 1 FROM admin_users WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(user_id),
        )
        if row:
            return user

    raise ForbiddenError("Admin access required.")


async def require_security_access(
    user: dict[str, Any] = Depends(get_current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    """Allow server owners, admins, or users with a designated security_audit role.

    Owner/admin/bot_manager status is already in the JWT. The only DB check
    remaining is for security_audit_roles (not yet in the token).
    """
    if user.get("is_owner") or user.get("is_admin"):
        return user

    guild_id = user.get("guild_id")
    if guild_id:
        row = await conn.fetchrow(
            "SELECT security_audit_roles FROM guild_settings WHERE guild_id=$1",
            int(guild_id),
        )
        if row:
            raw = (row["security_audit_roles"] or "").strip()
            if raw:
                return user

    raise ForbiddenError("Security log access requires server owner or a designated security role.")


def require_module(*modules: str):
    """Return a FastAPI ``Depends`` that rejects requests when **all** of the
    listed modules are disabled for the user's guild.  If *any* module in the
    list is enabled the request proceeds (OR logic, matching the bot's
    ``cog_check`` behaviour).

    Usage on a router::

        router = APIRouter(dependencies=[require_module("gambling", "games")])

    Or on a single endpoint::

        @router.post("/foo", dependencies=[require_module("lending")])
    """

    async def _check(
        user: dict[str, Any] = Depends(get_current_user),
        conn=Depends(get_db),
    ) -> None:
        guild_id = user.get("guild_id")
        if not guild_id:
            return
        row = await conn.fetchrow(
            "SELECT * FROM guild_settings WHERE guild_id=$1", int(guild_id),
        )
        if not row:
            return  # no settings row → all modules default to enabled
        settings = dict(row)
        for mod in modules:
            val = settings.get(f"module_{mod}")
            if val is None or val is True:
                return  # at least one module enabled → allow
        raise ModuleDisabledError(modules[0])

    return Depends(_check)
