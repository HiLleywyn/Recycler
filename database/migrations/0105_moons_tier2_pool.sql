-- 0105: Moons (MOON) economy - Slice 2 (Moon Pool: MOON -> DSD real yield)
--
-- Tier 2 pays MOON stakers a share of Moon Network's vault inflow in DSD.
-- Adds the moon_stakes table (player positions) and two columns on
-- network_vaults: distributable_balance (USD earmarked for stakers, drips
-- out 1/168 per hour over 7 days) and last_moon_distributed_at (drip
-- timestamp, DB-side clock for tick deltas).
--
-- Depends on 0104_moons_economy.sql (MOON token in crypto_prices, lunar_stakes
-- table, network_vaults already exists from earlier migrations).

BEGIN;

CREATE TABLE IF NOT EXISTS moon_stakes (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    amount         NUMERIC(36,0) NOT NULL DEFAULT 0,
    staked_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    session_earned NUMERIC(28,8) NOT NULL DEFAULT 0,
    total_earned   NUMERIC(28,8) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_moon_stakes_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_moon_stakes_amount CHECK (amount >= 0)
);
CREATE INDEX IF NOT EXISTS idx_moon_stakes_gid ON moon_stakes (guild_id);

ALTER TABLE network_vaults
    ADD COLUMN IF NOT EXISTS distributable_balance NUMERIC(28,8) NOT NULL DEFAULT 0;
ALTER TABLE network_vaults
    ADD COLUMN IF NOT EXISTS last_moon_distributed_at TIMESTAMPTZ;

COMMIT;
