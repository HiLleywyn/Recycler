"""
database/database.py  -  PostgreSQL-backed Database for Discoin v2.

Mirrors the interface of database/database.py (SQLite) but connects to
PostgreSQL via asyncpg. Both can coexist during the migration period.

Usage
─────
    from database.database import PgDatabase

    db = PgDatabase(dsn="postgresql://discoin:pw@localhost:5432/discoin")
    await db.connect()
    # db.users, db.markets, db.pools, etc. are all available
    await db.close()

Environment
───────────
    Set DB_BACKEND=postgres to use PostgreSQL in production.
    Set DB_BACKEND=sqlite (default) to keep using SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import os
import contextvars
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from core.database import create_pool
from .users import PgUsersRepo
from .transactions import PgTransactionsRepo
from .markets import PgMarketsRepo
from .pools import PgPoolsRepo
from .validators import PgValidatorsRepo
from .mining import PgMiningRepo
from .contracts import PgContractsRepo
from .guilds import PgGuildsRepo
from .reports import PgReportsRepo
from .nfts import PgNFTsRepo
from .predictions import PgPredictionsRepo
from .snapshots import PgSnapshotsRepo
from .moons import PgMoonsRepo

log = logging.getLogger("discoin.pg_database")

# Path to the v2 PostgreSQL schema file
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

class _TxCtx:
    __slots__ = ("conn", "lock", "active")

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()
        self.active = True


_tx_ctx: contextvars.ContextVar[_TxCtx | None] = contextvars.ContextVar("tx_ctx", default=None)

class _ContextAwarePool:
    """Wrapper around asyncpg.Pool that integrates with ContextVars for atomic blocks.

    If an active transaction exists for the current asyncio task,
    acquire() yields the active connection. Otherwise, it delegates to the real pool.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._real_pool = pool

    def __getattr__(self, name: str) -> Any:
        """Delegate asyncpg.Pool APIs used by diagnostics/other code.

        We only override `acquire()` for ContextVar-aware behavior.
        """
        return getattr(self._real_pool, name)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        tx = _tx_ctx.get()
        # If a child task inherited our contextvars value, `tx.active` will flip
        # to False when the outer atomic() block exits. In that case we must
        # NOT reuse the released connection; fall back to acquiring a fresh
        # connection from the real pool.
        if tx is not None and tx.active:
            async with tx.lock:
                yield tx.conn
        else:
            async with self._real_pool.acquire() as c:
                yield c

    async def close(self) -> None:
        await self._real_pool.close()


