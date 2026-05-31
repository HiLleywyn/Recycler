-- New module toggles for ape, nft, predictions, events
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS module_ape         BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS module_nft         BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS module_predictions BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS module_events      BOOLEAN DEFAULT TRUE;

-- New feed channels for nft, predictions, events, ape
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS nft_channel         BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS predictions_channel BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS events_channel      BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ape_channel         BIGINT;

-- New DM notification preferences (default OFF per user request)
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_events      BOOLEAN DEFAULT FALSE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_nft         BOOLEAN DEFAULT FALSE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_predictions BOOLEAN DEFAULT FALSE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_ape         BOOLEAN DEFAULT FALSE;
