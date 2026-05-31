-- ============================================================================
-- Discoin v2  -  PostgreSQL Schema
-- Complete, self-contained schema. Run from scratch on a fresh database.
-- ============================================================================

-- Required extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ============================================================================
-- Helper: auto-update updated_at on row modification
-- ============================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 1. CORE
-- ============================================================================

CREATE TABLE users (
    user_id       BIGINT       NOT NULL,
    guild_id      BIGINT       NOT NULL,
    username      TEXT         NOT NULL DEFAULT '',
    wallet        NUMERIC(36,0) NOT NULL DEFAULT 20000000000000000000,
    bank          NUMERIC(36,0) NOT NULL DEFAULT 0,
    daily_streak  INTEGER      NOT NULL DEFAULT 0,
    last_daily    TIMESTAMPTZ,
    last_work     TIMESTAMPTZ,
    last_activity TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT chk_users_wallet CHECK (wallet >= 0),
    CONSTRAINT chk_users_bank   CHECK (bank >= 0)
);
CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE guild_settings (
    guild_id                  BIGINT  NOT NULL PRIMARY KEY,
    trade_channel             BIGINT,
    mine_channel              BIGINT,
    staking_channel           BIGINT,
    validators_channel        BIGINT,
    contracts_channel         BIGINT,
    crypto_channel            BIGINT,
    gambling_channel          BIGINT,
    pools_channel             BIGINT,
    drops_channel             BIGINT,
    job_channel               BIGINT,
    drops_spawn_channel       BIGINT,
    faucet_channel            BIGINT,
    wallet_channel            BIGINT,
    error_channel             BIGINT,
    scam_channel              BIGINT,
    nft_channel               BIGINT,
    predictions_channel       BIGINT,
    events_channel            BIGINT,
    ape_channel               BIGINT,
    vault_feed_channel        BIGINT,
    prefix                    TEXT,
    embed_color               INTEGER,
    server_name               TEXT,
    currency_name             TEXT,
    ai_mm_enabled             BOOLEAN DEFAULT TRUE,
    ai_chat_enabled           BOOLEAN DEFAULT TRUE,
    ai_chat_threaded          BOOLEAN NOT NULL DEFAULT TRUE,
    ai_commentary_enabled     BOOLEAN DEFAULT TRUE,
    ai_flavor_enabled         BOOLEAN DEFAULT FALSE,
    ai_events_enabled         BOOLEAN DEFAULT TRUE,
    heal_ai_backend           TEXT    DEFAULT NULL,
    heal_ai_model             TEXT    DEFAULT NULL,
    heal_ai_base_url          TEXT    DEFAULT NULL,
    module_gambling           BOOLEAN DEFAULT TRUE,
    module_lending            BOOLEAN DEFAULT TRUE,
    module_staking            BOOLEAN DEFAULT TRUE,
    module_mining             BOOLEAN DEFAULT TRUE,
    module_drops              BOOLEAN DEFAULT TRUE,
    module_faucet             BOOLEAN DEFAULT TRUE,
    faucet_multiplier         NUMERIC(28,8) DEFAULT 1.0,
    faucet_tokens             TEXT NOT NULL DEFAULT '',
    module_savings            BOOLEAN DEFAULT TRUE,
    module_validators         BOOLEAN DEFAULT TRUE,
    module_pools              BOOLEAN DEFAULT TRUE,
    module_contracts          BOOLEAN DEFAULT TRUE,
    module_security           BOOLEAN DEFAULT TRUE,
    module_groups             BOOLEAN DEFAULT TRUE,
    module_chart              BOOLEAN DEFAULT TRUE,
    module_crypto             BOOLEAN DEFAULT TRUE,
    module_daily              BOOLEAN DEFAULT TRUE,
    module_work               BOOLEAN DEFAULT TRUE,
    module_economy            BOOLEAN DEFAULT TRUE,
    module_chain              BOOLEAN DEFAULT TRUE,
    module_shop               BOOLEAN DEFAULT TRUE,
    module_games              BOOLEAN DEFAULT TRUE,
    module_gambling_coinflip  BOOLEAN DEFAULT TRUE,
    module_gambling_dice      BOOLEAN DEFAULT TRUE,
    module_gambling_roulette  BOOLEAN DEFAULT TRUE,
    module_gambling_blackjack BOOLEAN DEFAULT TRUE,
    module_gambling_slots     BOOLEAN DEFAULT TRUE,
    module_ape                BOOLEAN DEFAULT TRUE,
    module_nft                BOOLEAN DEFAULT TRUE,
    module_predictions        BOOLEAN DEFAULT TRUE,
    module_events             BOOLEAN DEFAULT TRUE,
    module_rugpull            BOOLEAN DEFAULT TRUE,
    drop_interval             INTEGER,
    drop_min                  NUMERIC(36,0),
    drop_max                  NUMERIC(36,0),
    platform_fee_pct          NUMERIC(28,8),
    platform_fee_min          NUMERIC(36,0),
    platform_fee_max          NUMERIC(36,0),
    treasury_cut_pct          NUMERIC(28,8),
    halted_networks           TEXT    NOT NULL DEFAULT '',
    disabled_tokens           TEXT    NOT NULL DEFAULT '',
    bot_channels              TEXT    NOT NULL DEFAULT '',
    ai_chat_channels          TEXT    NOT NULL DEFAULT '',
    cmd_delete_after          INTEGER NOT NULL DEFAULT 0,
    reply_delete_after        INTEGER NOT NULL DEFAULT 0,
    scam_detection            BOOLEAN NOT NULL DEFAULT FALSE,
    scam_timeout_minutes      INTEGER NOT NULL DEFAULT 10,
    bot_manager_id            BIGINT DEFAULT 801280612111482890,
    bot_manager_auto_exempt   BOOLEAN NOT NULL DEFAULT TRUE,
    bot_manager_all_perms     BOOLEAN NOT NULL DEFAULT TRUE,
    security_audit_roles      TEXT,
    gm_announce_role_id       BIGINT,
    current_event             TEXT,
    event_vol_mult            NUMERIC(5,2) NOT NULL DEFAULT 1.0,
    event_bias                NUMERIC(8,6) NOT NULL DEFAULT 0.0,
    event_expires_at          TIMESTAMPTZ,
    disabled_events           TEXT    NOT NULL DEFAULT '',
    event_frequency           NUMERIC(8,6) NOT NULL DEFAULT 0.0005,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_guild_settings_updated BEFORE UPDATE ON guild_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE guild_tokens (
    guild_id    BIGINT        NOT NULL,
    symbol      TEXT          NOT NULL,
    name        TEXT          NOT NULL,
    emoji       TEXT          NOT NULL DEFAULT '●',
    consensus   TEXT          NOT NULL DEFAULT 'PoS',
    network     TEXT,
    start_price NUMERIC(28,8) NOT NULL DEFAULT 1.0,
    daily_vol   NUMERIC(28,8) NOT NULL DEFAULT 0.05,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, symbol),
    CONSTRAINT fk_guild_tokens_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_guild_tokens_start_price CHECK (start_price >= 0)
);
CREATE TRIGGER trg_guild_tokens_updated BEFORE UPDATE ON guild_tokens
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE guild_networks (
    guild_id     BIGINT NOT NULL,
    network_name TEXT   NOT NULL,
    stake_token  TEXT   NOT NULL,
    emoji        TEXT   NOT NULL DEFAULT '🌐',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, network_name),
    CONSTRAINT fk_guild_networks_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

-- ============================================================================
-- 2. FINANCE
-- ============================================================================

CREATE TABLE crypto_prices (
    symbol              TEXT          NOT NULL,
    guild_id            BIGINT        NOT NULL,
    price               NUMERIC(28,8) NOT NULL,
    open_price          NUMERIC(28,8) NOT NULL,
    day_high            NUMERIC(28,8) NOT NULL,
    day_low             NUMERIC(28,8) NOT NULL,
    ath                 NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    circulating_supply  NUMERIC(36,0) NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, guild_id),
    CONSTRAINT fk_crypto_prices_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_crypto_prices_price CHECK (price >= 0)
);

CREATE TABLE crypto_holdings (
    user_id  BIGINT        NOT NULL,
    guild_id BIGINT        NOT NULL,
    symbol   TEXT          NOT NULL,
    amount   NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, symbol),
    CONSTRAINT fk_crypto_holdings_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_crypto_holdings_amount CHECK (amount >= 0)
);
CREATE INDEX idx_crypto_holdings_user ON crypto_holdings (user_id, guild_id);

CREATE TABLE wallet_holdings (
    user_id  BIGINT        NOT NULL,
    guild_id BIGINT        NOT NULL,
    network  TEXT          NOT NULL,
    symbol   TEXT          NOT NULL,
    amount   NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, network, symbol),
    CONSTRAINT fk_wallet_holdings_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_wallet_holdings_amount CHECK (amount >= 0)
);
CREATE INDEX idx_wallet_holdings_user ON wallet_holdings (user_id, guild_id);

