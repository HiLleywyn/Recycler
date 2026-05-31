-- 0084: Group LP positions for cross-group token pools
--
-- When two groups accept a pool partnership their vault tokens are used to
-- seed the pool with initial liquidity. Each group receives LP shares equal
-- to half the initial mint (both sides contribute equal USD value so the
-- split is always 50/50).  These positions are tracked here, separately
-- from the per-user lp_positions table, so the group LP counts toward
-- pool total_lp (keeping users below the 50% concentration cap) without
-- appearing as any individual user's holding.
--
-- last_harvest_at enforces the 24-hour harvest cooldown per (group, pool).

CREATE TABLE IF NOT EXISTS group_lp_positions (
    group_id        TEXT          NOT NULL,
    guild_id        BIGINT        NOT NULL,
    pool_id         TEXT          NOT NULL,
    lp_shares       NUMERIC(36,0) NOT NULL DEFAULT 0,
    seeded_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_harvest_at TIMESTAMPTZ,
    PRIMARY KEY (group_id, guild_id, pool_id)
);

CREATE INDEX IF NOT EXISTS idx_group_lp_guild_pool
    ON group_lp_positions (guild_id, pool_id);
