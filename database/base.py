"""
database/base.py  -  Base repository for PostgreSQL-backed repos.

Mirrors the interface of database/base.py (SQLite) but uses an asyncpg
connection pool. Repos can inherit from PgBaseRepo instead of BaseRepo
to use PostgreSQL.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from core.database import _row, _rows


class PgBaseRepo:
    """Base repository backed by an asyncpg connection pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    # ── Query helpers ─────────────────────────────────────────────────────

    async def fetch_one(self, query: str, *args: Any) -> dict | None:
        """Execute a query and return a single row as a dict, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return _row(row)

    async def fetch_all(self, query: str, *args: Any) -> list[dict]:
        """Execute a query and return all rows as a list of dicts."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return _rows(rows)

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a query and return the status string (e.g. 'INSERT 0 1')."""
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def execute_many(self, query: str, args_list: list[tuple]) -> None:
        """Execute a query with multiple parameter sets."""
        async with self._pool.acquire() as conn:
            await conn.executemany(query, args_list)

    async def fetch_val(self, query: str, *args: Any) -> Any:
        """Execute a query and return the first column of the first row."""
        from core.database import _coerce
        async with self._pool.acquire() as conn:
            return _coerce(await conn.fetchval(query, *args))

    # ── Transaction helper ────────────────────────────────────────────────

    @asynccontextmanager
    async def transaction(self):
        """Acquire a connection and start a transaction.

        Usage::

            async with repo.transaction() as conn:
                await conn.execute("INSERT ...")
                await conn.execute("UPDATE ...")

        All statements inside the block run in a single transaction.
        On success the transaction is committed; on exception it is rolled back.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ── Status parsing helper ─────────────────────────────────────────────

    @staticmethod
    def _row_count(status: str) -> int:
        """Extract the affected row count from an asyncpg status string.

        Examples: 'UPDATE 3' → 3, 'DELETE 0' → 0, 'INSERT 0 1' → 1.
        """
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0
