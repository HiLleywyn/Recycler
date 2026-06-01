"""Tests for the paced BulkRunner that keeps mass actions under Discord limits."""
from __future__ import annotations

import asyncio

import discord
import pytest

from clanklib.ratelimit import BulkRunner, _retry_after


class _Resp429:
    status = 429
    reason = "Too Many Requests"
    headers = {"Retry-After": "0"}


def _make_429() -> discord.HTTPException:
    return discord.HTTPException(_Resp429(), {"message": "rate limited", "code": 0})


def _runner() -> BulkRunner:
    # Zero delays so tests are instant.
    return BulkRunner(base_delay=0.0, max_delay=0.0, max_consecutive_429=3, max_retries=1)


def test_all_succeed():
    seen = []

    async def act(x):
        seen.append(x)

    res = asyncio.run(_runner().run([1, 2, 3, 4, 5], act))
    assert res.total == 5 and res.succeeded == 5 and res.failed == 0
    assert not res.aborted
    assert seen == [1, 2, 3, 4, 5]


def test_non_429_failures_are_counted_and_do_not_abort():
    async def act(x):
        if x == 3:
            raise ValueError("boom")

    res = asyncio.run(_runner().run([1, 2, 3, 4], act))
    assert res.succeeded == 3 and res.failed == 1
    assert not res.aborted
    assert any("boom" in e for e in res.errors)


def test_consecutive_429s_abort_the_run():
    async def act(x):
        raise _make_429()

    res = asyncio.run(_runner().run(list(range(20)), act))
    # Aborts well before processing all 20, leaving a remainder.
    assert res.aborted
    assert "rate limit" in res.abort_reason.lower()
    assert res.processed < 20


def test_long_retry_after_aborts_immediately():
    class _RespLong:
        status = 429
        reason = "Too Many Requests"
        headers = {"Retry-After": "7200"}  # a 2-hour global ban signal

    async def act(x):
        raise discord.HTTPException(_RespLong(), {"message": "global", "code": 0})

    runner = BulkRunner(base_delay=0.0, long_retry_abort=60.0)
    res = asyncio.run(runner.run(list(range(10)), act))
    assert res.aborted
    assert "global rate limit" in res.abort_reason
    # It must not have tried to sleep through the 2h wait -- abort on the first.
    assert res.processed == 1
