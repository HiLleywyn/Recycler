-- 0225_user_crafting_specialty_chk_extra_slots.sql
--
-- Migration 0172 added a check constraint that hard-caps
-- ``active_specialties`` at 2:
--
--     array_length(active_specialties, 1) IS NULL
--       OR array_length(active_specialties, 1) <= 2
--
-- Migration 0179 then introduced ``extra_specialty_slots`` (the
-- purchasable third+ slot via ``,shop buy specialty_slot``), and the
-- service layer in ``services/crafting.add_specialty`` correctly
-- enforces ``cap = ACTIVE_SPECIALTY_CAP + extra_specialty_slots``.
-- But the DB-level CHECK was never updated, so the moment a player
-- with the third-slot unlock tries ``,craft specialize`` for their
-- third pick the row violates the old constraint and the UPDATE
-- fails with::
--
--     new row for relation "user_crafting" violates check constraint
--     "user_crafting_active_specialties_chk"
--
-- Replace the constraint with one that mirrors the service formula
-- (``2 + COALESCE(extra_specialty_slots, 0)``) so the DB and the
-- application always agree. Idempotent: drops the old constraint
-- before adding the new one and bails harmlessly if neither name
-- exists yet.

ALTER TABLE user_crafting
    DROP CONSTRAINT IF EXISTS user_crafting_active_specialties_chk;

ALTER TABLE user_crafting
    ADD CONSTRAINT user_crafting_active_specialties_chk
    CHECK (
        array_length(active_specialties, 1) IS NULL
        OR array_length(active_specialties, 1)
             <= 2 + COALESCE(extra_specialty_slots, 0)
    );
