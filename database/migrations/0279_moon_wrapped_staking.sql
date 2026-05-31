-- 0279_moon_wrapped_staking.sql
--
-- Moon Network overhaul: adds wrapped-asset dual-yield staking.
--
-- Players stake mBTC or mSUN into moon_wrapped_stakes and earn TWO legs on
-- the hourly Moon tick: more of the staked wrapped asset, plus MOON. Both
-- legs accrue into pending_self / pending_moon (raw-scaled) and are moved to
-- the wallet by ,moon stake claim -- unlike Lunar Mint (auto-credit), this
-- tier is claim-based so ,moon stake claim has a real job.
--
-- staked_at drives the 12h warmup; last_accrued_at is the accrual cursor so
-- the tick credits exactly the elapsed time once.

CREATE TABLE IF NOT EXISTS moon_wrapped_stakes (
    user_id          BIGINT        NOT NULL,
    guild_id         BIGINT        NOT NULL,
    symbol           TEXT          NOT NULL,                 -- 'MBTC' or 'MSUN'
    amount           NUMERIC(36,0) NOT NULL DEFAULT 0,       -- staked, raw-scaled
    staked_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),   -- warmup anchor
    last_accrued_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),   -- accrual cursor
    pending_self     NUMERIC(36,0) NOT NULL DEFAULT 0,       -- claimable mBTC/mSUN, raw
    pending_moon     NUMERIC(36,0) NOT NULL DEFAULT 0,       -- claimable MOON, raw
    session_earned   NUMERIC(28,8) NOT NULL DEFAULT 0,       -- USD-valued, current session
    total_earned     NUMERIC(28,8) NOT NULL DEFAULT 0,       -- USD-valued, lifetime
    PRIMARY KEY (user_id, guild_id, symbol),
    CONSTRAINT chk_moon_wrapped_symbol CHECK (symbol IN ('MBTC', 'MSUN'))
);

-- The hourly tick scans every active position in a guild.
CREATE INDEX IF NOT EXISTS idx_moon_wrapped_stakes_guild
    ON moon_wrapped_stakes (guild_id) WHERE amount > 0;
