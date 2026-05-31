"""
Tests for the Discoin Security System.

Tests the core engine pipeline: detectors, scoring, profiling, and enforcement.
All tests run without Redis or PostgreSQL (in-memory fallback).
"""
from __future__ import annotations

import time

import pytest

from security.config import (
    INCOME_VELOCITY_LIMIT,
    GAMBLING_VELOCITY_LIMIT,
    WASH_TRADE_MIN_CYCLES,
    TRANSFER_RING_MIN,
    TX_FLOOD_LIMIT,
    COMMAND_FLOOD_LIMIT,
)
from security.models import (
    SecurityEvent,
    EventSource,
    ThreatDetection,
    Severity,
    ResponseLevel,
    BehaviorBaseline,
)
from security.redis_cache import SecurityRedisCache
from security.detectors import ThreatDetectors
from security.threat_scorer import ThreatScorer
from security.behavior_profile import BehaviorProfiler
from security.response_engine import CircuitBreaker
from security.engine import SecurityEngine


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cache():
    """In-memory-only SecurityRedisCache (no Redis)."""
    return SecurityRedisCache(redis=None)


@pytest.fixture
def detectors(cache):
    return ThreatDetectors(cache)


@pytest.fixture
def scorer(cache):
    return ThreatScorer(cache)


@pytest.fixture
def profiler(cache):
    return BehaviorProfiler(cache)


@pytest.fixture
def engine():
    """SecurityEngine with in-memory fallback (no Redis, no DB)."""
    return SecurityEngine(redis=None, db=None, bus=None)


def _make_event(**kwargs) -> SecurityEvent:
    """Helper to create a SecurityEvent with defaults."""
    defaults = {
        "guild_id": 12345,
        "user_id": 67890,
        "event_type": "trade",
        "source": EventSource.BOT,
    }
    defaults.update(kwargs)
    return SecurityEvent(**defaults)


def _make_recent_events(tx_type: str, count: int, **extra) -> list[dict]:
    """Generate a list of recent event dicts."""
    now = time.time()
    events = []
    for i in range(count):
        event = {
            "tx_type": tx_type,
            "event_type": extra.get("event_type", "trade"),
            "timestamp": now - i,
            "amount_usd": extra.get("amount_usd", 100.0),
            "symbol": extra.get("symbol", "SUN"),
            "symbol_in": extra.get("symbol_in", ""),
            "symbol_out": extra.get("symbol_out", ""),
        }
        event.update(extra)
        events.append(event)
    return events


# ── Redis Cache Tests ────────────────────────────────────────────────────────