CREATE TABLE price_candles (
    guild_id BIGINT        NOT NULL,
    symbol   TEXT          NOT NULL,
    ts       TIMESTAMPTZ   NOT NULL,
    open     NUMERIC(28,8) NOT NULL,
    high     NUMERIC(28,8) NOT NULL,
    low      NUMERIC(28,8) NOT NULL,
    close    NUMERIC(28,8) NOT NULL,
    volume   NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, symbol, ts),
    CONSTRAINT fk_price_candles_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_candles_ts ON price_candles (guild_id, symbol, ts DESC);

-- ============================================================================
-- 3. TRADING
-- ============================================================================

CREATE TABLE transactions (
    tx_hash    TEXT          NOT NULL PRIMARY KEY,
    guild_id   BIGINT        NOT NULL,
    user_id    BIGINT,
    tx_type    TEXT          NOT NULL,
    symbol_in  TEXT,
    amount_in  NUMERIC(36,0),
    symbol_out TEXT,
    amount_out NUMERIC(36,0),
    price_at   NUMERIC(28,8),
    gas_fee    NUMERIC(36,0) NOT NULL DEFAULT 0,
    gas_coin   TEXT          NOT NULL DEFAULT '',
    block_num  INTEGER,
    ts         TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX idx_tx_guild ON transactions (guild_id, ts DESC);
CREATE INDEX idx_tx_user  ON transactions (user_id, ts DESC);
CREATE INDEX idx_tx_guild_block ON transactions (guild_id, block_num);

CREATE TABLE pools (
    pool_id   TEXT          NOT NULL,
    guild_id  BIGINT        NOT NULL,
    token_a   TEXT          NOT NULL,
    token_b   TEXT          NOT NULL,
    reserve_a NUMERIC(36,0) NOT NULL DEFAULT 0,
    reserve_b NUMERIC(36,0) NOT NULL DEFAULT 0,
    total_lp  NUMERIC(36,0) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (pool_id, guild_id),
    CONSTRAINT fk_pools_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_pools_reserve_a CHECK (reserve_a >= 0),
    CONSTRAINT chk_pools_reserve_b CHECK (reserve_b >= 0),
    CONSTRAINT chk_pools_total_lp  CHECK (total_lp  >= 0)
);
CREATE INDEX idx_pools_guild ON pools (guild_id);
CREATE TRIGGER trg_pools_updated BEFORE UPDATE ON pools
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE lp_positions (
    user_id   BIGINT        NOT NULL,
    guild_id  BIGINT        NOT NULL,
    pool_id   TEXT          NOT NULL,
    lp_shares NUMERIC(36,0) NOT NULL DEFAULT 0,
    added_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, pool_id),
    CONSTRAINT fk_lp_positions_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_lp_positions_shares CHECK (lp_shares >= 0)
);

CREATE TABLE lp_snapshots (
    user_id            BIGINT        NOT NULL,
    guild_id           BIGINT        NOT NULL,
    pool_id            TEXT          NOT NULL,
    entry_res_a_per_lp NUMERIC(36,0) NOT NULL,
    entry_res_b_per_lp NUMERIC(36,0) NOT NULL,
    PRIMARY KEY (user_id, guild_id, pool_id),
    CONSTRAINT fk_lp_snapshots_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

-- LP positions held collectively by mining groups in cross-group token pools.
-- Seeded automatically on pool acceptance from each group's vault_token_bal.
-- Separate from per-user lp_positions; counts toward pool total_lp so the
-- group seed acts as floor liquidity that users cannot dilute past 50%.
CREATE TABLE group_lp_positions (
    group_id        TEXT          NOT NULL,
    guild_id        BIGINT        NOT NULL,
    pool_id         TEXT          NOT NULL,
    lp_shares       NUMERIC(36,0) NOT NULL DEFAULT 0,
    seeded_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_harvest_at TIMESTAMPTZ,
    PRIMARY KEY (group_id, guild_id, pool_id),
    CONSTRAINT chk_group_lp_shares CHECK (lp_shares >= 0)
);
CREATE INDEX idx_group_lp_guild_pool ON group_lp_positions (guild_id, pool_id);

-- ============================================================================
-- 4. STAKING
-- ============================================================================

CREATE TABLE validators (
    validator_id  TEXT          NOT NULL,
    guild_id      BIGINT        NOT NULL,
    name          TEXT          NOT NULL,
    emoji         TEXT          NOT NULL,
    uptime_rate   NUMERIC(28,8) NOT NULL,
    reward_rate   NUMERIC(28,8) NOT NULL,
    slash_rate    NUMERIC(28,8) NOT NULL,
    PRIMARY KEY (validator_id, guild_id),
    CONSTRAINT fk_validators_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE stakes (
    user_id      BIGINT        NOT NULL,
    guild_id     BIGINT        NOT NULL,
    validator_id TEXT          NOT NULL,
    symbol       TEXT          NOT NULL,
    amount       NUMERIC(36,0) NOT NULL DEFAULT 0,
    staked_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, validator_id, symbol),
    CONSTRAINT fk_stakes_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_stakes_amount CHECK (amount >= 0)
);
CREATE INDEX idx_stakes_guild_validator ON stakes (guild_id, validator_id);

CREATE TABLE pos_validators (
    user_id               BIGINT        NOT NULL,
    guild_id              BIGINT        NOT NULL,
    network               TEXT          NOT NULL,
    stake_token           TEXT          NOT NULL,
    stake_amount          NUMERIC(36,0) NOT NULL DEFAULT 0,
    stake_locked_until    TIMESTAMPTZ,
    is_active             BOOLEAN       NOT NULL DEFAULT TRUE,
    total_blocks_validated INTEGER      NOT NULL DEFAULT 0,
    total_rewards_earned  NUMERIC(36,0) NOT NULL DEFAULT 0,
    slash_count           INTEGER       NOT NULL DEFAULT 0,
    registered_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, network),
    CONSTRAINT fk_pos_validators_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_pos_validators_stake CHECK (stake_amount >= 0)
);
CREATE INDEX idx_pos_val_guild ON pos_validators (guild_id, network);
CREATE TRIGGER trg_pos_validators_updated BEFORE UPDATE ON pos_validators
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE pos_delegations (
    id                BIGSERIAL     PRIMARY KEY,
    delegator_id      BIGINT        NOT NULL,
    validator_user_id BIGINT        NOT NULL,
    guild_id          BIGINT        NOT NULL,
    network           TEXT          NOT NULL,
    token             TEXT          NOT NULL,
    amount            NUMERIC(36,0) NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ   NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    total_earned      NUMERIC(36,0) NOT NULL DEFAULT 0,
    delegated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (delegator_id, validator_user_id, guild_id, network),
    CONSTRAINT chk_pos_delegations_amount CHECK (amount >= 0)
);
CREATE INDEX idx_pos_del_validator ON pos_delegations (validator_user_id, guild_id, network);
CREATE INDEX idx_pos_del_delegator ON pos_delegations (delegator_id, guild_id);
CREATE TRIGGER trg_pos_delegations_updated BEFORE UPDATE ON pos_delegations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- NOTE: pos_blocks from spec is implemented as validator_blocks (Section 7).

-- ============================================================================
-- 5. MINING
-- ============================================================================

CREATE TABLE mining_rigs (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    rig_id   TEXT    NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, rig_id),
    CONSTRAINT fk_mining_rigs_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_mining_rigs_quantity CHECK (quantity >= 0)
);

CREATE TABLE rig_chain_assignments (
    user_id      BIGINT  NOT NULL,
    guild_id     BIGINT  NOT NULL,
    rig_id       TEXT    NOT NULL,
    chain_symbol TEXT    NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, rig_id, chain_symbol),
    CONSTRAINT fk_rca_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_rca_quantity CHECK (quantity >= 0)
);
CREATE INDEX idx_rca_guild ON rig_chain_assignments (guild_id, chain_symbol);

