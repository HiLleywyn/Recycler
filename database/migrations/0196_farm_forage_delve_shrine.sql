-- Farm forage minigame + delve shrine pray bookkeeping.
--
-- Farming additions:
--   user_farming.last_forage_at TIMESTAMPTZ -- DB-clock cooldown for ,farm forage
--   user_farming.total_forages  BIGINT      -- lifetime forage count for stats
--
-- Delve additions:
--   user_dungeon.total_shrines_visited BIGINT -- lifetime ,delve pray count
--
-- Cooldown lives on the DB clock per project rule (no Python now() vs
-- Postgres TIMESTAMPTZ comparisons).

ALTER TABLE user_farming
    ADD COLUMN IF NOT EXISTS last_forage_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_forages  BIGINT NOT NULL DEFAULT 0;

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS total_shrines_visited BIGINT NOT NULL DEFAULT 0;