class TestRedisCache:
    @pytest.mark.asyncio
    async def test_get_set_memory_fallback(self, cache: SecurityRedisCache):
        await cache.set("test", "key1", {"value": 42}, ttl=60)
        result = await cache.get("test", "key1")
        assert result == {"value": 42}

    @pytest.mark.asyncio
    async def test_get_missing(self, cache: SecurityRedisCache):
        result = await cache.get("nonexistent", "key")
        assert result is None

    @pytest.mark.asyncio
    async def test_incr(self, cache: SecurityRedisCache):
        result1 = await cache.incr("counter", "test", ttl=60)
        assert result1 == 1
        result2 = await cache.incr("counter", "test", ttl=60)
        assert result2 == 2

    @pytest.mark.asyncio
    async def test_delete(self, cache: SecurityRedisCache):
        await cache.set("test", "del", "value", ttl=60)
        await cache.delete("test", "del")
        assert await cache.get("test", "del") is None

    @pytest.mark.asyncio
    async def test_threat_score(self, cache: SecurityRedisCache):
        await cache.set_threat_score(1, 2, 45.5)
        score = await cache.get_threat_score(1, 2)
        assert score == 45.5

    @pytest.mark.asyncio
    async def test_enforcement(self, cache: SecurityRedisCache):
        await cache.set_enforcement(1, 2, {
            "action_type": "freeze",
            "scope": "trade",
            "expires_at": time.time() + 3600,
        })
        result = await cache.get_enforcement(1, 2)
        assert result is not None
        assert result["action_type"] == "freeze"

    @pytest.mark.asyncio
    async def test_enforcement_check_scope(self, cache: SecurityRedisCache):
        await cache.set_enforcement(1, 2, {
            "action_type": "freeze",
            "scope": "trade",
            "expires_at": time.time() + 3600,
        })
        # Matching scope
        assert await cache.check_enforcement(1, 2, "trade") is not None
        # Non-matching scope
        assert await cache.check_enforcement(1, 2, "gamble") is None
        # "all" scope always matches
        await cache.set_enforcement(1, 3, {
            "action_type": "freeze",
            "scope": "all",
            "expires_at": time.time() + 3600,
        })
        assert await cache.check_enforcement(1, 3, "gamble") is not None

    @pytest.mark.asyncio
    async def test_sorted_set(self, cache: SecurityRedisCache):
        now = time.time()
        await cache.zadd(("events", 1, 2), score=now - 10, member="event1")
        await cache.zadd(("events", 1, 2), score=now - 5, member="event2")
        await cache.zadd(("events", 1, 2), score=now, member="event3")

        results = await cache.zrangebyscore(("events", 1, 2), now - 15, now)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_correlation(self, cache: SecurityRedisCache):
        data = await cache.update_correlation(1, 2, "bot")
        assert data["bot_events"] == 1
        assert data["api_events"] == 0

        data = await cache.update_correlation(1, 2, "api")
        assert data["bot_events"] == 1
        assert data["api_events"] == 1


# ── Detector Tests ───────────────────────────────────────────────────────────

