-- V3 Pillar 9: LP tax exemption + audit columns
--
-- Pre-V3 the wealth tax counted LP positions in the OWED amount (since
-- compute_bulk_net_worth.lp_value rolls into the total) but skipped LP
-- in the DRAIN because mid-cycle LP unwinds blow up. That asymmetry is
-- hostile: a player who put everything into LP to support liquidity
-- gets max-bracketed and pays it out of whatever liquid surface they
-- have left. Players who anticipated this withdrew, which is the
-- opposite of what the economy needs.
--
-- V3 carves LP out of the taxable amount entirely. ``wealth_redistribution_log``
-- gains explicit ``gross_nw_usd`` (with LP) and ``taxable_nw_usd``
-- (without LP) columns so anyone reading ``,drs equalizer`` sees the
-- carve-out instead of having to infer it.
--
-- ``net_worth_usd`` (the existing column) keeps its meaning -- it is
-- the gross NW the cycle saw -- so existing audit queries don't need to
-- migrate. The two new columns are additive context.

ALTER TABLE wealth_redistribution_log
    ADD COLUMN IF NOT EXISTS gross_nw_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS taxable_nw_usd  DOUBLE PRECISION NOT NULL DEFAULT 0;

-- Backfill: every existing tax row was computed against gross NW with
-- LP included, so taxable_nw_usd starts equal to net_worth_usd. The
-- new policy only applies to rows written AFTER this migration ships.
UPDATE wealth_redistribution_log
   SET gross_nw_usd   = net_worth_usd,
       taxable_nw_usd = net_worth_usd
 WHERE gross_nw_usd = 0 AND taxable_nw_usd = 0;
