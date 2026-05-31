-- 0217_disc_fun_proto_tokens.sql
--
-- Disc.Fun proto-token system: a Pump.fun-style bonding curve launchpad
-- on the Discoin Network. Anyone can deploy a "proto token" cheaply with
-- a name, symbol and emoji; everything else (supply, virtual liquidity,
-- graduation threshold, fees) is fixed to Disc.Fun defaults. The token
-- trades against a virtual DFUN reserve (constant-product AMM) until the
-- graduation threshold of real DFUN is collected, at which point it is
-- promoted to a full guild token with its own DFUN + DSC pools and
-- becomes swappable like any other deployed token.
--
-- proto_tokens             -- one row per proto deployment (curve state + config)
-- proto_token_holdings     -- per-user balance on the curve (raw scale, 1e18)
-- proto_token_trades       -- audit trail of buys/sells for charts and history
--
-- All numeric balances follow the project-wide raw-int convention
-- (NUMERIC(36,0) scaled by 10**18). Column names use ``quote`` rather than
-- a hardcoded ``dsd`` so the migration is currency-agnostic; the active
-- quote symbol is read from ``Config.DISCFUN["quote_symbol"]``.

CREATE TABLE IF NOT EXISTS proto_tokens (
    proto_id            SERIAL          PRIMARY KEY,
    guild_id            BIGINT          NOT NULL,
    creator_id          BIGINT          NOT NULL,
    symbol              TEXT            NOT NULL,
    name                TEXT            NOT NULL,
    emoji               TEXT            NOT NULL DEFAULT '🚀',
    quote_symbol        TEXT            NOT NULL DEFAULT 'DFUN',

    -- Live AMM state (raw, scaled by 1e18). virtual_quote / virtual_token
    -- give the current spot price; both update on every buy/sell.
    virtual_quote       NUMERIC(36, 0)  NOT NULL,
    virtual_token       NUMERIC(36, 0)  NOT NULL,

    -- Bonding-curve config (fixed at deploy time from Disc.Fun defaults).
    initial_virtual_quote   NUMERIC(36, 0)  NOT NULL,
    initial_virtual_token   NUMERIC(36, 0)  NOT NULL,
    total_supply            NUMERIC(36, 0)  NOT NULL,
    curve_supply            NUMERIC(36, 0)  NOT NULL,  -- tokens for sale on curve
    graduation_quote        NUMERIC(36, 0)  NOT NULL,  -- real quote threshold

    -- Cumulative trackers used to detect graduation and feed UI.
    real_quote_collected    NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    tokens_in_circulation   NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    volume_quote            NUMERIC(36, 0)  NOT NULL DEFAULT 0,  -- lifetime gross quote volume
    trade_count             INTEGER         NOT NULL DEFAULT 0,
    holder_count            INTEGER         NOT NULL DEFAULT 0,
    trade_fee_bps           INTEGER         NOT NULL DEFAULT 100,

    graduated           BOOLEAN         NOT NULL DEFAULT FALSE,
    graduated_at        TIMESTAMPTZ     DEFAULT NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT proto_tokens_symbol_uniq UNIQUE (guild_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_proto_tokens_active
    ON proto_tokens (guild_id, created_at DESC)
    WHERE graduated = FALSE;

CREATE INDEX IF NOT EXISTS idx_proto_tokens_creator
    ON proto_tokens (guild_id, creator_id);

-- For "near graduation" sort -- want active rows ordered by progress.
CREATE INDEX IF NOT EXISTS idx_proto_tokens_progress
    ON proto_tokens (guild_id, real_quote_collected DESC)
    WHERE graduated = FALSE;

CREATE TABLE IF NOT EXISTS proto_token_holdings (
    proto_id    INTEGER         NOT NULL REFERENCES proto_tokens(proto_id) ON DELETE CASCADE,
    guild_id    BIGINT          NOT NULL,
    user_id     BIGINT          NOT NULL,
    amount      NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    cost_basis  NUMERIC(36, 0)  NOT NULL DEFAULT 0,  -- net quote paid (for PnL display)
    PRIMARY KEY (proto_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_proto_holdings_user
    ON proto_token_holdings (guild_id, user_id)
    WHERE amount > 0;

CREATE INDEX IF NOT EXISTS idx_proto_holdings_top
    ON proto_token_holdings (proto_id, amount DESC)
    WHERE amount > 0;

CREATE TABLE IF NOT EXISTS proto_token_trades (
    trade_id        BIGSERIAL       PRIMARY KEY,
    proto_id        INTEGER         NOT NULL REFERENCES proto_tokens(proto_id) ON DELETE CASCADE,
    guild_id        BIGINT          NOT NULL,
    user_id         BIGINT          NOT NULL,
    side            TEXT            NOT NULL CHECK (side IN ('buy', 'sell')),
    quote_amount    NUMERIC(36, 0)  NOT NULL,
    token_amount    NUMERIC(36, 0)  NOT NULL,
    fee_quote       NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    price_after     NUMERIC(36, 18) NOT NULL,  -- spot price (quote per token) after this trade
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_proto_trades_proto_time
    ON proto_token_trades (proto_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_proto_trades_user_time
    ON proto_token_trades (guild_id, user_id, created_at DESC);

-- Idempotent rename for any environment that ran an older draft of this
-- migration with ``_dsd`` column names. Safe on a fresh DB (no-op).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_tokens' AND column_name='virtual_dsd') THEN
        ALTER TABLE proto_tokens RENAME COLUMN virtual_dsd TO virtual_quote;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_tokens' AND column_name='initial_virtual_dsd') THEN
        ALTER TABLE proto_tokens RENAME COLUMN initial_virtual_dsd TO initial_virtual_quote;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_tokens' AND column_name='real_dsd_collected') THEN
        ALTER TABLE proto_tokens RENAME COLUMN real_dsd_collected TO real_quote_collected;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_tokens' AND column_name='graduation_dsd') THEN
        ALTER TABLE proto_tokens RENAME COLUMN graduation_dsd TO graduation_quote;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_token_trades' AND column_name='dsd_amount') THEN
        ALTER TABLE proto_token_trades RENAME COLUMN dsd_amount TO quote_amount;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proto_token_trades' AND column_name='fee_dsd') THEN
        ALTER TABLE proto_token_trades RENAME COLUMN fee_dsd TO fee_quote;
    END IF;
END $$;
