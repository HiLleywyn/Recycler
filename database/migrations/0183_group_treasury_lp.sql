-- 0183_group_treasury_lp.sql
--
-- Adds founder-controlled treasury <-> LP plumbing on mining_groups.
-- The founder can deposit a % of the vault_token_bal into the
-- group's own group_token/USD AMM pool, or withdraw a % back into
-- the treasury. Single-sided ops (only the group token side moves)
-- so we don't have to drain reserve_usd in lockstep -- the founder
-- accepts the resulting price impact.
--
-- Safeguards live in services/group_lp.py:
--   * Founder-only (mining_groups.founder_id auth check).
--   * Per-action pct cap (default 25%).
--   * 24h cooldown via last_treasury_lp_at.
--   * Confirm dialog in the cog before either side runs.
--
-- Three columns:
--   treasury_lp_unlocked   -- master kill switch. Defaults FALSE; the
--                             founder must run ,group lp enable once
--                             before deposits / withdrawals work.
--                             A guild admin can re-disable to pause.
--   last_treasury_lp_at    -- timestamp of the last successful deposit
--                             or withdrawal; cooldown is checked off
--                             of NOW() - this column.
--   treasury_lp_total_raw  -- lifetime running total of the GROUP TOKEN
--                             amount the founder has put in across all
--                             deposits (raw 10^18). Lets the cooldown
--                             panel display "X total ever deposited"
--                             without joining tx_log.
--
-- Idempotent.

ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS treasury_lp_unlocked   BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_treasury_lp_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS treasury_lp_total_raw  NUMERIC(36, 0) NOT NULL DEFAULT 0;
