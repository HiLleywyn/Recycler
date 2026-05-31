"""
Security Middleware
====================

Sits in the FastAPI middleware stack (after rate limiter) and performs:

1. Extract request context (IP, user agent, JWT claims)
2. Check active enforcements  -  block frozen/banned users
3. Check circuit breakers  -  block guild-wide halted features
4. Feed API events into the SecurityEngine for profiling + detection
5. Fingerprint sessions for anomaly detection
"""
from __future__ import annotations

import hashlib
import logging
import time

import jwt as pyjwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.v2.config import get_settings

log = logging.getLogger("discoin.api.security")

# Paths that bypass security checks entirely
_EXEMPT_PATHS = frozenset({
    "/health", "/api/v2/health",
    "/api/docs", "/api/redoc", "/api/openapi.json",
})

_EXEMPT_PREFIXES = (
    "/api/v2/auth/",  # Auth flow must always work
    "/_next/",        # Static assets
    "/docs/",         # Documentation
)

# Map API path prefixes to enforcement scopes
_PATH_SCOPE_MAP = [
    ("/api/v2/trading/",   "trade"),
    ("/api/v2/pools/",     "pool"),
    ("/api/v2/staking/",   "stake"),
    ("/api/v2/mining/",    "mine"),
    ("/api/v2/savings/",   "earn"),
    ("/api/v2/lending/",   "loan"),
    ("/api/v2/games/",     "gamble"),
    ("/api/v2/shop/",      "trade"),
    ("/api/v2/contracts/", "trade"),
    ("/api/v2/nfts/",      "nft"),
]

# Only state-changing methods need security event processing
_STATE_CHANGING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _path_to_scope(path: str) -> str:
    """Map a request path to an enforcement scope."""
    for prefix, scope in _PATH_SCOPE_MAP:
        if path.startswith(prefix):
            return scope
    return "all"


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting proxy headers if configured."""
    settings = get_settings()
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidates = [ip.strip() for ip in forwarded.split(",")]
        ip = next((ip for ip in candidates if ip), None)
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


class SecurityMiddleware(BaseHTTPMiddleware):
    """Request-level security enforcement and event collection."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Skip exempt paths
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # Get security engine (may not be initialized yet)
        engine = getattr(request.app.state, "security_engine", None)
        if engine is None or not engine.is_running:
            return await call_next(request)

        # Extract JWT claims (lightweight  -  no full validation, that happens in dependencies)
        user_id = None
        guild_id = None
        is_admin = False
        is_owner = False
        is_exempt = False
        token_hash = ""

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                token = auth_header.removeprefix("Bearer ").strip()
                token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                settings = get_settings()
                payload = pyjwt.decode(
                    token, settings.JWT_SECRET,
                    algorithms=["HS256"],
                    options={"verify_exp": False},
                )
                user_id = int(payload.get("sub", 0))
                guild_id = int(payload.get("guild_id", 0))
                is_admin = payload.get("is_admin", False)
                is_owner = payload.get("is_owner", False)
            except Exception:
                pass

        if not user_id or not guild_id:
            # Unauthenticated request  -  still process for IP tracking on auth endpoints
            if path.startswith("/api/v2/auth/") and request.method == "POST":
                # Could be an auth failure  -  handled by the auth router itself
                pass
            return await call_next(request)

        # Hierarchy level 1  -  bot developer bypasses all enforcement checks
        if is_owner:
            return await call_next(request)

        # Admins are also exempt from security locks
        if is_admin:
            return await call_next(request)

        # Check owner-designated exemption list
        is_exempt = await engine.is_security_exempt(guild_id, user_id)

        # Also check if user is the bot_manager with auto_exempt enabled
        if not is_exempt:
            try:
                pool = getattr(request.app.state, "db_pool", None)
                bot = getattr(request.app.state, "bot", None)
                _pool = pool or (getattr(bot.db, "_pool", None) if bot and getattr(bot, "db", None) else None)
                if _pool:
                    async with _pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT bot_manager_id, bot_manager_auto_exempt "
                            "FROM guild_settings WHERE guild_id=$1",
                            guild_id,
                        )
                        if (row and row.get("bot_manager_auto_exempt")
                                and row.get("bot_manager_id")
                                and row["bot_manager_id"] == user_id):
                            is_exempt = True
            except Exception:
                pass  # Never let security middleware crash the request

        if is_exempt:
            return await call_next(request)

        # Determine scope for this request
        scope = _path_to_scope(path)

        # 1. Check enforcement  -  is the user blocked?
        if request.method in _STATE_CHANGING_METHODS:
            allowed, reason = await engine.check_user_allowed(guild_id, user_id, scope)
            if not allowed:
                log.warning(
                    "BLOCKED: user=%d guild=%d scope=%s path=%s reason=%s",
                    user_id, guild_id, scope, path, reason,
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "security_restricted",
                        "code": "SECURITY_RESTRICTED",
                        "detail": reason or "Your account is temporarily restricted by the security system.",
                    },
                )

        # 2. Process the request normally
        response = await call_next(request)

        # 3. Post-request: feed event to security engine (fire-and-forget, non-blocking)
        #    Only for state-changing requests that succeeded
        if (request.method in _STATE_CHANGING_METHODS
                and 200 <= response.status_code < 300):
            try:
                from security.models import SecurityEvent, EventSource

                event = SecurityEvent(
                    guild_id=guild_id,
                    user_id=user_id,
                    event_type=self._path_to_event_type(path),
                    source=EventSource.API,
                    ip_address=_get_client_ip(request),
                    user_agent=request.headers.get("user-agent", ""),
                    endpoint=path,
                    details={
                        "method": request.method,
                        "status_code": response.status_code,
                        "is_admin": is_admin,
                        "is_owner": is_owner,
                        "is_exempt": is_exempt,
                        "token_hash": token_hash,
                    },
                )

                # Process asynchronously  -  don't block the response
                import asyncio
                asyncio.ensure_future(self._process_event_safe(engine, event))
            except Exception:
                pass  # Never let security middleware crash the request

        # 4. Session fingerprinting
        if token_hash:
            try:
                import asyncio
                asyncio.ensure_future(
                    engine.cache.set_session_fingerprint(token_hash, {
                        "user_id": user_id,
                        "guild_id": guild_id,
                        "ip": _get_client_ip(request),
                        "user_agent": request.headers.get("user-agent", ""),
                        "last_seen": time.time(),
                    })
                )
            except Exception:
                pass

        return response

    @staticmethod
    async def _process_event_safe(engine, event) -> None:
        """Process a security event, catching all exceptions."""
        try:
            await engine.process_event(event)
        except Exception as exc:
            log.error("Security event processing failed: %s", exc)

    @staticmethod
    def _path_to_event_type(path: str) -> str:
        """Map an API path to a security event type."""
        if "/trading/" in path:
            if "/transfer" in path:
                return "transfer"
            return "trade"
        if "/games/" in path:
            return "gamble"
        if "/pools/" in path:
            return "pool"
        if "/staking/" in path:
            return "stake"
        if "/mining/" in path:
            return "mine"
        if "/savings/" in path:
            return "earn"
        if "/lending/" in path:
            return "loan"
        if "/admin/" in path:
            return "admin_action"
        if "/shop/" in path:
            return "trade"
        return "api_request"
