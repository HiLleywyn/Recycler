"""Tests for api/v2/auth/jwt.py  -  JWT creation, decoding, and security."""
from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import jwt as pyjwt
import pytest

# Patch settings before importing to avoid reading a real .env file
_mock_settings = MagicMock()
_mock_settings.JWT_SECRET = "super-secret-test-key-for-discoin-unit-tests-padding"  # >=48 bytes
_mock_settings.JWT_EXPIRE_SECONDS = 900  # 15 minutes


def _get_settings_mock():
    return _mock_settings


with patch("api.v2.config.get_settings", _get_settings_mock):
    from api.v2.auth.jwt import (
        create_access_token,
        create_partial_token,
        create_tfa_pending_token,
        decode_token,
        generate_refresh_token,
    )

# ── Shared helpers ─────────────────────────────────────────────────────────────

SECRET = _mock_settings.JWT_SECRET


def _decode_raw(token: str) -> dict:
    """Decode without verification to inspect payload."""
    return pyjwt.decode(token, SECRET, algorithms=["HS256"])


# ── create_access_token ────────────────────────────────────────────────────────

class TestCreateAccessToken:
    def test_returns_string(self):
        token = create_access_token("1", "2", "alice", None)
        assert isinstance(token, str)

    def test_payload_subject(self):
        token = create_access_token("999", "888", "bob", "avatar_url")
        payload = _decode_raw(token)
        assert payload["sub"] == "999"

    def test_payload_guild_id(self):
        token = create_access_token("1", "42", "alice", None)
        payload = _decode_raw(token)
        assert payload["guild_id"] == "42"

    def test_payload_username(self):
        token = create_access_token("1", "2", "charlie", None)
        payload = _decode_raw(token)
        assert payload["username"] == "charlie"

    def test_payload_avatar(self):
        token = create_access_token("1", "2", "alice", "https://cdn.example.com/avatar.png")
        payload = _decode_raw(token)
        assert payload["avatar"] == "https://cdn.example.com/avatar.png"

    def test_is_admin_false_by_default(self):
        token = create_access_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        assert payload["is_admin"] is False

    def test_is_admin_true(self):
        token = create_access_token("1", "2", "admin", None, is_admin=True)
        payload = _decode_raw(token)
        assert payload["is_admin"] is True

    def test_exp_in_future(self):
        token = create_access_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        assert payload["exp"] > int(time.time())

    def test_iat_is_recent(self):
        before = int(time.time())
        token = create_access_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        assert payload["iat"] >= before - 2

    def test_jti_present_and_unique(self):
        t1 = create_access_token("1", "2", "alice", None)
        t2 = create_access_token("1", "2", "alice", None)
        jti1 = _decode_raw(t1)["jti"]
        jti2 = _decode_raw(t2)["jti"]
        assert jti1 != jti2

    def test_avatar_none_allowed(self):
        token = create_access_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        assert payload["avatar"] is None


# ── create_partial_token ──────────────────────────────────────────────────────

class TestCreatePartialToken:
    def test_partial_flag_true(self):
        token = create_partial_token("1", "alice", None)
        payload = _decode_raw(token)
        assert payload.get("partial") is True

    def test_no_guild_id(self):
        token = create_partial_token("1", "alice", None)
        payload = _decode_raw(token)
        assert "guild_id" not in payload

    def test_expires_within_5_minutes(self):
        token = create_partial_token("1", "alice", None)
        payload = _decode_raw(token)
        remaining = payload["exp"] - int(time.time())
        assert remaining <= 300
        assert remaining > 0

    def test_subject_set(self):
        token = create_partial_token("777", "alice", None)
        payload = _decode_raw(token)
        assert payload["sub"] == "777"


# ── create_tfa_pending_token ──────────────────────────────────────────────────

