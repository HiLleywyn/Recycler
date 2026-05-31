-- 0286_clanktank_evidence.sql
-- Expands clanktank with evidence storage, account connections, and audit log.

-- Evidence: messages logged at clank time and during containment
CREATE TABLE IF NOT EXISTS clanker_evidence (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       BIGINT       NOT NULL,
    guild_id      BIGINT       NOT NULL,
    message_id    BIGINT,
    channel_id    BIGINT,
    content       TEXT         NOT NULL,
    sent_at       TIMESTAMPTZ,
    logged_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    evidence_type TEXT         NOT NULL DEFAULT 'pre_clank_message'
);

CREATE INDEX IF NOT EXISTS idx_clanker_evidence_user
    ON clanker_evidence (user_id, guild_id, logged_at DESC);

-- Account connections: detected similarity between clankers
CREATE TABLE IF NOT EXISTS clanker_connections (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    user_id_a     BIGINT       NOT NULL,
    user_id_b     BIGINT       NOT NULL,
    reasons       TEXT[]       NOT NULL DEFAULT '{}',
    name_score    FLOAT        NOT NULL DEFAULT 0.0,
    text_score    FLOAT        NOT NULL DEFAULT 0.0,
    detected_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, user_id_a, user_id_b)
);

CREATE INDEX IF NOT EXISTS idx_clanker_connections_a
    ON clanker_connections (guild_id, user_id_a);
CREATE INDEX IF NOT EXISTS idx_clanker_connections_b
    ON clanker_connections (guild_id, user_id_b);

-- Comprehensive audit log for all clanktank events
CREATE TABLE IF NOT EXISTS clanker_audit_log (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    user_id       BIGINT,
    actor_id      BIGINT,
    event_type    TEXT         NOT NULL,
    details       JSONB,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clanker_audit_guild
    ON clanker_audit_log (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clanker_audit_user
    ON clanker_audit_log (user_id, guild_id, created_at DESC);

-- Extend clanker_records with identity and context columns
ALTER TABLE clanker_records
    ADD COLUMN IF NOT EXISTS clank_context  TEXT,
    ADD COLUMN IF NOT EXISTS usernames      TEXT[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS display_names  TEXT[]  NOT NULL DEFAULT '{}';
