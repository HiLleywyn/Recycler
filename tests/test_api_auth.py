"""Tests for api/v2/dependencies.py  -  JWT dependency injection and FastAPI auth."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from fastapi import FastAPI

# We test the get_current_user dependency behavior by building a minimal FastAPI app.

SECRET = "test-jwt-secret-dependencies-padded"  # >=32 bytes

# Patch settings before importing
_mock_settings = MagicMock()
_mock_settings.JWT_SECRET = SECRET
_mock_settings.JWT_EXPIRE_SECONDS = 900


def _get_settings_mock():
    return _mock_settings


def _make_token(
    user_id: str = "1",
    guild_id: str = "2",
    is_admin: bool = False,
    is_owner: bool = False,
    partial: bool = False,
    tfa_pending: bool = False,
    expired: bool = False,
) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "guild_id": guild_id,
        "username": "testuser",
        "avatar": None,
        "is_admin": is_admin,
        "is_owner": is_owner,
        "iat": now,
        "exp": now - 60 if expired else now + 900,
        "jti": "test-jti",
    }
    if partial:
        payload["partial"] = True
        payload.pop("guild_id", None)
    if tfa_pending:
        payload["tfa_pending"] = True
    return pyjwt.encode(payload, SECRET, algorithm="HS256")


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app that exercises the get_current_user dependency."""
    app = FastAPI()

    with patch("api.v2.dependencies.get_settings", _get_settings_mock):

        @app.get("/protected")
        async def protected_endpoint(user=None):
            return {"user_id": "anonymous"}

        # Build a new endpoint that uses the dependency properly
        @app.get("/secured")
        async def secured_endpoint(
            authorization: str | None = None,
        ):
            return {"status": "ok"}

    return app


# We test get_current_user in isolation by calling it directly.

class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_valid_token_returns_payload(self):
        with patch("api.v2.dependencies.get_settings", _get_settings_mock):
            pass

        token = _make_token()
        mock_request = MagicMock()

        with patch("api.v2.dependencies.get_settings", _get_settings_mock):
            import api.v2.dependencies as dep_mod
            dep_mod.get_settings = _get_settings_mock
            payload = await dep_mod.get_current_user(mock_request, f"Bearer {token}")
        assert payload["user_id"] == "1"
        assert payload["guild_id"] == "2"

    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, None)

    @pytest.mark.asyncio
    async def test_malformed_header_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, "Token notbearer")

    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        token = _make_token(expired=True)
        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, f"Bearer {token}")

    @pytest.mark.asyncio
    async def test_wrong_secret_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        token = pyjwt.encode(
            {"sub": "1", "exp": int(time.time()) + 300},
            "wrong-secret-key-padded-to-32-bytes",
            algorithm="HS256",
        )
        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, f"Bearer {token}")

    @pytest.mark.asyncio
    async def test_partial_token_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        token = _make_token(partial=True)
        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, f"Bearer {token}")

    @pytest.mark.asyncio
    async def test_tfa_pending_token_raises_401(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import UnauthorizedError
        mock_request = MagicMock()

        token = _make_token(tfa_pending=True)
        with pytest.raises(UnauthorizedError):
            await dep_mod.get_current_user(mock_request, f"Bearer {token}")

    @pytest.mark.asyncio
    async def test_admin_flag_preserved(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        mock_request = MagicMock()
        token = _make_token(is_admin=True)
        payload = await dep_mod.get_current_user(mock_request, f"Bearer {token}")
        assert payload["is_admin"] is True


# ── require_admin dependency ──────────────────────────────────────────────────
# require_admin re-verifies admin status against the DB on every request.
# is_owner takes the fast path (no DB query); is_admin queries admin_users.

class _MockConn:
    """Minimal asyncpg connection mock for require_admin tests."""
    def __init__(self, is_admin_in_db: bool = False):
        self._is_admin = is_admin_in_db

    async def fetchrow(self, query, *args):
        return {"1": 1} if self._is_admin else None


class TestRequireAdmin:
    @pytest.mark.asyncio
    async def test_non_admin_raises_403(self):
        import api.v2.dependencies as dep_mod
        dep_mod.get_settings = _get_settings_mock

        from api.v2.exceptions import ForbiddenError

        non_admin_payload = {
            "user_id": "1", "guild_id": "2", "is_admin": False, "is_owner": False,
            "username": "alice", "avatar": None,
        }
        with pytest.raises(ForbiddenError):
            await dep_mod.require_admin(non_admin_payload, _MockConn(is_admin_in_db=False))

    @pytest.mark.asyncio
    async def test_admin_returns_payload(self):
        import api.v2.dependencies as dep_mod
        admin_payload = {
            "user_id": "1", "guild_id": "2", "is_admin": True, "is_owner": False,
            "username": "admin", "avatar": None,
        }
        result = await dep_mod.require_admin(admin_payload, _MockConn(is_admin_in_db=True))
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    async def test_owner_returns_payload(self):
        import api.v2.dependencies as dep_mod
        owner_payload = {
            "user_id": "1", "guild_id": "2", "is_admin": False, "is_owner": True,
            "username": "owner", "avatar": None,
        }
        # is_owner takes early return  -  conn is never queried
        result = await dep_mod.require_admin(owner_payload, _MockConn(is_admin_in_db=False))
        assert result["is_owner"] is True

    @pytest.mark.asyncio
    async def test_bot_manager_elevated_at_login_passes(self):
        """Bot manager is elevated to is_owner+is_admin at login time."""
        import api.v2.dependencies as dep_mod

        elevated_payload = {
            "user_id": "42", "guild_id": "10", "is_admin": True, "is_owner": True,
            "username": "botmgr", "avatar": None,
        }
        result = await dep_mod.require_admin(elevated_payload, _MockConn(is_admin_in_db=True))
        assert result["is_admin"] is True
        assert result["is_owner"] is True
