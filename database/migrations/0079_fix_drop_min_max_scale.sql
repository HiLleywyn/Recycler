-- Migration 0079: Fix drop_min / drop_max values saved without to_raw() scaling.
--
-- The PATCH /settings API endpoint was missing the to_raw() conversion for
-- drop_min and drop_max (NUMERIC(36,0)), so dashboards that saved faucet
-- settings through that endpoint stored human-readable values (e.g. 2000)
-- instead of raw-scaled values (e.g. 2000 * 10^18).  Any value below 10^18
-- ($1 in raw units) was stored as a human value and needs to be scaled up.

UPDATE guild_settings
SET
    drop_min = drop_min * 1000000000000000000,
    drop_max = drop_max * 1000000000000000000
WHERE
    drop_min IS NOT NULL
    AND drop_max IS NOT NULL
    AND drop_min < 1000000000000000000;
