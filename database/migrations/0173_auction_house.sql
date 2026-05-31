-- Auction House + per-item NFT-style token IDs.
--
-- Two tables that work together:
--
-- 1. item_instances -- the "NFT layer". Every ownable item gets a unique
--    stable token_id formatted as "<network>:<8-12 hex>" (e.g. bud:k889kak,
--    reel:81819kak, hrv:xxxx). The hex is content-derived from
--    (source_table, source_id, salt) so the same item always resolves to the
--    same token_id. Buddies / fish / crops / ore / weapons / armors /
--    crafted items / eggs / fungible tokens all map through here when they
--    enter the auction layer.
--
--      network         the network the item belongs to (bud / reel / lur /
--                      hrv / rune / cry / fge / dsc / eth ...). USD-bought
--                      items inherit the closest related crypto network at
--                      mint time (e.g. buddies bought with USD use 'bud').
--      kind            'buddy' | 'egg' | 'fish' | 'crop' | 'ore' | 'weapon'
--                      | 'armor' | 'consumable' | 'crafted' | 'token'
--      source_table    where the canonical row lives (e.g. 'cc_buddies',
--                      'user_fishing.fish_inventory[bass]'). Free-form so
--                      future item kinds plug in without schema churn.
--      source_id       the canonical PK / inventory key
--      owner_user_id   current owner (NULL when escrowed by an auction
--                      listing -- listing_id below points to the active
--                      listing row in that case).
--      metadata        free-form JSON (rarity, level, lbs, qty, etc.) so
--                      browsing the AH never has to JOIN back to the source
--                      table.
--      listing_id      FK to auction_listings.id when actively listed.
--
-- 2. auction_listings -- the marketplace itself. Replaces the old
--    Buddy-only market with a generic listings table that takes any item
--    kind. Pricing is in the item's network currency (or any token the
--    seller chose); buys from a different currency auto-route through the
--    AMM with normal slippage, same shape as ,buy / ,sell / ,trade swap.
--
--      currency        listed currency symbol (BUD / LURE / RUNE / etc.).
--      price_raw       sale price as raw 10^18-scaled int.
--      auction_fee_bps house fee in basis points (default 500 = 5%) burned
--                      on settle.
--      status          'active' | 'sold' | 'cancelled' | 'expired'
--
-- Migration is idempotent.

CREATE TABLE IF NOT EXISTS item_instances (
    token_id        TEXT         PRIMARY KEY,
    guild_id        BIGINT       NOT NULL,
    network         TEXT         NOT NULL,
    kind            TEXT         NOT NULL,
    source_table    TEXT         NOT NULL,
    source_id       TEXT         NOT NULL,
    owner_user_id   BIGINT,
    listing_id      BIGINT,
    metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT item_instances_kind_chk CHECK (kind IN (
        'buddy', 'egg', 'fish', 'crop', 'ore',
        'weapon', 'armor', 'consumable', 'crafted', 'token'
    ))
);

CREATE INDEX IF NOT EXISTS item_instances_owner_idx
    ON item_instances (guild_id, owner_user_id, kind)
    WHERE owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS item_instances_source_idx
    ON item_instances (source_table, source_id);

CREATE INDEX IF NOT EXISTS item_instances_listing_idx
    ON item_instances (listing_id) WHERE listing_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS auction_listings (
    id              BIGSERIAL    PRIMARY KEY,
    guild_id        BIGINT       NOT NULL,
    seller_user_id  BIGINT       NOT NULL,
    token_id        TEXT         NOT NULL REFERENCES item_instances (token_id) ON DELETE CASCADE,
    kind            TEXT         NOT NULL,
    qty             INTEGER      NOT NULL DEFAULT 1,
    currency        TEXT         NOT NULL,
    price_raw       NUMERIC(36, 0) NOT NULL,
    auction_fee_bps INTEGER      NOT NULL DEFAULT 500,
    status          TEXT         NOT NULL DEFAULT 'active',
    buyer_user_id   BIGINT,
    sold_price_raw  NUMERIC(36, 0),
    sold_currency   TEXT,
    listed_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    settled_at      TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    notes           TEXT,
    metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT auction_listings_status_chk CHECK (status IN (
        'active', 'sold', 'cancelled', 'expired'
    )),
    CONSTRAINT auction_listings_qty_chk CHECK (qty >= 1),
    CONSTRAINT auction_listings_price_chk CHECK (price_raw > 0)
);

-- Hot lookup paths: browse-active by guild, my-listings by seller,
-- expiring scan, and settled-history view.
CREATE INDEX IF NOT EXISTS auction_listings_active_idx
    ON auction_listings (guild_id, listed_at DESC)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS auction_listings_seller_idx
    ON auction_listings (guild_id, seller_user_id, status);

CREATE INDEX IF NOT EXISTS auction_listings_expires_idx
    ON auction_listings (expires_at)
    WHERE status = 'active' AND expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS auction_listings_kind_idx
    ON auction_listings (guild_id, kind, listed_at DESC)
    WHERE status = 'active';
