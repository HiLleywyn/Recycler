"""api/v2/auth/router.py  -  Discord OAuth2 + JWT + TOTP 2FA for FastAPI.

Works with the bot's PostgreSQL database (PgDatabase) when running inside the
bot process.  Redis is used opportunistically for OAuth state tokens but falls
back to an in-process dict when Redis is unavailable.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse

from api.v2.auth.jwt import (
    create_access_token,
    create_partial_token,
    create_tfa_pending_token,
    generate_refresh_token,
)
from api.v2.auth.oauth import exchange_code, get_discord_user, get_oauth_url, get_user_guilds
from api.v2.auth.schemas import (
    GuildInfo,
    GuildsResponse,
    GuildSelectRequest,
    TokenResponse,
    TwoFactorSetupResponse,
    TwoFactorStatusResponse,
    TwoFactorVerifyRequest,
    UserResponse,
)
from api.v2.config import get_settings
from api.v2.dependencies import get_current_user
from api.v2.exceptions import NotFoundError, UnauthorizedError, ValidationError

router = APIRouter(prefix="/auth", tags=["auth"])

# ── In-process fallbacks (used when Redis is unavailable) ───────────────────
_pending_states: dict[str, float] = {}  # state -> expiry timestamp
_discord_tokens: dict[str, tuple[str, float]] = {}  # user_id -> (access_token, expiry)
_pending_guilds: dict[str, tuple[list, float]] = {}  # user_id -> (guild_list, timestamp)
_2fa_setups: dict[str, tuple[dict, float]] = {}  # user_id -> (setup_data, expiry)
_STATE_TTL = 300
_GUILDS_TTL = 600

# ── Refresh token helpers ─────────────────────────────────────────────────

_REFRESH_COOKIE_OPTS = dict(
    key="refresh_token",
    httponly=True,
    secure=True,
    samesite="lax",
    path="/api/v2/auth",
)


async def _issue_refresh_token(
    response: Response,
    request: Request,
    user_id: str,
    guild_id: str,
) -> None:
    """Generate a refresh token, store its hash in the DB, and set the cookie."""
    settings = get_settings()
    raw, hashed = generate_refresh_token()
    expire_days = settings.REFRESH_TOKEN_EXPIRE_DAYS

    bot = _get_bot(request)
    if bot and hasattr(bot, "db"):
        try:
            await bot.db.execute(
                "INSERT INTO refresh_tokens (token_hash, user_id, guild_id, expires_at) "
                "VALUES ($1, $2, $3, now() + make_interval(days => $4))",
                hashed, int(user_id), int(guild_id), expire_days,
            )
        except Exception:
            pass  # graceful  -  login still works without refresh

    response.set_cookie(
        **_REFRESH_COOKIE_OPTS,
        value=raw,
        max_age=expire_days * 86400,
    )

_used_2fa_tokens: dict[str, float] = {}  # JWT signature -> expiry
_2fa_attempt_count: dict[str, int] = {}
_2FA_MAX_ATTEMPTS = 5


def _purge_expired() -> None:
    """Purge all expired in-process state."""
    now = time.time()
    for d in (_pending_states, _discord_tokens, _pending_guilds, _2fa_setups, _used_2fa_tokens):
        expired = [k for k, v in d.items() if (v if isinstance(v, (int, float)) else v[1] if isinstance(v, tuple) and len(v) == 2 else 0) < now]
        for k in expired:
            d.pop(k, None)
    for k in [k for k, v in _used_2fa_tokens.items() if v < now]:
        _2fa_attempt_count.pop(k, None)


async def _state_store(request: Request, state: str) -> None:
    """Store an OAuth state token in Redis or in-process dict."""
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            await redis.setex(f"oauth_state:{state}", _STATE_TTL, "1")
            return
        except Exception:
            pass
    _purge_expired()
    _pending_states[state] = time.time() + _STATE_TTL


async def _state_consume(request: Request, state: str) -> bool:
    """Consume an OAuth state token. Returns True if valid."""
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            stored = await redis.get(f"oauth_state:{state}")
            if stored:
                await redis.delete(f"oauth_state:{state}")
                return True
            # Fall through to in-process check
        except Exception:
            pass
    _purge_expired()
    return _pending_states.pop(state, None) is not None


def _get_bot(request: Request):
    """Get the bot instance from app state, or None."""
    return getattr(request.app.state, "bot", None)


# ---------------------------------------------------------------------------
# GET /auth/discord -- redirect to Discord OAuth
# ---------------------------------------------------------------------------
@router.get("/discord", summary="Start Discord OAuth2 flow")
async def discord_oauth_redirect(request: Request):
    """Redirect the user to Discord's OAuth2 authorization page."""
    state = secrets.token_urlsafe(32)
    await _state_store(request, state)
    url = get_oauth_url(state)
    return {"url": url}


