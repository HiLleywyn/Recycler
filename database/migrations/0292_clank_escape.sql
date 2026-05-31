-- 0292_clank_escape.sql
-- Per-user escape room progress for the Clanktank containment system.
CREATE TABLE IF NOT EXISTS clank_escape (
    user_id         BIGINT      NOT NULL,
    guild_id        BIGINT      NOT NULL,
    case_num        INT         NOT NULL DEFAULT (FLOOR(RANDOM() * 899999) + 100001)::INT,
    thread_id       BIGINT,
    step            SMALLINT    NOT NULL DEFAULT 0,
    step_data       JSONB       NOT NULL DEFAULT '{}',
    fail_count      SMALLINT    NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    step_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);
