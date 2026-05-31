-- DiscoAI: self-hosted LLM integration tables.
--
-- Backs the ai/ module: long-term facts, episodic summaries, full training
-- traces (for offline LoRA fine-tuning), and per-channel passive-learning
-- opt-in. All tables are namespaced by `scope` (e.g. "user:123:guild:456",
-- "guild:456", "lore") so a single Postgres can serve every server.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── disco_facts ─────────────────────────────────────────────────────────
-- Long-term, structured memory. UPSERT on (scope, key) so a fact about
-- a user/guild/lore item is overwritten in place with a new value and
-- updated_at. Trigram GIN over `value` powers fuzzy ILIKE recall and
-- similarity() ranking in ai/memory.py:search_facts. pgvector cosine
-- search remains future work once an embedding pipeline lands.
CREATE TABLE IF NOT EXISTS disco_facts (
    id          BIGSERIAL    PRIMARY KEY,
    scope       TEXT         NOT NULL,
    key         TEXT         NOT NULL,
    value       TEXT         NOT NULL,
    confidence  REAL         NOT NULL DEFAULT 0.5,
    source      TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (scope, key)
);
CREATE INDEX IF NOT EXISTS disco_facts_scope_idx
    ON disco_facts (scope);
CREATE INDEX IF NOT EXISTS disco_facts_value_trgm_idx
    ON disco_facts USING GIN (value gin_trgm_ops);

-- ── disco_episodes ──────────────────────────────────────────────────────
-- Conversation-summary memory. Each row is one summarized episode with
-- searchable tags so the orchestrator can recall "we talked about LP
-- impermanent loss with this user last week" without dragging the full
-- transcript into the prompt.
CREATE TABLE IF NOT EXISTS disco_episodes (
    id          BIGSERIAL    PRIMARY KEY,
    scope       TEXT         NOT NULL,
    summary     TEXT         NOT NULL,
    tags        TEXT[]       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS disco_episodes_scope_idx
    ON disco_episodes (scope);
CREATE INDEX IF NOT EXISTS disco_episodes_tags_idx
    ON disco_episodes USING GIN (tags);
CREATE INDEX IF NOT EXISTS disco_episodes_summary_trgm_idx
    ON disco_episodes USING GIN (summary gin_trgm_ops);

-- ── disco_training_turns ────────────────────────────────────────────────
-- Append-only training corpus. One row per orchestrator handle_message().
-- messages_json holds the full message list (system + history + tool
-- rounds + final assistant) so we can replay the exact training example
-- via scripts/export_training_data.py. feedback_score lands later from
-- 👍/👎 reactions and is used to filter the export set.
CREATE TABLE IF NOT EXISTS disco_training_turns (
    id              BIGSERIAL    PRIMARY KEY,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    user_id         BIGINT       NOT NULL,
    guild_id        BIGINT,
    channel_id      BIGINT,
    system_prompt   TEXT         NOT NULL,
    user_message    TEXT         NOT NULL,
    assistant_reply TEXT         NOT NULL,
    messages_json   JSONB        NOT NULL,
    tool_calls_json JSONB        NOT NULL DEFAULT '[]'::jsonb,
    model           TEXT         NOT NULL,
    rounds          INT          NOT NULL,
    latency_ms      INT          NOT NULL,
    finish_reason   TEXT         NOT NULL,
    feedback_score  SMALLINT
);
CREATE INDEX IF NOT EXISTS disco_training_turns_user_idx
    ON disco_training_turns (user_id);
CREATE INDEX IF NOT EXISTS disco_training_turns_created_idx
    ON disco_training_turns (created_at DESC);
CREATE INDEX IF NOT EXISTS disco_training_turns_feedback_idx
    ON disco_training_turns (feedback_score)
    WHERE feedback_score IS NOT NULL;

-- ── disco_passive_channels ──────────────────────────────────────────────
-- Per-channel opt-in for passive learning (DISCOAI_PASSIVE_LEARNING).
-- When enabled and the channel is in this table, the cog logs ambient
-- messages as episodes without responding.
CREATE TABLE IF NOT EXISTS disco_passive_channels (
    guild_id    BIGINT       NOT NULL,
    channel_id  BIGINT       NOT NULL,
    enabled_by  BIGINT       NOT NULL,
    enabled_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);
