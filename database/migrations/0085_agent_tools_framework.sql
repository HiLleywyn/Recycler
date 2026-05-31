-- 0085: Agent tools framework -- persistent task queue, triggers, chains, audit.
--
-- Supports the framework/agent_tools/ package. All tables survive restarts
-- so queued tasks, event triggers, and multi-step chain runs persist through
-- deploys and crashes.

-- ── Audit log for every tool invocation ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_tool_audit (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    actor        TEXT        NOT NULL DEFAULT 'user',
    tool         TEXT        NOT NULL,
    risk         TEXT        NOT NULL DEFAULT 'read',
    args         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ok           BOOLEAN     NOT NULL,
    error        TEXT        NOT NULL DEFAULT '',
    duration_ms  INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_tool_audit_guild_user
    ON agent_tool_audit (guild_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_tool_audit_tool
    ON agent_tool_audit (tool, created_at DESC);


-- ── Persistent task queue ───────────────────────────────────────────────────
-- Used for delayed tool invocations, chain steps that need to sleep, and
-- rate-limited retries. A background worker drains pending rows where
-- run_after <= NOW().
CREATE TABLE IF NOT EXISTS agent_task_queue (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    actor         TEXT        NOT NULL DEFAULT 'queue',
    tool          TEXT        NOT NULL,
    args          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT        NOT NULL DEFAULT 'pending',
        -- pending | running | done | failed | cancelled
    run_after     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    max_attempts  INTEGER     NOT NULL DEFAULT 3,
    attempts      INTEGER     NOT NULL DEFAULT 0,
    result        JSONB,
    claimed_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_task_queue_pending
    ON agent_task_queue (status, run_after)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS agent_task_queue_user
    ON agent_task_queue (guild_id, user_id, status);


-- ── Event-based triggers ───────────────────────────────────────────────────
-- When an event fires on the bus (prices_updated, market_event_started, etc.)
-- matching triggers invoke their configured tool. `condition` is a JSONB
-- document whose shape depends on `kind`:
--   price_above / price_below -> {symbol, threshold}
--   event                     -> {event}
--   portfolio_drop            -> {pct}
CREATE TABLE IF NOT EXISTS agent_triggers (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    name        TEXT        NOT NULL DEFAULT '',
    kind        TEXT        NOT NULL,
    condition   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tool        TEXT        NOT NULL,
    args        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    one_shot    BOOLEAN     NOT NULL DEFAULT TRUE,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    fire_count  INTEGER     NOT NULL DEFAULT 0,
    last_result JSONB,
    fired_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_triggers_lookup
    ON agent_triggers (guild_id, kind, enabled);

CREATE INDEX IF NOT EXISTS agent_triggers_user
    ON agent_triggers (guild_id, user_id);


-- ── Multi-step chain runs ───────────────────────────────────────────────────
-- Stores the full chain plan + per-step results. Lets a chain resume or be
-- audited after the fact.
CREATE TABLE IF NOT EXISTS agent_chain_runs (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    actor         TEXT        NOT NULL DEFAULT 'chain',
    steps         JSONB       NOT NULL,
    step_results  JSONB,
    status        TEXT        NOT NULL DEFAULT 'running',
        -- running | done | failed | cancelled
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_chain_runs_user
    ON agent_chain_runs (guild_id, user_id, created_at DESC);


-- ── Pending approval slots ─────────────────────────────────────────────────
-- A DANGER-risk tool call stops here until the user explicitly approves.
-- The chain/agent that created it polls by id.
CREATE TABLE IF NOT EXISTS agent_approvals (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    tool        TEXT        NOT NULL,
    args        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'pending',
        -- pending | approved | denied | expired
    decided_by  BIGINT,
    decided_at  TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_approvals_user_status
    ON agent_approvals (guild_id, user_id, status);
