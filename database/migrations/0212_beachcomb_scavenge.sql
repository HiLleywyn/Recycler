-- Free-roll wander commands for fishing (,fish beachcomb) and dungeon
-- (,delve scavenge) -- both modeled on ,farm forage. Each table gets a
-- DB-clock cooldown column + a lifetime counter so the embed footer can
-- show "you've foraged N times" without an extra query.
--
-- Cooldown checks must always use EXTRACT(EPOCH FROM (NOW() - col)) per
-- the project rule -- never compare Python now() to a Postgres TIMESTAMPTZ.

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS last_beachcomb_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_beachcombs  BIGINT NOT NULL DEFAULT 0;

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS last_scavenge_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_scavenges  BIGINT NOT NULL DEFAULT 0;
