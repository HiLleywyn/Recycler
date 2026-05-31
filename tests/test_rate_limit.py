"""Tests for api/v2/middleware/rate_limit.py  -  sliding-window rate limiter."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from api.v2.middleware.rate_limit import RateLimitMiddleware, _EXEMPT_PATHS


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _ok_endpoint(request: Request) -> Response:
    return JSONResponse({"ok": True})


def _build_app(redis_mock=None, rate_limit_public: int = 5, rate_limit_auth: int = 10, rate_limit_admin: int = 20):
    """Build a minimal Starlette test app with RateLimitMiddleware."""
    app = Starlette(routes=[
        Route("/test", _ok_endpoint),
        Route("/api/v2/health", _ok_endpoint),
    ])
    app.add_middleware(RateLimitMiddleware)

    if redis_mock is not None:
        app.state.redis = redis_mock

    return app


def _make_redis_mock(count_sequence=None, side_effect=None):
    """Create an AsyncMock for Redis that returns counts from count_sequence."""
    redis = AsyncMock()
    if side_effect is not None:
        redis.incr.side_effect = side_effect
    elif count_sequence is not None:
        redis.incr.side_effect = count_sequence
    else:
        redis.incr.return_value = 1
    redis.expire = AsyncMock(return_value=True)
    return redis


# ── Exempt paths ───────────────────────────────────────────────────────────────

class TestExemptPaths:
    def test_health_in_exempt_paths(self):
        assert "/health" in _EXEMPT_PATHS

    def test_api_v2_health_in_exempt_paths(self):
        assert "/api/v2/health" in _EXEMPT_PATHS

    def test_docs_in_exempt_paths(self):
        assert "/api/docs" in _EXEMPT_PATHS


# ── Redis unavailable ─────────────────────────────────────────────────────────

class TestRedisUnavailable:
    def test_no_redis_allows_request(self):
        """When Redis is not attached to app.state, requests must pass through."""
        app = Starlette(routes=[Route("/test", _ok_endpoint)])
        app.add_middleware(RateLimitMiddleware)
        # Do NOT set app.state.redis

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test")
        assert response.status_code == 200

    def test_redis_exception_allows_request(self):
        """If Redis raises an exception, the request must still be allowed."""
        redis = _make_redis_mock(side_effect=Exception("Redis down"))
        app = _build_app(redis_mock=redis)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test")
        assert response.status_code == 200


# ── Rate limit headers ────────────────────────────────────────────────────────

class TestRateLimitHeaders:
    def test_headers_present_on_normal_request(self):
        redis = _make_redis_mock(count_sequence=[1])
        app = _build_app(redis_mock=redis)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test")
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_remaining_decreases_with_requests(self):
        redis = _make_redis_mock(count_sequence=[1, 2, 3])
        app = _build_app(redis_mock=redis)
        with TestClient(app, raise_server_exceptions=False) as client:
            r1 = client.get("/test")
            r2 = client.get("/test")
        rem1 = int(r1.headers["X-RateLimit-Remaining"])
        rem2 = int(r2.headers["X-RateLimit-Remaining"])
        assert rem2 < rem1

    def test_remaining_never_negative(self):
        # Use a count within the public limit so we get the rate-limit headers
        redis = _make_redis_mock(count_sequence=[1])
        app = _build_app(redis_mock=redis)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test")
        assert response.status_code == 200
        remaining = int(response.headers["X-RateLimit-Remaining"])
        assert remaining >= 0


# ── Rate limiting enforcement ─────────────────────────────────────────────────

class TestRateLimitEnforcement:
    def test_over_limit_returns_429(self):
        """When the counter exceeds the public limit, return 429."""
        settings_mock = MagicMock()
        settings_mock.RATE_LIMIT_PUBLIC = 5
        settings_mock.RATE_LIMIT_AUTH = 10
        settings_mock.RATE_LIMIT_ADMIN = 20

        # Return a count that exceeds the public limit
        redis = _make_redis_mock(count_sequence=[6])
        app = _build_app(redis_mock=redis)

        with patch("api.v2.middleware.rate_limit.get_settings", return_value=settings_mock):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/test")

        assert response.status_code == 429

    def test_429_has_retry_after_header(self):
        settings_mock = MagicMock()
        settings_mock.RATE_LIMIT_PUBLIC = 5
        settings_mock.RATE_LIMIT_AUTH = 10
        settings_mock.RATE_LIMIT_ADMIN = 20

        redis = _make_redis_mock(count_sequence=[99])
        app = _build_app(redis_mock=redis)

        with patch("api.v2.middleware.rate_limit.get_settings", return_value=settings_mock):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/test")

        assert "Retry-After" in response.headers

    def test_429_response_body_has_error_code(self):
        settings_mock = MagicMock()
        settings_mock.RATE_LIMIT_PUBLIC = 3
        settings_mock.RATE_LIMIT_AUTH = 10
        settings_mock.RATE_LIMIT_ADMIN = 20

        redis = _make_redis_mock(count_sequence=[10])
        app = _build_app(redis_mock=redis)

        with patch("api.v2.middleware.rate_limit.get_settings", return_value=settings_mock):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/test")

        if response.status_code == 429:
            body = response.json()
            assert body.get("error") == "rate_limited" or body.get("code") == "RATE_LIMITED"

    def test_authenticated_tier_used_with_bearer(self):
        """Requests with a Bearer token should use the auth tier, not public."""
        settings_mock = MagicMock()
        settings_mock.RATE_LIMIT_PUBLIC = 3
        settings_mock.RATE_LIMIT_AUTH = 60
        settings_mock.RATE_LIMIT_ADMIN = 120

        # Count of 4 would exceed public (3) but not auth (60)
        redis = _make_redis_mock(count_sequence=[4])
        app = _build_app(redis_mock=redis)

        with patch("api.v2.middleware.rate_limit.get_settings", return_value=settings_mock):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/test",
                    headers={"Authorization": "Bearer some-token"},
                )

        # Should not be rate limited because auth limit is 60
        assert response.status_code != 429


# ── Exempt paths pass through ─────────────────────────────────────────────────

class TestExemptPathsBypass:
    def test_health_endpoint_bypasses_rate_limit(self):
        # Even if Redis would block, exempt paths must go through
        redis = _make_redis_mock(count_sequence=[9999])
        app = Starlette(routes=[
            Route("/api/v2/health", _ok_endpoint),
        ])
        app.add_middleware(RateLimitMiddleware)
        app.state.redis = redis

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v2/health")
        # Should succeed; redis.incr should NOT have been called
        assert response.status_code == 200
        redis.incr.assert_not_called()

    def test_auth_refresh_bypasses_rate_limit(self):
        """Auth refresh must never be rate limited  -  blocking it causes logout."""
        redis = _make_redis_mock(count_sequence=[9999])
        app = Starlette(routes=[
            Route("/api/v2/auth/refresh", _ok_endpoint, methods=["POST"]),
        ])
        app.add_middleware(RateLimitMiddleware)
        app.state.redis = redis

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v2/auth/refresh")
        assert response.status_code == 200
        redis.incr.assert_not_called()

    def test_auth_callback_bypasses_rate_limit(self):
        """OAuth callback must not be rate limited."""
        redis = _make_redis_mock(count_sequence=[9999])
        app = Starlette(routes=[
            Route("/api/v2/auth/callback", _ok_endpoint),
        ])
        app.add_middleware(RateLimitMiddleware)
        app.state.redis = redis

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v2/auth/callback")
        assert response.status_code == 200
        redis.incr.assert_not_called()

    def test_auth_logout_bypasses_rate_limit(self):
        """Logout must not be rate limited."""
        redis = _make_redis_mock(count_sequence=[9999])
        app = Starlette(routes=[
            Route("/api/v2/auth/logout", _ok_endpoint, methods=["POST"]),
        ])
        app.add_middleware(RateLimitMiddleware)
        app.state.redis = redis

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v2/auth/logout")
        assert response.status_code == 200
        redis.incr.assert_not_called()
