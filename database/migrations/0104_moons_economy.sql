-- 0104: Moons (MOON) economy - Slice 1 (Lunar Mint)
--
-- Adds the ``lunar_stakes`` table that powers the Lunar Mint: players stake
-- a group token into a row here and the hourly tick in cogs/moons.py credits
-- MOON into their Moon Network DeFi wallet. Also seeds the MOON price row
-- into every existing guild so the token is immediately pricable and
-- swappable.
--
-- Slice 2 (MOON -> DSD via Moon Pool) lands in a follow-up migration:
-- creates ``moon_stakes`` and adds ``distributable_balance`` to
-- ``network_vaults``. Not included here by design.
--
-- Primary key (user_id, guild_id, symbol):
--   A user can stake multiple group tokens at once (one row per group
--   token), but only one position per (user, group token). Re-staking the
--   same token tops up the existing row rather than creating duplicates.
--
-- staked_at:
--   Set on INSERT only. Subsequent tops-up keep the original timestamp so
--   the 12h warmup cannot be gamed by stake-topup cycling (see
--   upsert_lunar_stake in database/moons.py).

BEGIN;

CREATE TABLE IF NOT EXISTS lunar_stakes (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    symbol         TEXT          NOT NULL,
    amount         NUMERIC(36,0) NOT NULL DEFAULT 0,
    staked_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    session_earned NUMERIC(28,8) NOT NULL DEFAULT 0,
    total_earned   NUMERIC(28,8) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, symbol),
    CONSTRAINT fk_lunar_stakes_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT fk_lunar_stakes_token FOREIGN KEY (guild_id, symbol)
        REFERENCES guild_tokens(guild_id, symbol) ON DELETE CASCADE,
    CONSTRAINT chk_lunar_stakes_amount CHECK (amount >= 0)
);

-- Index for the per-guild tick loop aggregate.
CREATE INDEX IF NOT EXISTS idx_lunar_stakes_gid_sym
    ON lunar_stakes (guild_id, symbol);

-- Seed MOON price rows for every existing guild at $0.50. New guilds pick
-- the seed up from Config.TOKENS via the normal guild-init seeder.
INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low)
SELECT 'MOON', guild_id, 0.50, 0.50, 0.50, 0.50
  FROM guild_settings
ON CONFLICT (symbol, guild_id) DO NOTHING;

COMMIT;
