-- Safety Module: AAVE/DSY staking positions (mirrors Aave Safety Module mechanics)
-- Stakers earn yield in USDC (AAVE) or DSD (DSY) from protocol fees.
-- A 24-hour unstake cooldown must be started before withdrawal is allowed.

CREATE TABLE IF NOT EXISTS safety_module_stakes (
    user_id         BIGINT          NOT NULL,
    guild_id        BIGINT          NOT NULL,
    symbol          VARCHAR(16)     NOT NULL,  -- 'AAVE' or 'DSY'
    amount          NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    last_yield      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    staked_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    cooldown_at     TIMESTAMPTZ,               -- NULL = no active cooldown
    PRIMARY KEY (user_id, guild_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_sm_stakes_guild ON safety_module_stakes (guild_id, symbol);
