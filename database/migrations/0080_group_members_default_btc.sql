-- Migration 0080: Default group token network to Bitcoin Network.
--
-- Groups that have created a token (token_symbol IS NOT NULL) but never
-- explicitly bound it to a PoW network (token_network IS NULL) are
-- assigned to 'Bitcoin Network'.
--
-- Also syncs the matching guild_tokens.network column so both tables
-- stay in agreement (mirrors what migration 0068 did for the reverse case).
--
-- Groups that already have a token_network set are left unchanged.

UPDATE mining_groups
SET token_network = 'Bitcoin Network'
WHERE token_symbol IS NOT NULL
  AND token_network IS NULL;

UPDATE guild_tokens gt
SET    network          = 'Bitcoin Network',
       trading_enabled  = TRUE,
       vault_locked     = FALSE
FROM   mining_groups mg
WHERE  gt.guild_id = mg.guild_id
  AND  gt.symbol   = mg.token_symbol
  AND  mg.token_network = 'Bitcoin Network'
  AND  (gt.network IS NULL OR gt.network = '');
