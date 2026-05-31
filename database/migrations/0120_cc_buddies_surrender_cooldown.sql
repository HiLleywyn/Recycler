-- Surrender -> hatch exploit gate.
--
-- Without a cooldown, a user could surrender their buddy and immediately
-- hatch a new one, effectively paying nothing to reroll rarity / species
-- outside the limited free-reroll counter. Persisting the surrender time
-- on the append-only hatch-log row lets the hatch command enforce a
-- DB-side cooldown using the Postgres clock (see CLAUDE.md: "Never
-- compare Python datetime.now() against a Postgres timestamp").
--
-- NULL means "never surrendered" (or legacy row predating this migration).
-- The hatch command treats NULL as cooldown-cleared.

ALTER TABLE cc_buddy_hatches
    ADD COLUMN IF NOT EXISTS last_surrender_at TIMESTAMPTZ NULL;
