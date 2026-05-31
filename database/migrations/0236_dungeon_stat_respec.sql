-- Delve stat-point respec: per-delver counter that tracks how many times
-- the player has paid USD to refund all spent stat points (hp_alloc /
-- atk_alloc / spd_alloc / int_alloc) back to "available". Price doubles
-- each respec on the same delver:
--
--     respec #n -> RESPEC_BASE_PRICE_USD * 2 ** (n - 1)
--
-- Mirrors the buddy stat-respec counter added in 0143_buddy_respec, but
-- lives on user_dungeon (one delver per player) rather than per-buddy.

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS stat_respecs_used INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'user_dungeon_stat_respecs_chk'
    ) THEN
        ALTER TABLE user_dungeon
            ADD CONSTRAINT user_dungeon_stat_respecs_chk
            CHECK (stat_respecs_used >= 0) NOT VALID;
        ALTER TABLE user_dungeon VALIDATE CONSTRAINT user_dungeon_stat_respecs_chk;
    END IF;
END$$;
