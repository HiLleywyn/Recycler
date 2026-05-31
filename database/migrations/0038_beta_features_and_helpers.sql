-- Auto-compound settings: tracks which farms auto-compound for each user
CREATE TABLE IF NOT EXISTS auto_compound_settings (
    user_id           BIGINT NOT NULL,
    guild_id          BIGINT NOT NULL,
    validator_id      TEXT   NOT NULL,
    symbol            TEXT   NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    total_compounded  NUMERIC(28,8) NOT NULL DEFAULT 0,
    compound_count    INTEGER NOT NULL DEFAULT 0,
    last_compound_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, validator_id, symbol)
);

-- Track earnings per stake position
-- session_earned: resets on full unstake (shows "earned this position")
-- total_earned: lifetime accumulator (never resets, used for leaderboards)
ALTER TABLE stakes ADD COLUMN IF NOT EXISTS session_earned NUMERIC(28,8) NOT NULL DEFAULT 0;
ALTER TABLE stakes ADD COLUMN IF NOT EXISTS total_earned NUMERIC(28,8) NOT NULL DEFAULT 0;

-- Same split for delegations (total_earned already exists, add session_earned)
ALTER TABLE pos_delegations ADD COLUMN IF NOT EXISTS session_earned NUMERIC(28,8) NOT NULL DEFAULT 0;

-- Liqstone table (LP-based leveled stone, same schema as other stones)
CREATE TABLE IF NOT EXISTS liqstones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_sun  NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_liqstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_liqstones_level CHECK (level >= 1),
    CONSTRAINT chk_liqstones_xp    CHECK (xp >= 0)
);

-- Error feed severity filter setting
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS error_feed_levels TEXT DEFAULT 'WARNING,LOW,MEDIUM,HIGH,CRITICAL';

-- Price alerts: user-defined token price triggers
CREATE TABLE IF NOT EXISTS price_alerts (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    symbol       TEXT   NOT NULL,
    direction    TEXT   NOT NULL CHECK (direction IN ('above', 'below')),
    target_price NUMERIC(28,8) NOT NULL,
    triggered    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    triggered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_price_alerts_active
    ON price_alerts (guild_id, symbol) WHERE triggered = FALSE;

-- Game helpers / Game Masters
CREATE TABLE IF NOT EXISTS game_helpers (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    granted_by  BIGINT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, user_id)
);

-- Exploit / Crypto Heist game tables
CREATE TABLE IF NOT EXISTS exploit_shields (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    active_until TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS exploit_stats (
    user_id          BIGINT NOT NULL,
    guild_id         BIGINT NOT NULL,
    heists_attempted INTEGER NOT NULL DEFAULT 0,
    heists_won       INTEGER NOT NULL DEFAULT 0,
    total_stolen     NUMERIC(28,8) NOT NULL DEFAULT 0,
    times_targeted   INTEGER NOT NULL DEFAULT 0,
    times_defended   INTEGER NOT NULL DEFAULT 0,
    total_lost       NUMERIC(28,8) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS exploit_history (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    attacker_id BIGINT NOT NULL,
    target_id   BIGINT NOT NULL,
    tier        TEXT   NOT NULL,
    wager       NUMERIC(28,8) NOT NULL,
    stolen      NUMERIC(28,8) NOT NULL DEFAULT 0,
    won         BOOLEAN NOT NULL,
    shielded    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exploit_history_guild
    ON exploit_history (guild_id, created_at DESC);

-- Audit log for helper actions
CREATE TABLE IF NOT EXISTS helper_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    helper_id   BIGINT NOT NULL,
    action      TEXT   NOT NULL,
    target_id   BIGINT,
    details     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_helper_audit_guild
    ON helper_audit_log (guild_id, created_at DESC);
