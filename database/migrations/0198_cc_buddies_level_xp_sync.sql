-- 0198_cc_buddies_level_xp_sync.sql
--
-- Sync cc_buddies.xp and cc_buddies.level so the two never disagree.
--
-- Two distinct bug classes drove this:
--   * Wild captures inserted a buddy with level=opponent_level but xp=0,
--     so the buddy panel rendered Lv. 1 (level_from_xp(0)) while combat
--     embeds happily reported the captured rank.
--   * Expedition / battle / craft XP grants bumped xp without recomputing
--     level, so a level-4 buddy (by XP) would still get gated out of a
--     min-level-3 expedition zone because level=2 in the column.
--
-- The fix is to make `level` a derived value that always agrees with
-- `level_from_xp(xp)`. We bring xp UP to match the stored level (never
-- demote a buddy) and then recompute `level` from the new xp. Going
-- forward every UPDATE that adds xp also recomputes level, so this
-- backfill stays valid.
--
-- XP curve (mirrors buddies_config.level_from_xp):
--   xp     = 120 * L * (L - 1) / 2
--   level  = floor((1 + sqrt(1 + 8*xp/120)) / 2), capped at MAX_LEVEL=50

BEGIN;

-- Step 1: bring xp up to the floor for the stored level. Never lowers.
UPDATE cc_buddies
   SET xp = GREATEST(
       COALESCE(xp, 0),
       (120::bigint * GREATEST(1, LEAST(50, COALESCE(level, 1)))
                   * (GREATEST(1, LEAST(50, COALESCE(level, 1))) - 1)) / 2
   )
 WHERE COALESCE(xp, 0) <
       (120::bigint * GREATEST(1, LEAST(50, COALESCE(level, 1)))
                   * (GREATEST(1, LEAST(50, COALESCE(level, 1))) - 1)) / 2;

-- Step 2: recompute level from xp so the column matches the curve.
UPDATE cc_buddies
   SET level = GREATEST(
       1,
       LEAST(
           50,
           FLOOR((1.0 + SQRT(1.0 + 8.0 * COALESCE(xp, 0)::double precision / 120.0)) / 2.0)::int
       )
   );

COMMIT;
