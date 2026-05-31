-- 0083: Group pool partnerships
-- Adds is_group_pool flag to pools (bypasses circuit breaker).
-- Adds group_pool_proposals table for the cross-group LP invite system.

ALTER TABLE pools ADD COLUMN IF NOT EXISTS is_group_pool BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill existing pools that involve a group token on either side.
UPDATE pools
SET is_group_pool = TRUE
WHERE EXISTS (
    SELECT 1 FROM mining_groups mg
    WHERE mg.guild_id = pools.guild_id
      AND mg.token_symbol IS NOT NULL
      AND mg.token_symbol IN (pools.token_a, pools.token_b)
);

-- Pending cross-group LP partnership proposals.
-- One outstanding proposal per (guild, proposer_group, target_group) pair.
CREATE TABLE IF NOT EXISTS group_pool_proposals (
    id             BIGSERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL,
    proposer_group TEXT   NOT NULL,
    target_group   TEXT   NOT NULL,
    proposed_by    BIGINT NOT NULL,
    token_a        TEXT   NOT NULL,
    token_b        TEXT   NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, proposer_group, target_group)
);
