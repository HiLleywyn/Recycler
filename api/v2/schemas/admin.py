"""Admin Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

class GuildSettings(BaseModel):
    """Full guild settings."""

    guild_id: str = Field(..., description="Guild ID.")
    # Channels
    trade_channel: str | None = None
    mine_channel: str | None = None
    staking_channel: str | None = None
    validators_channel: str | None = None
    contracts_channel: str | None = None
    crypto_channel: str | None = None
    gambling_channel: str | None = None
    pools_channel: str | None = None
    drops_channel: str | None = None
    job_channel: str | None = None
    drops_spawn_channel: str | None = None
    faucet_channel: str | None = None
    wallet_channel: str | None = None
    error_channel: str | None = None
    scam_channel: str | None = None
    whale_alerts_channel: str | None = None
    reports_feed_channel: str | None = None
    security_log_channel: str | None = None
    # Basic config
    prefix: str | None = None
    embed_color: int | None = None
    server_name: str | None = None
    currency_name: str | None = None
    # Fees
    platform_fee_pct: float | None = None
    platform_fee_min: float | None = None
    platform_fee_max: float | None = None
    treasury_cut_pct: float | None = None
    # Halts
    halted_networks: str = ""
    disabled_tokens: str = ""
    # Drops
    drop_interval: int | None = None
    drop_min: float | None = None
    drop_max: float | None = None
    # Faucet
    faucet_multiplier: float | None = None
    faucet_tokens: str | None = None
    # Auto-delete
    cmd_delete_after: int | None = None
    reply_delete_after: int | None = None
    ai_cmd_delete_after: int | None = None
    ai_reply_delete_after: int | None = None
    # Scam detection
    scam_detection: bool | None = None
    scam_timeout_minutes: int | None = None
    # Whale alerts
    whale_alert_threshold: float | None = None
    # Reports feed
    reports_feed_categories: str | None = None
    # Security audit
    security_audit_roles: str | None = None
    # AI features
    ai_mm_enabled: bool | None = None
    ai_chat_enabled: bool | None = None
    ai_commentary_enabled: bool | None = None
    ai_flavor_enabled: bool | None = None
    ai_events_enabled: bool | None = None
    ai_prompt_chat: str | None = None
    ai_prompt_commentary: str | None = None
    ai_prompt_events: str | None = None
    ai_prompt_flavor: str | None = None
    ai_persona_name: str | None = None


class GuildSettingsUpdate(BaseModel):
    """Partial update for guild settings."""

    # Channels
    trade_channel: str | None = None
    mine_channel: str | None = None
    staking_channel: str | None = None
    validators_channel: str | None = None
    contracts_channel: str | None = None
    crypto_channel: str | None = None
    gambling_channel: str | None = None
    pools_channel: str | None = None
    drops_channel: str | None = None
    job_channel: str | None = None
    drops_spawn_channel: str | None = None
    faucet_channel: str | None = None
    wallet_channel: str | None = None
    error_channel: str | None = None
    scam_channel: str | None = None
    whale_alerts_channel: str | None = None
    reports_feed_channel: str | None = None
    security_log_channel: str | None = None
    # Basic config
    prefix: str | None = None
    embed_color: int | None = None
    server_name: str | None = None
    currency_name: str | None = None
    # Fees
    platform_fee_pct: float | None = None
    platform_fee_min: float | None = None
    platform_fee_max: float | None = None
    treasury_cut_pct: float | None = None
    # Drops
    drop_interval: int | None = None
    drop_min: float | None = None
    drop_max: float | None = None
    # Faucet
    faucet_multiplier: float | None = None
    faucet_tokens: str | None = None
    # Auto-delete
    cmd_delete_after: int | None = None
    reply_delete_after: int | None = None
    ai_cmd_delete_after: int | None = None
    ai_reply_delete_after: int | None = None
    # Scam detection
    scam_detection: bool | None = None
    scam_timeout_minutes: int | None = None
    # Whale alerts
    whale_alert_threshold: float | None = None
    # Reports feed
    reports_feed_categories: str | None = None
    # Security audit
    security_audit_roles: str | None = None
    # AI features
    ai_mm_enabled: bool | None = None
    ai_chat_enabled: bool | None = None
    ai_commentary_enabled: bool | None = None
    ai_flavor_enabled: bool | None = None
    ai_events_enabled: bool | None = None
    ai_prompt_chat: str | None = None
    ai_prompt_commentary: str | None = None
    ai_prompt_events: str | None = None
    ai_prompt_flavor: str | None = None
    ai_persona_name: str | None = None


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

class ModuleStatus(BaseModel):
    """Status of a single feature module."""

    module: str = Field(..., description="Module key name.")
    enabled: bool = Field(..., description="Whether the module is enabled.")


class ModuleToggle(BaseModel):
    """Toggle request for a module."""

    enabled: bool = Field(..., description="Desired enabled state.")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

class TokenCreate(BaseModel):
    """Request to create a new token.

    All fields that control the token's on-chain behaviour are configurable here,
    mirroring what you'd set when deploying a real EVM token.
    """

    symbol: str = Field(..., description="Token symbol (e.g. 'DSC').")
    name: str = Field(..., description="Token display name.")
    emoji: str = Field("●", description="Token emoji.")
    consensus: str = Field("PoS", description="Consensus type: 'PoW', 'PoS', or 'Fiat'.")
    network: str | None = Field(None, description="Network name, or null for standalone.")
    start_price: float = Field(1.0, gt=0, description="Initial oracle price in USD.")
    daily_vol: float = Field(0.05, ge=0, le=0.50,
        description="Daily volatility coefficient (0.0=stable, 0.05=5%/day, max 0.50).")
    max_supply: int | None = Field(None, gt=0,
        description="Hard cap on total token supply. Null = unlimited.")
    decimals: int = Field(18, ge=0, le=18,
        description="Decimal precision (8 for MTA-style, 6 for USDC, 18 for EVM).")
    tx_fee_rate: float = Field(0.001, ge=0.0, le=0.10,
        description="Fraction of transfer amount charged as fee (0.001 = 0.1%, max 10%).")
    gas_fee: float = Field(0.05, ge=0.0, le=100.0,
        description="Flat base gas cost per transaction in USD.")
    stablecoin: bool = Field(False, description="True = price-pegged stablecoin (daily_vol forced to 0).")


class TokenInfo(BaseModel):
    """Token details."""

    symbol: str
    name: str
    emoji: str
    consensus: str
    network: str | None
    start_price: float
    daily_vol: float
    max_supply: int | None = None
    decimals: int = 18
    tx_fee_rate: float = 0.001
    gas_fee: float = 0.05
    stablecoin: bool = False
    created_at: str | None = None


class SetPriceRequest(BaseModel):
    """Request to set a token's price."""

    price: float = Field(..., gt=0, description="New price.")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

