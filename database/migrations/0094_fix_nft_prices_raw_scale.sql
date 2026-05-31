-- Migration 0094: Fix NFT prices to use raw NUMERIC(36,0) scale (10^18)
-- Previously, mint_price and listing price were stored as human floats (e.g. 5.0)
-- but the columns are NUMERIC(36,0) which stores integers. Values like 5.0 were
-- stored as 5 (effectively 5e-18 of the token). Multiply by 10^18 to fix.
-- Only update rows where price looks like a human value (< 1e15) to avoid
-- double-applying on already-correct rows.

UPDATE nft_collections
SET mint_price = mint_price * 1000000000000000000
WHERE mint_price > 0 AND mint_price < 1000000000000000;

UPDATE nft_listings
SET price = price * 1000000000000000000
WHERE price > 0 AND price < 1000000000000000;