# ---------------------------------------------------------------------------
# GET /auth/callback -- exchange code, redirect to SPA with token
# ---------------------------------------------------------------------------
@router.get("/callback", summary="Discord OAuth2 callback")
async def discord_oauth_callback(
    code: str,
    state: str,
    request: Request,
):
    """Handle the OAuth2 callback from Discord.

    Exchanges the authorization ``code`` for a Discord access token, fetches
    the user profile and guilds, and redirects to the SPA with a partial JWT.
    """
    error = request.query_params.get("error", "")
    if error:
        return RedirectResponse(url="/dashboard?auth_error=denied")

    if not await _state_consume(request, state):
        return RedirectResponse(url="/dashboard?auth_error=invalid_state")

    try:
        access_token = await exchange_code(code)
        discord_user = await get_discord_user(access_token)
    except Exception:
        return RedirectResponse(url="/dashboard?auth_error=token_exchange_failed")

    user_id = discord_user["id"]
    username = discord_user["username"]
    avatar = discord_user.get("avatar")

    # Cache Discord token and guild list for the next steps
    try:
        user_guilds = await get_user_guilds(access_token)
    except Exception:
        user_guilds = []

    # Store in-process (reliable, no Redis needed)
    now = time.time()
    _discord_tokens[user_id] = (access_token, now + _STATE_TTL)
    _pending_guilds[user_id] = (user_guilds, now + _GUILDS_TTL)

    # Also try Redis
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            await redis.setex(f"discord_token:{user_id}", _STATE_TTL, access_token)
        except Exception:
            pass

    partial_token = create_partial_token(user_id, username, avatar)

    # Redirect to SPA  -  the frontend reads ?token= from the URL
    return RedirectResponse(url=f"/dashboard?token={partial_token}")


