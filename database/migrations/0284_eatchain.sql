-- 0284_eatchain.sql
--
-- EatChain expansion. The "Eat the Rich" minigame is rebranded as EatChain,
-- a satirical simulated Layer-2 DeFi ecosystem with its own token ($EAT),
-- staking + passive yield, a 100-level rank ladder, cosmetic titles, four
-- new DeFi tactics and mempool-snipe random targeting.
--
-- The original theft engine and its exploit_shields / exploit_stats /
-- exploit_history / eat_salad_bowl tables are KEPT -- the expansion layers
-- progression and the $EAT economy on top via new columns. The internal
-- exploit_* names predate the rebrand and stay so live player records and
-- cross-system triggers are not disturbed.
--
-- Every statement uses IF EXISTS / IF NOT EXISTS so the migration is safe to
-- re-run on a database at any prior state.

-- ── Progression + $EAT economy columns on exploit_stats ────────────────────
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_level         INTEGER NOT NULL DEFAULT 1;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_xp            NUMERIC(28,8) NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_staked        NUMERIC(36,0) NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_yield_at      TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_title         TEXT;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS rugs_pulled       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS insurance_charges INTEGER NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS insurance_until   TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS rug_vuln_until    TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_buff_until    TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS eat_buff_bonus    DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS chew_at           TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS chew_reward       NUMERIC(36,0) NOT NULL DEFAULT 0;

-- ── Per-eat mode tag on exploit_history (powers the new leaderboards) ──────
-- snipe / target / bite / nibble / feast / rug
ALTER TABLE exploit_history ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'target';

-- ── Veteran backfill ───────────────────────────────────────────────────────
-- Seed eat_xp / eat_level from legacy wins so existing players are not reset
-- to level 1 by the new ladder. XP_PER_VETERAN_WIN = 60, XP_CURVE_BASE = 50.
UPDATE exploit_stats
   SET eat_xp    = heists_won * 60,
       eat_level = LEAST(100, GREATEST(1,
                     FLOOR((1 + sqrt(1 + 8.0 * (heists_won * 60) / 50.0)) / 2)::int))
 WHERE eat_xp = 0 AND heists_won > 0;

-- ── Seed the $EAT oracle price for every existing guild ────────────────────
-- New guilds get this automatically via PgMarketsRepo.seed_prices iterating
-- Config.TOKENS; this covers databases that were seeded before $EAT existed.
INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low, ath)
SELECT DISTINCT 'EAT', guild_id, 0.25, 0.25, 0.25, 0.25, 0.25
  FROM crypto_prices
ON CONFLICT DO NOTHING;
