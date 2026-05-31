-- 0184_item_contract_native_price.sql
--
-- Adds native-currency catalog price columns to ``item_contracts`` so
-- ``,items`` and ``,db`` can render the price every catalog actually
-- uses (REEL for bait, RUNE for weapons / armor / consumables, FGD for
-- crafted, HRV for crops). Pre-0184 the bootstrap only filled
-- ``base_price_raw`` (USD) for the handful of catalogs that quote in
-- USD-pegged stable (shop / stone), which is why most kinds rendered
-- with no price column at all.
--
-- Two new columns:
--   base_price_native_raw  NUMERIC(36, 0)
--       Catalog price scaled to raw (10^18). Holds the unit price in
--       whichever currency the catalog quotes in.
--   base_price_currency    TEXT
--       Symbol the native price is denominated in -- REEL / RUNE / FGD
--       / HRV / DSD / USDC. NULL means no native price (the contract
--       is USD-only).
--
-- ``base_price_raw`` is unchanged: it stays the USD-pegged column so
-- shop / stone contracts (cost_stable, already USD raw) keep working
-- without any code change. Display code reads native first, then USD,
-- and runs an oracle conversion for the cross-display when the native
-- side has a price but USD is missing.
--
-- Idempotent. Safe to re-run.

ALTER TABLE item_contracts
    ADD COLUMN IF NOT EXISTS base_price_native_raw  NUMERIC(36, 0),
    ADD COLUMN IF NOT EXISTS base_price_currency    TEXT;
