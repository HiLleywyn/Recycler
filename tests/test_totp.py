"""Tests for core/framework/totp.py  -  TOTP 2FA implementation."""
from __future__ import annotations

import base64


from core.framework.totp import (
    generate_secret,
    otpauth_uri,
    totp_code,
    verify_totp,
)


# ── generate_secret ────────────────────────────────────────────────────────────

class TestGenerateSecret:
    def test_returns_string(self):
        assert isinstance(generate_secret(), str)

    def test_unique_each_call(self):
        secrets = {generate_secret() for _ in range(20)}
        assert len(secrets) == 20, "Expected unique secrets on every call"

    def test_valid_base32(self):
        secret = generate_secret()
        padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
        # Should not raise
        decoded = base64.b32decode(padded)
        assert len(decoded) == 20, "Decoded secret should be 20 bytes"

    def test_no_padding_chars(self):
        """The returned secret must have trailing '=' stripped."""
        secret = generate_secret()
        assert not secret.endswith("="), "generate_secret should strip trailing '='"

    def test_minimum_length(self):
        """RFC 4226 recommends at least 16 base32 chars for a 10-byte key."""
        assert len(generate_secret()) >= 16


# ── totp_code ─────────────────────────────────────────────────────────────────

class TestTotpCode:
    def test_returns_six_digits(self):
        secret = generate_secret()
        code = totp_code(secret)
        assert len(code) == 6
        assert code.isdigit()

    def test_zero_padded(self):
        """All codes must be exactly 6 characters, zero-padded if necessary."""
        secret = generate_secret()
        for _ in range(50):
            code = totp_code(secret)
            assert len(code) == 6

    def test_deterministic_for_same_time(self):
        secret = generate_secret()
        c1 = totp_code(secret)
        c2 = totp_code(secret)
        assert c1 == c2, "Same secret at same time step must produce same code"

    def test_different_secrets_differ(self):
        # Two random 6-digit codes collide ~1 in a million, so sample enough
        # secrets that all-identical would be statistically impossible
        # (~10**-54 with 10 samples).
        codes = {totp_code(generate_secret()) for _ in range(10)}
        assert len(codes) > 1

    def test_offset_minus_one_differs(self):
        secret = generate_secret()
        current = totp_code(secret, offset=0)
        prev = totp_code(secret, offset=-1)
        assert current != prev

    def test_custom_digits(self):
        secret = generate_secret()
        code = totp_code(secret, digits=8)
        assert len(code) == 8
        assert code.isdigit()

    def test_uppercase_secret(self):
        secret = generate_secret()
        assert totp_code(secret.lower()) == totp_code(secret.upper())


# ── verify_totp ───────────────────────────────────────────────────────────────

class TestVerifyTotp:
    def test_current_code_accepted(self):
        secret = generate_secret()
        code = totp_code(secret)
        assert verify_totp(secret, code)

    def test_wrong_code_rejected(self):
        secret = generate_secret()
        wrong = "000000"
        # Generate a code that definitely differs
        correct = totp_code(secret)
        if wrong == correct:
            wrong = "111111"
        assert not verify_totp(secret, wrong)

    def test_previous_window_accepted_within_window_1(self):
        secret = generate_secret()
        prev_code = totp_code(secret, offset=-1)
        assert verify_totp(secret, prev_code, window=1)

    def test_next_window_accepted_within_window_1(self):
        secret = generate_secret()
        next_code = totp_code(secret, offset=1)
        assert verify_totp(secret, next_code, window=1)

    def test_far_future_code_rejected(self):
        secret = generate_secret()
        far_code = totp_code(secret, offset=10)
        assert not verify_totp(secret, far_code, window=1)

    def test_empty_code_rejected(self):
        secret = generate_secret()
        assert not verify_totp(secret, "")

    def test_whitespace_stripped(self):
        secret = generate_secret()
        code = totp_code(secret)
        assert verify_totp(secret, f"  {code}  ")

    def test_short_code_rejected(self):
        secret = generate_secret()
        assert not verify_totp(secret, "123")

    def test_window_zero_strict(self):
        secret = generate_secret()
        prev = totp_code(secret, offset=-1)
        assert not verify_totp(secret, prev, window=0)

    def test_different_secret_rejected(self):
        s1 = generate_secret()
        s2 = generate_secret()
        code = totp_code(s1)
        assert not verify_totp(s2, code)


# ── otpauth_uri ───────────────────────────────────────────────────────────────

class TestOtpauthUri:
    def test_starts_with_otpauth(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "testuser")
        assert uri.startswith("otpauth://totp/")

    def test_contains_secret(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "testuser")
        assert f"secret={secret}" in uri

    def test_contains_issuer(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "testuser")
        assert "issuer=Discoin" in uri

    def test_contains_digits_6(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "testuser")
        assert "digits=6" in uri

    def test_contains_period_30(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "testuser")
        assert "period=30" in uri

    def test_username_encoded(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "user with spaces")
        # Spaces in the label should be percent-encoded
        assert " " not in uri

    def test_label_contains_discoin_prefix(self):
        secret = generate_secret()
        uri = otpauth_uri(secret, "alice")
        assert "Discoin" in uri
