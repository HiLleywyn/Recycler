-- V3 Pillar 9: restore item-LP positions wiped or drained by past wealth-tax cycles
--
-- Background: pre-V3, ``services.wealth_equalizer._drain_user`` would
-- liquidate items / stones / rigs / holdings / savings to satisfy the
-- owed tax. LP shares themselves were never directly drained (that
-- path was deliberately skipped because mid-cycle LP unwinds have too
-- many price-impact failure modes), but the *owed amount* included LP
-- value, which meant LP-heavy players were taxed harder against their
-- non-LP surfaces -- effectively a tax on having backed the AMMs.
--
-- This migration is a one-shot, idempotent record that the system
-- intends to apologise for that policy. It does NOT directly modify
-- ``lp_positions``: replaying LP creation cleanly against historical
-- pool reserves is risky because pool reserves themselves drift, and a
-- naive ``UPDATE lp_positions SET lp_shares = lp_shares + delta`` would
-- inflate the pool's total_lp without a matching reserve credit and
-- corrupt every other LPer's share. Instead, the migration:
--
--   1. Creates ``wealth_lp_restoration`` -- an audit row per affected
--      user enumerating each historical tax cycle where LP was in the
--      OWED amount, with the implied USD value that should be returned
--      ("USD-equivalent reimbursement").
--   2. Writes a ``WEALTH_LP_REFUND`` transaction crediting that USD
--      back to the player's wallet so they can rebuild LP positions
--      themselves at current pool prices -- cleaner than a synthetic
--      LP re-mint that would skew pool reserves.
--   3. Marks the migration applied so re-running it does nothing.
--
-- A new admin command (``,admin lp restore audit``) reads from
-- ``wealth_lp_restoration`` to show the planned diff before ship; the
-- migration is small enough to run inline on container boot. Going
-- forward (after migration 0247 adds the audit columns and the
-- equalizer service starts respecting them), LP is permanently
-- carved out of the taxable amount so this restoration never needs to
-- happen again.

CREATE TABLE IF NOT EXISTS wealth_lp_restoration (
    id              BIGSERIAL   PRIMARY KEY,
    guild_id        BIGINT      NOT NULL,
    user_id         BIGINT      NOT NULL,
    cycle_at        TIMESTAMPTZ NOT NULL,
    refund_raw      NUMERIC(36,0) NOT NULL,
    source_tax_raw  NUMERIC(36,0) NOT NULL,
    source_nw_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wealth_lp_restoration_user_idx
    ON wealth_lp_restoration (guild_id, user_id, cycle_at DESC);

CREATE INDEX IF NOT EXISTS wealth_lp_restoration_cycle_idx
    ON wealth_lp_restoration (guild_id, cycle_at DESC);

-- Idempotency tracker: this row is inserted at the end of the actual
-- restoration pass run by services.lp_restore.run_restore_pass(). The
-- migration itself only creates the table; the bot runs the pass on
-- boot and uses this row to gate the work to once-per-guild forever.
CREATE TABLE IF NOT EXISTS wealth_lp_restoration_runs (
    guild_id     BIGINT      PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    refunded_users  INTEGER  NOT NULL DEFAULT 0,
    refunded_raw    NUMERIC(36,0) NOT NULL DEFAULT 0,
    notes        TEXT
);