# ---------------------------------------------------------------------------
# GET /auth/guilds -- list mutual guilds
# ---------------------------------------------------------------------------
@router.get("/guilds", response_model=GuildsResponse, summary="List mutual guilds")
async def list_guilds(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Return the guilds the user shares with the Discoin bot."""
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Missing Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()

    try:
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token.")

    user_id = payload["sub"]

    # Try in-process cache first, then Redis
    user_guilds = None
    cached = _pending_guilds.get(user_id)
    if cached and cached[1] > time.time():
        user_guilds = cached[0]

    if user_guilds is None:
        # Try to fetch from Discord using cached token
        discord_token = None
        dt = _discord_tokens.get(user_id)
        if dt and dt[1] > time.time():
            discord_token = dt[0]

        if not discord_token:
            redis = getattr(request.app.state, "redis", None)
            if redis:
                try:
                    discord_token = await redis.get(f"discord_token:{user_id}")
                except Exception:
                    pass

        if not discord_token:
            raise UnauthorizedError("Discord token expired. Please re-authenticate.")

        user_guilds = await get_user_guilds(discord_token)

    # Filter to only guilds where the bot is present
    bot = _get_bot(request)
    if bot:
        bot_guild_ids = {str(g.id) for g in bot.guilds}
        mutual: list[GuildInfo] = [
            GuildInfo(id=str(g["id"]), name=g["name"], icon=g.get("icon"))
            for g in user_guilds
            if isinstance(g, dict) and str(g.get("id", "")) in bot_guild_ids
        ]
    else:
        mutual = [
            GuildInfo(id=str(g["id"]), name=g["name"], icon=g.get("icon"))
            for g in user_guilds
            if isinstance(g, dict)
        ]

    return GuildsResponse(guilds=mutual)


# ---------------------------------------------------------------------------
# POST /auth/select-guild -- issue full tokens
# ---------------------------------------------------------------------------
@router.post("/select-guild", response_model=TokenResponse, summary="Select guild and get tokens")
async def select_guild(
    body: GuildSelectRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
):
    """Finalise login by selecting a guild.

    Issues a full access token. If 2FA is enabled, returns a tfa_pending token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Missing Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()

    try:
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token.")

    user_id = payload["sub"]
    username = payload.get("username", "")
    avatar = payload.get("avatar")
    guild_id = body.guild_id

    # Verify guild membership and resolve permissions
    from core.config import Config as _Config
    is_admin = False
    is_owner = (int(user_id) == _Config.REPORT_TARGET_USER_ID)  # bot developer = highest tier
    bot = _get_bot(request)
    if bot:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            raise ValidationError("Bot is not in that guild.")
        try:
            member = guild.get_member(int(user_id))
            if member is None:
                member = await guild.fetch_member(int(user_id))
            if member:
                is_admin = member.guild_permissions.administrator
        except Exception:
            raise ValidationError("You are not a member of that guild.")

    # Also grant admin if user is in the admin_users table for this guild
    if not is_admin and not is_owner and bot and hasattr(bot, "db"):
        try:
            row = await bot.db.fetch_one(
                "SELECT 1 FROM admin_users WHERE guild_id = $1 AND user_id = $2",
                int(guild_id), int(user_id),
            )
            if row:
                is_admin = True
        except Exception:
            pass

    # Bot manager with all_perms gets admin privileges
    if not is_admin and not is_owner and bot and hasattr(bot, "db"):
        try:
            mgr_row = await bot.db.fetch_one(
                "SELECT bot_manager_id, bot_manager_all_perms FROM guild_settings WHERE guild_id = $1",
                int(guild_id),
            )
            if (mgr_row and mgr_row.get("bot_manager_all_perms")
                    and mgr_row.get("bot_manager_id")
                    and int(user_id) == mgr_row["bot_manager_id"]):
                is_admin = True
        except Exception:
            pass

    # Clean up cached guild list
    _pending_guilds.pop(user_id, None)
    _discord_tokens.pop(user_id, None)

    # Check if user has 2FA enabled (PostgreSQL via bot.db)
    has_2fa = False
    if bot and hasattr(bot, "db"):
        try:
            row = await bot.db.fetch_one(
                "SELECT totp_secret, enabled FROM user_2fa WHERE user_id = $1",
                int(user_id),
            )
            if row and row["enabled"]:
                has_2fa = True
        except Exception:
            pass

    if has_2fa:
        tfa_token = create_tfa_pending_token(user_id, guild_id, username, avatar)
        return TokenResponse(access_token=tfa_token, expires_in=300)

    access_token = create_access_token(user_id, guild_id, username, avatar, is_admin, is_owner)
    await _issue_refresh_token(response, request, user_id, guild_id)
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.JWT_EXPIRE_SECONDS,
    )


