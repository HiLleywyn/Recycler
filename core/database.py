"""
core/database.py  -  asyncpg connection pool helper for Discoin v2.

Backed by a PostgreSQL connection pool; provides the PgRow result
wrapper and the epoch-coercion helpers used across the data layer.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from datetime import datetime as _dt
from typing import Any

from core.framework.scale import to_human as _to_human

import ssl as _ssl

import asyncpg

log = logging.getLogger("discoin.pg")


def _needs_ssl(dsn: str) -> bool:
    """Return True if the DSN points to a remote host that likely needs SSL.

    Railway-managed Postgres (both internal .railway.internal and public
    proxy URLs) supports SSL.  Localhost / 127.0.0.1 never needs it.
    """
    lower = dsn.lower()
    if "@localhost" in lower or "@127.0.0.1" in lower:
        return False
    # If the user explicitly set sslmode=disable, respect it
    if "sslmode=disable" in lower:
        return False
    return True


def _skip_cert_verify(dsn: str) -> bool:
    """Return True if SSL cert verification should be skipped for this DSN.

    Railway postgres-ssl (both the internal .railway.internal hostname and
    the public .proxy.rlwy.net proxy) use self-signed certificates that
    won't pass standard CA verification.  Skip verification for both.
    Explicitly verified DSNs can opt out by setting DB_SSL_VERIFY=1.
    """
    import os
    if os.getenv("DB_SSL_VERIFY", "0") == "1":
        return False
    lower = dsn.lower()
    # Railway internal and proxy hostnames always use self-signed certs
    if ".railway.internal" in lower or ".rlwy.net" in lower:
        return True
    # Any URL with sslmode=require but no explicit CA cert path
    if "sslmode=require" in lower:
        return True
    return False


async def create_pool(
    dsn: str,
    *,
    min_size: int = 3,
    max_size: int = 10,
) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    Automatically enables SSL for non-localhost connections (Railway, etc.)
    and skips cert verification for Railway postgres-ssl self-signed certs.
    Set DB_SSL_VERIFY=1 to force full certificate verification.
    """
    ssl_ctx = None
    if _needs_ssl(dsn):
        ssl_ctx = _ssl.create_default_context()
        if _skip_cert_verify(dsn):
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE

    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
        ssl=ssl_ctx,
    )
    log.info(
        "PostgreSQL pool created (min=%d, max=%d, ssl=%s)",
        min_size, max_size, "on" if ssl_ctx else "off",
    )
    return pool


class PgRow(dict):
    """Dict-like row that supports both attribute and key access.

    Makes asyncpg Record objects compatible with code that uses
    dict-style access (row["key"])  -  matching aiosqlite.Row behavior.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def h(self, col: str, default: int = 0) -> float:
        """Return a raw NUMERIC(36,0) column as a human-readable float.

        Equivalent to ``to_human(int(row.get(col, 0) or 0))``.
        Use this in display/leaderboard code instead of repeating the
        to_human(int(...)) dance for every monetary field.
        """
        v = self.get(col, default)
        return _to_human(int(v) if v is not None else default)


def _coerce(value: Any) -> Any:
    """Convert Decimal and datetime to Python-native types.

    NUMERIC(36,0) raw-integer columns (exponent >= 0) are returned as Python int
    so that 10^18-scale arithmetic stays exact.  NUMERIC with a fractional part
    (prices, rates, etc.) are returned as float as before.
    datetime columns are returned as epoch float for fmt_ts() compatibility.
    """
    if isinstance(value, Decimal):
        t = value.as_tuple()
        if t.exponent >= 0:
            return int(value)
        return float(value)
    if isinstance(value, _dt):
        return value.timestamp()
    return value


def _row(record: asyncpg.Record | None) -> dict | None:
    """Convert an asyncpg Record to a dict, or return None."""
    if record is None:
        return None
    return PgRow({k: _coerce(v) for k, v in record.items()})


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    """Convert a list of asyncpg Records to a list of dicts."""
    return [PgRow({k: _coerce(v) for k, v in r.items()}) for r in records]
