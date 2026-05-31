"""
Economy constants  -  lock periods, cooldowns, lending rates, work caps.

Values that core/config.py reads from .env stay in core/config.py. This module holds
the hardcoded business rules that should never silently differ between files.
"""
from __future__ import annotations

STAKE_LOCK_PERIOD: int = 86_400
CHAIN_SWITCH_COOLDOWN: int = 600
MIN_COLLATERAL_RATIO: float = 1.5
BASE_DEPOSIT_APY: float = 0.05
BASE_BORROW_APY: float = 0.08
HOURS_48: int = 172_800
LEADERBOARD_PER_PAGE: int = 10
VALIDATORS_PER_PAGE: int = 3
ADMIN_LINES_PER_PAGE: int = 10
GROUP_RENAME_COST: float = 1_000.0
GROUP_RENAME_COOLDOWN: int = 86_400
AI_COOLDOWN_SECS: int = 5
AI_MSG_CAP: int = 500
AI_FLAVOR_TTL: int = 300
AI_QUOTA_WINDOW: int = 3600
AI_QUOTA_LIMIT: int = 25
REPORT_COOLDOWN: int = 300
OAUTH_STATE_TTL: int = 300
GUILDS_CACHE_TTL: int = 600
MAX_2FA_ATTEMPTS: int = 5
IDEMPOTENCY_TTL: int = 60
WS_HEARTBEAT_INTERVAL: int = 30
ERROR_MAX_PER_GUILD: int = 500
ERROR_MAX_GLOBAL: int = 200
ADMIN_MAX_UPLOAD_BYTES: int = 24 * 1024 * 1024
