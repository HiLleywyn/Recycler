-- Held buddy eggs: when a fishing/wild-battle buddy egg rolls but the
-- player's shelter is already at MAX_OWNED_BUDDIES, the egg lands in
-- this inventory instead of being silently converted to a LURE payout.
-- Held eggs are transferable (between players) and sellable (back to
-- the LURE wallet) and can be hatched later when the shelter has room.
--
-- Storage shape (jsonb array, one entry per egg):
--   [
--     {"species": "wecco", "rarity_tier": 3,
--      "rolled_at": "<iso8601 utc>", "from": "fishing"|"wild_battle"},
--     ...
--   ]
--
-- ``rarity_tier`` is rolled at LAY time (when the egg lands in the
-- inventory) so the sell price + later hatch result are pinned --
-- transferring an epic egg to another player gives them an epic, not a
-- re-rolled common.
--
-- ``rolled_at`` is set DB-side at INSERT time via the application code
-- using NOW() so the stored timestamp matches the DB clock per the
-- project rule about Python now() vs Postgres timestamps.
--
-- Caps: the application enforces fishing_config.MAX_HELD_EGGS so a whale
-- can't farm thousands of unhatched eggs. There's no DB CHECK here
-- because the cap is a balance dial we expect to tune.
--
-- Counters: three lifetime tallies for analytics + leaderboards. Bumped
-- by their respective service paths (egg laid into inventory, sold for
-- LURE, gifted to another player).

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS held_eggs           JSONB        NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS total_eggs_laid     BIGINT       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_eggs_sold     BIGINT       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_eggs_gifted   BIGINT       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_eggs_hatched  BIGINT       NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_fishing_eggs_nonneg_chk'
    ) THEN
        ALTER TABLE user_fishing
            ADD CONSTRAINT user_fishing_eggs_nonneg_chk
            CHECK (
                total_eggs_laid    >= 0
                AND total_eggs_sold    >= 0
                AND total_eggs_gifted  >= 0
                AND total_eggs_hatched >= 0
            ) NOT VALID;
        ALTER TABLE user_fishing VALIDATE CONSTRAINT user_fishing_eggs_nonneg_chk;
    END IF;
END$$;
