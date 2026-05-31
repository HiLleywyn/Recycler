-- 0223_disc_fun_moon_network_acceptance.sql
--
-- Disc.Fun graduated tokens are *primarily* on Discoin Network (the
-- curve quote is DFUN which lives on dsc, and holder balances get
-- credited to dsc wallets). They are ALSO seeded with a SYMBOL/MOON
-- bridge pool at graduation -- but the cog code only registered the
-- token on Discoin Network's accepted-token list. Result: the
-- SYMBOL/MOON pool was inferred as a Discoin Network pair (because
-- the custom token's home network is Discoin) and Moon Network
-- wallets couldn't hold the token.
--
-- This migration registers every existing graduated Disc.Fun token
-- as accepted on Moon Network too. The new graduation path
-- (services/discfun.graduate_proto_token) does the same on every
-- future graduation. Idempotent via ON CONFLICT on the unique
-- (guild_id, network, symbol) tuple.

INSERT INTO network_accepted_tokens (guild_id, network, symbol)
SELECT p.guild_id, 'Moon Network', p.symbol
  FROM proto_tokens p
 WHERE p.graduated = TRUE
ON CONFLICT DO NOTHING;
