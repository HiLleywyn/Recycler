-- 0181_item_event_gas.sql
--
-- Adds gas-fee columns to the per-token event log. Every state
-- transition on a token (transfer / list / unlist / sold) pays a
-- small gas fee in the network's native coin -- the fields here
-- record what was charged, so the inspect history can show it
-- and analytics can sum guild-wide gas burn over time.
--
-- Mints don't pay gas (the catch / harvest / craft path is the
-- "minter" and isn't a player-initiated transaction). Burns
-- (consumed in gameplay) also don't pay gas.
--
-- Idempotent.

ALTER TABLE item_token_events
    ADD COLUMN IF NOT EXISTS gas_raw      NUMERIC(36, 0),
    ADD COLUMN IF NOT EXISTS gas_currency TEXT;
