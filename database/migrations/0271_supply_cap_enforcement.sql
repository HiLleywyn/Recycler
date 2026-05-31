-- 0271_supply_cap_enforcement.sql
--
-- Companion to the _clamp_mint_delta cap enforcement added in
-- database/users.py.  The mint helper reads max_supply from
-- guild_tokens.max_supply for custom tokens, but the deploy paths
-- (cogs/nfts.py token deploy, services/discfun.py graduation, cogs/groups.py
-- group token mint) historically wrote it only into the JSON contract
-- params blob -- so the structured column stayed NULL and the cap could
-- not be enforced.
--
-- This migration:
--   1. Backfills guild_tokens.max_supply (raw NUMERIC(36,0) = human * 10^18)
--      from token_contracts.params->>'max_supply' for any row still NULL.
--   2. Applies a default cap of 100M tokens to any custom guild_token that
--      ended up with no max_supply at all (group tokens, legacy deploys).
--      100M matches the cap shape used for the network coins (DFUN, MOON,
--      REEL, GBC) so behaviour is uniform.
--   3. Clamps guild_tokens.circulating_supply to the resulting max_supply
--      so the supply display stops reading >100% on any tracker that drifted
--      past the cap before the mint chokepoint was fixed.
--
-- Built-in tokens (BTC/ETH/SUN/DFUN/MOON/REEL/GBC/PEPE/etc.) are seeded
-- from Config.TOKENS at startup, so their caps come from Python rather
-- than this table; the post-migration startup task in framework/bot.py
-- handles clamping crypto_prices.circulating_supply for those.

-- 1. Backfill from contract params JSON.  Uses (params->>'max_supply')::numeric
--    cast so non-numeric values silently become NULL (we'd rather see the
--    default kick in than crash the migration).
UPDATE guild_tokens gt
SET max_supply = (
        (tc.params->>'max_supply')::numeric * 1000000000000000000
    )::numeric(36,0)
FROM token_contracts tc
WHERE tc.guild_id = gt.guild_id
  AND tc.symbol   = gt.symbol
  AND gt.max_supply IS NULL
  AND (tc.params ? 'max_supply')
  AND (tc.params->>'max_supply') ~ '^[0-9]+(\.[0-9]+)?$';

-- 2. Default cap for anything still uncapped (100M tokens, raw-scaled).
UPDATE guild_tokens
SET max_supply = 100000000000000000000000000  -- 100M * 10^18
WHERE max_supply IS NULL;

-- 3. Clamp existing circulating_supply to the cap.  GREATEST(0, ...) keeps
--    the floor in case a row was negative for any reason.
UPDATE guild_tokens
SET circulating_supply = GREATEST(0, LEAST(circulating_supply, max_supply))
WHERE max_supply IS NOT NULL;
