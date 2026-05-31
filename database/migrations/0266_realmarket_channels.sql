-- Allowlist of channels where the $-prefixed real-crypto commands
-- ($chart / $info / $help / $channels) are permitted. Distinct from
-- guild_settings.bot_channels so admins can enable $-commands in chat
-- channels without also enabling the broader game-command surface there.
--
-- Stored as a comma-separated list of channel IDs to match the existing
-- bot_channels / ai_chat_channels convention. The cog treats the
-- effective allowlist as (bot_channels ∪ realmarket_channels). When both
-- are empty, $-commands run anywhere.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS realmarket_channels TEXT NOT NULL DEFAULT '';
