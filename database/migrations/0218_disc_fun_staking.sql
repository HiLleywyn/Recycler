-- 0218_disc_fun_staking.sql
--
-- Disc.Fun staking: holders of graduated proto tokens can stake them
-- back into the launchpad to earn DFUN yield. The yield is denominated
-- in DFUN and proportional to the staked position's spot value (looked
-- up via the SYMBOL/DFUN AMM pool) times Config.DISCFUN["staking_apy"].
--
-- Lazy accrual model (matches services/safety_module.py): the stake
-- row tracks last_accrue and pending_dfun. Each stake / unstake /
-- claim event re-computes the elapsed yield since last_accrue and adds
-- it to pending_dfun. No background tick needed; the position keeps
-- earning even if the bot is offline because the timestamp is on the
-- DB clock (NOW() everywhere).

CREATE TABLE IF NOT EXISTS discfun_stakes (
    user_id           BIGINT          NOT NULL,
    guild_id          BIGINT          NOT NULL,
    symbol            TEXT            NOT NULL,
    amount            NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    pending_dfun      NUMERIC(36, 0)  NOT NULL DEFAULT 0,  -- accrued + unclaimed
    total_claimed     NUMERIC(36, 0)  NOT NULL DEFAULT 0,  -- lifetime DFUN claimed
    auto_compound     BOOLEAN         NOT NULL DEFAULT FALSE,
    total_compounded  NUMERIC(36, 0)  NOT NULL DEFAULT 0,  -- lifetime SYM auto-restaked
    last_accrue       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    staked_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, symbol)
);

-- Idempotent column adds for any environment that ran an earlier draft
-- of this migration (no auto_compound / total_compounded columns).
ALTER TABLE discfun_stakes
    ADD COLUMN IF NOT EXISTS auto_compound    BOOLEAN        NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS total_compounded NUMERIC(36, 0) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_discfun_stakes_user
    ON discfun_stakes (guild_id, user_id)
    WHERE amount > 0;

CREATE INDEX IF NOT EXISTS idx_discfun_stakes_symbol
    ON discfun_stakes (guild_id, symbol)
    WHERE amount > 0;
