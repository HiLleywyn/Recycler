-- 0222_disc_fun_stakes_repair.sql
--
-- Rescue migration for environments whose ``discfun_stakes`` was
-- created from an early draft of 0218 that did NOT include the
-- ``auto_compound`` and ``total_compounded`` columns. The current
-- 0218 DOES include them in the CREATE TABLE plus an idempotent
-- ALTER TABLE follow-up, but ``schema_migrations`` records 0218 by
-- filename only -- so a host that ran the early draft has 0218
-- marked as applied and never re-runs the column-adds. Players hit
-- ``column auto_compound does not exist`` the moment they try
-- ``,fun autocompound SYM on``.
--
-- This migration force-adds the columns idempotently. Safe on a fresh
-- DB and on an already-correct DB (no-op via IF NOT EXISTS).

ALTER TABLE discfun_stakes
    ADD COLUMN IF NOT EXISTS auto_compound    BOOLEAN        NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS total_compounded NUMERIC(36, 0) NOT NULL DEFAULT 0;
