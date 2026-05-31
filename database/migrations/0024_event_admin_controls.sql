-- Admin controls for market events: disable specific events, adjust frequency
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS disabled_events TEXT NOT NULL DEFAULT '';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS event_frequency NUMERIC(8,6) NOT NULL DEFAULT 0.0005;
