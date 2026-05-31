-- Migration 0048: Add missing user_prefs columns that were in schema.sql but never in a migration
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_itemlevelup           BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_whale_alerts          BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_mining    TEXT    NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_staking   TEXT    NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_validator TEXT    NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_whale     TEXT    NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS pvp_enabled              BOOLEAN NOT NULL DEFAULT FALSE;
