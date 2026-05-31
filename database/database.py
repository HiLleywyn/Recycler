"""Recycler's own slim data layer -- clanker tables only, nothing economy.

Recycler is the standalone ,clanker containment bot, so it must NOT carry
Disco's economy schema. It ships this top-level ``database`` package, which the
framework picks up via ``from database import Database`` (see
``FrameworkBot._resolve_db_factory``) instead of the economy data plane bundled
with bot-framework.

The schema is exactly: the small set of framework-runtime tables (``schema.sql``)
plus the clanker tables, applied verbatim from the same migrations the feature
was built with (``migrations/0285``-``0295``) so behaviour is byte-for-byte
identical to the all-in-one build.

Only the methods the framework runtime and the clanktank cog actually call are
implemented. The generic asyncpg plumbing + row coercion is reused from
``core.database`` so result types (epoch-float timestamps, int/float NUMERIC)
match the rest of the framework exactly.
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from core.database import create_pool, _row, _rows

log = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
# Settings columns are interpolated into SQL by update_guild_setting, so the
# name is validated to a strict identifier to keep that injection-safe.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _parse_set(value: Any) -> set[str]:
    """Split a comma-separated settings string into a set of trimmed tokens."""
    if not value:
        return set()
    return {p.strip() for p in str(value).split(",") if p.strip()}


class _GuildsRepo:
    """The slice of the economy ``PgGuildsRepo`` the framework runtime calls.

    Behaviour matches bot-framework's guilds repo for these methods so prefix
    routing, the bot-channel gate and the global role check work identically.
    """

    def __init__(self, db: "Database") -> None:
        self._db = db

    async def get_guild_settings(self, guild_id: int) -> dict:
        return await self._db.get_guild_settings(guild_id)

    async def update_guild_setting(self, guild_id: int, column: str, value: Any) -> None:
        await self._db.update_guild_setting(guild_id, column, value)

    async def get_bot_channels(self, guild_id: int) -> list[int]:
        s = await self._db.get_guild_settings(guild_id)
        return [int(c) for c in _parse_set(s.get("bot_channels", "")) if c.isdigit()]

    async def get_command_allowed_roles(self, guild_id: int, command_name: str) -> list[int]:
        rows = await self._db.fetch_all(
            "SELECT role_id FROM guild_command_roles WHERE guild_id=$1 AND command_name=$2",
            guild_id, command_name,
        )
        return [r["role_id"] for r in rows]

    async def is_network_halted(self, guild_id: int, network: str) -> bool:
        s = await self._db.get_guild_settings(guild_id)
        return network.lower() in _parse_set(s.get("halted_networks", ""))

    async def get_fee_config(self, guild_id: int) -> dict:
        # Recycler has no economy; return empty so any incidental caller falls
        # back to its own defaults.
        return {}


class Database:
    """Slim asyncpg data layer for Recycler (clanker containment only)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self.guilds = _GuildsRepo(self)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        self._pool = await create_pool(
            self._dsn,
            min_size=int(os.getenv("DB_POOL_MIN", "2")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
        )
        await self._ensure_schema()
        await self._run_migrations()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def atomic(self) -> AsyncIterator[asyncpg.Connection]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ── schema / migrations ──────────────────────────────────────────────────
    async def _ensure_schema(self) -> None:
        with open(os.path.join(_HERE, "schema.sql"), encoding="utf-8") as f:
            schema_sql = f.read()
        async with self._pool.acquire() as conn:
            await conn.execute(schema_sql)
        log.info("Applied Recycler slim schema")

    async def _run_migrations(self) -> None:
        migrations_dir = os.path.join(_HERE, "migrations")
        if not os.path.isdir(migrations_dir):
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename TEXT PRIMARY KEY,"
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            applied = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            pending = sorted(
                f for f in os.listdir(migrations_dir)
                if f.endswith(".sql") and f not in applied
            )
            for fname in pending:
                with open(os.path.join(migrations_dir, fname), encoding="utf-8") as f:
                    sql = f.read()
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)", fname
                    )
                log.info("Applied migration %s", fname)
            if pending:
                log.info("Applied %d clanker migration(s)", len(pending))

    # ── generic query helpers (match core.database coercion) ──────────────────
    async def fetch_one(self, query: str, *args: Any) -> dict | None:
        async with self._pool.acquire() as conn:
            return _row(await conn.fetchrow(query, *args))

    async def fetch_all(self, query: str, *args: Any) -> list[dict]:
        async with self._pool.acquire() as conn:
            return _rows(await conn.fetch(query, *args))

    async def fetch_val(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def execute_many(self, query: str, args_iter: Any) -> None:
        async with self._pool.acquire() as conn:
            await conn.executemany(query, args_iter)

    # ── guild settings ───────────────────────────────────────────────────────
    async def get_guild_settings(self, guild_id: int) -> dict:
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )
        row = await self.fetch_one(
            "SELECT * FROM guild_settings WHERE guild_id=$1", guild_id
        )
        return row or {}

    async def update_guild_setting(self, guild_id: int, column: str, value: Any) -> None:
        if not _IDENT_RE.match(column):
            raise ValueError(f"Invalid settings column: {column!r}")
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )
        await self.execute(
            f"UPDATE guild_settings SET {column}=$1 WHERE guild_id=$2", value, guild_id
        )

    # ── economy no-ops (gated off for minimal bots; kept defensively) ─────────
    async def seed_pools(self, guild_id: int) -> None:
        return None
