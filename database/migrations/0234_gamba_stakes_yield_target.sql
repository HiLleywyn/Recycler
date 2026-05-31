-- 0234_gamba_stakes_yield_target.sql
--
-- Per-position yield-target on gamba game-token stakes:
--   * yield_target: 'GBC' (default, existing behaviour) or 'BUD'.
--     A staked PIP / ACE / VEIN / etc. drips one or the other, never
--     both. Players who want a split open separate positions over
--     time. Constraint stays application-side (services/gamba.py) so
--     a future REEL / HRV target is config-only.
--   * Renames pending_gbc -> pending_yield_raw because the column now
--     holds raw payout in whichever target the row points at.
--   * Adds an index keyed on (guild_id, user_id, yield_target) so
--     leaderboards / panels can pick out BUD-target positions cheaply.
--
-- Existing rows default to 'GBC', so behaviour is unchanged for every
-- current player on deploy. The eight game tokens (GAMBIT, CROWN,
-- VEIN, PIP, EDGE, ACE, NOIR, CHERRY) are the only symbols that ever
-- land in this table.

ALTER TABLE gamba_stakes
    ADD COLUMN IF NOT EXISTS yield_target VARCHAR(8) NOT NULL DEFAULT 'GBC';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'gamba_stakes'
           AND column_name = 'pending_gbc'
    ) AND NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'gamba_stakes'
           AND column_name = 'pending_yield_raw'
    ) THEN
        ALTER TABLE gamba_stakes RENAME COLUMN pending_gbc TO pending_yield_raw;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_gamba_stakes_target
    ON gamba_stakes (guild_id, user_id, yield_target)
    WHERE amount > 0;
