"""Per-user AI message quota tracking."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


_AI_QUOTA_WINDOW = 3600      # 1 hour in seconds
_AI_QUOTA_LIMIT = 25         # max messages per hour per user
_ai_user_timestamps: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque())
_ai_user_locks: dict[tuple[int, int], asyncio.Lock] = {}


def check_ai_quota(user_id: int, guild_id: int) -> tuple[bool, int]:
    """Return (allowed, remaining). Consumes a quota slot if allowed."""
    key = (user_id, guild_id)
    q = _ai_user_timestamps[key]
    now = time.monotonic()
    while q and now - q[0] > _AI_QUOTA_WINDOW:
        q.popleft()
    remaining = _AI_QUOTA_LIMIT - len(q)
    if remaining <= 0:
        return False, 0
    q.append(now)
    return True, remaining - 1


async def reserve_ai_quota(user_id: int, guild_id: int) -> tuple[bool, int, float | None]:
    """Atomically reserve an AI quota slot.

    Returns ``(allowed, remaining_after_reserve, reservation_ts)``. If the caller
    later abandons the request because the model failed or timed out, it should
    call :func:`cancel_ai_quota_reservation` with the returned timestamp.
    """
    key = (user_id, guild_id)
    if key not in _ai_user_locks:
        _ai_user_locks[key] = asyncio.Lock()
    async with _ai_user_locks[key]:
        q = _ai_user_timestamps[key]
        now = time.monotonic()
        while q and now - q[0] > _AI_QUOTA_WINDOW:
            q.popleft()
        remaining = _AI_QUOTA_LIMIT - len(q)
        if remaining <= 0:
            return False, 0, None
        q.append(now)
        return True, remaining - 1, now


def cancel_ai_quota_reservation(user_id: int, guild_id: int, reservation_ts: float) -> None:
    """Release a previously reserved AI quota slot."""
    q = _ai_user_timestamps.get((user_id, guild_id))
    if not q:
        return
    for i, ts in enumerate(q):
        if ts == reservation_ts:
            del q[i]
            break


def reset_ai_quota_state() -> None:
    """Reset in-memory quota state, mainly for tests."""
    _ai_user_timestamps.clear()
    _ai_user_locks.clear()
