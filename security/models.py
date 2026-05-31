"""
security/models.py  -  Pydantic models for the security system.

These models are shared across the engine, API, bot cog, and database layer.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

class EventSource(str, Enum):
    BOT = "bot"
    API = "api"
    SYSTEM = "system"  # internal (periodic scans, scheduled jobs)


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ResponseLevel(int, Enum):
    NONE = 0
    LOG = 1         # log + enhanced monitoring
    THROTTLE = 2    # reduce rate limits
    FREEZE = 3      # block specific actions
    FLAG = 4        # flag + admin alert
    LOCKDOWN = 5    # emergency guild-wide halt


class EnforcementAction(str, Enum):
    THROTTLE = "throttle"
    FREEZE = "freeze"
    FLAG = "flag"
    LOCKDOWN = "lockdown"
    BAN = "ban"


class RiskLevel(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"


class HierarchyLevel(int, Enum):
    """Ordered authority levels for the security system.

    Lower value = higher authority.  No level can override a level above it.
    """
    OWNER = 1      # Server owner  -  cannot be overridden by anything
    SECURITY = 2   # Security system itself
    BOT = 3        # Bot process / dashboard handler
    ADMIN = 4      # Server administrators
    MODERATOR = 5  # Moderators
    USER = 6       # Regular users


# ── Inbound Event ────────────────────────────────────────────────────────────

class SecurityEvent(BaseModel):
    """An action or event from the bot or API to be evaluated by the engine."""

    guild_id: int
    user_id: int
    event_type: str                    # e.g. "trade", "transfer", "command", "api_request", "auth_attempt"
    source: EventSource
    timestamp: float = Field(default_factory=time.time)
    details: dict[str, Any] = Field(default_factory=dict)

    # Optional context fields
    ip_address: str | None = None
    user_agent: str | None = None
    endpoint: str | None = None        # API path
    command: str | None = None          # bot command name
    amount_usd: float | None = None    # USD value of the action (if financial)
    symbol: str | None = None           # token symbol (if applicable)
    tx_type: str | None = None          # transaction type from ledger


# ── Detection Result ─────────────────────────────────────────────────────────

class ThreatDetection(BaseModel):
    """A single detection produced by a detector."""

    detector: str                       # detector function name
    severity: Severity
    score_delta: float                  # points to add to threat score
    description: str                    # human-readable explanation
    details: dict[str, Any] = Field(default_factory=dict)


# ── Engine Verdict ───────────────────────────────────────────────────────────

class SecurityVerdict(BaseModel):
    """The outcome of processing a SecurityEvent through the engine."""

    event: SecurityEvent
    detections: list[ThreatDetection] = Field(default_factory=list)
    previous_score: float = 0.0
    new_score: float = 0.0
    response_level: ResponseLevel = ResponseLevel.NONE
    enforcement_action: EnforcementAction | None = None
    enforcement_scope: str | None = None    # e.g. "trade", "transfer", "all"
    enforcement_duration: int | None = None  # seconds
    blocked: bool = False                    # whether the triggering action was blocked


# ── User Security Profile ────────────────────────────────────────────────────

class BehaviorBaseline(BaseModel):
    """Statistical baselines for a user's normal behavior."""

    tx_per_hour: dict[str, float] = Field(default_factory=dict)    # tx_type → mean
    tx_per_hour_std: dict[str, float] = Field(default_factory=dict)
    avg_amount: dict[str, float] = Field(default_factory=dict)     # tx_type → mean USD
    avg_amount_std: dict[str, float] = Field(default_factory=dict)
    active_hours: list[int] = Field(default_factory=list)          # 0-23
    common_ips: list[str] = Field(default_factory=list)
    gambling_win_rate: float | None = None
    api_requests_per_hour: float = 0.0
    sample_count: int = 0
    last_updated: float = 0.0


class UserSecurityProfile(BaseModel):
    """Full security profile for a user in a guild."""

    user_id: int
    guild_id: int
    threat_score: float = 0.0
    total_flags: int = 0
    last_flagged: float | None = None
    risk_level: RiskLevel = RiskLevel.NORMAL
    baseline: BehaviorBaseline = Field(default_factory=BehaviorBaseline)
    known_ips: list[str] = Field(default_factory=list)
    notes: str | None = None


# ── Enforcement Record ───────────────────────────────────────────────────────

class EnforcementRecord(BaseModel):
    """An active or historical enforcement action."""

    id: int | None = None
    guild_id: int
    user_id: int | None = None          # None for guild-wide actions
    action_type: EnforcementAction
    scope: str                           # "trade", "transfer", "gamble", "all", feature name
    reason: str
    enacted_by: str                      # "auto" or admin user_id
    expires_at: float | None = None      # unix timestamp
    lifted_at: float | None = None
    lifted_by: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


# ── API Response Models ──────────────────────────────────────────────────────

class ThreatSummary(BaseModel):
    """Lightweight threat info for list endpoints."""

    event_id: int
    guild_id: int
    user_id: int
    event_type: str
    severity: str
    score_delta: float
    description: str
    source: str
    created_at: str  # ISO format


class SecurityStats(BaseModel):
    """Aggregate security statistics."""

    total_events_24h: int = 0
    events_by_type: dict[str, int] = Field(default_factory=dict)
    events_by_severity: dict[str, int] = Field(default_factory=dict)
    active_enforcements: int = 0
    flagged_users: int = 0
    avg_threat_score: float = 0.0
    top_threats: list[ThreatSummary] = Field(default_factory=list)


class SecurityHealth(BaseModel):
    """Health metrics for the security system."""

    engine_running: bool = False
    redis_connected: bool = False
    db_connected: bool = False
    detectors_active: int = 0
    events_processed_total: int = 0
    last_scan_ts: float | None = None
    uptime_seconds: float = 0.0
