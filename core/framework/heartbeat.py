"""In-memory heartbeat registry for background task health tracking.

Each task loop calls ``pulse("task_name")`` on every successful iteration.
The dev/status system reads ``get_all()`` and ``stale_tasks()`` to detect
loops that have stopped firing.

Pure Python  -  no discord.py or framework imports.
"""
from __future__ import annotations

import time
import threading

_lock = threading.Lock()
_registry: dict[str, float] = {}   # task_name -> last_pulse_epoch
_expected: set[str] = set()         # task names that should be pulsing
_intervals: dict[str, float] = {}   # task_name -> expected interval in seconds


def expect(name: str) -> None:
    """Register a task that is expected to pulse regularly."""
    with _lock:
        _expected.add(name)


def pulse(name: str) -> None:
    """Record a heartbeat for *name* (call at end of each task loop iteration)."""
    with _lock:
        _registry[name] = time.time()
        _expected.add(name)


def register_interval(name: str, seconds: float) -> None:
    """Register the expected interval for a task so health checks can be interval-aware."""
    with _lock:
        _intervals[name] = seconds


def get_interval(name: str) -> float | None:
    """Return the registered interval for *name*, or None if not registered."""
    with _lock:
        return _intervals.get(name)


def get_all() -> dict[str, float]:
    """Return a snapshot of all heartbeats: ``{name: last_pulse_epoch}``."""
    with _lock:
        return dict(_registry)


def get_all_intervals() -> dict[str, float]:
    """Return a snapshot of all registered intervals."""
    with _lock:
        return dict(_intervals)


def stale_tasks(max_age: float = 300) -> list[str]:
    """Return task names that haven't pulsed within *max_age* seconds,
    or that are expected but have never pulsed.

    If a task has a registered interval, it uses ``interval × 3`` as the
    staleness threshold instead of *max_age*.
    """
    now = time.time()
    with _lock:
        stale = set()
        # Check all expected tasks (may never have pulsed)
        for name in _expected:
            threshold = _intervals.get(name, 0) * 3 if name in _intervals else max_age
            threshold = max(threshold, max_age)  # never less than max_age
            last = _registry.get(name)
            if last is None or (now - last) > threshold:
                stale.add(name)
        # Also check registry entries that are old (e.g. written directly)
        for name, last in _registry.items():
            threshold = _intervals.get(name, 0) * 3 if name in _intervals else max_age
            threshold = max(threshold, max_age)
            if (now - last) > threshold:
                stale.add(name)
        return sorted(stale)


def reset() -> None:
    """Clear all heartbeats (for testing)."""
    with _lock:
        _registry.clear()
        _expected.clear()
        _intervals.clear()
