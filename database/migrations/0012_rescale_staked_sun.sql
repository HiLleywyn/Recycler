-- 0012_rescale_staked_sun.sql
-- Rescales all staked_sun values ×1000 to match the SUN price rebase.
--
-- Background: all shop item prices were multiplied by 1000 (e.g. Hashstone
-- went from 75 SUN → 75,000 SUN). Items purchased under the old pricing have
-- staked_sun stored at the old scale. This migration brings existing holders
-- in line with the new scale so level-up costs (5% of staked_sun) are
-- proportional to the new prices.
--
-- Safety: only touches rows where staked_sun > 0 and is still at the old
-- scale. A guard column `staked_rescaled` is added to each table so the
-- migration is fully idempotent  -  re-running it will never double-rescale.

-- ── Hashstones ───────────────────────────────────────────────────────────────

ALTER TABLE hashstones ADD COLUMN IF NOT EXISTS staked_rescaled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE hashstones
   SET staked_sun       = staked_sun * 1000,
       staked_rescaled  = TRUE
 WHERE staked_sun > 0
   AND staked_rescaled  = FALSE;

-- ── Lockstones ───────────────────────────────────────────────────────────────

ALTER TABLE lockstones ADD COLUMN IF NOT EXISTS staked_rescaled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE lockstones
   SET staked_sun       = staked_sun * 1000,
       staked_rescaled  = TRUE
 WHERE staked_sun > 0
   AND staked_rescaled  = FALSE;

-- ── Vaultstones ──────────────────────────────────────────────────────────────

ALTER TABLE vaultstones ADD COLUMN IF NOT EXISTS staked_rescaled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE vaultstones
   SET staked_sun       = staked_sun * 1000,
       staked_rescaled  = TRUE
 WHERE staked_sun > 0
   AND staked_rescaled  = FALSE;

-- ── Gambastones ──────────────────────────────────────────────────────────────

ALTER TABLE gambastones ADD COLUMN IF NOT EXISTS staked_rescaled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE gambastones
   SET staked_sun       = staked_sun * 1000,
       staked_rescaled  = TRUE
 WHERE staked_sun > 0
   AND staked_rescaled  = FALSE;
