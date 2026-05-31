-- Auto-fix queue: per-report tracking of the AI-fix lifecycle.
--
-- One row per (report, attempt). The current implementation upserts on
-- (guild_id, report_id) so re-queueing a previously-failed report
-- restarts the lifecycle without leaving stale rows behind.
--
-- Status flow:
--   queued      -- admin asked for a proposal; worker hasn't picked it up yet
--   generating  -- worker is currently calling the LLM (transient)
--   proposed    -- patch validated locally, in-memory, awaiting Open PR click
--   pr_open     -- ,Open PR clicked + GitHub responded with a URL
--   discarded   -- admin clicked Discard
--   failed      -- LLM declined / path off-limits / GitHub API error
--   unfixable   -- AI returned UNKNOWN (no clear file)
--
-- ``proposed`` is in-memory on the cog. A bot restart drops the proposal
-- text and the row's status falls back to ``queued`` (the worker will
-- regenerate it on the next pass). That tradeoff means we don't have to
-- store full file bodies in the DB; only the metadata needed for status
-- displays + the PR URL once we have one.

CREATE TABLE IF NOT EXISTS report_autofix_queue (
    report_id        BIGINT       PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    requested_by     BIGINT       NOT NULL,
    status           TEXT         NOT NULL DEFAULT 'queued',
    proposed_path    TEXT,
    proposed_lines   INTEGER,
    -- GitHub issue tracking. Real reports get a tracking issue opened
    -- before the patch is generated so the admin sees the link from
    -- step one. The PR body later includes "Closes #<issue_number>"
    -- so merging the PR auto-closes the issue.
    issue_url        TEXT,
    issue_number     INTEGER,
    pr_url           TEXT,
    pr_number        INTEGER,
    last_error       TEXT,
    requested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT report_autofix_queue_status_chk CHECK (
        status IN ('queued','generating','proposed','pr_open',
                   'discarded','failed','unfixable')
    )
);

-- Worker pulls oldest queued row in the guild.
CREATE INDEX IF NOT EXISTS report_autofix_queue_worker_idx
    ON report_autofix_queue (guild_id, status, requested_at)
    WHERE status IN ('queued','generating');

-- Status-display rollups per guild.
CREATE INDEX IF NOT EXISTS report_autofix_queue_status_idx
    ON report_autofix_queue (guild_id, status);
