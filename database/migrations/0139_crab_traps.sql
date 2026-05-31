-- Crab traps: deployable passive collection on top of the fishing minigame.
--
-- Adds two JSONB columns and two analytics counters to ``user_fishing``:
--   crab_trap_inventory    -- {"wire_pot": 5, ...}  -- undeployed traps
--                             owned by the player. Counts capped at the
--                             ``max_stack`` value in fishing_config.CRAB_TRAPS.
--   placed_crab_traps      -- [{"key": "wire_pot",
--                                "zone": "ocean",
--                                "placed_at": <iso8601 utc>}]
--                             list of currently-soaking traps. Cap is
--                             fishing_config.CRAB_TRAP_PLACED_CAP rows.
--   total_crabs_collected  -- lifetime crab specimens pulled (analytics)
--   total_traps_placed     -- lifetime traps deployed (analytics)
--   last_trap_collect_at   -- DB-side cooldown clock for ,fish trap collect.
--
-- Cooldowns and "is the trap ready to haul" comparisons use the DB
-- clock per the project rule (EXTRACT(EPOCH FROM (NOW() - placed_at))),
-- so a player on a slow shard or a clock-skewed container never sees
-- a trap pop early.

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS crab_trap_inventory   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS placed_crab_traps     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS total_crabs_collected BIGINT       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_traps_placed    BIGINT       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_trap_collect_at  TIMESTAMPTZ;

-- Non-negative counters so a future bug can't silently underflow.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_fishing_traps_nonneg_chk'
    ) THEN
        ALTER TABLE user_fishing
            ADD CONSTRAINT user_fishing_traps_nonneg_chk
            CHECK (
                total_crabs_collected >= 0
                AND total_traps_placed >= 0
            ) NOT VALID;
        ALTER TABLE user_fishing VALIDATE CONSTRAINT user_fishing_traps_nonneg_chk;
    END IF;
END$$;