CREATE TABLE mining_network (
    guild_id       BIGINT        NOT NULL PRIMARY KEY,
    block_height   INTEGER       NOT NULL DEFAULT 0,
    total_hashrate NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    current_reward NUMERIC(36,0) NOT NULL DEFAULT 50000000000000000000,
    last_block_ts  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT fk_mining_network_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE TRIGGER trg_mining_network_updated BEFORE UPDATE ON mining_network
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE pow_network_state (
    guild_id             BIGINT        NOT NULL,
    chain_symbol         TEXT          NOT NULL,
    block_height         INTEGER       NOT NULL DEFAULT 0,
    total_hashrate       NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    current_reward       NUMERIC(36,0) NOT NULL DEFAULT 0,
    last_block_ts        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    difficulty           NUMERIC(28,8) NOT NULL DEFAULT 1.0,
    last_retarget_ts     TIMESTAMPTZ,
    last_retarget_height INTEGER       NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, chain_symbol),
    CONSTRAINT fk_pow_network_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_pow_net_guild ON pow_network_state (guild_id);
CREATE TRIGGER trg_pow_network_updated BEFORE UPDATE ON pow_network_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE mining_groups (
    group_id   TEXT        NOT NULL,
    guild_id   BIGINT      NOT NULL,
    name       TEXT        NOT NULL,
    founder_id BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, guild_id),
    CONSTRAINT fk_mining_groups_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE mining_group_members (
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    group_id   TEXT        NOT NULL,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_mgm_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_mgm_group ON mining_group_members (guild_id, group_id);

CREATE TABLE mining_group_weights (
    guild_id   BIGINT        NOT NULL,
    group_id   TEXT          NOT NULL,
    user_id    BIGINT        NOT NULL,
    weight     NUMERIC(28,8) NOT NULL DEFAULT 1.0,
    PRIMARY KEY (guild_id, group_id, user_id),
    CONSTRAINT chk_mgw_weight CHECK (weight >= 0)
);

CREATE TABLE mining_blocks (
    id             BIGSERIAL     PRIMARY KEY,
    guild_id       BIGINT        NOT NULL,
    block_height   INTEGER       NOT NULL,
    block_ts       TIMESTAMPTZ   NOT NULL,
    miner_id       BIGINT,
    reward         NUMERIC(36,0) NOT NULL,
    total_hashrate NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    symbol         VARCHAR(16)   NOT NULL DEFAULT 'SUN'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blocks_guild_sym ON mining_blocks (guild_id, symbol, block_height);
-- Migration for existing installs:
ALTER TABLE mining_blocks ADD COLUMN IF NOT EXISTS symbol VARCHAR(16) NOT NULL DEFAULT 'SUN';
CREATE UNIQUE INDEX IF NOT EXISTS idx_blocks_guild_sym ON mining_blocks (guild_id, symbol, block_height);

-- Legacy mining pool members
CREATE TABLE mining_pool_members (
    user_id  BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_mpm_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

-- ============================================================================
-- 6. LENDING
-- ============================================================================

CREATE TABLE loans (
    user_id       BIGINT        NOT NULL,
    guild_id      BIGINT        NOT NULL,
    principal     NUMERIC(36,0) NOT NULL,
    outstanding   NUMERIC(36,0) NOT NULL,
    collateral    NUMERIC(36,0) NOT NULL,
    last_interest TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_loans_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_loans_principal   CHECK (principal   >= 0),
    CONSTRAINT chk_loans_outstanding CHECK (outstanding >= 0),
    CONSTRAINT chk_loans_collateral  CHECK (collateral  >= 0)
);
CREATE TRIGGER trg_loans_updated BEFORE UPDATE ON loans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE sun_loans (
    user_id           BIGINT        NOT NULL,
    guild_id          BIGINT        NOT NULL,
    collateral_sun    NUMERIC(36,0) NOT NULL,
    borrow_symbol     TEXT          NOT NULL,
    borrow_amount     NUMERIC(36,0) NOT NULL,
    outstanding       NUMERIC(36,0) NOT NULL,
    last_interest     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_sun_loans_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_sun_loans_collateral CHECK (collateral_sun >= 0),
    CONSTRAINT chk_sun_loans_outstanding CHECK (outstanding >= 0)
);
CREATE TRIGGER trg_sun_loans_updated BEFORE UPDATE ON sun_loans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE savings_deposits (
    user_id       BIGINT        NOT NULL,
    guild_id      BIGINT        NOT NULL,
    symbol        TEXT          NOT NULL,
    amount        NUMERIC(36,0) NOT NULL,
    last_interest TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, symbol),
    CONSTRAINT fk_savings_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_savings_amount CHECK (amount >= 0)
);
CREATE TRIGGER trg_savings_updated BEFORE UPDATE ON savings_deposits
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 7. BLOCKCHAIN
-- ============================================================================

CREATE TABLE chain_blocks (
    guild_id   BIGINT      NOT NULL,
    network    TEXT        NOT NULL DEFAULT '',
    block_num  INTEGER     NOT NULL,
    block_hash TEXT        NOT NULL,
    tx_count   INTEGER     NOT NULL DEFAULT 0,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status     TEXT        NOT NULL DEFAULT 'pending',
    miner_id   BIGINT,
    mined_at   TIMESTAMPTZ,
    PRIMARY KEY (guild_id, network, block_num)
);
CREATE INDEX idx_chain_blocks_guild ON chain_blocks (guild_id, network, block_num DESC);

CREATE TABLE mempool (
    id           BIGSERIAL     PRIMARY KEY,
    guild_id     BIGINT        NOT NULL,
    network      TEXT          NOT NULL,
    user_id      BIGINT        NOT NULL,
    action_type  TEXT          NOT NULL,
    payload      JSONB         NOT NULL DEFAULT '{}',
    gas_price    TEXT          NOT NULL DEFAULT 'medium',
    gas_fee      NUMERIC(36,0) NOT NULL DEFAULT 0,
    status       TEXT          NOT NULL DEFAULT 'pending',
    submitted_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    block_id     BIGINT
);
CREATE INDEX idx_mempool_pending ON mempool (guild_id, network, status, gas_fee DESC);

CREATE TABLE validator_blocks (
    id                  BIGSERIAL     PRIMARY KEY,
    guild_id            BIGINT        NOT NULL,
    network             TEXT          NOT NULL,
    validator_id        BIGINT        NOT NULL,
    status              TEXT          NOT NULL DEFAULT 'pending',
    total_gas_collected NUMERIC(36,0) NOT NULL DEFAULT 0,
    validator_reward    NUMERIC(36,0) NOT NULL DEFAULT 0,
    treasury_cut        NUMERIC(36,0) NOT NULL DEFAULT 0,
    action_count        INTEGER       NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    confirmed_at        TIMESTAMPTZ
);
CREATE INDEX idx_vblocks_guild ON validator_blocks (guild_id, network, created_at DESC);

CREATE TABLE network_base_fees (
    guild_id    BIGINT        NOT NULL,
    network     TEXT          NOT NULL,
    base_fee    NUMERIC(36,0) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, network),
    CONSTRAINT fk_nbf_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE guild_treasury (
    guild_id   BIGINT        NOT NULL PRIMARY KEY,
    balance    NUMERIC(36,0) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT fk_treasury_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_treasury_balance CHECK (balance >= 0)
);

-- ============================================================================
-- 8. CONTRACTS
-- ============================================================================

CREATE TABLE smart_contracts (
    address      TEXT          NOT NULL PRIMARY KEY,
    guild_id     BIGINT        NOT NULL,
    owner_id     BIGINT        NOT NULL,
    name         TEXT          NOT NULL,
    network      TEXT          NOT NULL,
    type         TEXT          NOT NULL DEFAULT 'custom',
    definition   JSONB         NOT NULL DEFAULT '{}',
    state        JSONB         NOT NULL DEFAULT '{}',
    is_paused    BOOLEAN       NOT NULL DEFAULT FALSE,
    call_count   INTEGER       NOT NULL DEFAULT 0,
    virtual_uid  BIGINT        NOT NULL UNIQUE,
    deployed_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    description  TEXT          NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX idx_contracts_guild ON smart_contracts (guild_id, network);
CREATE TRIGGER trg_smart_contracts_updated BEFORE UPDATE ON smart_contracts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE contract_events (
    id       BIGSERIAL   PRIMARY KEY,
    guild_id BIGINT      NOT NULL,
    address  TEXT        NOT NULL,
    event    TEXT        NOT NULL,
    data     JSONB       NOT NULL DEFAULT '{}',
    block_id BIGINT,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cevents ON contract_events (guild_id, address, ts DESC);

CREATE TABLE token_contracts (
    guild_id   BIGINT NOT NULL,
    symbol     TEXT   NOT NULL,
    params     JSONB  NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, symbol),
    CONSTRAINT fk_token_contracts_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE TRIGGER trg_token_contracts_updated BEFORE UPDATE ON token_contracts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 9. ITEMS
-- ============================================================================

CREATE TABLE hashstones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_hashstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_hashstones_level CHECK (level >= 1),
    CONSTRAINT chk_hashstones_xp    CHECK (xp >= 0)
);

CREATE TABLE lockstones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_lockstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_lockstones_level CHECK (level >= 1),
    CONSTRAINT chk_lockstones_xp    CHECK (xp >= 0)
);

CREATE TABLE vaultstones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_vaultstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_vaultstones_level CHECK (level >= 1),
    CONSTRAINT chk_vaultstones_xp    CHECK (xp >= 0)
);

CREATE TABLE charm_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_charm_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_charm_count CHECK (count >= 0)
);

CREATE TABLE gambastones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_gambastones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_gambastones_level CHECK (level >= 1),
    CONSTRAINT chk_gambastones_xp    CHECK (xp >= 0)
);

CREATE TABLE active_charms (
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_active_charms_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

CREATE TABLE gambling_save_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_gambling_save_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_gambling_save_count CHECK (count >= 0)
);

CREATE TABLE validator_guard_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_validator_guard_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_validator_guard_count CHECK (count >= 0)
);

CREATE TABLE yield_guard_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_yield_guard_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_yield_guard_count CHECK (count >= 0)
);

-- ============================================================================
-- 10. USER META
-- ============================================================================

CREATE TABLE user_jobs (
    user_id      BIGINT        NOT NULL,
    guild_id     BIGINT        NOT NULL,
    job_id       TEXT          NOT NULL DEFAULT 'HOMELESS',
    work_count   INTEGER       NOT NULL DEFAULT 0,
    total_earned NUMERIC(36,0) NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_user_jobs_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);
