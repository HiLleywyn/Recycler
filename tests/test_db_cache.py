"""Tests for the PgDatabase per-guild settings cache (hot-path latency fix)."""
from __future__ import annotations

import asyncio

import pytest

# database.database imports the framework's core.database; skip on the
# dependency-light CI job.
pytest.importorskip("core.database")

from database.database import PgDatabase  # noqa: E402


class _CountingDB(PgDatabase):
    """A PgDatabase that counts SELECTs and serves rows from memory."""

    def __init__(self):
        # Skip the real __init__'s DSN/pool; we only exercise the cache + helpers.
        self._repos = {}
        self._gs_cache = {}
        self._gs_ttl = 15.0
        self.selects = 0
        self._row = {"guild_id": 1, "prefix": "."}

    async def fetch_one(self, query, *args):
        if query.strip().upper().startswith("SELECT"):
            self.selects += 1
        return dict(self._row)

    async def execute(self, query, *args):
        return "OK"


def test_settings_are_cached_between_reads():
    db = _CountingDB()
    asyncio.run(_read_twice(db))
    # Two reads, one DB SELECT thanks to the cache.
    assert db.selects == 1


async def _read_twice(db):
    a = await db.get_guild_settings(1)
    b = await db.get_guild_settings(1)
    assert a["prefix"] == "."
    assert b["prefix"] == "."


def test_write_invalidates_cache():
    db = _CountingDB()
    asyncio.run(_read_write_read(db))
    # read -> write (invalidates) -> read = two SELECTs.
    assert db.selects == 2


async def _read_write_read(db):
    await db.get_guild_settings(1)
    await db.update_guild_setting(1, "prefix", "!")
    await db.get_guild_settings(1)


def test_cache_returns_copies_not_the_stored_dict():
    db = _CountingDB()
    asyncio.run(_mutate_returned(db))


async def _mutate_returned(db):
    a = await db.get_guild_settings(1)
    a["prefix"] = "MUTATED"
    b = await db.get_guild_settings(1)
    # Mutating a returned dict must not corrupt the cached value.
    assert b["prefix"] == "."
