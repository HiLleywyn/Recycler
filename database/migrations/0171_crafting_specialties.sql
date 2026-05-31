-- Per-specialty XP and level for crafting (smithing / alchemy / cooking /
-- fletching / tinkering). Each recipe declares a ``specialty`` in
-- crafting_config.CRAFT_ITEMS; a successful ,craft make bumps both the
-- aggregate crafting_xp/level AND the matching specialty XP/level so a
-- player who only does Alchemy levels Alchemy independently of overall
-- crafting level. Aggregate level still gates the recipe (min_level on
-- the catalog entry); specialty levels unlock the next tier of recipes
-- inside a track and feed cosmetic / quest rewards.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS).

ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS smithing_xp     BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS smithing_level  INTEGER NOT NULL DEFAULT 1;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS alchemy_xp      BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS alchemy_level   INTEGER NOT NULL DEFAULT 1;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS cooking_xp      BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS cooking_level   INTEGER NOT NULL DEFAULT 1;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS fletching_xp    BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS fletching_level INTEGER NOT NULL DEFAULT 1;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS tinkering_xp    BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE user_crafting ADD COLUMN IF NOT EXISTS tinkering_level INTEGER NOT NULL DEFAULT 1;