class ValidatorCreate(BaseModel):
    """Request to create a validator."""

    validator_id: str = Field(..., description="Validator ID.")
    name: str = Field(..., description="Validator name.")
    emoji: str = Field("", description="Emoji.")
    uptime_rate: float = Field(0.99, ge=0, le=1, description="Uptime rate (0-1).")
    reward_rate: float = Field(0.05, ge=0, le=1.0, description="Reward rate (0-1, i.e. 0-100%).")
    slash_rate: float = Field(0.01, ge=0, le=0.50, description="Slash rate (0-0.50, i.e. 0-50%).")


class ValidatorUpdate(BaseModel):
    """Partial validator update."""

    name: str | None = None
    emoji: str | None = None
    uptime_rate: float | None = Field(None, ge=0, le=1)
    reward_rate: float | None = Field(None, ge=0, le=1.0)
    slash_rate: float | None = Field(None, ge=0, le=0.50)


class ValidatorInfo(BaseModel):
    """Validator details."""

    validator_id: str
    name: str
    emoji: str
    uptime_rate: float
    reward_rate: float
    slash_rate: float


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------

class NetworkCreate(BaseModel):
    """Request to create a network."""

    network_name: str = Field(..., description="Network name.")
    stake_token: str = Field(..., description="Stake token symbol.")
    emoji: str = Field("", description="Network emoji.")


