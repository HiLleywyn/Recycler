-- 0089: Unified staff audit log -- pretty, well-structured staff action feed.
--
-- Shared table backing the audit feeds exposed by ,admin, ,mod, ,drs, ,dev.
-- One row per staff action with a ``scope`` column so each surface can
-- filter to only rows relevant to it without maintaining a separate table.
--
-- Existing per-surface logs (helper_audit_log for DRS, agent_tool_audit for
-- agent tools, player_warnings for mod warns) are NOT replaced -- this table
-- is additive and gives each admin group a single unified feed to browse.

CREATE TABLE IF NOT EXISTS staff_audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    scope       TEXT        NOT NULL,
        -- 'admin' | 'mod' | 'drs' | 'dev' | 'ai'
    actor_id    BIGINT      NOT NULL,
    action      TEXT        NOT NULL,
    target_id   BIGINT,
    severity    TEXT        NOT NULL DEFAULT 'info',
        -- 'info' | 'warn' | 'danger'
    details     TEXT        NOT NULL DEFAULT '',
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS staff_audit_log_guild_scope_time
    ON staff_audit_log (guild_id, scope, created_at DESC);

CREATE INDEX IF NOT EXISTS staff_audit_log_actor
    ON staff_audit_log (guild_id, actor_id, created_at DESC);

CREATE INDEX IF NOT EXISTS staff_audit_log_target
    ON staff_audit_log (guild_id, target_id, created_at DESC)
    WHERE target_id IS NOT NULL;


-- ── Per-guild AI model defaults ─────────────────────────────────────────────
-- Stores the selectable default model for each tool category (chat, tools,
-- vision, image, search, code, reason, automation, defi, economy_sim). A
-- row's value is an opaque "provider:model" string, e.g. "openrouter:anthropic/claude-3.5-sonnet".
-- The AI client layer resolves this at call time, falling back to the env
-- defaults when no row exists.
CREATE TABLE IF NOT EXISTS ai_model_defaults (
    guild_id    BIGINT      NOT NULL,
    category    TEXT        NOT NULL,
    provider    TEXT        NOT NULL,
        -- 'openrouter' | 'ollama'
    model       TEXT        NOT NULL,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, category)
);

CREATE INDEX IF NOT EXISTS ai_model_defaults_guild
    ON ai_model_defaults (guild_id);
