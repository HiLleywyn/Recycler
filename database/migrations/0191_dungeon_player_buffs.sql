-- Delve crawler: per-player buff store.
--
-- The new class consumables (scroll_volley / scroll_mark_target /
-- thorn_aura_brew / wildshape_potion / regrowth_brew / scroll_sanctuary)
-- apply timed effects that need to persist across rounds + room changes
-- so a player can pre-cast a buff before opening a door. The mob_state
-- JSONB gets wiped on mob death so a separate JSONB column keeps player
-- buffs on the player row independently of any current encounter.
--
-- Schema:
--   player_buffs ::= { "<buff_name>": { "duration": int_rounds, "value": float, "source": "<consumable_key>" } }
--
-- services/dungeon.py:_apply_player_buff_tick decrements ``duration``
-- each combat round and removes the entry when duration <= 0. No CHECK
-- constraint on the JSONB shape -- the application owns validation so
-- adding a new buff kind stays a config-only change.

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS player_buffs JSONB NOT NULL DEFAULT '{}'::jsonb;
