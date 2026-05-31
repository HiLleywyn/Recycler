-- V3 Pillar 10 production fix: expand wealth_redistribution_log.kind CHECK.
--
-- Migration 0230 created the table with ``CHECK (kind IN ('tax', 'ubi'))``,
-- but ``services/token_health.py`` has been writing rows with
-- ``kind='token_burn'`` since well before V3 -- which means the legacy
-- CHECK was already dropped somewhere along the way and there are rows
-- in production with kinds outside the original tuple. V3 CWE also
-- writes per-tx rows with kind='tax_tx' / 'ubi_tx'.
--
-- The original v0251 form of this migration used a validated ADD
-- CONSTRAINT, which scans every existing row -- and any row that
-- doesn't match the new tuple (e.g. an old ``token_burn`` log row) hard
-- fails the ALTER. That crashed the bot on every restart of the V3
-- rollout. NOT VALID skips that scan; subsequent inserts and updates
-- are still validated. We don't care about legacy rows for the CHECK
-- semantics, only about preventing new bad inserts.
--
-- Idempotent: drops any pre-existing CHECK constraint on this column
-- by its conventional name, then re-adds with NOT VALID so the migration
-- always applies cleanly regardless of which kinds already exist.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'wealth_redistribution_log'::regclass
          AND conname = 'wealth_redistribution_log_kind_check'
    ) THEN
        ALTER TABLE wealth_redistribution_log
            DROP CONSTRAINT wealth_redistribution_log_kind_check;
    END IF;
END$$;

ALTER TABLE wealth_redistribution_log
    ADD CONSTRAINT wealth_redistribution_log_kind_check
    CHECK (kind IN ('tax', 'ubi', 'tax_tx', 'ubi_tx', 'token_burn'))
    NOT VALID;
