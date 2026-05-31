-- 0262_buddy_arena_v2.sql
-- Buddy arena expansion: special locations (shop / spring / dig /
-- trader) and boss captures, plus multi-clear progression on zone
-- trophies. New columns are additive; the existing arena flow continues
-- to work without touching them.

-- Spring + Dig cooldowns + boss captures roster on the progress row.
ALTER TABLE cc_buddy_map_progress
    ADD COLUMN IF NOT EXISTS last_spring_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_dig_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS captured_boss_zones  TEXT[] NOT NULL DEFAULT '{}';

-- Multi-clear progression on the trophy table. The new
-- ``required_clears`` column lets a zone require N wins before it's
-- marked permanently in cleared_zones. Boss rows backfill to 1 (still
-- one-shot to clear); normal rows default to 3.
ALTER TABLE cc_buddy_zone_trophies
    ADD COLUMN IF NOT EXISTS required_clears  INTEGER NOT NULL DEFAULT 3;

-- Index for the captured-boss lookup on the progress row.
CREATE INDEX IF NOT EXISTS cc_buddy_map_progress_captured_bosses_idx
    ON cc_buddy_map_progress USING GIN (captured_boss_zones);
