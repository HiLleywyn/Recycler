-- 0288_clanktank_clusters.sql
-- Cluster intelligence: cluster metadata, membership, named patterns,
-- and a history table for soft-deleted clanker records.

CREATE TABLE IF NOT EXISTS clanker_clusters (
    id           SERIAL       PRIMARY KEY,
    guild_id     BIGINT       NOT NULL,
    label        TEXT,
    confidence   FLOAT        NOT NULL DEFAULT 0.0,
    cleaved_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clanker_clusters_guild
    ON clanker_clusters (guild_id, confidence DESC);

-- Normalised cluster membership (one row per (cluster, user) pair)
CREATE TABLE IF NOT EXISTS clanker_cluster_members (
    cluster_id   INT          NOT NULL REFERENCES clanker_clusters(id) ON DELETE CASCADE,
    guild_id     BIGINT       NOT NULL,
    user_id      BIGINT       NOT NULL,
    added_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_clanker_cluster_members_user
    ON clanker_cluster_members (guild_id, user_id);

-- Named patterns extracted from clusters (token, num_suffix, separator, ...)
-- hits and weight grow with every confirmed cleave, giving the AI more signal.
CREATE TABLE IF NOT EXISTS clanker_patterns (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    cluster_id    INT          REFERENCES clanker_clusters(id) ON DELETE SET NULL,
    pattern_type  TEXT         NOT NULL,
    value         TEXT         NOT NULL,
    hits          INT          NOT NULL DEFAULT 1,
    weight        FLOAT        NOT NULL DEFAULT 1.0,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, pattern_type, value)
);

CREATE INDEX IF NOT EXISTS idx_clanker_patterns_lookup
    ON clanker_patterns (guild_id, pattern_type, weight DESC);

-- Soft history: written when a clanker record is deleted (released or cleaved).
-- Used for pattern matching against new joins.
CREATE TABLE IF NOT EXISTS clanker_history (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       BIGINT       NOT NULL,
    guild_id      BIGINT       NOT NULL,
    usernames     TEXT[]       NOT NULL DEFAULT '{}',
    display_names TEXT[]       NOT NULL DEFAULT '{}',
    reason        TEXT,
    final_score   INT          NOT NULL DEFAULT 0,
    cluster_id    INT          REFERENCES clanker_clusters(id) ON DELETE SET NULL,
    clanked_at    TIMESTAMPTZ,
    released_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clanker_history_user
    ON clanker_history (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_clanker_history_names
    ON clanker_history USING GIN (usernames);

-- Backlink from active records to their cluster
ALTER TABLE clanker_records
    ADD COLUMN IF NOT EXISTS cluster_id INT REFERENCES clanker_clusters(id) ON DELETE SET NULL;
