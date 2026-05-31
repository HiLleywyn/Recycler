-- Track lifetime LP yield paid to each position so ,mylp can show
-- "+$X yield earned" per row. The existing yield_summary line at the
-- bottom of ,mylp reads transactions(LP_YIELD) which is unattributed
-- to a specific pool -- a player who LPs across multiple pools sees
-- "Gain: $0" on each row even when the lp_yield_task has been paying
-- them every hour, because the per-row Gain only reflects swap-fee
-- accrual via lp_snapshot.
--
-- Bumping this counter inside services/lp_yield.py on every successful
-- per-position payout gives ,mylp something concrete to render per
-- pool, without changing how the LP_YIELD wallet credit / log_tx
-- happens.

ALTER TABLE lp_positions
    ADD COLUMN IF NOT EXISTS yield_paid_usd_raw NUMERIC(36, 0) NOT NULL DEFAULT 0;

ALTER TABLE group_lp_positions
    ADD COLUMN IF NOT EXISTS yield_paid_usd_raw NUMERIC(36, 0) NOT NULL DEFAULT 0;

-- Backfill the per-position counter from lifetime LP_YIELD transactions
-- so players who already earned yield over the past week / month see a
-- non-zero "Earned so far" line on every active position. The total is
-- distributed evenly across each user's currently-active positions --
-- not perfectly accurate (positions removed before this migration get
-- nothing), but better than starting every counter at $0.
WITH lifetime AS (
    SELECT user_id, guild_id, SUM(amount_out)::NUMERIC AS total
      FROM transactions
     WHERE tx_type = 'LP_YIELD'
     GROUP BY user_id, guild_id
),
counts AS (
    SELECT user_id, guild_id, COUNT(*)::NUMERIC AS n
      FROM lp_positions
     WHERE lp_shares > 0
     GROUP BY user_id, guild_id
)
UPDATE lp_positions lp
   SET yield_paid_usd_raw = (l.total / c.n)::NUMERIC(36, 0)
  FROM lifetime l, counts c
 WHERE lp.user_id  = l.user_id
   AND lp.guild_id = l.guild_id
   AND lp.user_id  = c.user_id
   AND lp.guild_id = c.guild_id
   AND lp.lp_shares > 0
   AND c.n > 0;