class TestDetectors:
    @pytest.mark.asyncio
    async def test_income_velocity_below_threshold(self, detectors: ThreatDetectors):
        events = _make_recent_events("WORK", INCOME_VELOCITY_LIMIT - 1)
        results = detectors._detect_income_velocity(events)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_income_velocity_above_threshold(self, detectors: ThreatDetectors):
        events = _make_recent_events("WORK", INCOME_VELOCITY_LIMIT + 5)
        results = detectors._detect_income_velocity(events)
        assert len(results) == 1
        assert results[0].detector == "income_velocity"
        assert results[0].severity in (Severity.MEDIUM, Severity.HIGH)

    @pytest.mark.asyncio
    async def test_gambling_abuse_detected(self, detectors: ThreatDetectors):
        events = _make_recent_events("GAMBLE_SLOTS", GAMBLING_VELOCITY_LIMIT + 10)
        results = detectors._detect_gambling_abuse(events)
        assert len(results) == 1
        assert results[0].detector == "gambling_abuse"

    @pytest.mark.asyncio
    async def test_wash_trading_detected(self, detectors: ThreatDetectors):
        events = []
        now = time.time()
        for i in range(WASH_TRADE_MIN_CYCLES):
            events.append({"tx_type": "BUY", "symbol": "SUN", "symbol_in": "", "symbol_out": "SUN", "timestamp": now - i, "amount_usd": 100})
            events.append({"tx_type": "SELL", "symbol": "SUN", "symbol_in": "SUN", "symbol_out": "", "timestamp": now - i - 0.5, "amount_usd": 100})
        results = detectors._detect_wash_trading(events)
        assert len(results) >= 1
        assert results[0].detector == "wash_trading"

    @pytest.mark.asyncio
    async def test_transfer_rings_detected(self, detectors: ThreatDetectors):
        events = _make_recent_events("TRANSFER", TRANSFER_RING_MIN + 3)
        results = detectors._detect_transfer_rings(events)
        assert len(results) == 1
        assert results[0].detector == "transfer_rings"

    @pytest.mark.asyncio
    async def test_lp_manipulation_detected(self, detectors: ThreatDetectors):
        events = []
        now = time.time()
        for i in range(3):
            events.append({"tx_type": "ADD_LP", "event_type": "pool", "timestamp": now - i, "amount_usd": 1000})
            events.append({"tx_type": "REMOVE_LP", "event_type": "pool", "timestamp": now - i - 0.5, "amount_usd": 1000})
        results = detectors._detect_lp_manipulation(events)
        assert len(results) == 1
        assert results[0].detector == "lp_manipulation"

    @pytest.mark.asyncio
    async def test_tx_flood_detected(self, detectors: ThreatDetectors):
        events = _make_recent_events("BUY", TX_FLOOD_LIMIT + 10)
        results = detectors._detect_tx_flood(events)
        assert len(results) == 1
        assert results[0].detector == "tx_flood"

    @pytest.mark.asyncio
    async def test_defi_exploit_flash_loan(self, detectors: ThreatDetectors):
        now = time.time()
        events = [
            {"tx_type": "LOAN_BORROW", "timestamp": now, "amount_usd": 100000, "symbol": "USD"},
            {"tx_type": "BUY", "timestamp": now + 5, "amount_usd": 100000, "symbol": "SUN"},
            {"tx_type": "LOAN_REPAY", "timestamp": now + 10, "amount_usd": 100000, "symbol": "USD"},
        ]
        results = detectors._detect_defi_exploit(events)
        assert any(d.details.get("pattern") == "flash_loan" for d in results)

    @pytest.mark.asyncio
    async def test_command_flood(self, detectors: ThreatDetectors):
        event = _make_event(command="trade", source=EventSource.BOT)
        # Simulate many commands
        for _ in range(COMMAND_FLOOD_LIMIT + 5):
            await detectors.cache.incr("cmd", 12345, 67890, int(time.time()) // 60, ttl=120)
        results = await detectors._detect_command_flood(event)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_cross_platform_no_detection_single_platform(self, detectors: ThreatDetectors):
        correlation = {"bot_events": 15, "api_events": 0, "last_bot_ts": 0, "last_api_ts": 0}
        event = _make_event()
        results = detectors._detect_cross_platform_abuse(event, correlation)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_cross_platform_detection(self, detectors: ThreatDetectors):
        correlation = {"bot_events": 8, "api_events": 7, "last_bot_ts": 0, "last_api_ts": 0}
        event = _make_event()
        results = detectors._detect_cross_platform_abuse(event, correlation)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_privilege_escalation_admin_endpoint(self, detectors: ThreatDetectors):
        event = _make_event(
            endpoint="/api/v2/admin/settings",
            details={"is_admin": False},
        )
        results = detectors._detect_privilege_escalation(event)
        assert len(results) == 1
        assert results[0].severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_transaction_integrity_duplicate(self, detectors: ThreatDetectors):
        now = time.time()
        event = _make_event(amount_usd=500.0, tx_type="TRANSFER")
        event.timestamp = now
        recent = [
            {"tx_type": "TRANSFER", "amount_usd": 500.0, "timestamp": now - 0.5},
            {"tx_type": "TRANSFER", "amount_usd": 500.0, "timestamp": now - 0.3},
            {"tx_type": "TRANSFER", "amount_usd": 500.0, "timestamp": now - 0.1},
        ]
        results = detectors._detect_transaction_integrity(event, recent)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_negative_amount_detection(self, detectors: ThreatDetectors):
        event = _make_event(amount_usd=-1000.0, tx_type="TRANSFER")
        results = detectors._detect_transaction_integrity(event, [])
        assert len(results) == 1
        assert "negative" in results[0].description.lower()


# ── Threat Scorer Tests ──────────────────────────────────────────────────────

class TestThreatScorer:
    @pytest.mark.asyncio
    async def test_initial_score_is_zero(self, scorer: ThreatScorer):
        score = await scorer.get_current_score(1, 2)
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_add_detections(self, scorer: ThreatScorer):
        detections = [
            ThreatDetection(
                detector="test", severity=Severity.MEDIUM,
                score_delta=15.0, description="Test detection",
            ),
        ]
        prev, new = await scorer.add_detections(1, 2, detections)
        assert prev == 0.0
        assert new == 15.0

    @pytest.mark.asyncio
    async def test_score_accumulates(self, scorer: ThreatScorer):
        d1 = [ThreatDetection(detector="a", severity=Severity.LOW, score_delta=10, description="A")]
        d2 = [ThreatDetection(detector="b", severity=Severity.LOW, score_delta=20, description="B")]
        await scorer.add_detections(1, 2, d1)
        _, new = await scorer.add_detections(1, 2, d2)
        assert new == pytest.approx(30.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_score_capped_at_100(self, scorer: ThreatScorer):
        detections = [
            ThreatDetection(detector="big", severity=Severity.CRITICAL, score_delta=150, description="Big"),
        ]
        _, new = await scorer.add_detections(1, 2, detections)
        assert new == 100.0

    def test_response_levels(self, scorer: ThreatScorer):
        assert scorer.determine_response_level(0) == ResponseLevel.NONE
        assert scorer.determine_response_level(10) == ResponseLevel.NONE
        assert scorer.determine_response_level(25) == ResponseLevel.LOG
        assert scorer.determine_response_level(45) == ResponseLevel.THROTTLE
        assert scorer.determine_response_level(65) == ResponseLevel.FREEZE
        assert scorer.determine_response_level(85) == ResponseLevel.FLAG
        assert scorer.determine_response_level(95) == ResponseLevel.LOCKDOWN

    @pytest.mark.asyncio
    async def test_reset_score(self, scorer: ThreatScorer):
        detections = [ThreatDetection(detector="t", severity=Severity.HIGH, score_delta=50, description="T")]
        await scorer.add_detections(1, 2, detections)
        await scorer.reset_score(1, 2)
        assert await scorer.get_current_score(1, 2) == 0.0


# ── Behavior Profiler Tests ──────────────────────────────────────────────────

class TestBehaviorProfiler:
    @pytest.mark.asyncio
    async def test_new_profile(self, profiler: BehaviorProfiler):
        profile = await profiler.get_profile(1, 2)
        assert profile.user_id == 2
        assert profile.guild_id == 1
        assert profile.threat_score == 0.0

    @pytest.mark.asyncio
    async def test_update_from_event(self, profiler: BehaviorProfiler):
        event = _make_event(ip_address="1.2.3.4", tx_type="BUY", amount_usd=500.0)
        profile = await profiler.update_from_event(event)
        assert "1.2.3.4" in profile.known_ips
        assert profile.baseline.sample_count == 1

    @pytest.mark.asyncio
    async def test_anomaly_detection_requires_samples(self, profiler: BehaviorProfiler):
        event = _make_event(amount_usd=1000000.0, tx_type="BUY")
        profile = await profiler.get_profile(event.guild_id, event.user_id)
        # Not enough samples → no anomalies
        anomalies = profiler.check_anomalies(event, profile)
        assert len(anomalies) == 0


# ── Circuit Breaker Tests ────────────────────────────────────────────────────

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_trip_and_check(self):
        cache = SecurityRedisCache(redis=None)
        cb = CircuitBreaker(cache)

        assert await cb.is_tripped(1, "trade") is False

        await cb.trip(1, "trade", "test reason", 60)
        assert await cb.is_tripped(1, "trade") is True

    @pytest.mark.asyncio
    async def test_reset(self):
        cache = SecurityRedisCache(redis=None)
        cb = CircuitBreaker(cache)

        await cb.trip(1, "trade", "test", 60)
        await cb.reset(1, "trade")
        assert await cb.is_tripped(1, "trade") is False


# ── Full Engine Integration Tests ────────────────────────────────────────────

class TestSecurityEngine:
    @pytest.mark.asyncio
    async def test_engine_lifecycle(self, engine: SecurityEngine):
        await engine.start()
        assert engine.is_running
        health = engine.get_health()
        assert health.engine_running is True
        await engine.stop()
        assert not engine.is_running

    @pytest.mark.asyncio
    async def test_process_normal_event(self, engine: SecurityEngine):
        await engine.start()
        event = _make_event()
        verdict = await engine.process_event(event)
        assert verdict.blocked is False
        assert verdict.response_level == ResponseLevel.NONE
        await engine.stop()

    @pytest.mark.asyncio
    async def test_check_user_allowed_no_enforcement(self, engine: SecurityEngine):
        await engine.start()
        allowed, reason = await engine.check_user_allowed(1, 2, "trade")
        assert allowed is True
        assert reason == ""
        await engine.stop()

    @pytest.mark.asyncio
    async def test_enforcement_blocks_subsequent_events(self, engine: SecurityEngine):
        await engine.start()

        # Manually set an enforcement
        await engine.cache.set_enforcement(12345, 67890, {
            "action_type": "freeze",
            "scope": "trade",
            "expires_at": time.time() + 3600,
            "reason": "Test enforcement",
        })

        # Now process a trade event  -  should be blocked
        event = _make_event(event_type="trade")
        verdict = await engine.process_event(event)
        assert verdict.blocked is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_admin_freeze_and_unfreeze(self, engine: SecurityEngine):
        await engine.start()

        await engine.admin_freeze(1, 2, "all", "Test", admin_id=999)
        allowed, _ = await engine.check_user_allowed(1, 2, "trade")
        assert allowed is False

        await engine.admin_unfreeze(1, 2, admin_id=999)
        allowed, _ = await engine.check_user_allowed(1, 2, "trade")
        assert allowed is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_admin_clear_score(self, engine: SecurityEngine):
        await engine.start()

        # Add a score
        await engine.cache.set_threat_score(1, 2, 50.0)
        assert await engine.get_threat_score(1, 2) == pytest.approx(50.0, abs=0.1)

        await engine.admin_clear_score(1, 2, admin_id=999)
        assert await engine.get_threat_score(1, 2) == 0.0

        await engine.stop()

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_events(self, engine: SecurityEngine):
        await engine.start()

        # Use same guild_id as _make_event default (12345)
        await engine.admin_lockdown(12345, "trade", "Test lockdown", admin_id=999)

        event = _make_event(event_type="trade")
        verdict = await engine.process_event(event)
        assert verdict.blocked is True
        assert verdict.response_level == ResponseLevel.LOCKDOWN

        await engine.admin_lift_lockdown(12345, "trade", admin_id=999)

        verdict2 = await engine.process_event(event)
        assert verdict2.blocked is False

        await engine.stop()

    @pytest.mark.asyncio
    async def test_high_volume_events_trigger_detection(self, engine: SecurityEngine):
        """Simulate enough events to trigger income velocity detection."""
        await engine.start()

        # Feed many income events
        for i in range(INCOME_VELOCITY_LIMIT + 10):
            event = _make_event(
                tx_type="WORK",
                event_type="earn",
                amount_usd=100.0,
            )
            await engine.process_event(event)

        # Check that the threat score increased
        score = await engine.get_threat_score(12345, 67890)
        assert score > 0

        await engine.stop()


# ── Model Tests ──────────────────────────────────────────────────────────────

class TestModels:
    def test_security_event_defaults(self):
        event = SecurityEvent(
            guild_id=1, user_id=2,
            event_type="trade", source=EventSource.BOT,
        )
        assert event.guild_id == 1
        assert event.timestamp > 0
        assert event.details == {}

    def test_threat_detection(self):
        d = ThreatDetection(
            detector="test",
            severity=Severity.HIGH,
            score_delta=20.0,
            description="Test",
        )
        assert d.score_delta == 20.0

    def test_behavior_baseline(self):
        b = BehaviorBaseline()
        assert b.sample_count == 0
        assert b.active_hours == []