class NetworkInfo(BaseModel):
    """Network details."""

    network_name: str
    stake_token: str
    emoji: str
    created_at: str | None = None


# ---------------------------------------------------------------------------
# User admin actions
# ---------------------------------------------------------------------------

class GiveRequest(BaseModel):
    """Give USD to a user."""

    amount: float = Field(..., gt=0, description="Amount to give.")


class TakeRequest(BaseModel):
    """Take USD from a user."""

    amount: float = Field(..., gt=0, description="Amount to take.")


class SetBalanceRequest(BaseModel):
    """Set a user's USD balance."""

    wallet: float | None = Field(None, ge=0, description="New wallet balance.")
    bank: float | None = Field(None, ge=0, description="New bank balance.")


# ---------------------------------------------------------------------------
# Halts
# ---------------------------------------------------------------------------

class HaltRequest(BaseModel):
    """Halt a network."""

    network: str = Field(..., description="Network to halt.")


class HaltInfo(BaseModel):
    """Network halt status."""

    network: str
    halted: bool


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

class ChannelAssignment(BaseModel):
    """Channel assignment for a feature."""

    channel_key: str = Field(..., description="Channel setting key.")
    channel_id: str | None = Field(None, description="Discord channel ID.")


class ChannelUpdate(BaseModel):
    """Partial channel assignment update."""

    assignments: dict[str, str | None] = Field(..., description="Map of channel_key -> channel_id.")


# ---------------------------------------------------------------------------
# AI features
# ---------------------------------------------------------------------------

class AIFeatureStatus(BaseModel):
    """Status of an AI feature."""

    feature: str = Field(..., description="Feature key.")
    enabled: bool = Field(..., description="Whether enabled.")


class AIFeatureToggle(BaseModel):
    """Toggle for an AI feature."""

    enabled: bool = Field(..., description="Desired state.")


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

class PersonaCreate(BaseModel):
    """Create a new MM persona."""

    name: str = Field(..., description="Persona name.")
    system_prompt: str = Field("", description="System prompt.")
    avatar_url: str = Field("", description="Avatar URL.")
    trade_bias: str = Field("neutral", description="Trade bias.")
    emoji: str = Field("", description="Emoji.")


class PersonaUpdate(BaseModel):
    """Update a persona."""

    name: str | None = None
    system_prompt: str | None = None
    avatar_url: str | None = None
    trade_bias: str | None = None
    emoji: str | None = None
    active: bool | None = None


class PersonaInfo(BaseModel):
    """Persona details."""

    id: int
    name: str
    system_prompt: str
    avatar_url: str
    trade_bias: str
    emoji: str
    active: bool
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Treasury
# ---------------------------------------------------------------------------

class TreasuryInfo(BaseModel):
    """Treasury balance."""

    balance: float = Field(0.0, description="Current treasury balance.")


class TreasuryAction(BaseModel):
    """Treasury give/drain action."""

    action: str = Field(..., description="Action: 'give' or 'drain'.")
    amount: float = Field(..., gt=0, description="Amount.")
    target_user_id: str | None = Field(None, description="Target user for 'give'.")


# ---------------------------------------------------------------------------
# Fee settings
# ---------------------------------------------------------------------------

class FeeSettings(BaseModel):
    """Platform fee configuration."""

    platform_fee_pct: float | None = None
    platform_fee_min: float | None = None
    platform_fee_max: float | None = None
    treasury_cut_pct: float | None = None


# ---------------------------------------------------------------------------
# Command roles
# ---------------------------------------------------------------------------

class CommandRole(BaseModel):
    """Command role permission."""

    command_name: str
    role_id: str


class CommandRoleCreate(BaseModel):
    """Request to add a command role."""

    command_name: str = Field(..., description="Command name.")
    role_id: str = Field(..., description="Discord role ID.")


# ---------------------------------------------------------------------------
# Drop settings
# ---------------------------------------------------------------------------

