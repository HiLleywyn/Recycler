-- 0178_auction_listing_tokens.sql
--
-- Multi-qty auction listings now escrow ONE per-unit token per qty
-- instead of a single "bundle" token. Adds a join table linking
-- auction_listings to N item_instances rows.
--
-- The legacy ``auction_listings.token_id`` stays as the "primary"
-- token (always the first of the N escrowed tokens) so existing
-- queries / FKs / display code keep working without a wholesale
-- rewrite. The join table holds the full list, including the primary,
-- so cancel / settle paths can sweep ownership across all of them.
--
-- Migration is idempotent.

CREATE TABLE IF NOT EXISTS auction_listing_tokens (
    listing_id  BIGINT NOT NULL REFERENCES auction_listings (id) ON DELETE CASCADE,
    token_id    TEXT   NOT NULL REFERENCES item_instances (token_id) ON DELETE CASCADE,
    PRIMARY KEY (listing_id, token_id)
);

CREATE INDEX IF NOT EXISTS auction_listing_tokens_token_idx
    ON auction_listing_tokens (token_id);

-- Backfill: every existing listing has exactly one (primary) token in
-- auction_listings.token_id. Mirror that into the join table so the
-- new code path treats single-token listings the same as multi-token
-- listings (just N=1).
INSERT INTO auction_listing_tokens (listing_id, token_id)
SELECT id, token_id
  FROM auction_listings
 WHERE token_id IS NOT NULL
ON CONFLICT (listing_id, token_id) DO NOTHING;
