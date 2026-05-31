-- Default every notification preference to OFF and flip every existing
-- row to OFF as well. Players have asked for an opt-in model: too many
-- DMs were firing without consent, and the CLAUDE.md project rule that
-- "DMs are user-visible behaviour" makes the previous opt-out default
-- a bad fit. After this migration:
--   * Every dm_* column on user_prefs has DEFAULT FALSE going forward
--     (new rows start fully muted).
--   * Every existing row is rewritten to FALSE so already-registered
--     players also stop getting DMs immediately.
--   * Players opt back in via ,notify <kind> on the same way they
--     always could -- no API change.
--
-- The set of columns affected matches database/users.py
-- (PgUsersRepo._PREF_DEFAULTS): the eight legacy categories
-- (mining / transfer / validator / staking / itemlevelup / whale_alerts /
--  2fa / autolevelup) plus the four feed-style ones (nft / predictions /
--  events / ape) that already shipped with FALSE defaults.

-- ── Defaults going forward ────────────────────────────────────────────────
ALTER TABLE user_prefs ALTER COLUMN dm_mining        SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_transfer      SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_validator     SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_staking       SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_2fa           SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_itemlevelup   SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_whale_alerts  SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_autolevelup   SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_nft           SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_predictions   SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_events        SET DEFAULT FALSE;
ALTER TABLE user_prefs ALTER COLUMN dm_ape           SET DEFAULT FALSE;

-- ── Flip every existing row off ───────────────────────────────────────────
UPDATE user_prefs
   SET dm_mining       = FALSE,
       dm_transfer     = FALSE,
       dm_validator    = FALSE,
       dm_staking      = FALSE,
       dm_2fa          = FALSE,
       dm_itemlevelup  = FALSE,
       dm_whale_alerts = FALSE,
       dm_autolevelup  = FALSE,
       dm_nft          = FALSE,
       dm_predictions  = FALSE,
       dm_events       = FALSE,
       dm_ape          = FALSE;
