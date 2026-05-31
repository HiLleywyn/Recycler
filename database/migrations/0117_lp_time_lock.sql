-- LP time-lock: let users commit an LP position for 7/30/90 days in exchange
-- for a Liqstone-XP multiplier. Breaking the lock early burns a slice of the
-- user's LP shares, so commitment has real teeth and flaky LPs can't gauge-
-- game the multiplier.
--
-- Columns:
--   lock_tier     SMALLINT NOT NULL DEFAULT 0
--       0 = no active lock (default for every existing row)
--       1 = 7-day lock,   Liqstone XP multiplier from Config.LP_LOCK_TIERS
--       2 = 30-day lock,  (ditto)
--       3 = 90-day lock,  (ditto)
--   locked_until  TIMESTAMPTZ NULL
--       Absolute expiry timestamp. NULL when tier=0. Liqstone tick treats
--       any row with locked_until <= NOW() as unlocked (no DB write needed
--       to lapse a lock -- it expires implicitly).
--
-- Safe to re-run: both columns are ADD IF NOT EXISTS + constraints gated on
-- NOT EXISTS lookups in pg_constraint.

ALTER TABLE lp_positions
    ADD COLUMN IF NOT EXISTS lock_tier    SMALLINT    NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'lp_positions_lock_tier_chk'
    ) THEN
        ALTER TABLE lp_positions
            ADD CONSTRAINT lp_positions_lock_tier_chk
            CHECK (lock_tier BETWEEN 0 AND 3) NOT VALID;
        ALTER TABLE lp_positions VALIDATE CONSTRAINT lp_positions_lock_tier_chk;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'lp_positions_lock_tier_pair_chk'
    ) THEN
        -- tier=0 iff locked_until IS NULL (both or neither). Keeps the
        -- two columns in sync so downstream code only has to check tier.
        ALTER TABLE lp_positions
            ADD CONSTRAINT lp_positions_lock_tier_pair_chk
            CHECK (
                (lock_tier = 0 AND locked_until IS NULL)
                OR (lock_tier > 0 AND locked_until IS NOT NULL)
            ) NOT VALID;
        ALTER TABLE lp_positions VALIDATE CONSTRAINT lp_positions_lock_tier_pair_chk;
    END IF;
END $$;
