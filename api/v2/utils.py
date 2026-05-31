"""Shared utilities for the Discoin v2 API."""
from __future__ import annotations

from datetime import datetime, timezone


def to_iso(val) -> str | None:
    """Convert a database timestamp value to an ISO 8601 string.

    Handles all formats the application may encounter:
      - ``datetime`` objects (PostgreSQL via asyncpg)
      - ``int`` / ``float`` unix-epoch seconds (SQLite)
      - ``str`` pass-through (already formatted)
      - ``None`` → ``None``
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
    if isinstance(val, str):
        return val
    return str(val)
