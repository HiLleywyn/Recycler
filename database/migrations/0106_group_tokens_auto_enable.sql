-- 0106: Group tokens are auto-enabled on creation.
--
-- Previously (migration 0061) the trading_enabled flag defaulted to FALSE,
-- forcing a server admin to run `.admin grouptoken enable <symbol>` for every
-- new group token before founders could trade. That gate was inconsistently
-- enforced in practice (buy/sell/swap did not check it, auto-bind set it
-- TRUE anyway) and produced a pointless admin chore. Flip the default to
-- TRUE and backfill every existing group token so founders can trade their
-- own tokens out of the box. The admin enable/disable commands still exist
-- for the rare case where a token needs to be halted manually.

BEGIN;

-- Flip the schema default for all future inserts.
ALTER TABLE guild_tokens
    ALTER COLUMN trading_enabled SET DEFAULT TRUE;

-- Backfill every group token row that was stuck disabled under the old default.
UPDATE guild_tokens
   SET trading_enabled = TRUE
 WHERE token_type = 'group'
   AND trading_enabled = FALSE;

COMMIT;
