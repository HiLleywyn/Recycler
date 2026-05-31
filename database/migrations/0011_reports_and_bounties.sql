-- 0011_reports_and_bounties.sql
-- Creates the reports/bounties tables and all dependent columns for existing
-- databases that were created before these were added to schema.sql.
-- All statements are fully idempotent (IF NOT EXISTS / IF NOT EXISTS column).

-- ── Reports ticket table ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reports (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    category      TEXT        NOT NULL,
    message       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'open',
    admin_note    TEXT,
    tags          TEXT,
    dm_message_id BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reports_guild ON reports (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_reports_user  ON reports (user_id);

-- reward_amount was added after the initial reports table was shipped
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reward_amount NUMERIC(28,8) DEFAULT 0;

-- ── Bounties table ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bounties (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    title         TEXT         NOT NULL,
    description   TEXT         NOT NULL DEFAULT '',
    category      TEXT         NOT NULL DEFAULT 'bugs',
    reward_amount NUMERIC(28,8) NOT NULL DEFAULT 0,
    max_claims    INTEGER      NOT NULL DEFAULT 0,  -- 0 = unlimited
    claims        INTEGER      NOT NULL DEFAULT 0,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by    BIGINT       NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bounties_guild ON bounties (guild_id, is_active);

-- ── guild_settings columns for the reports feed ──────────────────────────────

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reports_feed_channel    BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reports_feed_categories TEXT;
