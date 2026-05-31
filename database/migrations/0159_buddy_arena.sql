-- Buddy Network arena: PvE combat surface that mints BUD on win.
--
-- Adds counter columns to user_buddy_economy so achievements / quests /
-- challenges can target arena outcomes. Cooldown is enforced via the
-- last-arena-at TIMESTAMPTZ (DB-side clock, no Python clocks).

ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS arena_wins         BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arena_losses       BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arena_bud_earned_raw NUMERIC(36, 0) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_arena_at      TIMESTAMPTZ;

-- Leaderboard helper: top arena slayers per guild.
CREATE INDEX IF NOT EXISTS user_buddy_economy_arena_wins_idx
    ON user_buddy_economy (guild_id, arena_wins DESC)
    WHERE arena_wins > 0;
