-- Guild challenges: server-wide collective goals with a shared reward pool.
--
-- Admins start a challenge keyed by a bus-event trigger ("block_mined",
-- "trade_executed", etc.) with a numeric target, a reward pool, and a
-- deadline. Every event that fires while the challenge is active
-- increments both the global progress counter AND a per-user contribution
-- row, so when the target is hit the pool can be split proportionally.
--
-- Only ONE active challenge per (guild_id, trigger). Multiple active
-- challenges across DIFFERENT triggers are fine so admins can run a
-- "mine + trade" week with two parallel goals.

CREATE TABLE IF NOT EXISTS guild_challenges (
    challenge_id     BIGSERIAL    PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    name             TEXT         NOT NULL,
    description      TEXT         NOT NULL DEFAULT '',
    trigger          TEXT         NOT NULL,
    target           BIGINT       NOT NULL,
    progress         BIGINT       NOT NULL DEFAULT 0,
    reward_pool_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    started_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ends_at          TIMESTAMPTZ  NOT NULL,
    completed_at     TIMESTAMPTZ,
    status           TEXT         NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active', 'succeeded', 'failed'))
);

-- Only one active challenge per guild + trigger. Multiple finalized
-- challenges for the same trigger are fine (that's the history).
CREATE UNIQUE INDEX IF NOT EXISTS guild_challenges_one_active_per_trigger_idx
    ON guild_challenges (guild_id, trigger)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS guild_challenges_guild_status_idx
    ON guild_challenges (guild_id, status);

-- Per-user contribution ledger for proportional payout at success.
CREATE TABLE IF NOT EXISTS guild_challenge_contributions (
    challenge_id   BIGINT      NOT NULL REFERENCES guild_challenges(challenge_id)
                                      ON DELETE CASCADE,
    user_id        BIGINT      NOT NULL,
    guild_id       BIGINT      NOT NULL,
    contribution   BIGINT      NOT NULL DEFAULT 0,
    reward_paid    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (challenge_id, user_id)
);

CREATE INDEX IF NOT EXISTS guild_challenge_contributions_user_idx
    ON guild_challenge_contributions (user_id, guild_id);
