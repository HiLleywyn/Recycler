-- Sync: live mirroring between channels/guilds. Two kinds:
--   'messages' -- mirror messages from a source channel to a target webhook
--   'bans'     -- propagate bans from a source guild to a target guild
CREATE TABLE IF NOT EXISTS sync_links (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL,                         -- 'messages' | 'bans'
    source_id      BIGINT NOT NULL,                       -- channel id or guild id
    target_id      BIGINT NOT NULL,                       -- channel id or guild id
    target_webhook TEXT,                                  -- message sync only
    owner_id       BIGINT NOT NULL,
    guild_id       BIGINT NOT NULL,                       -- guild that owns the link
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sync_source ON sync_links(kind, source_id) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_sync_guild  ON sync_links(guild_id);
