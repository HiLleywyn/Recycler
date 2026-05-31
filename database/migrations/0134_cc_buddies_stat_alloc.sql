-- Buddy stat-point allocation.
--
-- Each level grants STAT_POINTS_PER_LEVEL points (see buddies_config.py).
-- Players spend points across three tracks via ,buddy upgrade:
--   hp_alloc  -- +STAT_POINT_HP_BONUS  to base max HP per point
--   atk_alloc -- +STAT_POINT_ATK_BONUS to base ATK per point
--   spd_alloc -- +STAT_POINT_SPD_BONUS to SPD per point (capped at 1.0)
--
-- Available points = level - (hp_alloc + atk_alloc + spd_alloc), so the
-- "spent <= level" invariant is enforced by the upgrade modal in the cog
-- (we deliberately do NOT add a CHECK against `level` here -- level
-- decreases would otherwise have to refund alloc, and the column-level
-- CHECK on a sum-vs-other-column constraint can't be expressed cleanly
-- without triggers).
--
-- Allocations are sticky across swap and across level changes; only
-- reroll (which destroys the buddy entirely) clears them. Defaults to 0
-- so existing buddies start with their full level worth of unspent points.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS hp_alloc  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS atk_alloc INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS spd_alloc INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_alloc_nonneg_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_alloc_nonneg_chk
            CHECK (hp_alloc >= 0 AND atk_alloc >= 0 AND spd_alloc >= 0) NOT VALID;
        ALTER TABLE cc_buddies VALIDATE CONSTRAINT cc_buddies_alloc_nonneg_chk;
    END IF;
END$$;
