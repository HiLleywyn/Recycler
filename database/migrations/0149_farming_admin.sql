-- Farming minigame: guild-level admin knobs.
-- Mirrors 0133_fishing_admin.sql.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS module_farming  BOOLEAN;