CREATE TRIGGER trg_user_jobs_updated BEFORE UPDATE ON user_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE user_prefs (
    user_id       BIGINT  NOT NULL,
    guild_id      BIGINT  NOT NULL,
    -- Every DM toggle is OPT-IN. Players have to ,notify <kind> on
    -- to receive the corresponding DMs. Migration 0208 also flipped
    -- every existing row to FALSE so already-registered players stop
    -- receiving DMs without re-opting-in.
    dm_mining     BOOLEAN DEFAULT FALSE,
    dm_transfer   BOOLEAN DEFAULT FALSE,
    dm_validator  BOOLEAN DEFAULT FALSE,
    dm_staking    BOOLEAN DEFAULT FALSE,
    dm_2fa        BOOLEAN DEFAULT FALSE,
    dm_events     BOOLEAN DEFAULT FALSE,
    dm_nft        BOOLEAN DEFAULT FALSE,
    dm_predictions BOOLEAN DEFAULT FALSE,
    dm_ape        BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_user_prefs_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

CREATE TABLE user_settings (
    user_id            BIGINT  NOT NULL,
    guild_id           BIGINT  NOT NULL,
    theme              TEXT    NOT NULL DEFAULT 'dark',
    currency_format    TEXT    NOT NULL DEFAULT 'usd',
    price_precision    INTEGER NOT NULL DEFAULT 2,
    default_chart_tf   TEXT    NOT NULL DEFAULT '1h',
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_user_settings_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

CREATE TABLE wallet_addresses (
    address    TEXT        NOT NULL PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    label      TEXT,
    is_temp    BOOLEAN     NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    network    TEXT        NOT NULL DEFAULT '',
    CONSTRAINT fk_wallet_addr_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_wallet_user ON wallet_addresses (user_id, guild_id);

-- Short links / vanity URLs
CREATE TABLE short_links (
    code       TEXT        NOT NULL PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    target_url TEXT        NOT NULL,
    clicks     INTEGER     NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_short_links_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

-- (user_notifications removed  -  use the v2 "notifications" table in section 14 instead)

-- ============================================================================
-- 11. GAMES (NEW v2)
-- ============================================================================

CREATE TABLE game_results (
    id          BIGSERIAL     PRIMARY KEY,
    guild_id    BIGINT        NOT NULL,
    user_id     BIGINT        NOT NULL,
    game_type   TEXT          NOT NULL,
    bet_amount  NUMERIC(36,0) NOT NULL,
    payout      NUMERIC(36,0) NOT NULL DEFAULT 0,
    profit      NUMERIC(36,0) NOT NULL DEFAULT 0,
    multiplier  NUMERIC(10,4),
    result_data JSONB,
    server_seed TEXT,
    client_seed TEXT,
    nonce       BIGINT,
    played_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_game_bet CHECK (bet_amount >= 0),
    CONSTRAINT chk_game_payout CHECK (payout >= 0)
);
CREATE INDEX idx_game_results_user ON game_results (user_id, guild_id, played_at DESC);
CREATE INDEX idx_game_results_type ON game_results (guild_id, game_type, played_at DESC);

CREATE TABLE game_sessions (
    id          UUID          DEFAULT gen_random_uuid() PRIMARY KEY,
    guild_id    BIGINT        NOT NULL,
    user_id     BIGINT        NOT NULL,
    game_type   TEXT          NOT NULL,
    bet_amount  NUMERIC(36,0) NOT NULL,
    state       JSONB         NOT NULL DEFAULT '{}',
    status      TEXT          NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    CONSTRAINT chk_session_status CHECK (status IN ('active', 'completed', 'expired', 'cancelled')),
    CONSTRAINT chk_session_bet CHECK (bet_amount >= 0)
);
CREATE INDEX idx_game_sessions_user ON game_sessions (user_id, guild_id, status);

-- ============================================================================
-- 12. PROFILES (NEW v2)
-- ============================================================================

CREATE TABLE user_profiles (
    user_id            BIGINT        NOT NULL,
    guild_id           BIGINT        NOT NULL,
    total_trades       INTEGER       NOT NULL DEFAULT 0,
    total_trade_volume NUMERIC(36,0) NOT NULL DEFAULT 0,
    realized_pnl       NUMERIC(36,0) NOT NULL DEFAULT 0,
    best_trade_pnl     NUMERIC(36,0) NOT NULL DEFAULT 0,
    worst_trade_pnl    NUMERIC(36,0) NOT NULL DEFAULT 0,
    win_count          INTEGER       NOT NULL DEFAULT 0,
    loss_count         INTEGER       NOT NULL DEFAULT 0,
    total_games        INTEGER       NOT NULL DEFAULT 0,
    total_wagered      NUMERIC(36,0) NOT NULL DEFAULT 0,
    total_game_profit  NUMERIC(36,0) NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_user_profiles_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);
CREATE TRIGGER trg_user_profiles_updated BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE badges (
    badge_id    TEXT  NOT NULL PRIMARY KEY,
    name        TEXT  NOT NULL,
    description TEXT,
    icon        TEXT,
    category    TEXT,
    requirement JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE user_badges (
    user_id   BIGINT      NOT NULL,
    guild_id  BIGINT      NOT NULL,
    badge_id  TEXT        NOT NULL,
    earned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, badge_id),
    CONSTRAINT fk_user_badges_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT fk_user_badges_badge FOREIGN KEY (badge_id)
        REFERENCES badges(badge_id) ON DELETE CASCADE
);

CREATE TABLE pnl_snapshots (
    user_id   BIGINT        NOT NULL,
    guild_id  BIGINT        NOT NULL,
    net_worth NUMERIC(36,0) NOT NULL DEFAULT 0,
    ts        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, ts)
);
CREATE INDEX idx_pnl_snapshots ON pnl_snapshots (user_id, guild_id, ts);

-- ============================================================================
-- 13. NOTIFICATIONS (NEW v2)
-- ============================================================================

CREATE TABLE notifications (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    type       TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    body       TEXT,
    data       JSONB,
    is_read    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications ON notifications (user_id, guild_id, is_read, created_at DESC);

-- ============================================================================
-- 14. AUTH (NEW v2)
-- ============================================================================

CREATE TABLE user_2fa (
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL DEFAULT 0,
    totp_secret  TEXT        NOT NULL,
    backup_codes JSONB,
    enabled      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE TRIGGER trg_user_2fa_updated BEFORE UPDATE ON user_2fa
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE refresh_tokens (
    token_hash TEXT        NOT NULL PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked    BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens (user_id);

-- ============================================================================
-- 15. ADMIN
-- ============================================================================

CREATE TABLE audit_log (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    admin_user_id BIGINT      NOT NULL,
    action        TEXT        NOT NULL,
    details       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_log_guild ON audit_log (guild_id, created_at DESC);

-- ============================================================================
-- 16. MISC / SUPPORTING TABLES
-- ============================================================================

-- One-time migration / metadata tracking
CREATE TABLE db_meta (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Discord webhook credentials for MM bot personas
CREATE TABLE mm_webhooks (
    guild_id      BIGINT NOT NULL PRIMARY KEY,
    webhook_id    TEXT   NOT NULL,
    webhook_token TEXT   NOT NULL,
    channel_id    BIGINT NOT NULL,
    CONSTRAINT fk_mm_webhooks_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

-- Per-guild MM personas
CREATE TABLE mm_personas (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT    NOT NULL,
    name          TEXT      NOT NULL,
    system_prompt TEXT      NOT NULL DEFAULT '',
    avatar_url    TEXT      NOT NULL DEFAULT '',
    trade_bias    TEXT      NOT NULL DEFAULT 'neutral',
    emoji         TEXT      NOT NULL DEFAULT '🤖',
    active        BOOLEAN   NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(guild_id, name),
    CONSTRAINT fk_mm_personas_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

-- Role-based command permissions per guild
CREATE TABLE guild_command_roles (
    guild_id     BIGINT NOT NULL,
    command_name TEXT   NOT NULL,
    role_id      BIGINT NOT NULL,
    PRIMARY KEY (guild_id, command_name, role_id),
    CONSTRAINT fk_gcr_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_gcr_guild ON guild_command_roles (guild_id, command_name);

-- Beta feature access per guild (per user or per role)
-- feature_name: 'command_chains', 'internal_commands', etc.
-- grant_type: 'user' or 'role'
-- grant_id: user_id or role_id
CREATE TABLE IF NOT EXISTS beta_features (
    guild_id     BIGINT NOT NULL,
    feature_name TEXT   NOT NULL,
    grant_type   TEXT   NOT NULL CHECK (grant_type IN ('user', 'role')),
    grant_id     BIGINT NOT NULL,
    granted_by   BIGINT NOT NULL,
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, feature_name, grant_type, grant_id)
);
CREATE INDEX IF NOT EXISTS idx_beta_guild ON beta_features (guild_id, feature_name);

-- ============================================================================
-- 17. MISSING COLUMNS (from SQLite migrations)
-- ============================================================================

-- validators.network (used by staking system)
ALTER TABLE validators ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT '';

-- pos_validators.commission_rate
ALTER TABLE pos_validators ADD COLUMN IF NOT EXISTS commission_rate NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- guild_settings: AI prompts, whale alerts, reports feed, AI delete timers
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_prompt_chat           TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_prompt_commentary     TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_prompt_events         TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_prompt_flavor         TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_persona_name          TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS whale_alerts_channel     BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS whale_alert_threshold    NUMERIC(36,0);
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reports_feed_channel     BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS income_channel           BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reports_feed_categories  TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_cmd_delete_after      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ai_reply_delete_after    INTEGER NOT NULL DEFAULT 0;

-- users: profile customization + badges + reputation
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_bio        TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_title      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_color      INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_banner_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS badges_earned      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS reputation         INTEGER NOT NULL DEFAULT 0;

-- user_prefs: extra DM toggles + per-network mutes. All DM toggles
-- default FALSE (opt-in), see migration 0208_notify_default_off.sql.
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_itemlevelup           BOOLEAN DEFAULT FALSE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_whale_alerts          BOOLEAN DEFAULT FALSE;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_mining    TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_staking   TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_validator TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_whale     TEXT NOT NULL DEFAULT '';

-- mining_groups: enhanced group features
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS tag         TEXT NOT NULL DEFAULT '';
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS image_url   TEXT NOT NULL DEFAULT '';
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS weight_mode TEXT NOT NULL DEFAULT 'hashrate';
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS is_public   BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS reserve_pct NUMERIC(28,8) NOT NULL DEFAULT 5.0;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS reserve_sun NUMERIC(36,0) NOT NULL DEFAULT 0;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS reserve_usd NUMERIC(36,0) NOT NULL DEFAULT 0;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS reserve_btc NUMERIC(36,0) NOT NULL DEFAULT 0;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS renamed_at  TIMESTAMPTZ;
-- Group token vault binding (migrations 0047, 0077, 0073)
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS token_network    TEXT;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS token_symbol     TEXT;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS vault_token_bal  NUMERIC(28,8) NOT NULL DEFAULT 0.0;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS mine_switched_at TIMESTAMPTZ;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS hall_thread_id   BIGINT;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS hall_channel_id  BIGINT;
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS hall_opened_at   TIMESTAMPTZ;
-- guild_tokens / pools vault locking (migration 0047)
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS vault_locked  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pools        ADD COLUMN IF NOT EXISTS vault_locked  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pools        ADD COLUMN IF NOT EXISTS is_group_pool BOOLEAN NOT NULL DEFAULT FALSE;
-- Migrate existing SUN reserves to USD-equivalent (SUN genesis price = $0.01)
UPDATE mining_groups SET reserve_usd = reserve_sun * 0.01 WHERE reserve_sun > 0 AND reserve_usd = 0;

-- guild_tokens: token_type, supply tracking
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS token_type          TEXT NOT NULL DEFAULT 'utility';
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS max_supply          NUMERIC(36,0);
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS circulating_supply  NUMERIC(36,0) NOT NULL DEFAULT 0;

-- ============================================================================
-- 18. MISSING TABLES (from SQLite migrations)
-- ============================================================================

-- Reports / ticket system
CREATE TABLE IF NOT EXISTS reports (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    category      TEXT        NOT NULL,
    message       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'open',
    admin_note    TEXT,
    tags          TEXT,
    dm_message_id BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_guild ON reports (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports (user_id);

-- Scam detection log
CREATE TABLE IF NOT EXISTS scam_log (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    username   TEXT        NOT NULL DEFAULT '',
    channel_id BIGINT      NOT NULL,
    content    TEXT        NOT NULL DEFAULT '',
    actions    TEXT        NOT NULL DEFAULT '',
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scam_log_guild ON scam_log (guild_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_scam_log_user ON scam_log (guild_id, user_id, ts DESC);

-- Scam notification subscribers
CREATE TABLE IF NOT EXISTS scam_notify_users (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Network-accepted tokens registry
CREATE TABLE IF NOT EXISTS network_accepted_tokens (
    guild_id BIGINT NOT NULL,
    network  TEXT   NOT NULL,
    symbol   TEXT   NOT NULL,
    PRIMARY KEY (guild_id, network, symbol)
);

-- Per-user mining mode (solo/pool/group)
CREATE TABLE IF NOT EXISTS user_mining_config (
    user_id           BIGINT      NOT NULL,
    guild_id          BIGINT      NOT NULL,
    mode              TEXT        NOT NULL DEFAULT 'pool',
    last_chain_switch TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);

-- Mining group invites
CREATE TABLE IF NOT EXISTS group_invites (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    group_id   TEXT        NOT NULL,
    invitee_id BIGINT      NOT NULL,
    invited_by BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, group_id, invitee_id)
);
CREATE INDEX IF NOT EXISTS idx_gi_invitee ON group_invites (invitee_id, guild_id);

-- Mining group upgrades
CREATE TABLE IF NOT EXISTS group_upgrades (
    guild_id     BIGINT      NOT NULL,
    group_id     TEXT        NOT NULL,
    upgrade_id   TEXT        NOT NULL,
    purchased_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, group_id, upgrade_id)
);

-- AI conversation history
CREATE TABLE IF NOT EXISTS ai_conversations (
    id           BIGSERIAL   PRIMARY KEY,
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL,
    role         TEXT        NOT NULL,
    content      TEXT        NOT NULL,
    history_key  TEXT        NOT NULL DEFAULT 'default',
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_conv ON ai_conversations (user_id, guild_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ai_conv_key ON ai_conversations (user_id, guild_id, history_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ai_conv_thread ON ai_conversations (guild_id, history_key, ts DESC);

-- Thread-based AI chat: one row per spawned Discord thread (see migration 0277)
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id         BIGINT       PRIMARY KEY,
    guild_id          BIGINT       NOT NULL,
    owner_id          BIGINT       NOT NULL,
    parent_channel_id BIGINT       NOT NULL,
    history_key       TEXT         NOT NULL,
    token             TEXT,
    title             TEXT         NOT NULL DEFAULT 'AI chat',
    saved             BOOLEAN      NOT NULL DEFAULT FALSE,
    summary           TEXT,
    status            TEXT         NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_activity     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    closed_at         TIMESTAMPTZ,
    CONSTRAINT chk_chat_thread_status CHECK (status IN ('active', 'deleted'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_threads_token ON chat_threads (token) WHERE token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chat_threads_idle ON chat_threads (last_activity) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_chat_threads_guild ON chat_threads (guild_id, status);

-- Persistent per-user AI memory
CREATE TABLE IF NOT EXISTS ai_user_memory (
    user_id           BIGINT      NOT NULL,
    guild_id          BIGINT      NOT NULL,
    memory            TEXT        NOT NULL DEFAULT '',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refreshed_at TIMESTAMPTZ,
    refresh_count     INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

-- Per-user tool activation frequency (AI context)
CREATE TABLE IF NOT EXISTS ai_tool_memory (
    user_id   BIGINT      NOT NULL,
    guild_id  BIGINT      NOT NULL,
    tool_key  TEXT        NOT NULL,
    use_count INTEGER     NOT NULL DEFAULT 1,
    last_used TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, tool_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_tool_mem_user ON ai_tool_memory (user_id, guild_id);

-- Per-user emoji reaction category patterns (AI context)
CREATE TABLE IF NOT EXISTS ai_reaction_memory (
    user_id   BIGINT      NOT NULL,
    guild_id  BIGINT      NOT NULL,
    category  TEXT        NOT NULL,
    use_count INTEGER     NOT NULL DEFAULT 1,
    last_used TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, category)
);
CREATE INDEX IF NOT EXISTS idx_ai_react_mem_user ON ai_reaction_memory (user_id, guild_id);

-- Layered personality traits with time-decay weights and confidence scoring
CREATE TABLE IF NOT EXISTS ai_user_traits (
    user_id          BIGINT              NOT NULL,
    guild_id         BIGINT              NOT NULL,
    trait_key        TEXT                NOT NULL,
    trait_value      TEXT                NOT NULL DEFAULT '',
    layer            TEXT                NOT NULL DEFAULT 'volatile',
    confidence       DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    weight           DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    sample_size      INTEGER             NOT NULL DEFAULT 1,
    source           TEXT                NOT NULL DEFAULT 'event',
    last_observed_at TIMESTAMPTZ         NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ         NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, trait_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_traits_user  ON ai_user_traits (user_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ai_traits_layer ON ai_user_traits (user_id, guild_id, layer);
CREATE INDEX IF NOT EXISTS ai_user_traits_source_idx
    ON ai_user_traits (source) WHERE source <> 'event';

-- Raw signal log for behavior shift detection (application-pruned to ~200 rows/user)
CREATE TABLE IF NOT EXISTS ai_user_events (
    id            BIGSERIAL   PRIMARY KEY,
    user_id       BIGINT      NOT NULL,
    guild_id      BIGINT      NOT NULL,
    event_type    TEXT        NOT NULL,
    event_subtype TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_events_user ON ai_user_events (user_id, guild_id, created_at DESC);

-- Per-guild AI context opt-out list. Users on this table are excluded
-- from ai_conversations, ai_user_memory, traits/tone ingest, channel_context,
-- and ambient chatter. They can still chat; Disco just refuses to learn.
CREATE TABLE IF NOT EXISTS ai_opt_outs (
    user_id   BIGINT      NOT NULL,
    guild_id  BIGINT      NOT NULL,
    opted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_opt_outs_guild ON ai_opt_outs (guild_id);

-- Reputation given tracking
CREATE TABLE IF NOT EXISTS rep_given (
    giver_id   BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    receiver_id BIGINT     NOT NULL,
    given_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (giver_id, guild_id, receiver_id)
);

-- ============================================================================
-- Widen NUMERIC(20,8) → NUMERIC(28,8) for meme-coin supply overflow
-- ============================================================================
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'numeric'
          AND numeric_precision = 20
          AND numeric_scale = 8
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN %I TYPE NUMERIC(28,8)',
            r.table_name, r.column_name
        );
    END LOOP;
END
$$;

-- ============================================================================
-- 19. TOKEN CONFIGURATION FIELDS
-- Adds per-token configurable parameters for decimals, transfer fees, and gas.
-- ============================================================================
-- decimals:     display precision (8 = MTA-style, 18 = EVM-style, 6 = USDC)
-- tx_fee_rate:  fraction of transfer amount charged as fee (e.g. 0.001 = 0.1%)
-- gas_fee:      flat base fee per transaction in USD
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS decimals     INTEGER       NOT NULL DEFAULT 18;
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS tx_fee_rate  NUMERIC(10,6) NOT NULL DEFAULT 0.001;
ALTER TABLE guild_tokens ADD COLUMN IF NOT EXISTS gas_fee      NUMERIC(36,0) NOT NULL DEFAULT 50000000000000000;

-- ============================================================================
-- 20. CHAIN-SWITCH COOLDOWN
-- Tracks when a user last reassigned rigs between chains to enforce a cooldown
-- preventing chain-hopping exploits (applies to solo, pool, and group miners).
-- ============================================================================
ALTER TABLE user_mining_config ADD COLUMN IF NOT EXISTS last_chain_switch TIMESTAMPTZ;

-- ============================================================================
-- 21. VALIDATOR COMMISSION COOLDOWN
-- Tracks when a validator last changed their commission rate to enforce a 24h
-- cooldown, preventing bait-and-switch attacks on delegators.
-- ============================================================================
ALTER TABLE pos_validators ADD COLUMN IF NOT EXISTS last_commission_change TIMESTAMPTZ;

-- ============================================================================
-- Report reward: admin can award coins when closing/resolving a report.
-- ============================================================================
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reward_amount NUMERIC(36,0) DEFAULT 0;

-- ============================================================================
-- Bounties: admin-created bounties that reward players for good bug reports.
-- ============================================================================
CREATE TABLE IF NOT EXISTS bounties (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    title         TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    category      TEXT        NOT NULL DEFAULT 'bugs',
    reward_amount NUMERIC(36,0) NOT NULL DEFAULT 0,
    max_claims    INTEGER     NOT NULL DEFAULT 0,  -- 0 = unlimited
    claims        INTEGER     NOT NULL DEFAULT 0,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by    BIGINT      NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bounties_guild ON bounties (guild_id, is_active);

-- ============================================================================
-- Anti-bot game lockout (persistent across restarts)
-- Stores the UTC timestamp until which a user is locked out from gambling.
-- NULL or past timestamps mean no active lockout.
-- ============================================================================
ALTER TABLE users ADD COLUMN IF NOT EXISTS game_lockout_until TIMESTAMPTZ;

-- guild_settings: security log channel + roles permitted to view audit logs
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS security_log_channel BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS security_audit_roles TEXT NOT NULL DEFAULT '';

-- ============================================================================
-- 12. SECURITY SYSTEM
-- ============================================================================

-- Security events  -  all detections from the security engine
CREATE TABLE IF NOT EXISTS security_events (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT       NOT NULL,
    event_type  TEXT         NOT NULL,
    severity    TEXT         NOT NULL,
    score_delta NUMERIC(5,2) NOT NULL DEFAULT 0,
    details     JSONB        NOT NULL DEFAULT '{}',
    source      TEXT         NOT NULL DEFAULT 'system',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_events_guild
    ON security_events (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_user
    ON security_events (guild_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_type
    ON security_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_events_severity
    ON security_events (severity, created_at DESC);

-- Active and historical enforcements
CREATE TABLE IF NOT EXISTS security_enforcements (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT,
    action_type TEXT         NOT NULL,
    scope       TEXT         NOT NULL,
    reason      TEXT         NOT NULL,
    enacted_by  TEXT         NOT NULL DEFAULT 'auto',
    expires_at  TIMESTAMPTZ,
    lifted_at   TIMESTAMPTZ,
    lifted_by   TEXT,
    details     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_enforce_active
    ON security_enforcements (guild_id, user_id)
    WHERE lifted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sec_enforce_guild
    ON security_enforcements (guild_id, created_at DESC);

-- Persistent user security profiles
CREATE TABLE IF NOT EXISTS security_profiles (
    user_id      BIGINT       NOT NULL,
    guild_id     BIGINT       NOT NULL,
    threat_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    total_flags  INTEGER      NOT NULL DEFAULT 0,
    last_flagged TIMESTAMPTZ,
    baseline     JSONB        NOT NULL DEFAULT '{}',
    known_ips    JSONB        NOT NULL DEFAULT '[]',
    risk_level   TEXT         NOT NULL DEFAULT 'normal',
    notes        TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE TRIGGER trg_sec_profiles_updated
    BEFORE UPDATE ON security_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Security audit log  -  admin actions on the security system
CREATE TABLE IF NOT EXISTS security_audit (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    admin_id    BIGINT       NOT NULL,
    action      TEXT         NOT NULL,
    target_user BIGINT,
    details     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_audit_guild
    ON security_audit (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_audit_admin
    ON security_audit (admin_id, created_at DESC);

-- Security exemptions  -  users/roles designated by server owner to bypass enforcement
CREATE TABLE IF NOT EXISTS security_exempt_users (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    target_type TEXT         NOT NULL CHECK (target_type IN ('user', 'role')),
    target_id   BIGINT       NOT NULL,
    granted_by  BIGINT       NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_sec_exempt_guild
    ON security_exempt_users (guild_id);

-- ============================================================================
-- Permission overrides  -  per-user/per-role permission grants
-- ============================================================================
CREATE TABLE IF NOT EXISTS permission_overrides (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    target_type TEXT         NOT NULL CHECK (target_type IN ('user', 'role')),
    target_id   BIGINT       NOT NULL,
    permission  TEXT         NOT NULL,
    granted_by  BIGINT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, target_type, target_id, permission)
);
CREATE INDEX IF NOT EXISTS idx_perm_overrides_guild
    ON permission_overrides (guild_id);

-- Admin users  -  users explicitly granted admin via the dashboard
CREATE TABLE IF NOT EXISTS admin_users (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT       NOT NULL,
    granted_by  BIGINT,
    notes       TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_admin_users_guild
    ON admin_users (guild_id);

-- ============================================================================
-- Migrations for existing installs
-- ============================================================================
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bot_manager_id BIGINT DEFAULT 801280612111482890;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bot_manager_auto_exempt BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bot_manager_all_perms BOOLEAN NOT NULL DEFAULT TRUE;

-- ============================================================================
-- NFT Collections & Marketplace
-- ============================================================================

CREATE TABLE IF NOT EXISTS nft_collections (
    id               SERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    name             TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    network          TEXT NOT NULL DEFAULT 'ARC',
    description      TEXT NOT NULL DEFAULT '',
    image_url        TEXT NOT NULL DEFAULT '',
    max_supply       INT,
    mint_price       NUMERIC(36,0) NOT NULL DEFAULT 0,
    mint_token       TEXT NOT NULL DEFAULT 'ARC',
    minted_count     INT NOT NULL DEFAULT 0,
    creator_id       BIGINT NOT NULL DEFAULT 0,
    contract_address TEXT NOT NULL DEFAULT '',
    slot_metadata    JSONB NOT NULL DEFAULT '[]',
    is_locked        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(guild_id, symbol)
);

CREATE TABLE IF NOT EXISTS nfts (
    id             SERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL,
    collection_id  INT NOT NULL REFERENCES nft_collections(id),
    token_id       INT NOT NULL,
    owner_id       BIGINT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    image_url      TEXT NOT NULL DEFAULT '',
    rarity         TEXT NOT NULL DEFAULT 'common',
    metadata       JSONB NOT NULL DEFAULT '{}',
    token_hash     TEXT NOT NULL DEFAULT '',
    minted_by      BIGINT NOT NULL,
    minted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(collection_id, token_id)
);
CREATE INDEX IF NOT EXISTS idx_nfts_owner ON nfts(owner_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_nfts_collection ON nfts(collection_id);
CREATE INDEX IF NOT EXISTS idx_nfts_token_hash ON nfts(token_hash) WHERE token_hash != '';

CREATE TABLE IF NOT EXISTS nft_listings (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    nft_id      INT NOT NULL REFERENCES nfts(id) ON DELETE CASCADE,
    seller_id   BIGINT NOT NULL,
    price       NUMERIC(36,0) NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    listed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(nft_id)
);

CREATE TABLE IF NOT EXISTS nft_sales (
    id            SERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    nft_id        INT NOT NULL REFERENCES nfts(id),
    collection_id INT NOT NULL REFERENCES nft_collections(id),
    seller_id     BIGINT NOT NULL,
    buyer_id      BIGINT NOT NULL,
    price         NUMERIC(36,0) NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    sold_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nft_sales_nft ON nft_sales(nft_id);
CREATE INDEX IF NOT EXISTS idx_nft_sales_collection ON nft_sales(collection_id);
CREATE INDEX IF NOT EXISTS idx_nft_sales_guild ON nft_sales(guild_id);

-- ============================================================================
-- Prediction Markets
-- ============================================================================

CREATE TABLE IF NOT EXISTS prediction_markets (
    id              SERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    question        TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    options         JSONB NOT NULL DEFAULT '["YES","NO"]',
    end_time        TIMESTAMPTZ NOT NULL,
    resolved_option TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    prize_pool      NUMERIC(36,0) NOT NULL DEFAULT 0,
    total_pool      NUMERIC(36,0) NOT NULL DEFAULT 0,
    created_by      BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pred_markets_guild ON prediction_markets(guild_id, status);

CREATE TABLE IF NOT EXISTS prediction_bets (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    market_id   INT NOT NULL REFERENCES prediction_markets(id),
    user_id     BIGINT NOT NULL,
    option      TEXT NOT NULL,
    amount      NUMERIC(36,0) NOT NULL,
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pred_bets_market ON prediction_bets(market_id);
CREATE INDEX IF NOT EXISTS idx_pred_bets_user ON prediction_bets(user_id, guild_id);

-- ============================================================================
-- 12. RUGPULL MINIGAME
-- ============================================================================

CREATE TABLE IF NOT EXISTS rugpull_king (
    guild_id              BIGINT  NOT NULL PRIMARY KEY,
    user_id               BIGINT  NOT NULL,
    vault_amount          NUMERIC(36,0) NOT NULL DEFAULT 0,
    crowned_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    defense_streak        INTEGER NOT NULL DEFAULT 0,
    tax_rate              NUMERIC(5,2) NOT NULL DEFAULT 1.00,
    sabotage_pool         NUMERIC(36,0) NOT NULL DEFAULT 0,
    bounty_pool           NUMERIC(36,0) NOT NULL DEFAULT 0,
    active_defense_until  TIMESTAMPTZ,
    active_defense_bonus  NUMERIC(6,4) NOT NULL DEFAULT 0,
    defense_last_used_at  TIMESTAMPTZ,
    CONSTRAINT fk_rugpull_king_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rugpull_gender (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    gender       TEXT   NOT NULL CHECK (gender IN ('male', 'female')),
    source       TEXT   NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_rugpull_gender_user ON rugpull_gender (user_id);

CREATE TABLE IF NOT EXISTS rugpull_stats (
    user_id            BIGINT NOT NULL,
    guild_id           BIGINT NOT NULL,
    wins               INTEGER NOT NULL DEFAULT 0,
    losses             INTEGER NOT NULL DEFAULT 0,
    total_wagered      NUMERIC(36,0) NOT NULL DEFAULT 0,
    total_hold_seconds BIGINT NOT NULL DEFAULT 0,
    longest_hold_secs  BIGINT NOT NULL DEFAULT 0,
    last_crowned_at    TIMESTAMPTZ,
    last_dethroned_at  TIMESTAMPTZ,
    defenses           INTEGER NOT NULL DEFAULT 0,
    sabotages_done     INTEGER NOT NULL DEFAULT 0,
    bounties_placed    NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_rugpull_stats_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rugpull_history (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    tier       TEXT NOT NULL,
    wager      NUMERIC(36,0) NOT NULL,
    won        BOOLEAN NOT NULL,
    king_id    BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rugpull_history_guild ON rugpull_history (guild_id, created_at DESC);

-- ============================================================================
-- 13. NETWORK VAULTS (server progression)
-- ============================================================================

CREATE TABLE IF NOT EXISTS network_vaults (
    guild_id   BIGINT        NOT NULL,
    network    TEXT          NOT NULL,   -- 'sun', 'mta', 'arc', 'dsc'
    balance    NUMERIC(36,0) NOT NULL DEFAULT 0,
    level      INTEGER       NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, network),
    CONSTRAINT fk_vault_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_vault_balance CHECK (balance >= 0)
);

-- ============================================================================
-- 14. NFT COLLECTION IMAGES (per-slot gallery)
-- ============================================================================

CREATE TABLE IF NOT EXISTS nft_collection_images (
    id            SERIAL PRIMARY KEY,
    collection_id INT  NOT NULL REFERENCES nft_collections(id) ON DELETE CASCADE,
    slot          INT  NOT NULL,          -- 1-indexed, matches token_id
    image_url     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_nft_collection_images_collection ON nft_collection_images (collection_id);

-- ============================================================================
-- 15. GUILD SECURITY CONFIG (per-guild override thresholds)
-- ============================================================================

CREATE TABLE IF NOT EXISTS guild_security_config (
    guild_id                    BIGINT      PRIMARY KEY
                                            REFERENCES guild_settings(guild_id) ON DELETE CASCADE,

    -- Detection windows
    scan_interval_seconds       INTEGER,
    lookback_seconds            INTEGER,

    -- Economy detectors
    income_velocity_limit       INTEGER,
    gambling_velocity_limit     INTEGER,
    wash_trade_min_cycles       INTEGER,
    transfer_ring_min           INTEGER,
    lp_churn_min                INTEGER,
    tx_flood_limit              INTEGER,

    -- API / Session detectors
    auth_failure_limit          INTEGER,
    auth_failure_window         INTEGER,
    session_ip_change_window    INTEGER,
    api_request_flood_limit     INTEGER,

    -- Override flags
    disable_income_velocity     BOOLEAN,
    disable_gambling_velocity   BOOLEAN,
    disable_wash_trade          BOOLEAN,
    disable_transfer_ring       BOOLEAN,
    disable_lp_churn            BOOLEAN,
    disable_tx_flood            BOOLEAN,
    disable_auth_failure        BOOLEAN,
    disable_session_ip_change   BOOLEAN,
    disable_api_request_flood   BOOLEAN,

    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- Server events: notable moments (catastrophes, jackpots, rugpulls, big wins)
-- Used by the AI to gossip about what happened in the server.
-- ============================================================================
CREATE TABLE IF NOT EXISTS server_events (
    id          BIGSERIAL       PRIMARY KEY,
    guild_id    BIGINT          NOT NULL,
    channel_id  BIGINT,
    user_id     BIGINT          NOT NULL,
    event_type  TEXT            NOT NULL,
    summary     TEXT            NOT NULL,
    amount      NUMERIC(36,0)   DEFAULT 0,
    metadata    JSONB           DEFAULT '{}',
    ts          TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_server_events_guild ON server_events (guild_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_server_events_user  ON server_events (user_id, guild_id, ts DESC);

-- ============================================================================
-- Channel context: tracks social interactions (reactions, edits, deletes)
-- for richer AI context awareness.
-- ============================================================================
CREATE TABLE IF NOT EXISTS channel_context (
    id              BIGSERIAL       PRIMARY KEY,
    guild_id        BIGINT          NOT NULL,
    channel_id      BIGINT          NOT NULL,
    user_id         BIGINT          NOT NULL,
    event_type      TEXT            NOT NULL,
    content         TEXT            NOT NULL DEFAULT '',
    target_user_id  BIGINT,
    metadata        JSONB           DEFAULT '{}',
    ts              TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_ctx_guild ON channel_context (guild_id, channel_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_channel_ctx_user  ON channel_context (user_id, guild_id, ts DESC);

-- Widen ai_user_memory to support richer context (up to 500 chars)
ALTER TABLE ai_user_memory ALTER COLUMN memory TYPE TEXT;

-- ── Liqstones (added via migration 0038/0039) ────────────────────────────────
CREATE TABLE IF NOT EXISTS liqstones (
    user_id       BIGINT        NOT NULL,
    guild_id      BIGINT        NOT NULL,
    level         INTEGER       NOT NULL DEFAULT 1,
    xp            NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount NUMERIC(36,0) NOT NULL DEFAULT 0,
    acquired_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_liqstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_liqstones_level CHECK (level >= 1),
    CONSTRAINT chk_liqstones_xp    CHECK (xp >= 0)
);

-- ── Economy snapshots (rollback support, migrations 0056/0057) ────────────────
CREATE TABLE IF NOT EXISTS economy_snapshots (
    id                  BIGSERIAL    PRIMARY KEY,
    guild_id            BIGINT       NOT NULL,
    taken_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    wallets             JSONB        NOT NULL DEFAULT '[]',
    crypto_holdings     JSONB        NOT NULL DEFAULT '[]',
    wallet_holdings     JSONB        NOT NULL DEFAULT '[]',
    prices              JSONB        NOT NULL DEFAULT '[]',
    pools               JSONB        NOT NULL DEFAULT '[]',
    stones              JSONB        NOT NULL DEFAULT '[]',
    lp_positions        JSONB        NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_economy_snapshots_guild_ts
    ON economy_snapshots (guild_id, taken_at DESC);

-- ── Auto-compound settings (migration 0038) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS auto_compound_settings (
    user_id           BIGINT        NOT NULL,
    guild_id          BIGINT        NOT NULL,
    validator_id      TEXT          NOT NULL,
    symbol            TEXT          NOT NULL,
    enabled           BOOLEAN       NOT NULL DEFAULT TRUE,
    total_compounded  NUMERIC(36,0) NOT NULL DEFAULT 0,
    compound_count    INTEGER       NOT NULL DEFAULT 0,
    last_compound_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, validator_id, symbol)
);

-- ── Custom webhooks (migration 0003) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS custom_webhooks (
    guild_id       BIGINT       NOT NULL,
    name           TEXT         NOT NULL,
    webhook_id     TEXT         NOT NULL,
    webhook_token  TEXT         NOT NULL DEFAULT '',
    channel_id     BIGINT       NOT NULL,
    avatar_url     TEXT         NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, name)
);

-- ── Eat the Rich tables (migration 0038/0039) ─────────────────────────────────
-- Back the ,eat / ,fortify class-warfare game. Table + column names predate
-- the rename and are kept to avoid migrating live player records.
CREATE TABLE IF NOT EXISTS exploit_shields (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    active_until TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS exploit_stats (
    user_id          BIGINT        NOT NULL,
    guild_id         BIGINT        NOT NULL,
    heists_attempted INTEGER       NOT NULL DEFAULT 0,
    heists_won       INTEGER       NOT NULL DEFAULT 0,
    total_stolen     NUMERIC(36,0) NOT NULL DEFAULT 0,
    times_targeted   INTEGER       NOT NULL DEFAULT 0,
    times_defended   INTEGER       NOT NULL DEFAULT 0,
    total_lost       NUMERIC(36,0) NOT NULL DEFAULT 0,
    prep_ready_at    TIMESTAMPTZ,
    cook_ready_at    TIMESTAMPTZ,
    salad_attempts   INTEGER       NOT NULL DEFAULT 0,
    salad_won        INTEGER       NOT NULL DEFAULT 0,
    -- EatChain progression + $EAT economy (migration 0284)
    eat_level         INTEGER          NOT NULL DEFAULT 1,
    eat_xp            NUMERIC(28,8)    NOT NULL DEFAULT 0,
    eat_staked        NUMERIC(36,0)    NOT NULL DEFAULT 0,
    eat_yield_at      TIMESTAMPTZ,
    eat_title         TEXT,
    rugs_pulled       INTEGER          NOT NULL DEFAULT 0,
    insurance_charges INTEGER          NOT NULL DEFAULT 0,
    insurance_until   TIMESTAMPTZ,
    rug_vuln_until    TIMESTAMPTZ,
    eat_buff_until    TIMESTAMPTZ,
    eat_buff_bonus    DOUBLE PRECISION NOT NULL DEFAULT 0,
    chew_at           TIMESTAMPTZ,
    chew_reward       NUMERIC(36,0)    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

-- Multi-currency salad bowl: fills with every currency stolen via ,eat.
-- One row per (guild_id, symbol). Amounts are raw NUMERIC(36,0) scaled by 10^18.
CREATE TABLE IF NOT EXISTS eat_salad_bowl (
    guild_id BIGINT        NOT NULL,
    symbol   TEXT          NOT NULL,
    amount   NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, symbol)
);

CREATE TABLE IF NOT EXISTS exploit_history (
    id          BIGSERIAL     PRIMARY KEY,
    guild_id    BIGINT        NOT NULL,
    attacker_id BIGINT        NOT NULL,
    target_id   BIGINT        NOT NULL,
    tier        TEXT          NOT NULL,
    wager       NUMERIC(36,0) NOT NULL,
    stolen      NUMERIC(36,0) NOT NULL DEFAULT 0,
    won         BOOLEAN       NOT NULL,
    shielded    BOOLEAN       NOT NULL DEFAULT FALSE,
    mode        TEXT          NOT NULL DEFAULT 'target',
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exploit_history_guild
    ON exploit_history (guild_id, created_at DESC);

-- ── Game helpers / Game Masters (migration 0038) ──────────────────────────────
CREATE TABLE IF NOT EXISTS game_helpers (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    granted_by BIGINT      NOT NULL,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS helper_audit_log (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    helper_id  BIGINT      NOT NULL,
    action     TEXT        NOT NULL,
    target_id  BIGINT,
    details    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_helper_audit_guild
    ON helper_audit_log (guild_id, created_at DESC);

-- ── Price alerts (migration 0038) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_alerts (
    id           BIGSERIAL     PRIMARY KEY,
    user_id      BIGINT        NOT NULL,
    guild_id     BIGINT        NOT NULL,
    symbol       TEXT          NOT NULL,
    direction    TEXT          NOT NULL CHECK (direction IN ('above', 'below')),
    target_price NUMERIC(28,8) NOT NULL,
    triggered    BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    triggered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_price_alerts_active
    ON price_alerts (guild_id, symbol) WHERE triggered = FALSE;

-- ── Stake batches (migration 0040) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stake_batches (
    id           BIGSERIAL     PRIMARY KEY,
    user_id      BIGINT        NOT NULL,
    guild_id     BIGINT        NOT NULL,
    validator_id TEXT          NOT NULL,
    symbol       TEXT          NOT NULL,
    amount       NUMERIC(36,0) NOT NULL DEFAULT 0,
    staked_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_stake_batch_amount CHECK (amount >= 0)
);
CREATE INDEX IF NOT EXISTS idx_stake_batches_lookup
    ON stake_batches (user_id, guild_id, validator_id, staked_at ASC);

-- Governance (migration 0059) ------------------------------------------------
-- Voting power = DSC held across all positions (CeFi + DeFi + staked + delegated).
-- Quorum and pass threshold mirror Compound/VTR/Cardano governance mechanics.
CREATE TABLE IF NOT EXISTS governance_proposals (
    id              SERIAL        PRIMARY KEY,
    guild_id        BIGINT        NOT NULL,
    title           TEXT          NOT NULL,
    description     TEXT          NOT NULL,
    created_by      BIGINT        NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    ends_at         TIMESTAMPTZ   NOT NULL,
    status          TEXT          NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'passed', 'failed', 'cancelled')),
    quorum_pct      NUMERIC(5,2)  NOT NULL DEFAULT 5.0,
    pass_threshold  NUMERIC(5,2)  NOT NULL DEFAULT 51.0,
    supply_snapshot NUMERIC(36,0) NOT NULL DEFAULT 0,
    CONSTRAINT fk_gov_proposals_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gov_proposals_guild
    ON governance_proposals (guild_id, status);

CREATE TABLE IF NOT EXISTS governance_votes (
    proposal_id  INT           NOT NULL,
    user_id      BIGINT        NOT NULL,
    vote         TEXT          NOT NULL CHECK (vote IN ('yes', 'no', 'abstain')),
    voting_power NUMERIC(36,0) NOT NULL DEFAULT 0,
    voted_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (proposal_id, user_id),
    CONSTRAINT fk_gov_votes_proposal FOREIGN KEY (proposal_id)
        REFERENCES governance_proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gov_votes_proposal
    ON governance_votes (proposal_id);

-- ============================================================================
-- 22. AGENT TOOLS FRAMEWORK (migrations 0085, 0086)
-- Persistent task queue, event triggers, multi-step chain runs, audit log,
-- and approval slots for the framework/agent_tools/ subsystem.
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_tool_audit (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    actor        TEXT        NOT NULL DEFAULT 'user',
    tool         TEXT        NOT NULL,
    risk         TEXT        NOT NULL DEFAULT 'read',
    args         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ok           BOOLEAN     NOT NULL,
    error        TEXT        NOT NULL DEFAULT '',
    duration_ms  INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_tool_audit_guild_user
    ON agent_tool_audit (guild_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_tool_audit_tool
    ON agent_tool_audit (tool, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_task_queue (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    actor         TEXT        NOT NULL DEFAULT 'queue',
    tool          TEXT        NOT NULL,
    args          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT        NOT NULL DEFAULT 'pending',
    run_after     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    max_attempts  INTEGER     NOT NULL DEFAULT 3,
    attempts      INTEGER     NOT NULL DEFAULT 0,
    result        JSONB,
    claimed_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    approval_id   BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_task_queue_pending
    ON agent_task_queue (status, run_after)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS agent_task_queue_user
    ON agent_task_queue (guild_id, user_id, status);

CREATE TABLE IF NOT EXISTS agent_triggers (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    name        TEXT        NOT NULL DEFAULT '',
    kind        TEXT        NOT NULL,
    condition   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tool        TEXT        NOT NULL,
    args        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    one_shot    BOOLEAN     NOT NULL DEFAULT TRUE,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    fire_count  INTEGER     NOT NULL DEFAULT 0,
    last_result JSONB,
    fired_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_triggers_lookup
    ON agent_triggers (guild_id, kind, enabled);
CREATE INDEX IF NOT EXISTS agent_triggers_user
    ON agent_triggers (guild_id, user_id);

CREATE TABLE IF NOT EXISTS agent_chain_runs (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    actor         TEXT        NOT NULL DEFAULT 'chain',
    steps         JSONB       NOT NULL,
    step_results  JSONB,
    status        TEXT        NOT NULL DEFAULT 'running',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_chain_runs_user
    ON agent_chain_runs (guild_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_approvals (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    tool        TEXT        NOT NULL,
    args        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'pending',
    decided_by  BIGINT,
    decided_at  TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_approvals_user_status
    ON agent_approvals (guild_id, user_id, status);

CREATE TABLE IF NOT EXISTS bot_config (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- Done. All tables, indexes, constraints, and triggers created.
-- ============================================================================
