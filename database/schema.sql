-- Recycler slim schema -- clanker containment only, nothing from the economy.
--
-- This is the *framework-runtime* substrate Recycler needs (guild settings,
-- the command-role allowlist, and small stubs the core runtime touches under
-- try/except) -- NOT Disco's economy plane. The clanker tables themselves are
-- added on top by the verbatim feature migrations in ./migrations (0285-0295),
-- so the clanker schema is byte-for-byte what the feature was built against.
--
-- Everything is CREATE TABLE IF NOT EXISTS so re-applying on every boot is safe.

-- Per-guild settings. The framework reads most columns tolerantly (SELECT * +
-- dict.get), so only the columns written directly need to exist here; the
-- clanker settings columns (clamp_*, clasp_*, scam_*, clank_escape_thread,
-- automod_auto_clank) are added by the clanker migrations.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id             BIGINT PRIMARY KEY,
    prefix               TEXT,
    server_name          TEXT,
    bot_channels         TEXT    DEFAULT '',
    realmarket_channels  TEXT    DEFAULT '',
    error_channel        BIGINT,
    error_feed_levels    TEXT,
    cmd_delete_after     INTEGER DEFAULT 0,
    reply_delete_after   INTEGER DEFAULT 0,
    ai_cmd_delete_after  INTEGER DEFAULT 0,
    ai_reply_delete_after INTEGER DEFAULT 0,
    halted_networks      TEXT    DEFAULT '',
    disabled_tokens      TEXT    DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-command role allowlist (framework's global role check). Empty = no
-- restriction. The check fails open if this is empty/absent, so it's only here
-- to keep the admin role commands working.
CREATE TABLE IF NOT EXISTS guild_command_roles (
    guild_id     BIGINT NOT NULL,
    command_name TEXT   NOT NULL,
    role_id      BIGINT NOT NULL,
    PRIMARY KEY (guild_id, command_name, role_id)
);

-- Group-hall prefixless toggle: the framework's prefix resolver probes this
-- under try/except. A stub keeps that path quiet on a bot with no groups.
CREATE TABLE IF NOT EXISTS mining_groups (
    guild_id        BIGINT NOT NULL,
    hall_thread_id  BIGINT,
    hall_prefixless BOOLEAN DEFAULT FALSE
);

-- Orphaned-game recovery scans this at boot. Always empty on Recycler (it runs
-- no games), so recovery is a no-op -- the columns just need to exist.
CREATE TABLE IF NOT EXISTS game_sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guild_id   BIGINT,
    user_id    BIGINT,
    game_type  TEXT,
    bet_amount NUMERIC(36,0) DEFAULT 0,
    state      JSONB DEFAULT '{}'::jsonb,
    status     TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ,admin commandstats usage logging (best-effort; framework wraps writes).
CREATE TABLE IF NOT EXISTS command_usage (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT,
    user_id      BIGINT,
    command_path TEXT,
    args_text    TEXT,
    at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS command_usage_totals (
    guild_id     BIGINT  NOT NULL DEFAULT 0,
    command_path TEXT    NOT NULL,
    args_text    TEXT    NOT NULL DEFAULT '',
    total_count  BIGINT  NOT NULL DEFAULT 0,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, command_path, args_text)
);

-- Chat level lookup used by clanker mass-scan to skip established members.
-- Empty on Recycler -> the lookup returns NULL and the member isn't skipped.
CREATE TABLE IF NOT EXISTS chat_levels (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    level    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
