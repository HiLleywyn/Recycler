"""Per-provider circuit breaker.

After N consecutive failures the provider is marked DOWN for ``cool_off``
seconds; the router skips it during that window. A single success resets
the counter. Keeps the dispatcher resilient when one upstream goes flaky
without dragging the whole ``$`` namespace down.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


class HealthStatus(str, enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"


@dataclass
class HealthEntry:
    name: str
    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0
    cool_off_until: float = 0.0
    reason: str = ""


class HealthRegistry:
    """Tracks provider health. Single instance lives on the
    :class:`Registry`."""

    DEGRADED_AFTER = 2
    DOWN_AFTER = 5
    COOL_OFF_SECONDS = 60.0

    def __init__(self) -> None:
        self._entries: dict[str, HealthEntry] = {}

    def get(self, name: str) -> HealthEntry:
        entry = self._entries.get(name)
        if entry is None:
            entry = HealthEntry(name=name)
            self._entries[name] = entry
        return entry

    def all(self) -> dict[str, HealthEntry]:
        return dict(self._entries)

    def is_available(self, name: str) -> bool:
        entry = self.get(name)
        if entry.status is HealthStatus.DISABLED:
            return False
        if entry.status is HealthStatus.DOWN and time.monotonic() < entry.cool_off_until:
            return False
        return True

    def mark_disabled(self, name: str, reason: str) -> None:
        entry = self.get(name)
        entry.status = HealthStatus.DISABLED
        entry.reason = reason

    def mark_success(self, name: str) -> None:
        entry = self.get(name)
        entry.consecutive_failures = 0
        entry.last_success_ts = time.monotonic()
        if entry.status is not HealthStatus.DISABLED:
            entry.status = HealthStatus.HEALTHY
            entry.reason = ""

    def mark_failure(self, name: str, reason: str = "") -> None:
        entry = self.get(name)
        if entry.status is HealthStatus.DISABLED:
            return
        entry.consecutive_failures += 1
        entry.last_failure_ts = time.monotonic()
        entry.reason = reason
        if entry.consecutive_failures >= self.DOWN_AFTER:
            entry.status = HealthStatus.DOWN
            entry.cool_off_until = time.monotonic() + self.COOL_OFF_SECONDS
            log.warning(
                "[market.health] provider=%s DOWN reason=%r cool_off=%ss",
                name, reason, self.COOL_OFF_SECONDS,
            )
        elif entry.consecutive_failures >= self.DEGRADED_AFTER:
            entry.status = HealthStatus.DEGRADED
