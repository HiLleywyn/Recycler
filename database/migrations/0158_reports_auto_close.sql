-- Reports auto-close toggle. Two behaviours unified under one switch:
--
--   1. Auto-REJECT a freshly-submitted report when the AI realness
--      verdict comes back ``spam`` or ``likely_fake`` with high
--      confidence. Mirrors the way ``reports_auto_diagnose`` /
--      ``reports_auto_fix`` toggle the optional sides of the pipeline.
--
--   2. Auto-RESOLVE a report when its associated auto-fix PR is merged
--      on GitHub. The pr_watcher tasks.loop polls open PRs in the
--      ``report_autofix_queue`` and flips the report's status to
--      ``resolved`` once GitHub reports the PR as merged.
--
-- Default NULL (treated as FALSE). When OFF, every status change
-- requires a human click on the existing triage view.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS reports_auto_close BOOLEAN;
