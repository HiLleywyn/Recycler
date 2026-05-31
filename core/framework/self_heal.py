"""core/framework/self_heal.py  -  Periodic self-healing scheduler for Discoin.

Ported from Hydra's HydraRuntime._scheduler_loop and Profile._connect_adapter_with_retry /
_schedule_profile_retry patterns.

Runs background checks every 60 s:
  - Redis bus  : reconnect if offline (exponential backoff, up to 5 attempts, capped 300 s)
                 Uses bus.ping() to catch ghost connections where is_connected looks True
                 but the TCP socket is hung.  Force-closes before every retry to clear
                 stale file descriptors.
  - Task loops : restart any failed discord.ext.tasks.Loop instances across all cogs.
                 Uses cancel → sleep(1) → start instead of restart() to avoid the race
                 condition where a loop is still tearing down when restart() fires.
                 Circuit-breaker: after 5 consecutive failures a loop is moved to the
                 degraded set and no longer touched (preventing infinite CPU spin).
  - Heartbeats : log warnings for stale tasks (from core/framework/heartbeat.py registry)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import tasks as _tasks

from constants.ui import C_ERROR, C_WARNING
from core.framework import heartbeat
from core.framework.embed import card

if TYPE_CHECKING:
    from core.framework.bot import Discoin

log = logging.getLogger("discoin.self_heal")

# ── Retry defaults (mirrors Hydra profile.py) ──────────────────────────────
_REDIS_RETRY_MAX: int = 5
_REDIS_RETRY_BASE_DELAY: float = 5.0   # doubles each attempt, capped at 300 s
_LOOP_INTERVAL: float = 60.0            # scheduler tick interval
_LOOP_CIRCUIT_BREAKER: int = 5          # consecutive failures before giving up on a loop
_PING_INTERVAL: float = 300.0           # how often to verify Redis with a real PING


class SelfHealScheduler:
    """Periodic self-healing background task for the Discoin bot.

    Mirrors Hydra's _scheduler_loop / profile-level retry logic, adapted for
    discord.py's single-bot model.  Start it once after on_ready; it runs
    silently until the bot closes.
    """

    def __init__(self, bot: "Discoin") -> None:
        import os
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._redis_retry_attempt: int = 0
        self._redis_retry_task: asyncio.Task | None = None
        self._started_at: float = 0.0
        self._last_redis_ping: float = 0.0

        # Circuit-breaker state: consecutive failure count per loop label
        self._loop_fail_counts: dict[str, int] = {}
        # Loops that hit the circuit-breaker threshold  -  no longer auto-restarted
        self._degraded_loops: set[str] = set()

        # Notification toggle  -  controls error-channel posts and dev DMs.
        # Defaults to on; set SELF_HEAL_NOTIFY=0 env var or call .notify = False to disable.
        self.notify_enabled: bool = os.getenv("SELF_HEAL_NOTIFY", "1") != "0"

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the self-heal scheduler background task."""
        if self._task and not self._task.done():
            return
        self._started_at = time.time()
        self._task = asyncio.create_task(
            self._loop(), name="discoin.self_heal"
        )
        log.info("Self-heal scheduler started (interval=%.0fs)", _LOOP_INTERVAL)

    def stop(self) -> None:
        """Cancel the scheduler and any pending retry tasks."""
        if self._task and not self._task.done():
            self._task.cancel()
        if self._redis_retry_task and not self._redis_retry_task.done():
            self._redis_retry_task.cancel()
        log.info("Self-heal scheduler stopped")

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the scheduler was started (0 if not started)."""
        return time.time() - self._started_at if self._started_at else 0.0

    # ── Main loop ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Periodic maintenance loop  -  mirrors Hydra's _scheduler_loop."""
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(_LOOP_INTERVAL)
                await self._run_checks()
            except asyncio.CancelledError:
                return
            except Exception:
                # Log but keep running  -  the scheduler must not die silently.
                log.exception("Self-heal scheduler tick raised an unexpected exception")

    async def _run_checks(self) -> None:
        """Run all self-healing checks in sequence."""
        await self._check_redis_bus()
        await self._check_task_loops()
        self._check_heartbeats()

    # ── Redis bus recovery (exponential backoff + ghost-connection ping) ───

    async def _check_redis_bus(self) -> None:
        """Reconnect the Redis event bus if it has gone offline.

        Uses the same exponential-backoff scheduling pattern as Hydra's
        _schedule_profile_retry: doubles the wait each attempt, caps at 300 s,
        and resets the counter once the bus is healthy again.

        Additionally performs a periodic real PING every _PING_INTERVAL seconds
        to catch ghost connections where is_connected looks True but the TCP
        socket is hung.  If the PING fails the bus is force-closed before the
        reconnect attempt to clear the stale file descriptor.
        """
        bus = getattr(self.bot, "bus", None)
        if bus is None:
            return

        now = time.time()
        connected = bus.is_connected

        if connected:
            # Periodic real PING to detect ghost connections
            if now - self._last_redis_ping >= _PING_INTERVAL:
                self._last_redis_ping = now
                alive = await bus.ping()
                if not alive:
                    log.warning(
                        "Redis PING failed despite is_connected=True  -  forcing close "
                        "to clear stale socket before reconnect"
                    )
                    try:
                        await bus.close()
                    except Exception:
                        pass
                    connected = False
                else:
                    if self._redis_retry_attempt > 0:
                        log.info("Redis bus recovered  -  resetting retry counter")
                        self._redis_retry_attempt = 0
                    return
            else:
                if self._redis_retry_attempt > 0:
                    log.info("Redis bus recovered  -  resetting retry counter")
                    self._redis_retry_attempt = 0
                return

        # Only schedule one retry at a time (duplicate-guard mirrors Hydra)
        if self._redis_retry_task and not self._redis_retry_task.done():
            return

        if self._redis_retry_attempt >= _REDIS_RETRY_MAX:
            log.error(
                "Redis bus has exhausted all %d reconnect attempts  -  staying disconnected",
                _REDIS_RETRY_MAX,
            )
            embed = (
                card(
                    "🚨 Self-Heal: Redis Offline",
                    description=(
                        f"Redis has been unreachable for all **{_REDIS_RETRY_MAX}** reconnect attempts.\n\n"
                        f"The bot is running in in-memory fallback mode. "
                        f"Pub/sub events are offline until Redis recovers."
                    ),
                    color=C_ERROR,
                )
                .timestamp(discord.utils.utcnow())
                .footer("Self-Heal Scheduler  |  run ,health heal to retry")
                .build()
            )
            asyncio.create_task(self._post_to_error_channels(embed))
            asyncio.create_task(self._dm_dev(
                f"🚨 **Discoin Redis offline**\n"
                f"All {_REDIS_RETRY_MAX} reconnect attempts exhausted. "
                f"Bot is in in-memory fallback mode.\n"
                f"Run `,health heal` to retry."
            ))
            return

        self._redis_retry_attempt += 1
        wait = min(_REDIS_RETRY_BASE_DELAY * (2 ** (self._redis_retry_attempt - 1)), 300.0)
        log.warning(
            "Redis bus offline  -  scheduling reconnect in %.0fs (attempt %d/%d)",
            wait,
            self._redis_retry_attempt,
            _REDIS_RETRY_MAX,
        )
        self._redis_retry_task = asyncio.create_task(
            self._reconnect_redis_after(wait),
            name="discoin.self_heal.redis_retry",
        )

    async def _reconnect_redis_after(self, delay: float) -> None:
        """Wait *delay* seconds then force-close and reconnect the Redis bus.

        Force-closing first clears any stale file descriptor so the new connect()
        gets a fresh socket rather than attempting a handshake on a hung connection.
        """
        try:
            await asyncio.sleep(delay)
            bus = getattr(self.bot, "bus", None)
            if bus is None or bus.is_connected:
                return

            # Force-close to clear any stale / hung socket before reconnecting
            log.info("Force-closing stale Redis connection before reconnect attempt…")
            try:
                await bus.close()
            except Exception:
                pass

            log.info("Attempting Redis bus reconnect…")
            await bus.connect()
            if bus.is_connected:
                self._redis_retry_attempt = 0
                self._last_redis_ping = time.time()
                log.info("Redis bus successfully reconnected")
            else:
                log.warning("Redis reconnect did not establish a connection")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Redis bus reconnect attempt failed")

    # ── Task loop recovery ─────────────────────────────────────────────────

    async def _check_task_loops(self) -> None:
        """Restart failed discord.ext.tasks.Loop instances across all cogs.

        Mirrors Hydra's _run_health_checks which iterates workers and calls
        restart on unhealthy ones.  One-shot loops (count=1) are intentionally
        skipped  -  they are expected to stop after a single run.

        Restart strategy: cancel (if still running) → sleep 1 s → start, which
        avoids the race condition where restart() fires while the loop's internal
        asyncio task is still in a "tearing down" state.

        Circuit-breaker: if a loop fails _LOOP_CIRCUIT_BREAKER times in a row it
        is added to _degraded_loops and skipped from that point on, preventing the
        healer from becoming a resource hog on a permanently broken loop.
        """
        for cog_name, cog in list(self.bot.cogs.items()):
            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name, None)
                    if not isinstance(attr, _tasks.Loop):
                        continue
                    # Skip intentionally-stopped one-shot loops
                    if getattr(attr, "count", None) == 1:
                        continue

                    label = f"{cog_name}.{attr_name}"

                    # Skip loops explicitly marked as on-demand (e.g. test fixtures)
                    if getattr(attr, "_heal_skip", False):
                        continue

                    failed = attr.failed() if callable(attr.failed) else attr.failed
                    running = attr.is_running() if callable(attr.is_running) else attr.is_running

                    if not failed:
                        if running:
                            # Healthy and running - clear any failure tracking
                            if label in self._loop_fail_counts:
                                del self._loop_fail_counts[label]
                            if label in self._degraded_loops:
                                self._degraded_loops.discard(label)
                                log.info("Task loop %s recovered  -  removed from degraded set", label)
                        # Not failed and not running = intentionally idle / on-demand - skip
                        continue

                    # Circuit-breaker: stop touching loops that keep failing
                    if label in self._degraded_loops:
                        continue

                    # Capture the exception for better root-cause logging
                    exc_info: str = ""
                    if failed:
                        try:
                            inner_task = getattr(attr, "_task", None)
                            if inner_task is not None and inner_task.done():
                                exc = inner_task.exception()
                                if exc is not None:
                                    exc_info = f"  -  {type(exc).__name__}: {exc}"
                        except Exception:
                            pass

                    log.warning("Detected failed/stopped task loop: %s%s", label, exc_info)
                    asyncio.create_task(
                        self._restart_loop_safe(attr, label),
                        name=f"discoin.self_heal.restart.{label}",
                    )
                except Exception:
                    pass

    async def _restart_loop_safe(self, loop: _tasks.Loop, label: str) -> None:
        """Cancel (if needed) → sleep 1 s → start a failed Loop.

        Avoids the restart() race condition where the loop is still tearing down
        when the new iteration starts.  Records failures for the circuit-breaker.
        """
        try:
            running = loop.is_running() if callable(loop.is_running) else loop.is_running
            if running:
                loop.cancel()
                await asyncio.sleep(1.0)

            loop.start()
            log.info("Task loop %s restarted successfully", label)
            self._loop_fail_counts.pop(label, None)

            # Notify error channels: loop was down and has been restarted
            embed = (
                card(
                    "🔁 Self-Heal: Loop Restarted",
                    color=C_WARNING,
                    description=f"`{label}` was in a failed state and has been restarted automatically.",
                )
                .timestamp()
                .footer("Self-Heal Scheduler  |  run ,health heal for full diagnostic")
                .build()
            )
            asyncio.create_task(self._post_to_error_channels(embed))

            try:
                from core.framework.error_tracker import ErrorSource, Severity
                self.bot.errors.record(
                    ErrorSource.TASK,
                    f"Self-heal restarted task loop: {label}",
                    severity=Severity.WARNING,
                    error_type="TaskLoopRestart",
                )
            except Exception:
                pass

        except Exception:
            log.exception("Failed to restart task loop: %s", label)
            count = self._loop_fail_counts.get(label, 0) + 1
            self._loop_fail_counts[label] = count
            if count >= _LOOP_CIRCUIT_BREAKER:
                self._degraded_loops.add(label)
                log.error(
                    "Task loop %s has failed %d consecutive restart attempts  -  "
                    "moving to degraded (manual intervention required)",
                    label, count,
                )

                # Circuit-breaker tripped: post to error channels AND DM dev
                embed = (
                    card(
                        "🚨 Self-Heal: Circuit Breaker Tripped",
                        description=(
                            f"`{label}` has failed **{count}** consecutive restart attempts "
                            f"and has been moved to **degraded** state.\n\n"
                            f"Automatic restarts have stopped. Run `,health heal` to inspect "
                            f"and manually recover this loop."
                        ),
                        color=C_ERROR,
                    )
                    .timestamp(discord.utils.utcnow())
                    .footer("Self-Heal Scheduler  |  manual intervention required")
                    .build()
                )
                asyncio.create_task(self._post_to_error_channels(embed))
                asyncio.create_task(self._dm_dev(
                    f"🚨 **Discoin self-heal circuit breaker tripped**\n"
                    f"Loop `{label}` failed {count} restart attempts and is now degraded.\n"
                    f"Run `,health heal` to diagnose."
                ))

                try:
                    from core.framework.error_tracker import ErrorSource, Severity
                    self.bot.errors.record(
                        ErrorSource.TASK,
                        f"Task loop circuit-breaker tripped: {label} ({count} failures)",
                        severity=Severity.ERROR,
                        error_type="TaskLoopDegraded",
                    )
                except Exception:
                    pass

    # ── Notification helpers ───────────────────────────────────────────────

    async def _post_to_error_channels(self, embed: discord.Embed) -> None:
        """Post an embed to every configured guild error channel (no-op if notify disabled)."""
        if not self.notify_enabled:
            return
        for guild in self.bot.guilds:
            try:
                settings = await self.bot.db.get_guild_settings(guild.id)
                ch_id = settings.get("error_channel")
                if not ch_id:
                    continue
                ch = guild.get_channel_or_thread(ch_id)
                if ch is None:
                    continue
                if isinstance(ch, discord.Thread) and ch.archived:
                    await ch.edit(archived=False)
                await ch.send(embed=embed)
            except Exception:
                pass

    async def _dm_dev(self, content: str) -> None:
        """DM the configured dev user (REPORT_TARGET_USER_ID). No-op if notify disabled."""
        if not self.notify_enabled:
            return
        try:
            from core.config import Config
            uid = getattr(Config, "REPORT_TARGET_USER_ID", 0)
            if not uid:
                return
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(content)
        except Exception:
            pass

    # ── Heartbeat staleness checks ─────────────────────────────────────────

    def _check_heartbeats(self) -> None:
        """Warn about stale heartbeats  -  tasks that stopped pulsing.

        Uses the core/framework/heartbeat.py registry that background tasks pulse
        on every successful iteration.
        """
        stale = heartbeat.stale_tasks(max_age=300)
        if stale:
            log.warning(
                "Stale heartbeat tasks detected (%d): %s",
                len(stale),
                ", ".join(stale),
            )

    # ── Status snapshot (used by .heal command) ────────────────────────────

    def status(self) -> dict:
        """Return a snapshot of current self-heal state."""
        bus = getattr(self.bot, "bus", None)
        redis_ok = bool(bus and bus.is_connected)

        failed_loops: list[str] = []
        for cog_name, cog in list(self.bot.cogs.items()):
            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name, None)
                    if not isinstance(attr, _tasks.Loop):
                        continue
                    if getattr(attr, "count", None) == 1:
                        continue
                    failed = attr.failed() if callable(attr.failed) else attr.failed
                    running = attr.is_running() if callable(attr.is_running) else attr.is_running
                    if failed or not running:
                        failed_loops.append(f"{cog_name}.{attr_name}")
                except Exception:
                    pass

        return {
            "redis_connected": redis_ok,
            "redis_retry_attempt": self._redis_retry_attempt,
            "failed_loops": failed_loops,
            "degraded_loops": sorted(self._degraded_loops),
            "stale_heartbeats": heartbeat.stale_tasks(max_age=300),
            "scheduler_running": bool(self._task and not self._task.done()),
            "uptime_seconds": self.uptime_seconds,
            "notify_enabled": self.notify_enabled,
        }
