"""core/framework/totp.py  -  TOTP (RFC 6238) helpers, pure stdlib.

Shared between the REST API auth layer and the Discord bot 2FA cog.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote


def generate_secret() -> str:
    """Generate a random 20-byte secret, return as base32 string."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret_b32: str, time_step: int = 30, digits: int = 6, offset: int = 0) -> str:
    """Compute TOTP code for the given time offset (0 = now, -1 = prev window, etc.)."""
    padded = secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(padded)
    counter = int(time.time()) // time_step + offset
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    """Verify a TOTP code, allowing +-window time steps for clock drift."""
    found = False
    for offset in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret_b32, offset=offset), code.strip()):
            found = True
    return found


def otpauth_uri(secret_b32: str, username: str) -> str:
    """Build otpauth:// URI for QR code scanning."""
    label = quote(f"Discoin:{username}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer=Discoin&digits=6&period=30"
