-- ============================================================================
-- 0010: Security guild settings columns and exempt-users table
-- Adds security_log_channel and security_audit_roles to guild_settings, and
-- creates the security_exempt_users table for owner-granted bypass entries.
-- ============================================================================

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS security_log_channel  BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS security_audit_roles  TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS security_exempt_users (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    target_type TEXT         NOT NULL CHECK (target_type IN ('user', 'role')),
    target_id   BIGINT       NOT NULL,
    granted_by  BIGINT       NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_sec_exempt_guild
    ON security_exempt_users (guild_id);
