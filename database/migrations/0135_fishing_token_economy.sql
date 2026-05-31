-- Fishing token economy: USD payouts retired in favor of LURE / REEL.
--
-- Catalogue rename:
--   user_fishing.total_payout_raw   -> total_lure_earned_raw
--   fishing_catches.payout_raw      -> payout_lure_raw
--
-- New columns on user_fishing for the LURE staking pool:
--   lure_staked_raw         -- currently locked LURE (raw NUMERIC(36,0))
--   lure_yield_pending_raw  -- accrued REEL not yet claimed (raw)
--   last_stake_yield_at     -- DB-side clock; yield accrues on the DB clock
--                              per the project rule against datetime.now()
--                              vs Postgres timestamp
--   total_reel_earned_raw   -- lifetime REEL earned (analytics)
--   total_usd_cashout_raw   -- lifetime USD wallet credit from REEL burns
--                              (analytics; the actual wallet credit goes
--                              through users.wallet via update_wallet)
--
-- New column on fishing_catches:
--   payout_symbol  -- 'LURE' or 'USD' (legacy rows from before the cutover
--                     are tagged 'USD' so reports / leaderboards can split
--                     pre-/post-cutover values).
--
-- Cutover policy: existing rows had USD-denominated totals. Since the
-- fishing system was merged hours before this migration with negligible
-- live data, we zero the USD totals out and let players accrue clean
-- LURE-denominated totals from this point. The original USD they earned
-- already paid into their wallet at sell/cast time -- nothing is taken
-- away from them, only the LIFETIME-EARNED counter restarts.

ALTER TABLE user_fishing
    RENAME COLUMN total_payout_raw TO total_lure_earned_raw;

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS lure_staked_raw         NUMERIC(36, 0) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lure_yield_pending_raw  NUMERIC(36, 0) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_stake_yield_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_reel_earned_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_usd_cashout_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0;

-- Zero the renamed USD totals: they are no longer comparable to LURE.
UPDATE user_fishing SET total_lure_earned_raw = 0;

-- CHECK constraints for the new columns. NOT VALID first then VALIDATE
-- so we never lock the table on a fresh-merged install.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_fishing_stake_nonneg_chk'
    ) THEN
        ALTER TABLE user_fishing
            ADD CONSTRAINT user_fishing_stake_nonneg_chk
            CHECK (
                lure_staked_raw         >= 0
                AND lure_yield_pending_raw >= 0
                AND total_reel_earned_raw  >= 0
                AND total_usd_cashout_raw  >= 0
            ) NOT VALID;
        ALTER TABLE user_fishing VALIDATE CONSTRAINT user_fishing_stake_nonneg_chk;
    END IF;
END$$;

-- Drop the old payout-ordered leaderboard index and rebuild on the
-- renamed column so ,fish lb stays fast.
DROP INDEX IF EXISTS user_fishing_payout_idx;
CREATE INDEX IF NOT EXISTS user_fishing_lure_idx
    ON user_fishing (guild_id, total_lure_earned_raw DESC);

-- Catch log: rename payout column and add an explicit currency tag.
ALTER TABLE fishing_catches
    RENAME COLUMN payout_raw TO payout_lure_raw;

ALTER TABLE fishing_catches
    ADD COLUMN IF NOT EXISTS payout_symbol TEXT NOT NULL DEFAULT 'LURE';

-- Tag every pre-cutover row as USD-denominated so any historical query
-- can filter / convert correctly. Going forward the application writes
-- 'LURE' explicitly.
UPDATE fishing_catches SET payout_symbol = 'USD' WHERE caught_at < NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fishing_catches_payout_symbol_chk'
    ) THEN
        ALTER TABLE fishing_catches
            ADD CONSTRAINT fishing_catches_payout_symbol_chk
            CHECK (payout_symbol IN ('LURE', 'USD')) NOT VALID;
        ALTER TABLE fishing_catches VALIDATE CONSTRAINT fishing_catches_payout_symbol_chk;
    END IF;
END$$;
