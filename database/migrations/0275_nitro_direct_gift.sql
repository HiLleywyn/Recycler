-- 0275_nitro_direct_gift.sql
--
-- Extend the Nitro sharing system (migration 0274) with direct gifting:
-- a host can send a Nitro / Nitro Basic gift straight to one specific
-- player instead of running a lottery.
--
-- Direct gifts reuse the nitro_lotteries table with kind = 'direct',
-- status = 'drawn' and winner_id set to the chosen recipient at creation
-- time -- so they are skipped by the draw loop (status = 'open' only) and
-- by the .nitro list view, and the recipient claims through the same
-- private DM + winner-locked "Reveal my gift" button as a lottery winner.
--
-- ADD COLUMN IF NOT EXISTS keeps this safe whether or not 0274 has already
-- been applied on a given database.

ALTER TABLE nitro_lotteries
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'lottery';
