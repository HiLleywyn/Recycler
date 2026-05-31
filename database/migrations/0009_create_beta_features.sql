-- ============================================================================
-- 0009: Beta Feature Access Table
-- Per-guild beta feature grants (by user or role).
-- feature_name: 'command_chains', 'internal_commands', etc.
-- grant_type: 'user' or 'role'; grant_id: user_id or role_id
-- ============================================================================

CREATE TABLE IF NOT EXISTS beta_features (
    guild_id     BIGINT NOT NULL,
    feature_name TEXT   NOT NULL,
    grant_type   TEXT   NOT NULL CHECK (grant_type IN ('user', 'role')),
    grant_id     BIGINT NOT NULL,
    granted_by   BIGINT NOT NULL,
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, feature_name, grant_type, grant_id)
);

CREATE INDEX IF NOT EXISTS idx_beta_guild ON beta_features (guild_id, feature_name);
