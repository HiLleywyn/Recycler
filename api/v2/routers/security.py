"""
Security API Router
====================

Admin-only endpoints for the security dashboard:

    GET  /security/threats           -  Active threats (paginated)
    GET  /security/threats/{id}      -  Single threat detail
    GET  /security/user/{id}/profile  -  User security profile + score
    GET  /security/audit             -  Security audit log
    GET  /security/stats             -  Detection statistics
    GET  /security/health            -  Security system health metrics
    GET  /security/enforcements      -  Active enforcements
    POST /security/action            -  Admin enforcement actions
    POST /security/acknowledge       -  Dismiss a threat
    GET  /security/config            -  Per-guild security threshold config
    PATCH /security/config           -  Update per-guild security thresholds
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.v2.dependencies import get_db, require_admin, require_security_access
from api.v2.exceptions import NotFoundError, ValidationError
import security.config as _sec_cfg
from api.v2.schemas.admin import SecurityConfigUpdate

router = APIRouter(prefix="/security", tags=["security"])


# ── Request / Response Models ────────────────────────────────────────────────

class ActionRequest(BaseModel):
    action: str = Field(..., description="freeze, unfreeze, clear_score, lockdown, lift_lockdown")
    user_id: int | None = Field(None, description="Target user ID (for user actions)")
    scope: str = Field("all", description="Enforcement scope: trade, transfer, gamble, all, etc.")
    reason: str = Field("Admin action", description="Reason for the action")
    duration: int | None = Field(None, description="Duration in seconds (None = default)")
    feature: str | None = Field(None, description="Feature name (for lockdown actions)")


class AcknowledgeRequest(BaseModel):
    event_id: int


class ExemptRequest(BaseModel):
    target_type: str = Field(..., description="'user' or 'role'")
    target_id: int = Field(..., description="Discord user ID or role ID")
    notes: str | None = Field(None, description="Optional note about the exemption")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_engine(request: Request):
    """Get the SecurityEngine from app state."""
    engine = getattr(request.app.state, "security_engine", None)
    if engine is None:
        raise HTTPException(503, "Security engine not initialized")
    return engine


def _get_security_db(request: Request):
    """Get SecurityRepository from app state."""
    db = getattr(request.app.state, "security_db", None)
    if db is None:
        raise HTTPException(503, "Security database not initialized")
    return db


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/health")
async def security_health(
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Get security system health metrics."""
    engine = _get_engine(request)
    health = engine.get_health()
    return health.model_dump()


@router.get("/stats")
async def security_stats(
    request: Request,
    _admin: dict = Depends(require_admin),
    hours: int = Query(24, ge=1, le=168),
) -> dict:
    """Get aggregate security statistics for the last N hours."""
    admin = _admin
    guild_id = admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    db = _get_security_db(request)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stats = await db.get_stats(int(guild_id), since)

    # Add average threat score from engine
    engine = _get_engine(request)
    stats["engine_health"] = engine.get_health().model_dump()

    return stats