class DropSettings(BaseModel):
    """Drop configuration."""

    drop_interval: int | None = None
    drop_min: float | None = None
    drop_max: float | None = None


# ---------------------------------------------------------------------------
# Shop admin
# ---------------------------------------------------------------------------

class ShopItemCreate(BaseModel):
    """Admin request to create a shop item config."""

    key: str = Field(..., description="Unique item key.")
    name: str = Field(..., description="Display name.")
    price: float = Field(0.0, ge=0, description="Price in USD.")
    description: str = Field("", description="Item description.")


# ---------------------------------------------------------------------------
# Auto-delete
# ---------------------------------------------------------------------------

class AutoDeleteSettings(BaseModel):
    """Auto-delete configuration."""

    cmd_delete_after: int = Field(0, ge=0, le=3600, description="Delete command after N seconds (0=off, max 3600).")
    reply_delete_after: int = Field(0, ge=0, le=3600, description="Delete reply after N seconds (0=off, max 3600).")
    ai_cmd_delete_after: int = Field(0, ge=0, le=3600, description="Delete AI (.ask) command after N seconds (0=off, max 3600).")
    ai_reply_delete_after: int = Field(0, ge=0, le=3600, description="Delete AI reply after N seconds (0=off, max 3600).")


# ---------------------------------------------------------------------------
# Scam detection
# ---------------------------------------------------------------------------

class ScamDetectionSettings(BaseModel):
    """Scam detection configuration."""

    scam_detection: bool = Field(False, description="Scam detection enabled.")
    scam_timeout_minutes: int = Field(10, ge=1, description="Timeout duration in minutes.")


# ---------------------------------------------------------------------------
# Security config
# ---------------------------------------------------------------------------

