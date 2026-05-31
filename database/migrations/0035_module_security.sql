-- Add per-guild toggle to disable the security system entirely.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS module_security BOOLEAN NOT NULL DEFAULT TRUE;
