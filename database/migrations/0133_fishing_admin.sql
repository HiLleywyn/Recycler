-- Fishing admin: per-guild module toggle + splash channel.
--
-- Both columns are nullable so NULL means "use the default" (module
-- enabled, no dedicated splash channel; splash falls back to
-- events_channel).  Guards.guilds.set_channel /
-- update_guild_setting allowlists are extended in the same patch
-- to accept these column names.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS module_fishing  BOOLEAN;

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS fishing_channel BIGINT;
