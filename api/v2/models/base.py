from __future__ import annotations

from typing import Any

import asyncpg


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    Args:
        dsn: PostgreSQL DSN (e.g. ``postgresql://user:pass@host:port/db``).
        min_size: Minimum number of connections to keep open.
        max_size: Maximum number of connections in the pool.
    """
    return await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)


async def fetch_one(
    pool: asyncpg.Pool,
    query: str,
    *args: Any,
) -> asyncpg.Record | None:
    """Execute a query and return the first row, or ``None``."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(
    pool: asyncpg.Pool,
    query: str,
    *args: Any,
) -> list[asyncpg.Record]:
    """Execute a query and return all result rows."""
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(
    pool: asyncpg.Pool,
    query: str,
    *args: Any,
) -> str:
    """Execute a query and return the status string (e.g. ``INSERT 0 1``)."""
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
