-- 0182_item_instances_kinds.sql
--
-- Widens the ``item_instances_kind_chk`` constraint to allow the
-- four new kinds added by migration 0176:
--   bait / junk / shop / stone.
--
-- Migration 0176 added these kinds to ``item_contracts_kind_chk`` but
-- forgot to mirror the change on ``item_instances`` -- which has its
-- own kind-allowlist CHECK from migration 0173. Result: deploy_*
-- contracts succeed (item_contracts allows them) but per-unit mints
-- of bait/junk/shop/stone fail with CheckViolationError on every
-- insert. The Phase-1 backfill drops to 0 rows for those kinds.
--
-- Idempotent: drops the old CHECK if present, re-adds the widened one.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'item_instances_kind_chk'
    ) THEN
        ALTER TABLE item_instances
            DROP CONSTRAINT item_instances_kind_chk;
    END IF;
END
$$;

ALTER TABLE item_instances
    ADD CONSTRAINT item_instances_kind_chk CHECK (kind IN (
        'buddy', 'egg', 'fish', 'crop', 'ore',
        'weapon', 'armor', 'consumable', 'crafted',
        'bait', 'junk', 'shop', 'stone', 'token'
    ));
