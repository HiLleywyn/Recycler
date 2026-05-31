"""Runtime stats sampler -- in-memory ring buffers for the bot's own health.

A single background task started by the Help cog records the bot's
gateway latency, process CPU%, RSS memory, system memory %, and guild
count once per sample interval. Samples are stored in fixed-size deques
so memory usage stays bounded regardless of uptime.

The ``,botinfo`` view consumes these snapshots to render sparkline
"charts" (Unicode block characters) so users can see how the bot has
behaved over the last ~hour without needing image rendering or
external services.

All data is local and ephemeral: nothing is persisted to the DB and no
PII (IPs, tokens, channel content, user data) is recorded.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

import psutil

from core.framework.heartbeat import pulse, register_interval

log = logging.getLogger(__name__)

# Sample every 30s, keep 120 samples = last 60 minutes of history.
SAMPLE_INTERVAL_SECONDS = 30.0
HISTORY_LEN = 120


class RuntimeStats:
    """Ring-buffer sampler for the bot process's own runtime stats."""

    __slots__ = (
        "_bot", "_proc", "_task",
        "ts", "latency_ms", "cpu_pct", "rss_mb",
        "sys_cpu_pct", "sys_mem_pct", "guilds",
    )

    def __init__(self, bot) -> None:
        self._bot = bot
        self._proc = psutil.Process()
        self._task: asyncio.Task | None = None
        self.ts:           collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.latency_ms:   collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.cpu_pct:      collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.rss_mb:       collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.sys_cpu_pct:  collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.sys_mem_pct:  collections.deque[float] = collections.deque(maxlen=HISTORY_LEN)
        self.guilds:       collections.deque[int]   = collections.deque(maxlen=HISTORY_LEN)
        # Prime psutil's CPU counter so the first reading isn't 0.0.
        try:
            self._proc.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        register_interval("runtime_stats", SAMPLE_INTERVAL_SECONDS)
        self._task = asyncio.create_task(self._loop(), name="runtime_stats_sampler")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # First sample immediately so the chart isn't empty when a user
        # opens ,botinfo right after the bot starts.
        await self._sample()
        while True:
            try:
                await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
                await self._sample()
                pulse("runtime_stats")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("runtime_stats sampler tick failed")

    async def _sample(self) -> None:
        try:
            mem = self._proc.memory_info()
            cpu = self._proc.cpu_percent(interval=None)
            sys_cpu = psutil.cpu_percent(interval=None)
            sys_mem = psutil.virtual_memory().percent
            lat = self._bot.latency * 1000 if self._bot.latency else 0.0
            # Discord's gateway sometimes reports inf/nan when the
            # connection is dropping. Coerce to 0 so the sparkline stays
            # readable instead of spiking off the chart.
            if lat != lat or lat == float("inf"):
                lat = 0.0
            self.ts.append(time.time())
            self.latency_ms.append(float(lat))
            self.cpu_pct.append(float(cpu))
            self.rss_mb.append(float(mem.rss / 1024 / 1024))
            self.sys_cpu_pct.append(float(sys_cpu))
            self.sys_mem_pct.append(float(sys_mem))
            self.guilds.append(len(self._bot.guilds))
        except Exception:
            log.exception("runtime_stats _sample failed")

    def snapshot(self) -> dict:
        """Return a copy of the current ring buffers as plain lists."""
        return {
            "ts":          list(self.ts),
            "latency_ms":  list(self.latency_ms),
            "cpu_pct":     list(self.cpu_pct),
            "rss_mb":      list(self.rss_mb),
            "sys_cpu_pct": list(self.sys_cpu_pct),
            "sys_mem_pct": list(self.sys_mem_pct),
            "guilds":      list(self.guilds),
            "interval":    SAMPLE_INTERVAL_SECONDS,
            "history_len": HISTORY_LEN,
        }


_INSTANCE: RuntimeStats | None = None


def install(bot) -> RuntimeStats:
    """Create (if needed) and start the singleton sampler. Idempotent."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = RuntimeStats(bot)
    _INSTANCE.start()
    return _INSTANCE


def get() -> RuntimeStats | None:
    """Return the running sampler, or None if it hasn't been installed."""
    return _INSTANCE
