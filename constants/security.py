"""
Security constants  -  re-exports from security/config.py.

security/config.py is the canonical source (with SEC_ env var overrides).
This module provides a constants.security namespace for consistency.
"""
from security.config import (  # noqa: F401
    SCAN_INTERVAL_SECONDS,
    LOOKBACK_SECONDS,
    INCOME_VELOCITY_LIMIT,
    GAMBLING_VELOCITY_LIMIT,
    WASH_TRADE_MIN_CYCLES,
    TRANSFER_RING_MIN,
    LP_CHURN_MIN,
    TX_FLOOD_LIMIT,
    ALERT_COOLDOWN_SECONDS,
    REPEAT_OFFENDER_LIMIT,
    WHALE_CONCENTRATION_LIMIT,
)
