-- 0272_sage_cycle_expansion.sql
--
-- Add the Cycle Phase game (,cycle) to the Sage Network and prepare the
-- schema for compound Pattern-Lab rounds (no new tables needed for those;
-- compound state lives in-memory inside the run, then resolves through
-- the existing sage_runs row).
--
--   * user_sage gains a best_cycle_streak column mirroring the existing
--     best_pattern_streak / best_gauge_streak / best_tknom_streak fields.
--   * sage_runs.game CHECK constraint is rebuilt to accept 'cycle' alongside
--     the original three game types.

ALTER TABLE user_sage
    ADD COLUMN IF NOT EXISTS best_cycle_streak INTEGER NOT NULL DEFAULT 0;

-- Replace the old game CHECK constraint atomically so a partial drop never
-- leaves the table without one.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'sage_runs_game_check'
    ) THEN
        ALTER TABLE sage_runs DROP CONSTRAINT sage_runs_game_check;
    END IF;
    ALTER TABLE sage_runs
        ADD CONSTRAINT sage_runs_game_check
        CHECK (game IN ('pattern', 'gauge', 'tknom', 'cycle'));
END $$;
