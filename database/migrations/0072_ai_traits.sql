-- Migration 0072: Layered trait engine for AI user memory.
-- Adds ai_user_traits (time-decayed, confidence-scored personality traits) and
-- ai_user_events (raw signal log for behavior shift detection).
--
-- ai_user_traits stores one row per (user, guild, trait_key). Traits are
-- bucketed into three layers:
--   stable      - high sample count (>=20) AND high confidence (>=0.7)
--   volatile    - recently observed (last hour), lower sample count
--   interaction - everything else (medium frequency, medium confidence)
--
-- Weight decays exponentially with time via DB-side clock so no Python drift.
-- Confidence = 1 - exp(-sample_size / K) where K=10, reaching ~0.86 at 20 obs.

-- ── ai_user_traits ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_user_traits (
    user_id          BIGINT              NOT NULL,
    guild_id         BIGINT              NOT NULL,
    trait_key        TEXT                NOT NULL,
    trait_value      TEXT                NOT NULL DEFAULT '',
    layer            TEXT                NOT NULL DEFAULT 'volatile',
    confidence       DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    weight           DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    sample_size      INTEGER             NOT NULL DEFAULT 1,
    last_observed_at TIMESTAMPTZ         NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ         NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, trait_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_traits_user  ON ai_user_traits (user_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ai_traits_layer ON ai_user_traits (user_id, guild_id, layer);

-- ── ai_user_events ────────────────────────────────────────────────────────────
-- Short-lived raw signal log. Pruned to the last 200 rows per user by
-- application-side maintenance (not enforced automatically by the database).
-- Used for behavior shift detection (recent vs baseline distribution comparison).
CREATE TABLE IF NOT EXISTS ai_user_events (
    id               BIGSERIAL           PRIMARY KEY,
    user_id          BIGINT              NOT NULL,
    guild_id         BIGINT              NOT NULL,
    event_type       TEXT                NOT NULL,
    event_subtype    TEXT                NOT NULL,
    created_at       TIMESTAMPTZ         NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_events_user ON ai_user_events (user_id, guild_id, created_at DESC);
