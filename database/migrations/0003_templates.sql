-- Community templates: a shareable guild blueprint (structure only, never
-- messages or members). Free for everyone -- no premium gating.
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,                         -- short public slug
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_id    BIGINT NOT NULL,
    data        JSONB NOT NULL,                           -- serialized structure
    uses        INTEGER NOT NULL DEFAULT 0,
    featured    BOOLEAN NOT NULL DEFAULT FALSE,
    listed      BOOLEAN NOT NULL DEFAULT TRUE,            -- shown in the browser
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_templates_owner  ON templates(owner_id);
CREATE INDEX IF NOT EXISTS idx_templates_uses   ON templates(uses DESC);
CREATE INDEX IF NOT EXISTS idx_templates_listed ON templates(listed) WHERE listed;
