-- Delve junk inventory.
--
-- Mirrors user_fishing.junk_inventory: a JSONB counter dict keyed by
-- junk type ("broken_blade": 3, "monster_fang": 1, ...). Junk drops
-- fire on combat wins, chest opens, and ore mining; the catalog +
-- drop logic live in dungeon_config.py and services/dungeon.py.
--
-- New columns are nullable + default-empty so existing rows backfill
-- on first read without a separate UPDATE pass.

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS junk_inventory JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS total_junk_collected BIGINT NOT NULL DEFAULT 0;
