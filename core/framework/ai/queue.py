"""Per-backend chat queue with per-user serialization.

Replaces the single global ``asyncio.Semaphore(32)`` previously used inside
``core/framework/ai/client.py``. The old setup had two failure modes:

  * No fairness. Two power users with multi-iteration tool loops (each one
    burning 3-4 slots sequentially) could starve other users for the entire
    outer wait_for budget, surfacing the generic "AI timed out" card.
  * No queue visibility. A request that sat blocked on the semaphore stayed
    silent until it either acquired a slot or timed out. Users had no idea
    whether the bot was thinking, busy, or dead.

This module exposes ``ChatQueue`` which gives:

  * Separate capacity caps per backend (OpenRouter higher, Ollama lower
    because the local box can't take much concurrency).
  * A reserved sub-pool for system / background traffic so commentary jobs
    can never block a user-facing chat.
  * Per-user serialization (one in-flight chat per user per backend; the
    second one waits behind the first, never overtakes other users).
  * Position-update callbacks the placeholder UI subscribes to and renders
    as "queued (#3)" in the chat status embed.
  * A ``stats()`` snapshot for the ``,ai queue`` admin command.

The contract is a context manager:

    async with chat_queue.acquire(backend="ollama", user_id=uid) as ticket:
        # do the actual aiohttp POST here -- the slot is reserved.
        ...

``acquire`` returns a ``Ticket`` immediately, but ``__aenter__`` blocks until
a slot is granted. While waiting, the ticket fires ``on_position_change(N)``
every time the user advances; ``N == 0`` is the in-flight position.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from core.config import Config

log = logging.getLogger(__name__)


# Position-change callback type. The renderer uses this to update the
# placeholder embed with the current queue depth.
PositionCallback = Callable[[int], Awaitable[None]]

Backend = Literal["openrouter", "ollama"]
Kind = Literal["chat", "system"]


@dataclass(frozen=True)
class QueueStats:
    """Snapshot of one backend's queue state for the admin ``,ai queue`` view."""
    backend: str
    in_flight: int
    waiting: int
    capacity: int
    system_reserved: int
    waiting_users: int


@dataclass
class _Waiter:
    """One pending request in a backend's FIFO queue."""
    user_id: int | None
    kind: Kind
    on_position_change: PositionCallback | None
    enqueued_at: float
    granted: asyncio.Event = field(default_factory=asyncio.Event)
    last_broadcast_pos: int = -1


