"""
security/engine.py  -  Central Security Engine orchestrator.

The SecurityEngine is the single entry point for all security processing.
It coordinates detection, scoring, profiling, correlation, and response
into a unified pipeline.  One instance runs per process (shared between
bot cogs and API middleware).
"""
from __future__ import annotations

import asyncio
import logging
import time

from security.behavior_profile import BehaviorProfiler
from security.config import LOOKBACK_SECONDS
from security.correlation import CrossPlatformCorrelator
from security.detectors import ThreatDetectors
from security.models import (
    SecurityEvent,
    SecurityVerdict,
    ResponseLevel,
    EnforcementAction,
    SecurityHealth,
)
from security.redis_cache import SecurityRedisCache
from security.response_engine import ResponseEngine, CircuitBreaker
from security.threat_scorer import ThreatScorer

log = logging.getLogger("discoin.security.engine")


class SecurityEngine:
    """Central orchestrator for the Discoin security system.

    Usage::

        engine = SecurityEngine(redis=redis_client, db=db_instance)
        await engine.start()

        verdict = await engine.process_event(SecurityEvent(...))
        if verdict.blocked:
            # deny the action
            ...

        await engine.stop()
    """

    def __init__(self, redis=None, db=None, bus=None) -> None:
        self.cache = SecurityRedisCache(redis)
        self.detectors = ThreatDetectors(self.cache)
        self.scorer = ThreatScorer(self.cache)
        self.profiler = BehaviorProfiler(self.cache)
        self.correlator = CrossPlatformCorrelator(self.cache)
        self.circuit_breaker = CircuitBreaker(self.cache)

        # DB and bus are injected; may be None during tests
        self._db = db
        self._bus = bus
        self.responder = ResponseEngine(self.cache, db, bus)

        # Metrics
        self._started_at: float = 0.0
        self._events_processed: int = 0
        self._last_scan_ts: float = 0.0
        self._running: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the engine.  Call once at startup."""
        self._started_at = time.time()
        self._running = True
        log.info("Security engine started")

    async def startup_clear_locks(self) -> None:
        """One-time startup routine: lift all existing security locks.

        Uses a Redis/memory flag so this runs exactly once per deployment.
        Clears both the Redis enforcement cache and the database records.
        """
        already_done = await self.cache.get("startup_clear_done")
        if already_done:
            return

        log.info("One-time startup lock clear: lifting all active enforcements")

        cleared_cache = await self.cache.clear_all_enforcements()

        cleared_db = 0
        if self._db is not None:
            try:
                cleared_db = await self._db.lift_all_enforcements("startup")
            except Exception as exc:
                log.error("Failed to lift DB enforcements on startup: %s", exc)

        log.info(
            "Startup lock clear complete  -  cache: %d, DB: %d enforcements removed",
            cleared_cache,
            cleared_db,
        )

        # Mark done for 30 days so normal restarts don't re-clear
        await self.cache.set("startup_clear_done", True, ttl=86400 * 30)

    async def stop(self) -> None:
        """Shut down the engine."""
        self._running = False
        log.info(
            "Security engine stopped  -  processed %d events in %.0fs",
            self._events_processed,
            time.time() - self._started_at,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Main Pipeline ────────────────────────────────────────────────────────

    async def process_event(
        self,
        event: SecurityEvent,
        *,
        pre_checked: bool = False,
    ) -> SecurityVerdict:
        """Process a single security event through the full pipeline.

        1. Check existing enforcement (pre-check)
        2. Update behavior profile
        3. Record event + get correlation data
        4. Run all applicable detectors
        5. Update threat score
        6. Determine response level
        7. Execute enforcement (if any)
        8. Return verdict

        This is the primary entry point  -  called from both bot cog and API middleware.

        If *pre_checked* is True the caller already verified enforcement and
        circuit-breaker state (e.g. bot_check), so we skip the duplicate Redis
        lookups.
        """
        self._events_processed += 1

        # Hierarchy level 1  -  bot developer and designated-exempt users are never
        # profiled or scored; return a clean verdict immediately.
        if event.details.get("is_developer") or event.details.get("is_owner") or event.details.get("is_exempt"):
            return SecurityVerdict(
                event=event,
                blocked=False,
                response_level=ResponseLevel.NONE,
            )

        scope = self._event_to_scope(event)

        # 0. Pre-check: is user already under enforcement that blocks this action?
        # Skip when the caller (bot_check) already did these Redis lookups.
        if not pre_checked:
            existing_enforcement = await self.cache.check_enforcement(
                event.guild_id, event.user_id, scope,
            )
            if existing_enforcement:
                action_type = existing_enforcement.get("action_type", "")
                if action_type in ("freeze", "flag", "lockdown", "ban"):
                    return SecurityVerdict(
                        event=event,
                        blocked=True,
                        response_level=ResponseLevel.FREEZE,
                        enforcement_action=EnforcementAction(action_type),
                        enforcement_scope=existing_enforcement.get("scope", "all"),
                    )

            # 0b. Check circuit breaker for guild-wide halts
            if await self.circuit_breaker.is_tripped(event.guild_id, scope):
                return SecurityVerdict(
                    event=event,
                    blocked=True,
                    response_level=ResponseLevel.LOCKDOWN,
                    enforcement_action=EnforcementAction.LOCKDOWN,
                    enforcement_scope=scope,
                )

        # 1-2. Fetch profile, record event, and get correlation in parallel
        #      (these are independent Redis operations).
        event_data = event.model_dump(mode="json")
        profile_task = self.profiler.update_from_event(event)
        record_task = self.cache.record_event(event.guild_id, event.user_id, event_data)
        correlation_task = self.correlator.record_and_get(event)

        profile, _, correlation = await asyncio.gather(
            profile_task, record_task, correlation_task,
        )

        # 3. Get recent events for detectors
        recent_events = await self.cache.get_recent_events(
            event.guild_id, event.user_id, window_seconds=LOOKBACK_SECONDS,
        )

        # 4. Run all detectors
        detections = await self.detectors.run_all(
            event=event,
            recent_events=recent_events,
            profile=profile.model_dump(mode="json"),
            correlation=correlation,
        )

        # 4b. Check behavior anomalies (add as low-severity detections)
        anomalies = self.profiler.check_anomalies(event, profile)
        for anomaly in anomalies:
            from security.models import ThreatDetection, Severity
            detections.append(ThreatDetection(
                detector="behavior_anomaly",
                severity=Severity.LOW,
                score_delta=5.0,
                description=anomaly.get("description", "Behavioral anomaly detected"),
                details=anomaly,
            ))

        # 5. Update threat score
        previous_score, new_score = await self.scorer.add_detections(
            event.guild_id, event.user_id, detections,
        )

        # 6. Determine response level
        response_level = self.scorer.determine_response_level(new_score)

        # 7. Update risk level on profile
        await self.profiler.update_risk_level(profile, new_score)

        # 8. Execute enforcement
        enforcement_record = None
        if detections and response_level.value >= ResponseLevel.THROTTLE.value:
            enforcement_record = await self.responder.execute(
                event, detections, response_level, new_score,
            )

            # Level 5: also trip circuit breaker
            if response_level == ResponseLevel.LOCKDOWN:
                reason = f"Threat score {new_score:.1f} from user {event.user_id}"
                await self.circuit_breaker.trip(event.guild_id, scope, reason)
        elif detections and response_level == ResponseLevel.LOG:
            # Still log at Level 1
            await self.responder.execute(event, detections, response_level, new_score)

        # Build verdict
        verdict = SecurityVerdict(
            event=event,
            detections=detections,
            previous_score=previous_score,
            new_score=new_score,
            response_level=response_level,
            blocked=False,
        )

        if enforcement_record:
            verdict.enforcement_action = enforcement_record.action_type
            verdict.enforcement_scope = enforcement_record.scope
            verdict.enforcement_duration = (
                int(enforcement_record.expires_at - time.time())
                if enforcement_record.expires_at else None
            )
            # Freeze/Flag/Lockdown blocks the triggering action
            if enforcement_record.action_type in (
                EnforcementAction.FREEZE,
                EnforcementAction.FLAG,
                EnforcementAction.LOCKDOWN,
                EnforcementAction.BAN,
            ):
                verdict.blocked = True

        return verdict

    # ── Quick Checks (for middleware hot-path) ───────────────────────────────

    async def check_user_allowed(
        self,
        guild_id: int,
        user_id: int,
        scope: str = "all",
        is_developer: bool = False,
        is_exempt: bool = False,
    ) -> tuple[bool, str]:
        """Fast check: is the user allowed to perform an action in the given scope?

        Returns (allowed, reason).  Does NOT process a full event  -  just checks
        existing enforcement and circuit breaker state.

        Hierarchy level 1 (bot developer) and designated exemptions always return allowed.
        """
        # Hierarchy level 1  -  bot developer always allowed
        if is_developer:
            return True, ""

        # Owner-designated exempt users also bypass enforcement
        if is_exempt:
            return True, ""

        # Check user enforcement
        enforcement = await self.cache.check_enforcement(guild_id, user_id, scope)
        if enforcement:
            action_type = enforcement.get("action_type", "")
            if action_type in ("freeze", "flag", "lockdown", "ban"):
                return False, enforcement.get("reason", "Account restricted")
            if action_type == "throttle":
                # Throttle doesn't block, but callers may want to reduce rate limits
                return True, f"throttled:{LOOKBACK_SECONDS}"

        # Check circuit breaker
        if await self.circuit_breaker.is_tripped(guild_id, scope):
            return False, f"Feature '{scope}' is temporarily halted for this server"

        return True, ""

    async def is_security_exempt(
        self,
        guild_id: int,
        user_id: int,
        role_ids: list[int] | None = None,
    ) -> bool:
        """Check if a user or any of their roles is in the owner-granted exemption list."""
        if self._db is None:
            return False
        try:
            return await self._db.is_exempt(guild_id, user_id, role_ids or [])
        except Exception:
            return False

    async def get_threat_score(self, guild_id: int, user_id: int) -> float:
        """Get a user's current threat score (with decay applied)."""
        return await self.scorer.get_current_score(guild_id, user_id)

    # ── Admin Actions ────────────────────────────────────────────────────────

    async def admin_freeze(
        self,
        guild_id: int,
        user_id: int,
        scope: str,
        reason: str,
        admin_id: int,
        duration: int | None = None,
    ) -> None:
        """Admin action: manually freeze a user."""
        from security.models import EnforcementRecord

        record = EnforcementRecord(
            guild_id=guild_id,
            user_id=user_id,
            action_type=EnforcementAction.FREEZE,
            scope=scope,
            reason=reason,
            enacted_by=str(admin_id),
            expires_at=time.time() + (duration or LOOKBACK_SECONDS * 6),
        )

        await self.cache.set_enforcement(guild_id, user_id, record.model_dump(mode="json"))

        if self._db:
            try:
                await self._db.create_enforcement(record)
                await self._db.create_security_audit(
                    guild_id=guild_id,
                    admin_id=admin_id,
                    action="freeze",
                    target_user=user_id,
                    details={"scope": scope, "reason": reason, "duration": duration},
                )
            except Exception as exc:
                log.error("Failed to persist admin freeze: %s", exc)

    async def admin_unfreeze(self, guild_id: int, user_id: int, admin_id: int) -> bool:
        """Admin action: lift enforcement on a user."""
        success = await self.responder.lift_enforcement(guild_id, user_id, str(admin_id))
        if success and self._db:
            try:
                await self._db.create_security_audit(
                    guild_id=guild_id,
                    admin_id=admin_id,
                    action="unfreeze",
                    target_user=user_id,
                    details={},
                )
            except Exception:
                pass
        return success

    async def admin_clear_score(self, guild_id: int, user_id: int, admin_id: int) -> None:
        """Admin action: reset a user's threat score."""
        await self.scorer.reset_score(guild_id, user_id)
        if self._db:
            try:
                await self._db.create_security_audit(
                    guild_id=guild_id,
                    admin_id=admin_id,
                    action="clear_score",
                    target_user=user_id,
                    details={},
                )
            except Exception:
                pass

    async def admin_lockdown(
        self, guild_id: int, feature: str, reason: str, admin_id: int, duration: int = 1800,
    ) -> None:
        """Admin action: trip a circuit breaker for a feature."""
        await self.circuit_breaker.trip(guild_id, feature, reason, duration)
        if self._db:
            try:
                await self._db.create_security_audit(
                    guild_id=guild_id,
                    admin_id=admin_id,
                    action="lockdown",
                    target_user=None,
                    details={"feature": feature, "reason": reason, "duration": duration},
                )
            except Exception:
                pass

    async def admin_lift_lockdown(self, guild_id: int, feature: str, admin_id: int) -> None:
        """Admin action: reset a circuit breaker."""
        await self.circuit_breaker.reset(guild_id, feature)
        if self._db:
            try:
                await self._db.create_security_audit(
                    guild_id=guild_id,
                    admin_id=admin_id,
                    action="lift_lockdown",
                    target_user=None,
                    details={"feature": feature},
                )
            except Exception:
                pass

    # ── Health / Metrics ─────────────────────────────────────────────────────

    def get_health(self) -> SecurityHealth:
        """Return current engine health metrics."""
        return SecurityHealth(
            engine_running=self._running,
            redis_connected=self.cache.is_connected,
            db_connected=self._db is not None,
            detectors_active=12,  # number of detector categories
            events_processed_total=self._events_processed,
            last_scan_ts=self._last_scan_ts or None,
            uptime_seconds=time.time() - self._started_at if self._started_at else 0,
        )

    def update_scan_ts(self) -> None:
        """Called by the periodic scanner to update last scan timestamp."""
        self._last_scan_ts = time.time()

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _event_to_scope(event: SecurityEvent) -> str:
        """Map an event to an enforcement scope."""
        mapping = {
            "trade": "trade",
            "transfer": "transfer",
            "gamble": "gamble",
            "earn": "earn",
            "pool": "pool",
            "loan": "loan",
            "mine": "mine",
            "stake": "stake",
            "command": "all",
            "api_request": "all",
            "auth_attempt": "all",
            "auth_failure": "all",
        }
        return mapping.get(event.event_type, "all")
