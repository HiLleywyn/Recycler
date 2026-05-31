-- Migration 0018: Add all-time high (ath) column to crypto_prices
-- Tracks the highest price ever recorded per symbol per guild.
-- Used by the depeg-protection system to detect when a token is
-- trading significantly below its historical peak.

ALTER TABLE crypto_prices
    ADD COLUMN IF NOT EXISTS ath NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- Back-fill: use day_high as the best available proxy for the true ATH.
-- day_high is always >= price (it is the max of all price updates for the
-- current day), so it is the closest approximation we can derive from
-- existing data without querying candle history.
UPDATE crypto_prices SET ath = day_high WHERE ath = 0.0;
