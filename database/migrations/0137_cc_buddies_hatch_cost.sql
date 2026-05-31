-- Dynamic-cost buddy hatching.
--
-- Hatching used to be free. New economy:
--   * First HATCH_FREE_COUNT (=3) lifetime hatches per user are free.
--   * Hatch #4 costs HATCH_BASE_PRICE_USD ($10,000), and every paid hatch
--     after that doubles the price (10k -> 20k -> 40k -> 80k -> ...).
--   * After HATCH_STREAK_RESET_SECONDS (= 7 days) with NO new hatch, the
--     "paid streak" resets and the next hatch is back to $10k.
--
-- Three new columns on cc_buddy_hatches (one row per user per guild,
-- already lifetime-keyed by (guild_id, user_id)):
--
--   hatch_count          INTEGER -- lifetime hatches this user has performed
--                                   (free + paid). Drives the "free 3" gate.
--   paid_streak          INTEGER -- consecutive paid hatches in the current
--                                   streak. Used as the doubling exponent:
--                                   cost = base * 2 ** paid_streak BEFORE
--                                   the hatch is committed; on commit we
--                                   set paid_streak = paid_streak + 1.
--   last_paid_hatch_at   TIMESTAMPTZ -- DB clock for the 7-day reset.
--                                       NULL means "never paid yet" (which
--                                       the hatch command treats the same
--                                       as a fully decayed streak).
--
-- Existing rows default to 0 / NULL so legacy users get a fresh "first 3
-- free" allowance under the new system. This is intentional and one-time:
-- nobody on the bot today has been charged for a hatch, so reading their
-- prior hatch history into hatch_count would be punishing them for a
-- mechanic that did not exist when they hatched. Cheap to give back, and
-- the lifetime curve still applies from this point forward.

ALTER TABLE cc_buddy_hatches
    ADD COLUMN IF NOT EXISTS hatch_count        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS paid_streak        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_paid_hatch_at TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddy_hatches_count_nonneg_chk'
    ) THEN
        ALTER TABLE cc_buddy_hatches
            ADD CONSTRAINT cc_buddy_hatches_count_nonneg_chk
            CHECK (hatch_count >= 0 AND paid_streak >= 0) NOT VALID;
        ALTER TABLE cc_buddy_hatches VALIDATE CONSTRAINT cc_buddy_hatches_count_nonneg_chk;
    END IF;
END$$;
