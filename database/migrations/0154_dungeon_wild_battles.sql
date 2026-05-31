-- Wild-buddy battles in delves. Mirrors fishing's wild-battle counter
-- columns on user_fishing so the same achievement / quest / challenge
-- triggers can fire off a single counter UPDATE in
-- services/dungeon.resolve_wild_battle.
--
-- All three columns default to 0 and are bumped by the resolver. They are
-- additive and never reset; ,delve reset / disband flows do not touch
-- them.

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS wild_battles_won      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_battles_lost     BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_buddies_captured BIGINT NOT NULL DEFAULT 0;

-- Leaderboard helper: top wild-battle slayers in a guild.
CREATE INDEX IF NOT EXISTS user_dungeon_wild_won_idx
    ON user_dungeon (guild_id, wild_battles_won DESC)
    WHERE wild_battles_won > 0;
