-- ============================================================================
-- 0008: Security System Tables
-- Institutional-grade threat detection, enforcement, and audit trail.
-- ============================================================================

-- Security events  -  all detections from the security engine
CREATE TABLE IF NOT EXISTS security_events (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT       NOT NULL,
    event_type  TEXT         NOT NULL,
    severity    TEXT         NOT NULL,
    score_delta NUMERIC(5,2) NOT NULL DEFAULT 0,
    details     JSONB        NOT NULL DEFAULT '{}',
    source      TEXT         NOT NULL DEFAULT 'system',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sec_events_guild
    ON security_events (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_user
    ON security_events (guild_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_type
    ON security_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_severity
    ON security_events (severity, created_at DESC);

-- Active and historical enforcements
CREATE TABLE IF NOT EXISTS security_enforcements (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT,
    action_type TEXT         NOT NULL,
    scope       TEXT         NOT NULL,
    reason      TEXT         NOT NULL,
    enacted_by  TEXT         NOT NULL DEFAULT 'auto',
    expires_at  TIMESTAMPTZ,
    lifted_at   TIMESTAMPTZ,
    lifted_by   TEXT,
    details     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sec_enforce_active
    ON security_enforcements (guild_id, user_id)
    WHERE lifted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sec_enforce_guild
    ON security_enforcements (guild_id, created_at DESC);

-- Persistent user security profiles
CREATE TABLE IF NOT EXISTS security_profiles (
    user_id      BIGINT       NOT NULL,
    guild_id     BIGINT       NOT NULL,
    threat_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    total_flags  INTEGER      NOT NULL DEFAULT 0,
    last_flagged TIMESTAMPTZ,
    baseline     JSONB        NOT NULL DEFAULT '{}',
    known_ips    JSONB        NOT NULL DEFAULT '[]',
    risk_level   TEXT         NOT NULL DEFAULT 'normal',
    notes        TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE TRIGGER trg_sec_profiles_updated
    BEFORE UPDATE ON security_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Security audit log  -  admin actions on the security system
CREATE TABLE IF NOT EXISTS security_audit (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    admin_id    BIGINT       NOT NULL,
    action      TEXT         NOT NULL,
    target_user BIGINT,
    details     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sec_audit_guild
    ON security_audit (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_audit_admin
    ON security_audit (admin_id, created_at DESC);