# ---------------------------------------------------------------------------
# POST /auth/refresh -- exchange refresh cookie for new tokens
# ---------------------------------------------------------------------------
@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh_tokens_endpoint(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
):
    """Exchange the httpOnly ``refresh_token`` cookie for a new access token."""
    if not refresh_token:
        raise UnauthorizedError("Missing refresh token cookie.")

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    settings = get_settings()

    bot = _get_bot(request)
    if not bot or not hasattr(bot, "db"):
        raise UnauthorizedError("Database unavailable for token validation.")

    # Look up the refresh token
    row = await bot.db.fetch_one(
        "SELECT user_id, guild_id, expires_at, revoked "
        "FROM refresh_tokens WHERE token_hash = $1",
        token_hash,
    )
    if not row:
        raise UnauthorizedError("Invalid refresh token.")

    if row["revoked"]:
        raise UnauthorizedError("Refresh token has been revoked.")

    # Check expiry (expires_at is a datetime, compare server-side)
    expired = await bot.db.fetch_val(
        "SELECT expires_at < now() FROM refresh_tokens WHERE token_hash = $1",
        token_hash,
    )
    if expired:
        raise UnauthorizedError("Refresh token has expired.")

    user_id = str(row["user_id"])
    guild_id = str(row["guild_id"])

    # Fetch user info for token claims
    from core.config import Config as _Config
    bot_instance = _get_bot(request)
    username = ""
    avatar = None
    is_admin = False
    is_owner = (int(user_id) == _Config.REPORT_TARGET_USER_ID)  # bot developer = highest tier
    if bot_instance:
        try:
            guild = bot_instance.get_guild(int(guild_id))
            if guild:
                member = guild.get_member(int(user_id))
                if member:
                    username = member.display_name
                    avatar = str(member.display_avatar.url) if member.display_avatar else None
                    is_admin = member.guild_permissions.administrator
        except Exception:
            pass

    # Also grant admin if user is in the admin_users table for this guild
    if not is_admin and not is_owner and bot_instance and hasattr(bot_instance, "db"):
        try:
            admin_row = await bot_instance.db.fetch_one(
                "SELECT 1 FROM admin_users WHERE guild_id = $1 AND user_id = $2",
                int(guild_id), int(user_id),
            )
            if admin_row:
                is_admin = True
        except Exception:
            pass

    # Bot manager with all_perms gets admin privileges
    if not is_admin and not is_owner and bot_instance and hasattr(bot_instance, "db"):
        try:
            mgr_row = await bot_instance.db.fetch_one(
                "SELECT bot_manager_id, bot_manager_all_perms FROM guild_settings WHERE guild_id = $1",
                int(guild_id),
            )
            if (mgr_row and mgr_row.get("bot_manager_all_perms")
                    and mgr_row.get("bot_manager_id")
                    and int(user_id) == mgr_row["bot_manager_id"]):
                is_admin = True
        except Exception:
            pass

    # Revoke old token and issue new pair (rotate)
    await bot.db.execute(
        "UPDATE refresh_tokens SET revoked = TRUE WHERE token_hash = $1",
        token_hash,
    )

    access_token = create_access_token(user_id, guild_id, username, avatar, is_admin, is_owner)
    await _issue_refresh_token(response, request, user_id, guild_id)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.JWT_EXPIRE_SECONDS,
    )