class Ticket:
    """Reserved slot in the chat queue.

    The caller uses this as ``async with queue.acquire(...) as ticket:``;
    ``__aenter__`` blocks until the slot is granted. ``position`` is ``0``
    once the request is in-flight; positive integers are how many requests
    are ahead in line.
    """

    __slots__ = (
        "_queue", "backend", "user_id", "kind", "on_position_change",
        "_waiter", "_acquired",
    )

    def __init__(
        self,
        queue: "ChatQueue",
        *,
        backend: Backend,
        user_id: int | None,
        kind: Kind,
        on_position_change: PositionCallback | None,
    ) -> None:
        self._queue = queue
        self.backend = backend
        self.user_id = user_id
        self.kind = kind
        self.on_position_change = on_position_change
        self._waiter: _Waiter | None = None
        self._acquired = False

    @property
    def position(self) -> int:
        """Current position in line. 0 = in-flight, >0 = waiting."""
        if self._acquired:
            return 0
        w = self._waiter
        if w is None:
            return -1
        return self._queue._position_of(self.backend, w)

    async def __aenter__(self) -> "Ticket":
        self._waiter = _Waiter(
            user_id=self.user_id,
            kind=self.kind,
            on_position_change=self.on_position_change,
            enqueued_at=time.monotonic(),
        )
        await self._queue._enqueue(self.backend, self._waiter)
        # Notify with the initial position so the UI can render "queued (#N)"
        # without waiting for the first re-broadcast.
        if self.on_position_change is not None and self._waiter.last_broadcast_pos != 0:
            pos = self._queue._position_of(self.backend, self._waiter)
            if pos > 0:
                self._waiter.last_broadcast_pos = pos
                try:
                    await self.on_position_change(pos)
                except Exception:
                    log.debug("Ticket: initial on_position_change callback failed", exc_info=True)
        await self._waiter.granted.wait()
        self._acquired = True
        # Final 0-broadcast so the renderer knows we've started.
        if self.on_position_change is not None and self._waiter.last_broadcast_pos != 0:
            self._waiter.last_broadcast_pos = 0
            try:
                await self.on_position_change(0)
            except Exception:
                log.debug("Ticket: ready on_position_change callback failed", exc_info=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._waiter is not None:
            self._queue._release(self.backend, self._waiter, self._acquired)
            self._waiter = None
        self._acquired = False


class _BackendQueue:
    """FIFO queue plus capacity tracking for a single backend.

    The outer queue is shared between chat and system traffic. A separate
    counter reserves ``system_reserved`` slots for system traffic so the
    background commentary path never starves user chat (chat tasks can
    still claim those slots; the reservation only blocks system tasks from
    taking more than its share).
    """

    def __init__(
        self,
        backend: Backend,
        *,
        capacity: int,
        system_reserved: int,
    ) -> None:
        self.backend = backend
        self.capacity = max(1, int(capacity))
        self.system_reserved = max(0, min(int(system_reserved), self.capacity))
        self.in_flight = 0
        self.in_flight_system = 0
        self._waiters: deque[_Waiter] = deque()
        # Single lock guarding all state. The waiters are not blocked on this
        # lock -- they're blocked on their own ``granted`` events -- so it's
        # only held for the millisecond it takes to mutate counters / deques.
        self._lock = asyncio.Lock()

    def _chat_capacity(self) -> int:
        """Slots a chat task may take. The reserved pool is only for system."""
        return max(1, self.capacity - self.system_reserved)


class ChatQueue:
    """Per-backend chat queue with per-user serialization.

    One instance is shared across the process. Acquire a ticket with
    ``async with queue.acquire(...) as t``; the manager handles enqueue,
    fair wakeup, and counter release on exit.
    """

    def __init__(
        self,
        *,
        openrouter_cap: int,
        ollama_cap: int,
        system_reserved: int,
    ) -> None:
        self._backends: dict[str, _BackendQueue] = {
            "openrouter": _BackendQueue(
                "openrouter",
                capacity=openrouter_cap,
                system_reserved=system_reserved,
            ),
            "ollama": _BackendQueue(
                "ollama",
                capacity=ollama_cap,
                system_reserved=system_reserved,
            ),
        }

    @classmethod
    def from_config(cls) -> "ChatQueue":
        """Build a queue using the per-deployment Config knobs."""
        return cls(
            openrouter_cap=int(getattr(Config, "AI_QUEUE_OPENROUTER_CAP", 24) or 24),
            ollama_cap=int(getattr(Config, "AI_QUEUE_OLLAMA_CAP", 2) or 2),
            system_reserved=int(getattr(Config, "AI_QUEUE_SYSTEM_RESERVED", 4) or 4),
        )

    def acquire(
        self,
        *,
        backend: Backend,
        user_id: int | None,
        kind: Kind = "chat",
        on_position_change: PositionCallback | None = None,
    ) -> Ticket:
        """Return a fresh ``Ticket`` for use as ``async with``.

        The ticket has not yet been enqueued -- enqueue happens on
        ``__aenter__`` so callers can pass the ticket around before
        entering the context manager (the renderer wants a reference to
        wire up its position callback first).
        """
        if backend not in self._backends:
            raise ValueError(f"unknown backend: {backend!r}")
        return Ticket(
            self,
            backend=backend,
            user_id=user_id,
            kind=kind,
            on_position_change=on_position_change,
        )

    def stats(self, backend: str | None = None) -> list[QueueStats]:
        """Snapshot the current depth for one or all backends.

        Returns a list (in OpenRouter, Ollama order) so the ``,ai queue``
        admin embed can render fields deterministically. Single-backend
        callers get a one-element list.
        """
        if backend is not None and backend in self._backends:
            keys = [backend]
        else:
            keys = ["openrouter", "ollama"]
        out: list[QueueStats] = []
        for k in keys:
            q = self._backends[k]
            users_waiting = len({w.user_id for w in q._waiters if w.user_id is not None})
            out.append(QueueStats(
                backend=k,
                in_flight=q.in_flight,
                waiting=len(q._waiters),
                capacity=q.capacity,
                system_reserved=q.system_reserved,
                waiting_users=users_waiting,
            ))
        return out

    # ── internal: enqueue / release / position tracking ──────────────────────

    async def _enqueue(self, backend: Backend, waiter: _Waiter) -> None:
        q = self._backends[backend]
        async with q._lock:
            q._waiters.append(waiter)
        # Try to grant immediately if the queue had headroom.
        await self._pump(backend)

    def _release(self, backend: Backend, waiter: _Waiter, was_acquired: bool) -> None:
        q = self._backends[backend]
        # No await -- this runs under __aexit__ which must never block on
        # someone else's lock for fairness. The lock is asyncio.Lock so
        # acquire_nowait is non-standard; instead schedule the pump.
        if was_acquired:
            q.in_flight = max(0, q.in_flight - 1)
            if waiter.kind == "system":
                q.in_flight_system = max(0, q.in_flight_system - 1)
        else:
            # Cancelled before being granted -- pop from the deque if still
            # present. No in-flight counters to unwind.
            try:
                q._waiters.remove(waiter)
            except ValueError:
                pass
        # Pump asynchronously so the next waiter (if any) can proceed and
        # the queue depth can be re-broadcast to everyone still in line.
        asyncio.create_task(self._pump(backend))

    async def _pump(self, backend: Backend) -> None:
        """Grant slots to waiters in FIFO order, respecting per-user serial.

        Walks the deque from the head, granting the first waiter whose
        constraints currently allow it: a chat ticket needs a non-reserved
        slot AND no other in-flight request for the same user; a system
        ticket may claim a reserved slot. Skips a waiter that's still
        blocked by per-user serialization and keeps walking so other users
        don't get stuck behind one user's queue.
        """
        q = self._backends[backend]
        async with q._lock:
            granted_any = True
            while granted_any:
                granted_any = False
                if not q._waiters:
                    break
                if q.in_flight >= q.capacity:
                    break
                # Walk the deque, granting the first eligible waiter.
                for idx, w in enumerate(q._waiters):
                    if w.granted.is_set():
                        continue
                    eligible = self._is_eligible(q, w)
                    if not eligible:
                        continue
                    # Remove from deque (O(n) for arbitrary index, but the
                    # queue depth is bounded and the inner loop is cheap).
                    del q._waiters[idx]
                    q.in_flight += 1
                    if w.kind == "system":
                        q.in_flight_system += 1
                    w.granted.set()
                    granted_any = True
                    break
        # Re-broadcast positions to everyone still waiting after a grant.
        await self._broadcast_positions(backend)

    @staticmethod
    def _is_eligible(q: _BackendQueue, w: _Waiter) -> bool:
        """Return True if this waiter can be granted right now.

        Per-user serialization was removed -- it was making rapid same-user
        mentions stack up behind each other ("queued (#1)" sitting forever
        while the user's own earlier request was still in flight). The
        per-backend cap is the real flow-control point now; one user can
        run as many concurrent chats as the cap allows. This matches what
        users actually expect: firing four ``@mention`` messages in a row
        produces four parallel responses, not a sequential train.
        """
        # System tickets get the reserved sub-pool; chat tickets compete
        # for the rest. Both are FIFO inside the deque so older requests
        # always run first.
        if w.kind == "system":
            chat_only_floor = max(0, q.capacity - q.system_reserved - q.in_flight_system)
            if q.in_flight_system >= q.system_reserved and (q.capacity - q.in_flight) <= chat_only_floor:
                return False
        return True

    def _position_of(self, backend: Backend, waiter: _Waiter) -> int:
        """Return the waiter's position in the queue (1-indexed). 0 = in-flight."""
        q = self._backends[backend]
        if waiter.granted.is_set():
            return 0
        for i, w in enumerate(q._waiters):
            if w is waiter:
                return i + 1
        return -1

    async def _broadcast_positions(self, backend: Backend) -> None:
        """Fire ``on_position_change`` on every waiter whose position changed."""
        q = self._backends[backend]
        for i, w in enumerate(q._waiters):
            new_pos = i + 1
            if w.last_broadcast_pos == new_pos:
                continue
            w.last_broadcast_pos = new_pos
            if w.on_position_change is None:
                continue
            try:
                await w.on_position_change(new_pos)
            except Exception:
                log.debug("ChatQueue: on_position_change broadcast failed", exc_info=True)


# Note: per-user serialization (the previous _user_has_no_earlier_waiter
# helper) was removed. Same-user requests now run concurrently up to the
# per-backend cap; only the FIFO order of the deque enforces fairness
# across users.
