-- Quests: daily + weekly rotating objectives.
--
-- The quest catalog (id, name, target, reward, trigger, period) lives in
-- quests_config.py, same pattern as achievements. This migration creates
-- the user-facing progress table only.
--
-- A user's quest for a given period/slot is identified by
-- (user_id, guild_id, period, period_key, slot). period_key is a string
-- scoped to the period type:
--   daily  : YYYY-MM-DD (UTC)
--   weekly : YYYY-Www  (ISO week, UTC)
-- This lets the assignment stay idempotent across multiple views in the
-- same window -- the first view populates the row and subsequent views
-- read it back.

CREATE TABLE IF NOT EXISTS user_quests (
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL,
    period       TEXT        NOT NULL CHECK (period IN ('daily', 'weekly')),
    period_key   TEXT        NOT NULL,
    slot         INT         NOT NULL,
    quest_id     TEXT        NOT NULL,
    progress     INT         NOT NULL DEFAULT 0,
    target       INT         NOT NULL,
    reward_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    claimed      BOOLEAN     NOT NULL DEFAULT FALSE,
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at   TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id, period, period_key, slot)
);

-- Fast lookup for the "quests for this user right now" card.
CREATE INDEX IF NOT EXISTS user_quests_current_idx
    ON user_quests (user_id, guild_id, period, period_key);

-- Lets us cheaply find unclaimed completed quests (for auto-claim or
-- claim-all commands).
CREATE INDEX IF NOT EXISTS user_quests_unclaimed_idx
    ON user_quests (user_id, guild_id)
    WHERE NOT claimed;
