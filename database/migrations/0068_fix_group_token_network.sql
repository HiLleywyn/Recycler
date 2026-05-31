-- Fix group tokens that have a network set on mining_groups but NULL on guild_tokens.
-- This caused them to show as "Other / PoW" in .crypto instead of their actual network.
UPDATE guild_tokens gt
SET    network = mg.token_network
FROM   mining_groups mg
WHERE  gt.guild_id = mg.guild_id
  AND  gt.symbol   = mg.token_symbol
  AND  mg.token_network IS NOT NULL
  AND  (gt.network IS NULL OR gt.network = '');