class TestCreateTfaPendingToken:
    def test_tfa_pending_flag_true(self):
        token = create_tfa_pending_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        assert payload.get("tfa_pending") is True

    def test_guild_id_present(self):
        token = create_tfa_pending_token("1", "42", "alice", None)
        payload = _decode_raw(token)
        assert payload["guild_id"] == "42"

    def test_expires_within_5_minutes(self):
        token = create_tfa_pending_token("1", "2", "alice", None)
        payload = _decode_raw(token)
        remaining = payload["exp"] - int(time.time())
        assert remaining <= 300
        assert remaining > 0


# ── decode_token ──────────────────────────────────────────────────────────────

class TestDecodeToken:
    def test_round_trip(self):
        token = create_access_token("123", "456", "dave", None)
        payload = decode_token(token)
        assert payload["sub"] == "123"
        assert payload["guild_id"] == "456"

    def test_expired_token_raises(self):
        now = int(time.time())
        payload = {"sub": "1", "exp": now - 60, "iat": now - 120}
        token = pyjwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(pyjwt.ExpiredSignatureError):
            decode_token(token)

    def test_wrong_secret_raises(self):
        token = pyjwt.encode({"sub": "1", "exp": int(time.time()) + 300}, "wrong-secret-key-padded-to-32-bytes", algorithm="HS256")
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token(token)

    def test_tampered_token_raises(self):
        token = create_access_token("1", "2", "alice", None)
        tampered = token[:-4] + "XXXX"
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token(tampered)

    def test_empty_token_raises(self):
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token("")

    def test_random_string_raises(self):
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token("not.a.jwt")

    def test_none_algorithm_rejected(self):
        """Token signed with 'none' algorithm must be rejected."""
        payload = {"sub": "1", "exp": int(time.time()) + 300}
        token = pyjwt.encode(payload, "", algorithm="none")
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token(token)


# ── generate_refresh_token ────────────────────────────────────────────────────

class TestGenerateRefreshToken:
    def test_returns_two_strings(self):
        raw, hashed = generate_refresh_token()
        assert isinstance(raw, str)
        assert isinstance(hashed, str)

    def test_raw_and_hash_differ(self):
        raw, hashed = generate_refresh_token()
        assert raw != hashed

    def test_hash_is_sha256_hex(self):
        import hashlib
        raw, hashed = generate_refresh_token()
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert hashed == expected

    def test_unique_each_call(self):
        tokens = {generate_refresh_token()[0] for _ in range(20)}
        assert len(tokens) == 20

    def test_raw_token_length(self):
        raw, _ = generate_refresh_token()
        # secrets.token_hex(32) => 64 hex characters
        assert len(raw) == 64

    def test_hash_length(self):
        _, hashed = generate_refresh_token()
        assert len(hashed) == 64  # SHA-256 hex digest


# ── Security edge-cases ───────────────────────────────────────────────────────

class TestJWTSecurity:
    def test_is_admin_not_elevatable(self):
        """A non-admin token's is_admin flag cannot be changed client-side."""
        token = create_access_token("1", "2", "alice", None, is_admin=False)
        # Manually craft a forged payload that claims admin
        parts = token.split(".")
        import base64, json
        forged_payload = {"sub": "1", "guild_id": "2", "username": "alice",
                          "avatar": None, "is_admin": True,
                          "iat": int(time.time()), "exp": int(time.time()) + 900,
                          "jti": "fake"}
        b64 = base64.urlsafe_b64encode(
            json.dumps(forged_payload).encode()
        ).rstrip(b"=").decode()
        forged_token = f"{parts[0]}.{b64}.{parts[2]}"
        with pytest.raises(pyjwt.InvalidSignatureError):
            decode_token(forged_token)

    def test_hs256_algorithm_required(self):
        """Tokens signed with RS256 or other algorithms are rejected."""
        # PyJWT won't encode with RS256 without an RSA key; just confirm
        # the decode function only accepts HS256 by passing an unexpected algo.
        payload = {"sub": "1", "exp": int(time.time()) + 300}
        # Sign with HS384 but same key  -  should still be rejected by decode_token
        token = pyjwt.encode(payload, SECRET, algorithm="HS384")
        with pytest.raises(pyjwt.InvalidAlgorithmError):
            decode_token(token)
