-- LP yield clock: track when each LP position last received a yield payout so
-- the hourly tick can skip rows that already paid this cycle (and resume cleanly
-- after a bot restart or downtime longer than one tick).
--
-- The yield service ALSO clamps elapsed time to 24h before paying so that long
-- downtime can't dump a month of inflation into the economy at once -- this
-- column just lets us know when "now" is relative to the last credit.
--
-- Default NOW() so existing rows are treated as "just paid" on rollout, which
-- means no retroactive bonanza payout. Players start accruing yield from this
-- migration's runtime forward.
--
-- Mirror column on group_lp_positions for cross-group partnership pools, which
-- get paid into reserve_usd by the same tick.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS.

ALTER TABLE lp_positions
    ADD COLUMN IF NOT EXISTS last_yield_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE group_lp_positions
    ADD COLUMN IF NOT EXISTS last_yield_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
