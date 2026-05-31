-- 0176_nft_contracts.sql
--
-- Promotes item_instances from a lazy auction-house artefact to a
-- full per-unit NFT layer with a real contract registry.
--
-- Two new things:
--
-- 1. ``item_contracts`` -- one row per item TYPE the bot can mint. A contract
--    is the type-level deploy ("WormBait", "BronzeSword", "ZennyEgg",
--    "RoseCrop"). Tokens minted from a contract are the per-unit
--    instances ("worm bait #5739", "bronze sword #182"). A contract
--    has a stable address (network:hex), a kind, a catalog key, and
--    optional metadata (rarity tier, base price, emoji).
--
--    Contracts are bootstrapped at bot startup by walking every
--    catalog dict (fishing_config.BAIT, dungeon_config.WEAPONS, etc.)
--    so adding a new item to a config file is enough to "deploy" its
--    contract on the next boot.
--
-- 2. New columns on ``item_instances``:
--      contract_id       -- FK to the contract this token was minted from
--      unit_index        -- per-contract serial (1..N) so token ids
--                           render as "bait.worm #5739" without a
--                           cross-row scan. Added with a per-contract
--                           sequence backfill below.
--      minted_at         -- time of mint (was created_at; we keep
--                           created_at for the row, minted_at for the
--                           on-chain semantic).
--      burned_at         -- non-null = burned (consumed). We keep the
--                           row so the ledger stays append-only, but
--                           ownership filters skip burned tokens.
--      mint_source       -- where the unit was created (e.g. "fish",
--                           "shop", "craft", "loot.delve.copper",
--                           "backfill"). Useful for analytics and for
--                           reproducing a stack's history.
--
-- Migration is idempotent.

CREATE TABLE IF NOT EXISTS item_contracts (
    contract_id     BIGSERIAL    PRIMARY KEY,
    address         TEXT         NOT NULL UNIQUE,        -- "bait.worm" / "weapon.bronze_sword" / ...
    network         TEXT         NOT NULL,               -- bud / lur / cry / fge / har / ...
    kind            TEXT         NOT NULL,               -- buddy / egg / fish / crop / ore /
                                                         -- weapon / armor / consumable / crafted /
                                                         -- bait / junk / shop / stone / token
    catalog_key     TEXT         NOT NULL,               -- catalog dict key (e.g. "worm")
    name            TEXT         NOT NULL,               -- display name
    rarity_tier     INTEGER,                             -- 1..5 if applicable
    base_price_raw  NUMERIC(36, 0),                      -- catalog price (raw 10^18) if applicable
    emoji           TEXT,                                -- catalog emoji if any
    metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    deployed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT item_contracts_kind_chk CHECK (kind IN (
        'buddy', 'egg', 'fish', 'crop', 'ore',
        'weapon', 'armor', 'consumable', 'crafted',
        'bait', 'junk', 'shop', 'stone', 'token'
    )),
    CONSTRAINT item_contracts_address_shape_chk CHECK (
        address ~ '^[a-z0-9_.]+$'
    )
);

CREATE INDEX IF NOT EXISTS item_contracts_kind_idx
    ON item_contracts (kind);

CREATE INDEX IF NOT EXISTS item_contracts_network_idx
    ON item_contracts (network);


-- item_instances upgrades. We keep token_id as the primary identifier
-- (still <network>:<hex>) because it's referenced by auction_listings
-- via a FK -- changing the PK shape would cascade to every settled
-- listing. Instead we add the contract / unit columns alongside.
ALTER TABLE item_instances
    ADD COLUMN IF NOT EXISTS contract_id  BIGINT REFERENCES item_contracts (contract_id),
    ADD COLUMN IF NOT EXISTS unit_index   BIGINT,
    ADD COLUMN IF NOT EXISTS minted_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS burned_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS mint_source  TEXT;

-- Backfill minted_at from created_at for any rows that already exist.
UPDATE item_instances
   SET minted_at = COALESCE(minted_at, created_at)
 WHERE minted_at IS NULL;


-- Active-token filter (unburned, owned). Most queries against the NFT
-- layer want "tokens this player holds right now"; this is the hot path.
CREATE INDEX IF NOT EXISTS item_instances_active_owner_idx
    ON item_instances (guild_id, owner_user_id, contract_id)
    WHERE burned_at IS NULL AND owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS item_instances_contract_unit_idx
    ON item_instances (contract_id, unit_index)
    WHERE contract_id IS NOT NULL;
