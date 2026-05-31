-- 0179_specialty_slot_purchase.sql
--
-- Adds a per-user purchasable extra crafting-specialty slot. Default
-- cap is ``crafting_config.ACTIVE_SPECIALTY_CAP`` (2). Buying the
-- premium third-slot unlock bumps the user's effective cap by one.
--
-- Cost is gated in the buy command (USD wallet debit); this migration
-- only adds the storage column. Idempotent.

ALTER TABLE user_crafting
    ADD COLUMN IF NOT EXISTS extra_specialty_slots INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'user_crafting_extra_slots_chk'
    ) THEN
        ALTER TABLE user_crafting
            ADD CONSTRAINT user_crafting_extra_slots_chk
            CHECK (extra_specialty_slots >= 0 AND extra_specialty_slots <= 5);
    END IF;
END
$$;