@router.get("/threats")
async def list_threats(
    request: Request,
    _admin: dict = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: int | None = Query(None),
    event_type: str | None = Query(None),
    severity: str | None = Query(None),
    hours: int = Query(24, ge=1, le=168),
) -> dict:
    """List security events (threats) with optional filtering."""
    admin = _admin
    guild_id = admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    db = _get_security_db(request)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    events = await db.get_security_events(
        guild_id=int(guild_id),
        limit=limit,
        offset=offset,
        user_id=user_id,
        event_type=event_type,
        severity=severity,
        since=since,
    )

    total = await db.count_security_events(int(guild_id), since=since)

    return {
        "events": _serialize_rows(events),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/threats/{event_id}")
async def get_threat(
    event_id: int,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Get a single security event by ID."""
    db = _get_security_db(request)
    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    events = await db.get_security_events(
        guild_id=int(guild_id), limit=1, offset=0,
    )
    # Filter by ID (the repo doesn't have a get-by-id method, so we query and filter)
    # For a production system you'd add a dedicated query
    for e in events:
        if e.get("id") == event_id:
            return _serialize_row(e)

    # Try direct query
    row = await db.fetch_one(
        "SELECT * FROM security_events WHERE id = $1 AND guild_id = $2",
        event_id, int(guild_id),
    )
    if not row:
        raise NotFoundError("Security event not found")
    return _serialize_row(row)


@router.get("/user/{user_id}/profile")
async def get_user_profile(
    user_id: int,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Get a user's security profile, threat score, and recent events."""
    admin = _admin
    guild_id = admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    engine = _get_engine(request)
    db = _get_security_db(request)

    # Get current threat score from Redis (with decay)
    threat_score = await engine.get_threat_score(int(guild_id), user_id)

    # Get persistent profile from DB
    profile = await db.get_profile(int(guild_id), user_id)

    # Get recent events
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_events = await db.get_security_events(
        guild_id=int(guild_id), user_id=user_id, limit=20, since=since,
    )

    # Get enforcements
    enforcements = await db.get_user_enforcements(int(guild_id), user_id, include_expired=True)

    # Check if currently restricted
    allowed, reason = await engine.check_user_allowed(int(guild_id), user_id)

    return {
        "user_id": user_id,
        "guild_id": int(guild_id),
        "threat_score": threat_score,
        "profile": _serialize_row(profile) if profile else None,
        "recent_events": _serialize_rows(recent_events),
        "enforcements": _serialize_rows(enforcements),
        "currently_restricted": not allowed,
        "restriction_reason": reason if not allowed else None,
    }


@router.get("/enforcements")
async def list_enforcements(
    request: Request,
    _admin: dict = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List active enforcements for the guild."""
    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    db = _get_security_db(request)
    enforcements = await db.get_active_enforcements(int(guild_id), limit, offset)
    total = await db.count_active_enforcements(int(guild_id))

    return {
        "enforcements": _serialize_rows(enforcements),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/audit")
async def security_audit_log(
    request: Request,
    _admin: dict = Depends(require_security_access),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Get the security audit log.

    Accessible by server owners, admins, and users with a designated security_audit role.
    """
    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    db = _get_security_db(request)
    entries = await db.get_security_audit(int(guild_id), limit, offset)

    return {
        "entries": _serialize_rows(entries),
        "limit": limit,
        "offset": offset,
    }


@router.get("/exempt")
async def list_exemptions(
    request: Request,
    _admin: dict = Depends(require_security_access),
) -> dict:
    """List all security exemptions for the guild."""
    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    db = _get_security_db(request)
    exemptions = await db.get_exemptions(int(guild_id))
    return {"exemptions": _serialize_rows(exemptions)}


@router.post("/exempt")
async def add_exemption(
    body: ExemptRequest,
    request: Request,
    _admin: dict = Depends(require_security_access),
) -> dict:
    """Add a security exemption."""

    guild_id = _admin.get("guild_id")
    admin_id = _admin.get("user_id")
    if not guild_id or not admin_id:
        raise ValidationError("Guild and user context required")

    if body.target_type not in ("user", "role"):
        raise ValidationError("target_type must be 'user' or 'role'")

    db = _get_security_db(request)
    exemption_id = await db.add_exempt(
        int(guild_id), body.target_type, body.target_id, int(admin_id), body.notes,
    )
    return {
        "status": "ok",
        "id": exemption_id,
        "message": f"{body.target_type.capitalize()} {body.target_id} added to security exemptions.",
    }


@router.delete("/exempt/{target_type}/{target_id}")
async def remove_exemption(
    target_type: str,
    target_id: int,
    request: Request,
    _admin: dict = Depends(require_security_access),
) -> dict:
    """Remove a security exemption."""

    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    if target_type not in ("user", "role"):
        raise ValidationError("target_type must be 'user' or 'role'")

    db = _get_security_db(request)
    removed = await db.remove_exempt(int(guild_id), target_type, target_id)
    if not removed:
        raise NotFoundError("Exemption not found")
    return {"status": "ok", "message": f"{target_type.capitalize()} {target_id} removed from security exemptions."}


@router.post("/action")
async def security_action(
    body: ActionRequest,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Execute an admin security action."""
    admin = _admin
    guild_id = admin.get("guild_id")
    admin_id = admin.get("user_id")
    if not guild_id or not admin_id:
        raise ValidationError("Guild and admin context required")

    engine = _get_engine(request)
    guild_id = int(guild_id)
    admin_id = int(admin_id)

    if body.action == "freeze":
        if not body.user_id:
            raise ValidationError("user_id required for freeze action")
        await engine.admin_freeze(
            guild_id, body.user_id, body.scope, body.reason, admin_id, body.duration,
        )
        return {"status": "ok", "message": f"User {body.user_id} frozen (scope: {body.scope})"}

    elif body.action == "unfreeze":
        if not body.user_id:
            raise ValidationError("user_id required for unfreeze action")
        success = await engine.admin_unfreeze(guild_id, body.user_id, admin_id)
        if not success:
            raise NotFoundError("No active enforcement found for this user")
        return {"status": "ok", "message": f"User {body.user_id} unfrozen"}

    elif body.action == "clear_score":
        if not body.user_id:
            raise ValidationError("user_id required for clear_score action")
        await engine.admin_clear_score(guild_id, body.user_id, admin_id)
        return {"status": "ok", "message": f"Threat score cleared for user {body.user_id}"}

    elif body.action == "lockdown":
        feature = body.feature or body.scope
        if not feature:
            raise ValidationError("feature or scope required for lockdown")
        duration = body.duration or 1800
        await engine.admin_lockdown(guild_id, feature, body.reason, admin_id, duration)
        return {"status": "ok", "message": f"Feature '{feature}' locked down for {duration}s"}

    elif body.action == "lift_lockdown":
        feature = body.feature or body.scope
        if not feature:
            raise ValidationError("feature required for lift_lockdown")
        await engine.admin_lift_lockdown(guild_id, feature, admin_id)
        return {"status": "ok", "message": f"Lockdown lifted for feature '{feature}'"}

    else:
        raise ValidationError(f"Unknown action: {body.action}")


@router.post("/acknowledge")
async def acknowledge_threat(
    body: AcknowledgeRequest,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Acknowledge/dismiss a security event."""
    admin = _admin
    guild_id = admin.get("guild_id")
    admin_id = admin.get("user_id")
    if not guild_id or not admin_id:
        raise ValidationError("Guild and admin context required")

    db = _get_security_db(request)
    try:
        await db.create_security_audit(
            guild_id=int(guild_id),
            admin_id=int(admin_id),
            action="acknowledge",
            target_user=None,
            details={"event_id": body.event_id},
        )
    except Exception as exc:
        raise ValidationError(f"Failed to acknowledge: {exc}")

    return {"status": "ok", "message": f"Event {body.event_id} acknowledged"}


# ── Per-guild Security Configuration ─────────────────────────────────────────

# Maps DB column names → (global default attr, python type)
_CONFIG_FIELDS: dict[str, tuple[str, type]] = {
    "scan_interval_seconds":    ("SCAN_INTERVAL_SECONDS",      int),
    "lookback_seconds":         ("LOOKBACK_SECONDS",            int),
    "income_velocity_limit":    ("INCOME_VELOCITY_LIMIT",      int),
    "gambling_velocity_limit":  ("GAMBLING_VELOCITY_LIMIT",    int),
    "wash_trade_min_cycles":    ("WASH_TRADE_MIN_CYCLES",      int),
    "transfer_ring_min":        ("TRANSFER_RING_MIN",          int),
    "lp_churn_min":             ("LP_CHURN_MIN",               int),
    "tx_flood_limit":           ("TX_FLOOD_LIMIT",             int),
    "auth_failure_limit":       ("AUTH_FAILURE_LIMIT",         int),
    "auth_failure_window":      ("AUTH_FAILURE_WINDOW",        int),
    "session_ip_change_window": ("SESSION_IP_CHANGE_WINDOW",   int),
    "api_request_flood_limit":  ("API_REQUEST_FLOOD_LIMIT",    int),
    "api_request_flood_window": ("API_REQUEST_FLOOD_WINDOW",   int),
    "command_flood_limit":      ("COMMAND_FLOOD_LIMIT",        int),
    "command_flood_window":     ("COMMAND_FLOOD_WINDOW",       int),
    "identical_command_limit":  ("IDENTICAL_COMMAND_LIMIT",    int),
    "correlation_window":       ("CORRELATION_WINDOW",         int),
    "correlation_event_min":    ("CORRELATION_EVENT_MIN",      int),
    "flash_loan_window":        ("FLASH_LOAN_WINDOW",          int),
    "oracle_manipulation_trades":  ("ORACLE_MANIPULATION_TRADES",  int),
    "oracle_manipulation_window":  ("ORACLE_MANIPULATION_WINDOW",  int),
    "score_decay_half_life":    ("SCORE_DECAY_HALF_LIFE",      float),
    "level_1_threshold":        ("LEVEL_1_THRESHOLD",          float),
    "level_2_threshold":        ("LEVEL_2_THRESHOLD",          float),
    "level_3_threshold":        ("LEVEL_3_THRESHOLD",          float),
    "level_4_threshold":        ("LEVEL_4_THRESHOLD",          float),
    "level_5_threshold":        ("LEVEL_5_THRESHOLD",          float),
    "throttle_duration":        ("THROTTLE_DURATION",          int),
    "freeze_duration":          ("FREEZE_DURATION",            int),
    "flag_duration":            ("FLAG_DURATION",              int),
    "lockdown_duration":        ("LOCKDOWN_DURATION",          int),
    "throttled_rate_limit":     ("THROTTLED_RATE_LIMIT",       int),
    "alert_cooldown_seconds":   ("ALERT_COOLDOWN_SECONDS",     int),
    "anomaly_stddev_threshold": ("ANOMALY_STDDEV_THRESHOLD",   float),
    "baseline_min_samples":     ("BASELINE_MIN_SAMPLES",       int),
    "whale_concentration_limit":("WHALE_CONCENTRATION_LIMIT",  int),
    "repeat_offender_limit":    ("REPEAT_OFFENDER_LIMIT",      int),
}


@router.get("/config")
async def get_security_config(
    _admin: dict = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    """Return the effective per-guild security config merged with global defaults."""
    guild_id = _admin.get("guild_id")
    if not guild_id:
        raise ValidationError("Guild context required")

    row = await db.fetchrow(
        "SELECT * FROM guild_security_config WHERE guild_id = $1",
        int(guild_id),
    )
    overrides: dict[str, Any] = dict(row) if row else {}

    result: dict[str, Any] = {}
    for col, (attr, _) in _CONFIG_FIELDS.items():
        db_val = overrides.get(col)
        result[col] = db_val if db_val is not None else getattr(_sec_cfg, attr)

    # score_weights: DB JSONB overrides the module-level dict
    raw_weights = overrides.get("score_weights")
    if raw_weights is not None:
        result["score_weights"] = raw_weights if isinstance(raw_weights, dict) else {}
    else:
        result["score_weights"] = dict(_sec_cfg.SCORE_WEIGHTS)

    # Indicate which fields are currently overridden vs using the global default
    result["_overrides"] = [c for c in _CONFIG_FIELDS if overrides.get(c) is not None]
    if overrides.get("score_weights") is not None:
        result["_overrides"].append("score_weights")

    return result


@router.patch("/config")
async def update_security_config(
    body: SecurityConfigUpdate,
    _admin: dict = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    """Upsert per-guild security config overrides.

    Only fields that are not ``None`` are written; passing ``null`` for a
    previously-set field resets it to the global default.
    """
    guild_id = _admin.get("guild_id")
    admin_id = _admin.get("user_id")
    if not guild_id or not admin_id:
        raise ValidationError("Guild and admin context required")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": "ok", "message": "No changes."}

    # Ensure the guild has a row (upsert) and update atomically
    async with db.transaction():
        await db.execute(
            "INSERT INTO guild_security_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            int(guild_id),
        )

        set_parts = []
        values: list[Any] = [int(guild_id)]
        idx = 2
        for key, val in updates.items():
            if key == "score_weights" and isinstance(val, dict):
                val = json.dumps(val)
            set_parts.append(f"{key} = ${idx}")
            values.append(val)
            idx += 1
        set_parts.append("updated_at = now()")

        await db.execute(
            f"UPDATE guild_security_config SET {', '.join(set_parts)} WHERE guild_id = $1",
            *values,
        )

    # Write to security audit log (outside transaction  -  audit failure must never block the save)
    try:
        sec_db = getattr(db, "_security_db", None)
        if sec_db:
            await sec_db.create_security_audit(
                guild_id=int(guild_id),
                admin_id=int(admin_id),
                action="update_security_config",
                target_user=None,
                details=updates,
            )
    except Exception:
        pass

    return {"status": "ok", "message": "Security config updated.", "updated": list(updates.keys())}


# ── Serialization Helpers ────────────────────────────────────────────────────

def _serialize_row(row: dict | None) -> dict | None:
    """Convert asyncpg Record values to JSON-safe types."""
    if row is None:
        return None
    result = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            result[k] = v.isoformat()
        elif hasattr(v, '__json__'):
            result[k] = v
        else:
            result[k] = v
    return result


def _serialize_rows(rows: list[dict]) -> list[dict]:
    return [_serialize_row(r) for r in rows if r is not None]