# ---------------------------------------------------------------------------
# POST /auth/logout -- revoke refresh token
# ---------------------------------------------------------------------------
@router.post("/logout", summary="Log out and revoke refresh token")
async def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
):
    """Revoke the refresh token in the database and clear the cookie.

    Also blacklists the access token's jti in Redis so it cannot be used
    until it naturally expires, even if the client still holds it.
    """
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        bot = _get_bot(request)
        if bot and hasattr(bot, "db"):
            try:
                await bot.db.execute(
                    "UPDATE refresh_tokens SET revoked = TRUE WHERE token_hash = $1",
                    token_hash,
                )
            except Exception:
                pass

    # Blacklist the access token jti so it can't be reused before natural expiry
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from api.v2.config import get_settings
            _settings = get_settings()
            _token = auth_header.removeprefix("Bearer ").strip()
            _payload = pyjwt.decode(
                _token, _settings.JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
            _jti = _payload.get("jti")
            _exp = _payload.get("exp", 0)
            if _jti:
                _ttl = max(1, int(_exp) - int(time.time()))
                _redis = getattr(request.app.state, "redis", None)
                if _redis is not None:
                    try:
                        await _redis.setex(f"discoin:jwt_blacklist:{_jti}", _ttl, "1")
                    except Exception:
                        pass
        except Exception:
            pass  # don't block logout on JWT parse errors

    response.delete_cookie(**_REFRESH_COOKIE_OPTS)
    return {"success": True, "message": "Logged out successfully."}


# ---------------------------------------------------------------------------
# GET /auth/me -- return user info from JWT
# ---------------------------------------------------------------------------
@router.get("/me", response_model=UserResponse, summary="Get current user")
async def get_me(user: dict[str, Any] = Depends(get_current_user)):
    """Return the currently authenticated user's profile from the JWT claims."""
    return UserResponse(
        user_id=user["user_id"],
        username=user["username"],
        avatar=user.get("avatar"),
        guild_id=user.get("guild_id"),
        is_admin=user.get("is_admin", False),
    )


# ---------------------------------------------------------------------------
# POST /auth/2fa/setup -- generate TOTP secret
# ---------------------------------------------------------------------------
@router.post(
    "/2fa/setup",
    response_model=TwoFactorSetupResponse,
    summary="Begin 2FA setup",
)
async def setup_2fa(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Generate a new TOTP secret for the user."""
    from core.framework.totp import generate_secret as gen_secret, otpauth_uri

    bot = _get_bot(request)
    user_id = int(user["user_id"])
    username = user["username"]

    # Check if already enabled
    if bot and hasattr(bot, "db"):
        try:
            row = await bot.db.fetch_one(
                "SELECT enabled FROM user_2fa WHERE user_id = $1", user_id,
            )
            if row and row["enabled"]:
                raise ValidationError("2FA is already enabled.")
        except ValidationError:
            raise
        except Exception:
            pass

    secret = gen_secret()
    uri = otpauth_uri(secret, username)

    # Store pending setup in bot's DB (upsert, not yet enabled)
    if bot and hasattr(bot, "db"):
        await bot.db.execute(
            "INSERT INTO user_2fa (user_id, totp_secret, enabled) VALUES ($1, $2, FALSE) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET totp_secret = $2, enabled = FALSE",
            user_id, secret,
        )

    return TwoFactorSetupResponse(
        secret=secret,
        uri=uri,
        backup_codes=[],  # backup codes not used in v1 schema
    )


# ---------------------------------------------------------------------------
# POST /auth/2fa/verify-setup -- enable 2FA
# ---------------------------------------------------------------------------
@router.post("/2fa/verify-setup", summary="Confirm and enable 2FA")
async def verify_2fa_setup(
    body: TwoFactorVerifyRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Verify a TOTP code to finalize 2FA enrollment."""
    from core.framework.totp import verify_totp

    bot = _get_bot(request)
    user_id = int(user["user_id"])

    if not bot or not hasattr(bot, "db"):
        raise ValidationError("Database unavailable.")

    row = await bot.db.fetch_one(
        "SELECT totp_secret FROM user_2fa WHERE user_id = $1", user_id,
    )
    if not row:
        raise ValidationError("No pending 2FA setup. Call /2fa/setup first.")

    if not verify_totp(row["totp_secret"], body.code):
        raise UnauthorizedError("Invalid TOTP code.")

    await bot.db.execute(
        "UPDATE user_2fa SET enabled = TRUE WHERE user_id = $1", user_id,
    )
    return {"success": True, "message": "Two-factor authentication enabled."}


# ---------------------------------------------------------------------------
# POST /auth/2fa/verify -- verify during login
# ---------------------------------------------------------------------------
@router.post("/2fa/verify", response_model=TokenResponse, summary="Verify 2FA during login")
async def verify_2fa(
    body: TwoFactorVerifyRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
):
    """Verify a TOTP code during login to upgrade a tfa_pending token."""
    from core.framework.totp import verify_totp

    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Missing Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()

    try:
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token.")

    if not payload.get("tfa_pending"):
        raise UnauthorizedError("Token is not a 2FA-pending token.")

    # Replay & rate-limit protection
    _purge_expired()
    sig = token.rsplit(".", 1)[-1]

    if sig in _used_2fa_tokens:
        raise UnauthorizedError("Token already used.")

    if _2fa_attempt_count.get(sig, 0) >= _2FA_MAX_ATTEMPTS:
        raise UnauthorizedError("Too many attempts.")

    _2fa_attempt_count[sig] = _2fa_attempt_count.get(sig, 0) + 1

    user_id = payload["sub"]
    guild_id = payload.get("guild_id")
    username = payload.get("username", "")
    avatar = payload.get("avatar")
    is_admin = payload.get("is_admin", False)
    is_owner = payload.get("is_owner", False)

    bot = _get_bot(request)
    if not bot or not hasattr(bot, "db"):
        raise NotFoundError("Database unavailable.")

    row = await bot.db.fetch_one(
        "SELECT totp_secret, enabled FROM user_2fa WHERE user_id = $1 AND enabled = TRUE",
        int(user_id),
    )
    if not row:
        raise NotFoundError("2FA is not enabled for this user.")

    if not verify_totp(row["totp_secret"], body.code):
        raise UnauthorizedError("Invalid TOTP code.")

    # Resolve permissions (same as login  -  bot developer, admin_users, bot_manager)
    from core.config import Config as _Config
    is_owner = (int(user_id) == _Config.REPORT_TARGET_USER_ID)  # bot developer = highest tier
    if guild_id and bot:
        guild = bot.get_guild(int(guild_id))
        if guild:
            try:
                member = guild.get_member(int(user_id))
                if member is None:
                    member = await guild.fetch_member(int(user_id))
                if member:
                    is_admin = is_admin or member.guild_permissions.administrator
            except Exception:
                pass

        if not is_admin and not is_owner:
            try:
                admin_row = await bot.db.fetch_one(
                    "SELECT 1 FROM admin_users WHERE guild_id = $1 AND user_id = $2",
                    int(guild_id), int(user_id),
                )
                if admin_row:
                    is_admin = True
            except Exception:
                pass

        if not is_admin and not is_owner:
            try:
                mgr_row = await bot.db.fetch_one(
                    "SELECT bot_manager_id, bot_manager_all_perms FROM guild_settings WHERE guild_id = $1",
                    int(guild_id),
                )
                if (mgr_row and mgr_row.get("bot_manager_all_perms")
                        and mgr_row.get("bot_manager_id")
                        and int(user_id) == mgr_row["bot_manager_id"]):
                    is_admin = True
            except Exception:
                pass

    # Mark token as used
    _used_2fa_tokens[sig] = time.time() + 300

    access_token = create_access_token(user_id, guild_id, username, avatar, is_admin, is_owner)
    await _issue_refresh_token(response, request, user_id, guild_id)
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.JWT_EXPIRE_SECONDS,
    )


# ---------------------------------------------------------------------------
# POST /auth/2fa/disable -- disable 2FA
# ---------------------------------------------------------------------------
@router.post("/2fa/disable", summary="Disable 2FA")
async def disable_2fa(
    body: TwoFactorVerifyRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Disable two-factor authentication. Requires a valid TOTP code."""
    from core.framework.totp import verify_totp

    bot = _get_bot(request)
    user_id = int(user["user_id"])

    if not bot or not hasattr(bot, "db"):
        raise NotFoundError("Database unavailable.")

    row = await bot.db.fetch_one(
        "SELECT totp_secret, enabled FROM user_2fa WHERE user_id = $1 AND enabled = TRUE",
        int(user_id),
    )
    if not row:
        raise NotFoundError("2FA is not enabled for this user.")

    if not verify_totp(row["totp_secret"], body.code):
        raise UnauthorizedError("Invalid TOTP code.")

    await bot.db.execute("DELETE FROM user_2fa WHERE user_id = $1", user_id)
    return {"success": True, "message": "Two-factor authentication disabled."}


# ---------------------------------------------------------------------------
# GET /auth/2fa/status -- check 2FA status
# ---------------------------------------------------------------------------
@router.get(
    "/2fa/status",
    response_model=TwoFactorStatusResponse,
    summary="Check 2FA status",
)
async def get_2fa_status(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Check whether the current user has 2FA enabled."""
    bot = _get_bot(request)
    enabled = False

    if bot and hasattr(bot, "db"):
        try:
            row = await bot.db.fetch_one(
                "SELECT enabled FROM user_2fa WHERE user_id = $1",
                int(user["user_id"]),
            )
            enabled = bool(row and row["enabled"])
        except Exception:
            pass

    return TwoFactorStatusResponse(enabled=enabled)
