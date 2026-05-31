-- 0285_clanktank.sql
-- Clanktank containment system: stores state for CLANKER users.
CREATE TABLE IF NOT EXISTS clanker_records (
    user_id               BIGINT          NOT NULL,
    guild_id              BIGINT          NOT NULL,
    stored_roles          BIGINT[]        NOT NULL DEFAULT '{}',
    clanked_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    reason                TEXT,
    message_count         INTEGER         NOT NULL DEFAULT 0,
    last_message_at       TIMESTAMPTZ,
    blocked_command_count INTEGER         NOT NULL DEFAULT 0,
    escape_attempts       INTEGER         NOT NULL DEFAULT 0,
    score                 INTEGER         NOT NULL DEFAULT 0,
    flags                 TEXT[]          NOT NULL DEFAULT '{}',
    linked_accounts       BIGINT[]        NOT NULL DEFAULT '{}',
    expires_at            TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_clanker_records_guild
    ON clanker_records (guild_id);

CREATE INDEX IF NOT EXISTS idx_clanker_records_score
    ON clanker_records (guild_id, score DESC);
