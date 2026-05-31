-- 0054: Add missing mining columns for existing installs.
--
-- schema.sql gained two inline ALTERs that only apply to fresh installs.
-- Existing DBs never ran them, causing two recurring errors:
--
--   mining_tick failed: column "reserve_usd" does not exist
--   _on_pow_mining_tick raised: column "symbol" of relation "mining_blocks" does not exist
--
-- Fix 1: mining_groups.reserve_usd
--   Used by add_group_reserve_usd() / spend_group_reserve_usd() to track the
--   USD-equivalent value of the per-block reserve cut. Backfill from reserve_sun
--   at a nominal 0.01 USD/SUN rate (same as the schema.sql inline migration).

ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS reserve_usd NUMERIC(28,8) NOT NULL DEFAULT 0.0;

UPDATE mining_groups
SET reserve_usd = reserve_sun * 0.01
WHERE reserve_sun > 0 AND reserve_usd = 0;

-- Fix 2: mining_blocks.symbol
--   Stores which PoW network produced each block (SUN, BTC, etc.).
--   log_block() always writes this; all existing rows are SUN blocks.

ALTER TABLE mining_blocks
    ADD COLUMN IF NOT EXISTS symbol VARCHAR(16) NOT NULL DEFAULT 'SUN';

CREATE UNIQUE INDEX IF NOT EXISTS idx_blocks_guild_sym
    ON mining_blocks (guild_id, symbol, block_height);