class PgDatabase:
    """PostgreSQL-backed database matching the SQLite Database interface."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: _ContextAwarePool | None = None
        self._atomic_depth: int = 0  # Track nested atomic() calls
        self._atomic_lock: asyncio.Lock = asyncio.Lock()  # Prevent concurrent atomic blocks

        # Repos (initialized in connect())
        self.users: PgUsersRepo | None = None
        self.transactions: PgTransactionsRepo | None = None
        self.markets: PgMarketsRepo | None = None
        self.pools: PgPoolsRepo | None = None
        self.validators: PgValidatorsRepo | None = None
        self.mining: PgMiningRepo | None = None
        self.contracts: PgContractsRepo | None = None
        self.guilds: PgGuildsRepo | None = None
        self.reports: PgReportsRepo | None = None
        self.nfts: PgNFTsRepo | None = None
        self.predictions: PgPredictionsRepo | None = None
        self.games = None       # NEW in v2 (not yet implemented)
        self.profiles = None    # NEW in v2 (not yet implemented)
        self.notifications_repo = None  # NEW in v2 (not yet implemented)
        self._repos: list = []

    # ── Method delegation ──────────────────────────────────────────────────
    # Cogs call db.get_price(), db.ensure_user(), db.log_tx(), etc. directly.
    # Delegate any unknown attribute to the repos that define it.

    def __getattr__(self, name: str) -> Any:
        # Avoid infinite recursion for private/dunder attrs or pre-connect access
        if name.startswith("_") or name in (
            "users", "transactions", "markets", "pools", "validators",
            "mining", "contracts", "guilds", "reports", "nfts",
            "predictions", "snapshots", "moons",
            "games", "profiles", "notifications_repo",
        ):
            raise AttributeError(name)
        for repo in self._repos:
            method = getattr(repo, name, None)
            if method is not None:
                return method
        raise AttributeError(
            f"'{type(self).__name__}' has no attribute '{name}' "
            f"(not found in any repo)"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create connection pool and initialize repos."""
        import os
        raw_pool = await create_pool(
            self._dsn,
            min_size=int(os.getenv("DB_POOL_MIN", "10")),
            max_size=int(os.getenv("DB_POOL_MAX", "50")),
        )
        self._pool = _ContextAwarePool(raw_pool)

        # Apply schema if tables don't exist
        await self._ensure_schema()

        # Run incremental migrations (safe to re-run on every startup)
        await self._run_migrations()

        # Deploy / refresh per-unit NFT contract registry. Walks every
        # catalog dict (BAIT, FISH, WEAPONS, etc.) and ensures each entry
        # has an item_contracts row. Idempotent + best-effort -- a single
        # broken catalog entry shouldn't block bot startup.
        try:
            from services.nft_bootstrap import deploy_all_contracts
            summary = await deploy_all_contracts(self)
            log.info("NFT bootstrap: %s", summary)
        except Exception:
            log.exception("NFT bootstrap failed (non-fatal, will retry next boot)")

        # One-shot backfill: mint a per-unit token for every existing
        # inventory row. Checkpointed via nft_backfill_state so reruns
        # only retry kinds that failed previously.
        try:
            from services.nft_backfill import run_backfill
            backfill_summary = await run_backfill(self)
            log.info("NFT backfill: %s", backfill_summary)
        except Exception:
            log.exception("NFT backfill failed (non-fatal, will retry next boot)")

        # Initialize repos
        self.users        = PgUsersRepo(self._pool)
        self.transactions = PgTransactionsRepo(self._pool)
        self.markets      = PgMarketsRepo(self._pool)
        self.pools        = PgPoolsRepo(self._pool)
        self.validators   = PgValidatorsRepo(self._pool)
        self.mining       = PgMiningRepo(self._pool)
        self.contracts    = PgContractsRepo(self._pool)
        self.guilds       = PgGuildsRepo(self._pool)
        self.reports      = PgReportsRepo(self._pool)
        self.nfts         = PgNFTsRepo(self._pool)
        self.predictions  = PgPredictionsRepo(self._pool)
        self.snapshots    = PgSnapshotsRepo(self._pool)
        self.moons        = PgMoonsRepo(self._pool)
        self._repos = [
            self.users, self.transactions, self.markets, self.pools,
            self.validators, self.mining, self.contracts, self.guilds,
            self.reports, self.nfts, self.predictions, self.snapshots,
            self.moons,
        ]
        log.info("PgDatabase connected to %s", self._dsn.split("@")[-1])

    async def _ensure_schema(self) -> None:
        """Apply the v2 schema if the database is empty.

        On a fresh install the full schema.sql is executed, and all existing
        migration files are recorded as already applied so ``_run_migrations``
        does not re-run them.
        """
        async with self._pool.acquire() as conn:
            # Check if the users table exists
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users')"
            )
            if not exists:
                if os.path.exists(_SCHEMA_PATH):
                    # Drop any orphaned tables left by a prior partial init
                    # (e.g. entrypoint hotfix creating crypto_holdings before
                    # schema.sql ran).  CASCADE to handle any dependent objects.
                    orphans = await conn.fetch(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                    for row in orphans:
                        tbl = row["tablename"]
                        log.info("Dropping orphan table %s from partial init", tbl)
                        await conn.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')

                    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
                        schema_sql = f.read()
                    await conn.execute(schema_sql)
                    log.info("Applied v2 schema from %s", _SCHEMA_PATH)

                    # Mark all current migrations as applied since schema.sql
                    # already includes their changes.
                    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
                    if os.path.isdir(migrations_dir):
                        await conn.execute("""
                            CREATE TABLE IF NOT EXISTS schema_migrations (
                                filename   TEXT        PRIMARY KEY,
                                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                            )
                        """)
                        for fname in sorted(os.listdir(migrations_dir)):
                            if fname.endswith(".sql"):
                                await conn.execute(
                                    "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
                                    fname,
                                )
                        log.info("Marked existing migrations as applied (fresh install)")
                else:
                    log.warning("Schema file not found at %s  -  database may be empty", _SCHEMA_PATH)

    async def _run_migrations(self) -> None:
        """Run pending SQL migrations from database/migrations/.

        Migrations are numbered SQL files (e.g. 0001_description.sql) stored in
        the ``database/migrations/`` directory.  A ``schema_migrations`` table
        tracks which files have already been applied.  On each startup only
        unapplied migrations are executed, in order.

        Each migration runs inside its own transaction so a failure won't leave
        the database in a half-migrated state.
        """
        migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
        if not os.path.isdir(migrations_dir):
            log.warning("Migrations directory not found at %s", migrations_dir)
            return

        async with self._pool.acquire() as conn:
            # Ensure the tracking table exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT        PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

            # Determine which migrations have already been applied
            applied = {
                row["filename"]
                for row in await conn.fetch("SELECT filename FROM schema_migrations")
            }

            # Discover and sort migration files
            migration_files = sorted(
                f for f in os.listdir(migrations_dir)
                if f.endswith(".sql")
            )

            for filename in migration_files:
                if filename in applied:
                    continue

                filepath = os.path.join(migrations_dir, filename)
                with open(filepath, "r", encoding="utf-8") as fh:
                    sql = fh.read()

                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)",
                        filename,
                    )
                log.info("Migration applied: %s", filename)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            log.info("PgDatabase pool closed")

    # ── Atomic transactions ─────────────────────────────────────────────────

    @asynccontextmanager
    async def atomic(self) -> AsyncIterator[asyncpg.Connection]:
        """Execute a block of DB operations inside a single PostgreSQL transaction.

        Usage::

            async with db.atomic() as conn:
                await db.update_wallet(...)
                await db.update_holding(...)

        If any exception is raised inside the block the transaction is
        automatically rolled back.  On normal exit the transaction is committed.

        Transactions utilize contextvars to automatically share the connection
        amongst database query methods invoked in the same asyncio task context,
        avoiding race conditions caused by mutating shared repository variables.

        Nested calls are supported: inner calls share the existing connection but
        do not manage the transaction lifecycle, leaving that to the outermost block.
        """
        if self._pool is None:
            raise RuntimeError("PgDatabase not connected")

        # Check if we are already inside a transaction for this task context
        existing_tx = _tx_ctx.get()
        if existing_tx is not None and existing_tx.active:
            # Nested atomic(): reuse the existing connection context.
            yield existing_tx.conn
            return

        # Start a new transaction context
        async with self._pool._real_pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()

            tx = _TxCtx(conn)
            token = _tx_ctx.set(tx)
            
            try:
                yield conn
            except BaseException:
                await tr.rollback()
                raise
            else:
                await tr.commit()
            finally:
                # Mark connection as unusable for any tasks that inherited the
                # tx_ctx value.
                tx.active = False
                _tx_ctx.reset(token)

    # ── Raw pool access (for migration period) ────────────────────────────

    @property
    def pool(self) -> asyncpg.Pool:
        """Direct pool access for queries not yet migrated to repos."""
        if self._pool is None:
            raise RuntimeError("PgDatabase not connected")
        return self._pool._real_pool

    async def fetch_one(self, query: str, *args: Any) -> dict | None:
        """Execute a query and return a single row as a dict."""
        from core.database import _row
        async with self._pool.acquire() as conn:
            return _row(await conn.fetchrow(query, *args))

    async def fetch_all(self, query: str, *args: Any) -> list[dict]:
        """Execute a query and return all rows as dicts."""
        from core.database import _rows
        async with self._pool.acquire() as conn:
            return _rows(await conn.fetch(query, *args))

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a query and return the status string."""
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def get_bot_config(self, key: str) -> str | None:
        """Get a bot-wide config value by key. Returns None if not set."""
        return await self.fetch_val(
            "SELECT value FROM bot_config WHERE key = $1", key
        )

    async def set_bot_config(self, key: str, value: str) -> None:
        """Upsert a bot-wide config value."""
        await self.execute(
            "INSERT INTO bot_config (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            key, value,
        )

    async def fetch_val(self, query: str, *args: Any) -> Any:
        """Execute a query and return first column of first row."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    # ── Compatibility shims ───────────────────────────────────────────────
    # These methods provide backwards compatibility with code that calls
    # db.get_guild_settings(guild_id) etc. directly on the Database object.

    async def get_guild_settings(self, guild_id: int) -> dict:
        """Get guild settings (compat shim  -  delegates to guilds repo)."""
        if self.guilds:
            return await self.guilds.get_guild_settings(guild_id)
        # Fallback if repos not initialized
        row = await self.fetch_one(
            "SELECT * FROM guild_settings WHERE guild_id = $1",
            guild_id,
        )
        if row is None:
            await self.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                guild_id,
            )
            row = await self.fetch_one(
                "SELECT * FROM guild_settings WHERE guild_id = $1",
                guild_id,
            )
        return row or {}

    async def seed_pools(self, guild_id: int) -> None:
        """Seed default AMM pools for a guild (compat shim  -  delegates to pools repo)."""
        if self.pools:
            await self.pools.seed_pools(guild_id)
        else:
            log.debug("seed_pools called for guild %d  -  pools repo not initialized", guild_id)

    async def deposit_to_bank(
        self, user_id: int, guild_id: int, amount: int
    ) -> tuple[int, int]:
        """Atomic wallet -> bank transfer. ``amount`` is a raw scaled int."""
        if self.users is None:
            raise RuntimeError("users repo not initialized")
        return await self.users.deposit_to_bank(user_id, guild_id, amount)

    async def withdraw_from_bank(
        self, user_id: int, guild_id: int, amount: int
    ) -> tuple[int, int]:
        """Atomic bank -> wallet transfer. ``amount`` is a raw scaled int."""
        if self.users is None:
            raise RuntimeError("users repo not initialized")
        return await self.users.withdraw_from_bank(user_id, guild_id, amount)

    async def transfer_wallet(
        self,
        guild_id: int,
        sender_id: int,
        recipient_id: int,
        amount: int,
    ) -> str:
        """Atomic wallet-to-wallet USD transfer.

        ``amount`` is a raw scaled integer (``to_raw(human)``) matching the
        wallet column storage. Passing a human float will be rejected by
        the downstream ``require_raw`` / ``_sanitize_amount`` guards.
        """
        if self.users is None or self.transactions is None:
            raise RuntimeError("database repos not initialized")
        if amount <= 0:
            raise ValueError("Transfer amount must be positive.")

        await self.users.ensure_user(sender_id, guild_id)
        await self.users.ensure_user(recipient_id, guild_id)
        async with self.atomic():
            await self.users.update_wallet(sender_id, guild_id, -amount)
            await self.users.update_wallet(recipient_id, guild_id, amount)
            return await self.transactions.log_tx(
                guild_id,
                sender_id,
                "TRANSFER",
                "USD",
                amount,
                "USD",
                amount,
            )


def get_database(dsn: str | None = None) -> PgDatabase:
    """Factory function to create a PgDatabase with env-based DSN."""
    if dsn is None:
        dsn = os.getenv(
            "DATABASE_URL",
            "postgresql://discoin:discoin_dev@localhost:5432/discoin",
        )
    return PgDatabase(dsn)
