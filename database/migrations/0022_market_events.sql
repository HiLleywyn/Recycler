-- Market events: store current active event per guild
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS current_event TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS event_vol_mult NUMERIC(5,2) NOT NULL DEFAULT 1.0;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS event_bias NUMERIC(8,6) NOT NULL DEFAULT 0.0;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS event_expires_at TIMESTAMPTZ;
