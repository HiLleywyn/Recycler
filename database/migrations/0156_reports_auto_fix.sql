-- Reports auto-fix toggle (Tier A: AI drafts a patch + opens a draft PR).
--
-- When TRUE, every newly-submitted report whose AI realness verdict comes
-- back as "real" or "likely_real" with confidence != "low" kicks off the
-- auto-fix pipeline: pick a buggy file, draft a patch, validate it, open
-- a DRAFT pull request on the configured GitHub repo. The PR is never
-- auto-merged -- a human reviews and merges, then Railway redeploys.
--
-- Defaults to NULL (treated as FALSE). Even when FALSE, ,admin reports
-- autofix <id> remains available as a manual trigger so admins can opt
-- in per-report without flipping the auto path.
--
-- This is a per-guild toggle so a multi-tenant deployment can have one
-- guild on auto-fix while every other server stays read-only.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS reports_auto_fix BOOLEAN;
