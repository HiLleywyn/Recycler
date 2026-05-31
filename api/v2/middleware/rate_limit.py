from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.v2.config import get_settings

# Paths that are exempt from rate limiting
_EXEMPT_PATHS = {"/health", "/api/v2/health", "/api/docs", "/api/redoc", "/api/openapi.json"}

# Auth-critical paths that should never be blocked by rate limiting  - 
# blocking these causes cascading failures (e.g. failed refresh → logout).
_EXEMPT_PREFIXES = ("/api/v2/auth/refresh", "/api/v2/auth/callback", "/api/v2/auth/logout")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis-backed fixed-window rate limiter.

    Uses INCR + EXPIRE on a key like ``rl:{ip}:{tier}:{window}`` to enforce
    rate limits over a 10-second window:

    * **public** -- unauthenticated requests (default 30 / 10s)
    * **authenticated** -- requests with a valid Bearer token (default 60 / 10s)
    """

    WINDOW_SECONDS = 10

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip rate limiting for exempted paths
        if request.url.path in _EXEMPT_PATHS or request.url.path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # Determine the caller's IP, respecting X-Forwarded-For when
        # TRUST_PROXY_HEADERS is enabled (only safe behind a trusted proxy).
        settings = get_settings()
        if settings.TRUST_PROXY_HEADERS:
            forwarded_for = request.headers.get("x-forwarded-for", "")
            # Find the first non-empty IP in the comma-separated list; the
            # leftmost entry is the original client when the proxy is trusted.
            candidates = [ip.strip() for ip in forwarded_for.split(",")]
            client_ip = next(
                (ip for ip in candidates if ip),
                request.client.host if request.client else "unknown",
            )
        else:
            client_ip = request.client.host if request.client else "unknown"

        # Determine tier from the Authorization header (cheap heuristic --
        # full JWT validation happens later in the dependency chain).
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            # We peek at the token claims later; for now treat all
            # Bearer-carrying requests as "authenticated".
            tier = "auth"
        else:
            tier = "public"

        limit_map = {
            "public": settings.RATE_LIMIT_PUBLIC,
            "auth": settings.RATE_LIMIT_AUTH,
        }
        max_requests = limit_map[tier]

        # Build the Redis key scoped to the current 10-second window
        window = int(time.time()) // self.WINDOW_SECONDS
        redis_key = f"rl:{client_ip}:{tier}:{window}"

        try:
            redis = getattr(request.app.state, 'redis', None)
            if redis is None:
                return await call_next(request)
            count = await redis.incr(redis_key)
            if count == 1:
                await redis.expire(redis_key, self.WINDOW_SECONDS + 1)
        except Exception:
            # If Redis is unavailable, allow the request through rather
            # than blocking all traffic.
            return await call_next(request)

        if count > max_requests:
            retry_after = self.WINDOW_SECONDS - (int(time.time()) % self.WINDOW_SECONDS)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "code": "RATE_LIMITED",
                    "detail": "Rate limit exceeded. Please try again later.",
                },
                headers={"Retry-After": str(retry_after)},
            )

        response = await call_next(request)

        # Attach informational rate-limit headers
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(max(0, max_requests - count))
        response.headers["X-RateLimit-Reset"] = str((window + 1) * self.WINDOW_SECONDS)

        return response
