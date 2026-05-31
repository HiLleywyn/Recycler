-- Widen item_contracts_kind_chk and item_instances_kind_chk to allow
-- the new ``relic`` kind. nft_bootstrap deploys relics from
-- dungeon_config.RELICS but every INSERT was crashing with
-- CheckViolationError on the original 14-kind allowlist from 0176/0182.
--
-- Idempotent: drops the old CHECKs if present, re-adds widened ones.
-- Safe to run on a DB that already has these constraints.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'item_contracts_kind_chk'
    ) THEN
        ALTER TABLE item_contracts
            DROP CONSTRAINT item_contracts_kind_chk;
    END IF;
END
$$;

ALTER TABLE item_contracts
    ADD CONSTRAINT item_contracts_kind_chk CHECK (kind IN (
        'buddy', 'egg', 'fish', 'crop', 'ore',
        'weapon', 'armor', 'consumable', 'crafted',
        'bait', 'junk', 'shop', 'stone', 'token',
        'relic'
    ));

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
        'bait', 'junk', 'shop', 'stone', 'token',
        'relic'
    ));
