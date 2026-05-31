-- ============================================================================
-- 0013: Per-guild security configuration table
-- Stores optional per-guild overrides for every SEC_* threshold variable.
-- NULL in any column means "use the global default from security/config.py".
-- ============================================================================

CREATE TABLE IF NOT EXISTS guild_security_config (
    guild_id                    BIGINT      PRIMARY KEY
                                            REFERENCES guild_settings(guild_id) ON DELETE CASCADE,

    -- Detection windows
    scan_interval_seconds       INTEGER,
    lookback_seconds            INTEGER,

    -- Economy detectors
    income_velocity_limit       INTEGER,
    gambling_velocity_limit     INTEGER,
    wash_trade_min_cycles       INTEGER,
    transfer_ring_min           INTEGER,
    lp_churn_min                INTEGER,
    tx_flood_limit              INTEGER,

    -- API / Session detectors
    auth_failure_limit          INTEGER,
    auth_failure_window         INTEGER,
    session_ip_change_window    INTEGER,
    api_request_flood_limit     INTEGER,
    api_request_flood_window    INTEGER,

    -- Command flood (bot)
    command_flood_limit         INTEGER,
    command_flood_window        INTEGER,
    identical_command_limit     INTEGER,

    -- Cross-platform correlation
    correlation_window          INTEGER,
    correlation_event_min       INTEGER,

    -- DeFi exploit patterns
    flash_loan_window           INTEGER,
    oracle_manipulation_trades  INTEGER,
    oracle_manipulation_window  INTEGER,

    -- Threat scoring
    score_decay_half_life       NUMERIC(10, 2),
    score_weights               JSONB,

    -- Response level thresholds
    level_1_threshold           NUMERIC(6, 2),
    level_2_threshold           NUMERIC(6, 2),
    level_3_threshold           NUMERIC(6, 2),
    level_4_threshold           NUMERIC(6, 2),
    level_5_threshold           NUMERIC(6, 2),

    -- Enforcement durations (seconds)
    throttle_duration           INTEGER,
    freeze_duration             INTEGER,
    flag_duration               INTEGER,
    lockdown_duration           INTEGER,
    throttled_rate_limit        INTEGER,

    -- Alert deduplication
    alert_cooldown_seconds      INTEGER,

    -- Behavior profiling
    anomaly_stddev_threshold    NUMERIC(5, 2),
    baseline_min_samples        INTEGER,

    -- Whale / repeat-offender limits
    whale_concentration_limit   INTEGER,
    repeat_offender_limit       INTEGER,

    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
