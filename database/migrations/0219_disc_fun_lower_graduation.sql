-- 0219_disc_fun_lower_graduation.sql
--
-- Disc.Fun graduation threshold lowered from 50M DFUN to 10M DFUN. Any
-- non-graduated protos that were deployed under the older 50M target get
-- their graduation_quote rewritten in place so they reach the milestone
-- on the new schedule -- existing real_quote_collected progress carries
-- over (a proto that already had, say, 6M DFUN collected is now 60% of
-- the way to graduation, not 12%).
--
-- The bonding-curve reserves themselves (virtual_quote / virtual_token)
-- are intentionally left alone. The old curve was tuned for V_q = 5M /
-- V_t = 880M and is mid-flight; rewriting reserves would either reset
-- progress or make the math discontinuous. Lowering the threshold means
-- the next ``buy_proto_token`` whose accumulated real_quote_collected
-- crosses 10M triggers graduation, and ``graduate_proto_token`` already
-- handles whatever circulation / LP-slice ratio the actual curve state
-- produces (lp_token_raw = total_supply - tokens_in_circulation).
--
-- Idempotent: only rewrites rows whose graduation_quote is still at the
-- legacy 50M value, leaves anything already at 10M alone, and skips
-- already-graduated protos.

-- Postgres parses 10000000 and 1000000000000000000 as int8 literals, and
-- their product (1e25) overflows int8. Cast at least one operand to
-- NUMERIC so the multiplication happens in unbounded precision before
-- it lands in the NUMERIC(36,0) column.
UPDATE proto_tokens
   SET graduation_quote = 10000000::NUMERIC * 1000000000000000000::NUMERIC
 WHERE graduated = FALSE
   AND graduation_quote = 50000000::NUMERIC * 1000000000000000000::NUMERIC;
