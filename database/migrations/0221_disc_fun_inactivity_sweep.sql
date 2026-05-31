-- 0221_disc_fun_inactivity_sweep.sql
--
-- Disc.Fun proto-token "use it or lose it" sweep: protos that get no
-- buyer for a full week are destroyed (proto_tokens row + cascade
-- delete of holdings / trades). Sells do NOT refresh the timer; only
-- buys count, since a proto with only sellers is dead by definition.
-- Adds a ``last_buy_at`` clock column so the sweep can run on the DB
-- side (``NOW() - last_buy_at >= 7 days``) without a Python clock.
--
-- Backfill: take the latest buy from proto_token_trades when one
-- exists, otherwise fall back to ``created_at`` so a brand-new proto
-- still gets a full 7-day window. Idempotent: ADD COLUMN IF NOT
-- EXISTS keeps the migration safe to re-run.

ALTER TABLE proto_tokens
    ADD COLUMN IF NOT EXISTS last_buy_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE proto_tokens p
   SET last_buy_at = COALESCE(
       (SELECT MAX(t.created_at)
          FROM proto_token_trades t
         WHERE t.proto_id = p.proto_id AND t.side = 'buy'),
       p.created_at
   )
 WHERE p.last_buy_at = p.created_at  -- only touch rows the migration just defaulted
    OR p.last_buy_at IS NULL;

-- Used by the inactivity sweep -- find ungraduated protos whose
-- last_buy_at is older than the configured threshold. Partial index
-- keeps the live trade hot path (live AMM updates) cheap.
CREATE INDEX IF NOT EXISTS idx_proto_tokens_last_buy
    ON proto_tokens (last_buy_at)
    WHERE graduated = FALSE;
