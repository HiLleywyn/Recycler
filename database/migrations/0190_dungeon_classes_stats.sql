-- Delve crawler: archer + druid classes, stat-point allocation, class reroll.
--
-- 1) Widen user_dungeon.class_chk so 'archer' and 'druid' can be persisted.
--    Old constraint was ('warrior','mage','rogue') -- rerunning the bot
--    after this migration with archer/druid catalogued in dungeon_config
--    would otherwise fail on set_class with a CHECK violation.
--
-- 2) Add stat-allocation columns. Mirrors the buddy upgrade system
--    (cc_buddies.hp_alloc / atk_alloc / spd_alloc), with int_alloc added
--    so caster classes (Mage / Druid) have a meaningful spend lane that
--    scales spell damage. Constants live in dungeon_config.STAT_POINT_*.
--
-- 3) Add class-reroll columns. class_rerolls_used drives the geometric
--    cost ramp (CLASS_REROLL_BASE_USD * 2^n); last_class_reroll_at gates
--    a CLASS_REROLL_COOLDOWN_S window so reroll-spamming isn't free.

ALTER TABLE user_dungeon
    DROP CONSTRAINT IF EXISTS user_dungeon_class_chk;

ALTER TABLE user_dungeon
    ADD CONSTRAINT user_dungeon_class_chk
        CHECK (class_key IS NULL
               OR class_key IN ('warrior', 'mage', 'rogue', 'archer', 'druid'));

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS hp_alloc  INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS atk_alloc INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS spd_alloc INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS int_alloc INTEGER     NOT NULL DEFAULT 0;

ALTER TABLE user_dungeon
    DROP CONSTRAINT IF EXISTS user_dungeon_alloc_chk;

ALTER TABLE user_dungeon
    ADD CONSTRAINT user_dungeon_alloc_chk
        CHECK (hp_alloc >= 0 AND atk_alloc >= 0 AND spd_alloc >= 0 AND int_alloc >= 0);

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS class_rerolls_used    INTEGER      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_class_reroll_at  TIMESTAMPTZ;

ALTER TABLE user_dungeon
    DROP CONSTRAINT IF EXISTS user_dungeon_rerolls_chk;

ALTER TABLE user_dungeon
    ADD CONSTRAINT user_dungeon_rerolls_chk
        CHECK (class_rerolls_used >= 0);
