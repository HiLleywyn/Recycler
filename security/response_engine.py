"""
security/response_engine.py  -  Graduated enforcement engine.

Translates a ResponseLevel into concrete enforcement actions, persists them
to both Redis (for fast runtime checks) and PostgreSQL (for audit trail),
and publishes events so the bot cog and dashboard can react.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from security.config import (
    THROTTLE_DURATION,
    FREEZE_DURATION,
    FLAG_DURATION,
    LOCKDOWN_DURATION,
)
from security.models import (
    SecurityEvent,
    ThreatDetection,
    ResponseLevel,
    EnforcementAction,
    EnforcementRecord,
)
from security.redis_cache import SecurityRedisCache

log = logging.getLogger("discoin.security.response")

# Map event types to enforcement scopes
_SCOPE_MAP = {
    "trade": "trade",
    "transfer": "transfer",
    "gamble": "gamble",
    "earn": "earn",
    "pool": "pool",
    "loan": "loan",
    "mine": "mine",
    "stake": "stake",
    # Detections that affect everything
    "api_abuse": "all",
    "privilege_escalation": "all",
    "session_anomaly": "all",
    "cross_platform_abuse": "all",
}


class ResponseEngine:
    """Determines and executes enforcement actions based on threat level."""

    def __init__(self, cache: SecurityRedisCache, db=None, bus=None) -> None:
        self.cache = cache
        self.db = db       # SecurityRepository (injected)
        self.bus = bus     # RedisBus (injected, optional)

    def determine_action(
        self,
        event: SecurityEvent,
        detections: list[ThreatDetection],
        response_level: ResponseLevel,
    ) -> tuple[EnforcementAction | None, str, int | None]:
        """Determine what enforcement action to take.

        Returns (action_type, scope, duration_seconds) or (None, "", None).
        """
        if response_level == ResponseLevel.NONE or response_level == ResponseLevel.LOG:
            return None, "", None

        # Determine scope from the highest-severity detection
        scope = self._determine_scope(event, detections)

        if response_level == ResponseLevel.THROTTLE:
            return EnforcementAction.THROTTLE, scope, THROTTLE_DURATION

        if response_level == ResponseLevel.FREEZE:
            return EnforcementAction.FREEZE, scope, FREEZE_DURATION

        if response_level == ResponseLevel.FLAG:
            return EnforcementAction.FLAG, "all", FLAG_DURATION

        if response_level == ResponseLevel.LOCKDOWN:
            return EnforcementAction.LOCKDOWN, "all", LOCKDOWN_DURATION

        return None, "", None

    def _determine_scope(
        self, event: SecurityEvent, detections: list[ThreatDetection],
    ) -> str:
        """Determine what scope to restrict based on detections."""
        if not detections:
            return _SCOPE_MAP.get(event.event_type, "all")

        # Use the highest-severity detection to determine scope
        highest = max(detections, key=lambda d: d.score_delta)
        scope = _SCOPE_MAP.get(highest.detector, None)
        if scope:
            return scope

        # Fall back to event type mapping
        return _SCOPE_MAP.get(event.event_type, "all")

    async def execute(
        self,
        event: SecurityEvent,
        detections: list[ThreatDetection],
        response_level: ResponseLevel,
        new_score: float,
    ) -> EnforcementRecord | None:
        """Execute the enforcement action. Returns the record if action was taken."""

        action_type, scope, duration = self.determine_action(
            event, detections, response_level,
        )

        # Level 1 (LOG): just log the event, no enforcement
        if response_level == ResponseLevel.LOG:
            await self._log_event(event, detections, response_level, new_score)
            return None

        if action_type is None:
            return None

        # Check for existing enforcement  -  don't stack
        existing = await self.cache.get_enforcement(event.guild_id, event.user_id)
        if existing:
            existing_action = existing.get("action_type", "")
            # Only escalate, never downgrade
            action_priority = {
                "throttle": 1, "freeze": 2, "flag": 3, "lockdown": 4, "ban": 5,
            }
            if action_priority.get(existing_action, 0) >= action_priority.get(action_type.value, 0):
                log.debug(
                    "Skipping enforcement  -  existing %s >= proposed %s for user %d",
                    existing_action, action_type.value, event.user_id,
                )
                return None

        # Build enforcement record
        now = time.time()
        expires_at = now + duration if duration else None

        reason = self._build_reason(detections, response_level, new_score)

        record = EnforcementRecord(
            guild_id=event.guild_id,
            user_id=event.user_id,
            action_type=action_type,
            scope=scope,
            reason=reason,
            enacted_by="auto",
            expires_at=expires_at,
            details={
                "detections": [d.model_dump(mode="json") for d in detections],
                "response_level": response_level.value,
                "threat_score": new_score,
                "trigger_event": event.model_dump(mode="json"),
            },
        )

        # Store in Redis for fast runtime checks
        await self.cache.set_enforcement(
            event.guild_id,
            event.user_id,
            record.model_dump(mode="json"),
        )

        # Persist to PostgreSQL
        if self.db is not None:
            try:
                record_id = await self.db.create_enforcement(record)
                record.id = record_id
            except Exception as exc:
                log.error("Failed to persist enforcement to DB: %s", exc)

        # Log the event
        await self._log_event(event, detections, response_level, new_score)

        # Publish enforcement event to bus
        await self._publish_enforcement(event, record, detections)

        log.warning(
            "ENFORCEMENT: %s scope=%s user=%d guild=%d score=%.1f duration=%ss reason=%s",
            action_type.value, scope, event.user_id, event.guild_id,
            new_score, duration, reason[:100],
        )

        return record

    async def lift_enforcement(
        self,
        guild_id: int,
        user_id: int,
        lifted_by: str,
    ) -> bool:
        """Admin action: lift an active enforcement."""
        enforcement = await self.cache.get_enforcement(guild_id, user_id)
        if not enforcement:
            return False

        await self.cache.clear_enforcement(guild_id, user_id)

        # Update DB record
        if self.db is not None:
            try:
                await self.db.lift_enforcement(guild_id, user_id, lifted_by)
            except Exception as exc:
                log.error("Failed to lift enforcement in DB: %s", exc)

        # Publish lift event
        if self.bus is not None:
            try:
                await self.bus.publish(
                    "security_enforcement",
                    guild_id=guild_id,
                    user_id=user_id,
                    action="lifted",
                    lifted_by=lifted_by,
                )
            except Exception:
                pass

        log.info(
            "Enforcement lifted: user=%d guild=%d by=%s",
            user_id, guild_id, lifted_by,
        )
        return True

    def _build_reason(
        self,
        detections: list[ThreatDetection],
        response_level: ResponseLevel,
        score: float,
    ) -> str:
        """Build a human-readable reason string."""
        detection_summary = "; ".join(
            f"{d.detector}: {d.description}" for d in detections[:3]
        )
        return (
            f"Level {response_level.value} response (score: {score:.1f}). "
            f"Detections: {detection_summary}"
        )

    async def _log_event(
        self,
        event: SecurityEvent,
        detections: list[ThreatDetection],
        response_level: ResponseLevel,
        score: float,
    ) -> None:
        """Log security events to the database."""
        if self.db is None:
            return
        for detection in detections:
            try:
                await self.db.create_security_event(
                    guild_id=event.guild_id,
                    user_id=event.user_id,
                    event_type=detection.detector,
                    severity=detection.severity.value,
                    score_delta=detection.score_delta,
                    details={
                        **detection.details,
                        "description": detection.description,
                        "response_level": response_level.value,
                        "threat_score": score,
                    },
                    source=event.source.value,
                )
            except Exception as exc:
                log.error("Failed to log security event: %s", exc)

    async def _publish_enforcement(
        self,
        event: SecurityEvent,
        record: EnforcementRecord,
        detections: list[ThreatDetection],
    ) -> None:
        """Publish enforcement event to the Redis bus."""
        if self.bus is None:
            return

        # Deduplication check
        alert_hash = hashlib.sha256(
            f"{event.guild_id}:{event.user_id}:{record.action_type.value}:{record.scope}".encode()
        ).hexdigest()[:16]

        if await self.cache.is_alert_duplicate(alert_hash):
            return
        await self.cache.mark_alert_sent(alert_hash)

        try:
            await self.bus.publish(
                "security_enforcement",
                guild_id=event.guild_id,
                user_id=event.user_id,
                action=record.action_type.value,
                scope=record.scope,
                reason=record.reason,
                expires_at=record.expires_at,
                threat_score=record.details.get("threat_score", 0),
                detections=[
                    {"detector": d.detector, "severity": d.severity.value, "description": d.description}
                    for d in detections
                ],
            )
        except Exception as exc:
            log.error("Failed to publish enforcement event: %s", exc)

        # Also publish a security_alert for the dashboard
        try:
            await self.bus.publish(
                "security_alert",
                guild_id=event.guild_id,
                user_id=event.user_id,
                alerts=[d.description for d in detections],
                flag_count=len(detections),
                response_level=record.action_type.value,
            )
        except Exception:
            pass


class CircuitBreaker:
    """Guild-wide feature circuit breaker.

    When a Level 5 (LOCKDOWN) response triggers, the circuit breaker halts
    the specified feature for the entire guild.
    """

    def __init__(self, cache: SecurityRedisCache) -> None:
        self.cache = cache

    async def trip(self, guild_id: int, feature: str, reason: str, duration: int = LOCKDOWN_DURATION) -> None:
        """Trip the circuit breaker for a feature."""
        await self.cache.set_circuit_breaker(
            guild_id, feature,
            {
                "tripped": True,
                "reason": reason,
                "tripped_at": time.time(),
                "expires_at": time.time() + duration,
            },
            ttl=duration,
        )
        log.warning(
            "CIRCUIT BREAKER TRIPPED: guild=%d feature=%s duration=%ds reason=%s",
            guild_id, feature, duration, reason,
        )

    async def is_tripped(self, guild_id: int, feature: str) -> bool:
        """Check if a feature's circuit breaker is tripped."""
        data = await self.cache.get_circuit_breaker(guild_id, feature)
        if data is None:
            return False
        expires_at = data.get("expires_at", 0)
        if time.time() > expires_at:
            await self.cache.clear_circuit_breaker(guild_id, feature)
            return False
        return data.get("tripped", False)

    async def reset(self, guild_id: int, feature: str) -> None:
        """Manually reset a circuit breaker."""
        await self.cache.clear_circuit_breaker(guild_id, feature)
        log.info("Circuit breaker reset: guild=%d feature=%s", guild_id, feature)

    async def get_status(self, guild_id: int, feature: str) -> dict[str, Any] | None:
        """Get circuit breaker status."""
        return await self.cache.get_circuit_breaker(guild_id, feature)