class SecurityConfigUpdate(BaseModel):
    """Per-guild overrides for every SEC_* threshold variable.

    Any field left as ``None`` is not written; the engine falls back to the
    global default from ``security/config.py`` (i.e. the SEC_* env var).
    """

    # Detection windows
    scan_interval_seconds: int | None = Field(None, ge=10, le=3600,
        description="How often (seconds) the background scan loop runs.")
    lookback_seconds: int | None = Field(None, ge=30, le=86400,
        description="Look-back window (seconds) for detector queries.")

    # Economy detectors
    income_velocity_limit: int | None = Field(None, ge=1,
        description="Max income events within lookback_seconds before flagging.")
    gambling_velocity_limit: int | None = Field(None, ge=1,
        description="Max gambling transactions within lookback_seconds.")
    wash_trade_min_cycles: int | None = Field(None, ge=2,
        description="Min cyclic trade count to flag wash trading.")
    transfer_ring_min: int | None = Field(None, ge=2,
        description="Min nodes in a circular transfer ring to flag.")
    lp_churn_min: int | None = Field(None, ge=2,
        description="Min LP add/remove cycles to flag pool manipulation.")
    tx_flood_limit: int | None = Field(None, ge=1,
        description="Max transactions within lookback_seconds to flag TX flood.")

    # API / Session detectors
    auth_failure_limit: int | None = Field(None, ge=1,
        description="Max failed auth attempts per auth_failure_window before flagging.")
    auth_failure_window: int | None = Field(None, ge=60,
        description="Auth failure counting window in seconds.")
    session_ip_change_window: int | None = Field(None, ge=10,
        description="Flag if IP changes within this many seconds.")
    api_request_flood_limit: int | None = Field(None, ge=10,
        description="Max API requests per api_request_flood_window per user.")
    api_request_flood_window: int | None = Field(None, ge=10,
        description="API flood counting window in seconds.")

    # Command flood (bot)
    command_flood_limit: int | None = Field(None, ge=1,
        description="Max distinct bot commands per command_flood_window.")
    command_flood_window: int | None = Field(None, ge=10,
        description="Command flood counting window in seconds.")
    identical_command_limit: int | None = Field(None, ge=1,
        description="Max identical bot command invocations per command_flood_window.")

    # Cross-platform correlation
    correlation_window: int | None = Field(None, ge=30,
        description="Window (seconds) for cross-platform event correlation.")
    correlation_event_min: int | None = Field(None, ge=2,
        description="Min events from both platforms to trigger cross-platform flag.")

    # DeFi exploits
    flash_loan_window: int | None = Field(None, ge=5,
        description="Borrow→trade→repay within N seconds triggers flash-loan flag.")
    oracle_manipulation_trades: int | None = Field(None, ge=2,
        description="Rapid same-token trades within oracle_manipulation_window to flag.")
    oracle_manipulation_window: int | None = Field(None, ge=10,
        description="Oracle manipulation counting window in seconds.")

    # Threat scoring
    score_decay_half_life: float | None = Field(None, ge=60,
        description="Half-life (seconds) for threat score exponential decay.")
    score_weights: dict[str, float] | None = Field(None,
        description="Override point values per detection type.")

    # Response level thresholds
    level_1_threshold: float | None = Field(None, ge=1,
        description="Threat score to trigger level-1 response (log + monitor).")
    level_2_threshold: float | None = Field(None, ge=1,
        description="Threat score to trigger level-2 response (throttle).")
    level_3_threshold: float | None = Field(None, ge=1,
        description="Threat score to trigger level-3 response (freeze).")
    level_4_threshold: float | None = Field(None, ge=1,
        description="Threat score to trigger level-4 response (flag + alert).")
    level_5_threshold: float | None = Field(None, ge=1,
        description="Threat score to trigger level-5 response (lockdown).")

    # Enforcement durations (seconds)
    throttle_duration: int | None = Field(None, ge=30,
        description="Duration (seconds) of a level-2 throttle enforcement.")
    freeze_duration: int | None = Field(None, ge=30,
        description="Duration (seconds) of a level-3 freeze enforcement.")
    flag_duration: int | None = Field(None, ge=30,
        description="Duration (seconds) of a level-4 flag enforcement.")
    lockdown_duration: int | None = Field(None, ge=30,
        description="Duration (seconds) of a level-5 lockdown enforcement.")
    throttled_rate_limit: int | None = Field(None, ge=1,
        description="Requests per 10-second window allowed for throttled users.")

    # Alert deduplication
    alert_cooldown_seconds: int | None = Field(None, ge=30,
        description="Minimum seconds between admin alerts for the same user.")

    # Behavior profiling
    anomaly_stddev_threshold: float | None = Field(None, ge=0.5,
        description="Std-deviation multiplier to flag statistical anomalies.")
    baseline_min_samples: int | None = Field(None, ge=5,
        description="Minimum data-points needed before anomaly detection activates.")

    # Whale / repeat-offender
    whale_concentration_limit: int | None = Field(None, ge=1,
        description="Max tokens a single user may hold (multiples of avg) before whale flag.")
    repeat_offender_limit: int | None = Field(None, ge=1,
        description="Enforcement events before a user is considered a repeat offender.")


# ---------------------------------------------------------------------------
# Blocks admin
# ---------------------------------------------------------------------------

class BlockBundleRequest(BaseModel):
    """Request to bundle pending transactions into a block."""

    network: str = Field(..., description="Network name.")


class BlockRejectRequest(BaseModel):
    """Request to reject a pending block."""

    network: str = Field(..., description="Network name.")
    block_num: int = Field(..., description="Block number to reject.")


class BlockStatus(BaseModel):
    """Block pipeline status."""

    network: str
    pending_txs: int = 0
    latest_block: int = 0
    status: str = "ok"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class BackupInfo(BaseModel):
    """Backup metadata."""

    id: str = Field(..., description="Backup identifier.")
    created_at: str = Field(..., description="When backup was created.")
    size_bytes: int = Field(0, description="Backup size.")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthInfo(BaseModel):
    """Admin health check response."""

    db_connected: bool = False
    redis_connected: bool = False
    active_ws_connections: int = 0
    uptime_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLogEntry(BaseModel):
    """Single audit log entry."""

    id: int
    admin_user_id: str
    action: str
    details: dict[str, Any] | None = None
    created_at: str
