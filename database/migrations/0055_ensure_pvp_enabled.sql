-- 0055: Ensure pvp_enabled exists in user_prefs.
--
-- Migration 0048 was supposed to add this column, but some installs have
-- 0048 recorded as applied in schema_migrations while the column is absent
-- (partial application or the column was added to 0048 after it first ran).
-- Using IF NOT EXISTS makes this idempotent.

ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS pvp_enabled BOOLEAN NOT NULL DEFAULT FALSE;
