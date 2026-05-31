-- Buddy + egg P2P infrastructure: direct transfers (gifts) and a
-- market listings table. Both surfaces share the same tables so a
-- "transfer" is the audit row and a "listing" is the open offer
-- whose successful purchase ALSO writes a transfer row of kind
-- 'sale'. Direct gifts skip the listings table entirely.
--
-- Schema:
--
-- cc_buddy_transfers      -- append-only audit log; one row per move
--                            of a buddy or egg between two users.
-- cc_buddy_listings       -- open + closed marketplace offers; status
--                            transitions active -> sold|cancelled.
-- cc_buddies (existing)   -- gains for_sale BOOLEAN + active_listing_id
--                            so battle / level / cast / shelter paths
--                            can fast-filter out listed buddies without
--                            a JOIN.
--
-- Why one table for both buddies AND eggs?
--   The two share the same lifecycle (owner moves -> recipient
--   receives), the same fee/tax math, the same audit needs. An
--   egg listing carries its species + rarity_tier + rolled_at in
--   ``egg_payload`` JSONB; a buddy listing carries the buddy_id
--   foreign key. ``buddy_id IS NULL XOR egg_payload IS NULL`` is
--   enforced via a CHECK so a single render path can switch on
--   "which side is set" without surprise nulls.

-- ---- cc_buddy_transfers (audit log) ------------------------------

CREATE TABLE IF NOT EXISTS cc_buddy_transfers (
    transfer_id      BIGSERIAL    PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    from_user_id     BIGINT       NOT NULL,
    to_user_id       BIGINT       NOT NULL,
    buddy_id         BIGINT       NULL,                 -- set for buddy moves
    egg_payload      JSONB        NULL,                 -- set for egg moves
    transfer_kind    TEXT         NOT NULL,             -- 'gift'|'sale'|'admin'
    price_raw        NUMERIC(36, 0) NOT NULL DEFAULT 0, -- USD paid (raw, scaled)
    fee_raw          NUMERIC(36, 0) NOT NULL DEFAULT 0, -- USD fee taken (raw)
    transferred_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT cc_buddy_transfers_kind_chk
        CHECK (transfer_kind IN ('gift', 'sale', 'admin')),
    CONSTRAINT cc_buddy_transfers_payload_chk
        CHECK (
            (buddy_id IS NOT NULL AND egg_payload IS NULL)
         OR (buddy_id IS NULL AND egg_payload IS NOT NULL)
        ),
    CONSTRAINT cc_buddy_transfers_distinct_chk
        CHECK (from_user_id <> to_user_id),
    CONSTRAINT cc_buddy_transfers_nonneg_chk
        CHECK (price_raw >= 0 AND fee_raw >= 0)
);

-- "Activity for user X" = OR of from_user_id / to_user_id; index both
-- so the buddy / egg history panel renders fast either way.
CREATE INDEX IF NOT EXISTS cc_buddy_transfers_from_idx
    ON cc_buddy_transfers (guild_id, from_user_id, transferred_at DESC);
CREATE INDEX IF NOT EXISTS cc_buddy_transfers_to_idx
    ON cc_buddy_transfers (guild_id, to_user_id, transferred_at DESC);
-- Buddy lineage: "show me every owner this buddy has had".
CREATE INDEX IF NOT EXISTS cc_buddy_transfers_buddy_idx
    ON cc_buddy_transfers (buddy_id, transferred_at DESC)
    WHERE buddy_id IS NOT NULL;


-- ---- cc_buddy_listings (market table) ----------------------------

CREATE TABLE IF NOT EXISTS cc_buddy_listings (
    listing_id       BIGSERIAL    PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    seller_user_id   BIGINT       NOT NULL,
    buddy_id         BIGINT       NULL,                 -- set for buddy listings
    egg_payload      JSONB        NULL,                 -- set for egg listings
    asking_price_raw NUMERIC(36, 0) NOT NULL,           -- USD asked (raw, scaled)
    listed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status           TEXT         NOT NULL DEFAULT 'active', -- 'active'|'sold'|'cancelled'
    buyer_user_id    BIGINT       NULL,                 -- set on sale
    sold_at          TIMESTAMPTZ  NULL,                 -- set on sale
    cancelled_at     TIMESTAMPTZ  NULL,                 -- set on cancel
    CONSTRAINT cc_buddy_listings_status_chk
        CHECK (status IN ('active', 'sold', 'cancelled')),
    CONSTRAINT cc_buddy_listings_payload_chk
        CHECK (
            (buddy_id IS NOT NULL AND egg_payload IS NULL)
         OR (buddy_id IS NULL AND egg_payload IS NOT NULL)
        ),
    CONSTRAINT cc_buddy_listings_price_chk
        CHECK (asking_price_raw > 0)
);

-- Browse: ,buddy market scrolls active listings ordered by listed_at
-- DESC. Per-guild index keeps that paginated read O(log n).
CREATE INDEX IF NOT EXISTS cc_buddy_listings_active_idx
    ON cc_buddy_listings (guild_id, listed_at DESC)
    WHERE status = 'active';
-- Seller's own listings (delist / status panel).
CREATE INDEX IF NOT EXISTS cc_buddy_listings_seller_idx
    ON cc_buddy_listings (guild_id, seller_user_id, listed_at DESC);
-- Fast lookup: "is THIS buddy currently listed?" used by battle /
-- level / cast guards. Partial index keeps it tiny.
CREATE UNIQUE INDEX IF NOT EXISTS cc_buddy_listings_buddy_active_uq
    ON cc_buddy_listings (buddy_id)
    WHERE status = 'active' AND buddy_id IS NOT NULL;


-- ---- cc_buddies: for_sale + active_listing_id --------------------
-- Denormalised so the existing ownership / battle queries can short-
-- circuit out a listed buddy without joining the listings table on
-- every read. Listings service keeps these in sync with the row.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS for_sale          BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS active_listing_id BIGINT  NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_for_sale_consistency_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_for_sale_consistency_chk
            CHECK (
                (for_sale = TRUE  AND active_listing_id IS NOT NULL)
             OR (for_sale = FALSE AND active_listing_id IS NULL)
            ) NOT VALID;
        ALTER TABLE cc_buddies VALIDATE CONSTRAINT cc_buddies_for_sale_consistency_chk;
    END IF;
END$$;
