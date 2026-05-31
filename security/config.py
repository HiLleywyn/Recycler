"""
security/config.py  -  All tuneable thresholds and constants for the security system.

Every value here can be overridden via environment variables prefixed with
``SEC_`` (e.g. ``SEC_INCOME_VELOCITY_LIMIT=30``).  Defaults are intentionally
conservative  -  they should catch clear abuse without flagging normal play.
"""
from __future__ import annotations

import os

def _env_int(key: str, default: int) -> int:
    return int(os.getenv(f"SEC_{key}", default))

def _env_float(key: str, default: float) -> float:
    return float(os.getenv(f"SEC_{key}", default))

# ── Detection Windows ────────────────────────────────────────────────────────

SCAN_INTERVAL_SECONDS       = _env_int("SCAN_INTERVAL", 120)
LOOKBACK_SECONDS            = _env_int("LOOKBACK", 300)

# ── Economy Detectors ────────────────────────────────────────────────────────

INCOME_VELOCITY_LIMIT       = _env_int("INCOME_VELOCITY_LIMIT", 20)
GAMBLING_VELOCITY_LIMIT     = _env_int("GAMBLING_VELOCITY_LIMIT", 100)
WASH_TRADE_MIN_CYCLES       = _env_int("WASH_TRADE_MIN_CYCLES", 6)
TRANSFER_RING_MIN           = _env_int("TRANSFER_RING_MIN", 4)
LP_CHURN_MIN                = _env_int("LP_CHURN_MIN", 4)
TX_FLOOD_LIMIT              = _env_int("TX_FLOOD_LIMIT", 80)

# ── API / Session Detectors ──────────────────────────────────────────────────

AUTH_FAILURE_LIMIT           = _env_int("AUTH_FAILURE_LIMIT", 10)       # per 5-min window
AUTH_FAILURE_WINDOW          = _env_int("AUTH_FAILURE_WINDOW", 300)
SESSION_IP_CHANGE_WINDOW     = _env_int("SESSION_IP_CHANGE_WINDOW", 60)  # suspicious if IP changes within 60s
API_REQUEST_FLOOD_LIMIT      = _env_int("API_REQUEST_FLOOD_LIMIT", 200)  # per 60s per user
API_REQUEST_FLOOD_WINDOW     = _env_int("API_REQUEST_FLOOD_WINDOW", 60)

# ── Command Flood (Bot) ─────────────────────────────────────────────────────

COMMAND_FLOOD_LIMIT          = _env_int("COMMAND_FLOOD_LIMIT", 60)     # commands per 60s
COMMAND_FLOOD_WINDOW         = _env_int("COMMAND_FLOOD_WINDOW", 60)
IDENTICAL_COMMAND_LIMIT      = _env_int("IDENTICAL_COMMAND_LIMIT", 30) # same command per 60s

# ── Cross-Platform Correlation ───────────────────────────────────────────────

CORRELATION_WINDOW           = _env_int("CORRELATION_WINDOW", 300)     # 5 minutes
CORRELATION_EVENT_MIN        = _env_int("CORRELATION_EVENT_MIN", 10)   # suspicious if 10+ events from both platforms

# ── DeFi Exploit Patterns ───────────────────────────────────────────────────

FLASH_LOAN_WINDOW            = _env_int("FLASH_LOAN_WINDOW", 30)      # borrow+trade+repay within 30s
ORACLE_MANIPULATION_TRADES   = _env_int("ORACLE_MANIPULATION_TRADES", 8) # rapid same-token trades
ORACLE_MANIPULATION_WINDOW   = _env_int("ORACLE_MANIPULATION_WINDOW", 60)

# ── Threat Scoring ───────────────────────────────────────────────────────────

SCORE_DECAY_HALF_LIFE        = _env_float("SCORE_DECAY_HALF_LIFE", 3600.0)  # 1 hour

# Points awarded per detection type
SCORE_WEIGHTS: dict[str, float] = {
    "income_velocity":       15.0,
    "gambling_abuse":        12.0,
    "wash_trading":          20.0,
    "transfer_rings":        18.0,
    "lp_manipulation":       20.0,
    "api_abuse":             25.0,
    "session_anomaly":       15.0,
    "privilege_escalation":  30.0,
    "transaction_integrity": 35.0,
    "defi_exploit":          30.0,
    "command_flood":         10.0,
    "cross_platform_abuse":  22.0,
    "tx_flood":              12.0,
}

# ── Response Levels ──────────────────────────────────────────────────────────

LEVEL_1_THRESHOLD = _env_float("LEVEL_1_THRESHOLD", 21.0)   # log + monitor
LEVEL_2_THRESHOLD = _env_float("LEVEL_2_THRESHOLD", 41.0)   # throttle
LEVEL_3_THRESHOLD = _env_float("LEVEL_3_THRESHOLD", 61.0)   # freeze
LEVEL_4_THRESHOLD = _env_float("LEVEL_4_THRESHOLD", 81.0)   # flag + admin alert
LEVEL_5_THRESHOLD = _env_float("LEVEL_5_THRESHOLD", 91.0)   # emergency lockdown

# Enforcement durations (seconds)
THROTTLE_DURATION            = _env_int("THROTTLE_DURATION", 600)       # 10 minutes
FREEZE_DURATION              = _env_int("FREEZE_DURATION", 900)         # 15 minutes
FLAG_DURATION                = _env_int("FLAG_DURATION", 3600)          # 1 hour
LOCKDOWN_DURATION            = _env_int("LOCKDOWN_DURATION", 1800)      # 30 minutes

# Throttled rate limits (requests per 10-second window)
THROTTLED_RATE_LIMIT         = _env_int("THROTTLED_RATE_LIMIT", 10)

# ── Alert & Deduplication ────────────────────────────────────────────────────

ALERT_COOLDOWN_SECONDS       = _env_int("ALERT_COOLDOWN", 600)         # 10 min between alerts per user
ALERT_DEDUP_TTL              = _env_int("ALERT_DEDUP_TTL", 600)

# ── Redis Key Prefixes ──────────────────────────────────────────────────────

REDIS_PREFIX = "discoin:sec"

# ── Behavior Profiling ──────────────────────────────────────────────────────

PROFILE_TTL                  = _env_int("PROFILE_TTL", 86400)          # 24 hours
PROFILE_UPDATE_INTERVAL      = _env_int("PROFILE_UPDATE_INTERVAL", 300) # recalc every 5 min
ANOMALY_STDDEV_THRESHOLD     = _env_float("ANOMALY_STDDEV_THRESHOLD", 3.0)
BASELINE_MIN_SAMPLES         = _env_int("BASELINE_MIN_SAMPLES", 20)     # need 20+ data points
BASELINE_TTL                 = _env_int("BASELINE_TTL", 21600)          # 6 hours

# ── Whale Tracking ──────────────────────────────────────────────────────────

WHALE_CONCENTRATION_LIMIT    = _env_int("WHALE_CONCENTRATION_LIMIT", 3)

# ── Repeat Offender ─────────────────────────────────────────────────────────

REPEAT_OFFENDER_LIMIT        = _env_int("REPEAT_OFFENDER_LIMIT", 3)
