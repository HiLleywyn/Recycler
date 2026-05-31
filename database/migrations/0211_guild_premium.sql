-- Per-guild premium subscription gating + PayPal webhook ledger.
--
-- Discoin is now a shared multi-tenant bot: any guild can invite it but
-- the cost-heavy features (AI, fishing, crafting, delves, expeditions,
-- buddy battles/breeding/market) are gated behind a per-guild premium
-- subscription. The host guild (Config.HOST_GUILD_ID) is auto-unlocked
-- in code, NOT in this table -- its absence here is intentional.
--
-- The trading economy, gambling, bank/profile, and basic buddy management
-- (hatch/rename/storage/economy) remain free everywhere.

CREATE TABLE IF NOT EXISTS guild_premium (
    guild_id                BIGINT          PRIMARY KEY,
    tier                    VARCHAR(32)     NOT NULL DEFAULT 'premium',
    -- active | cancelled | expired | suspended
    -- 'cancelled' = user pressed cancel, period still running until expires_at;
    -- 'expired'  = period ended; 'suspended' = PayPal payment failure.
    status                  VARCHAR(32)     NOT NULL DEFAULT 'active',
    -- admin | paypal | host (host is reserved; never written here, see code)
    source                  VARCHAR(32)     NOT NULL DEFAULT 'admin',
    -- Discord user that owns the subscription (server owner).
    subscriber_user_id      BIGINT,
    paypal_subscription_id  VARCHAR(64),
    paypal_plan_id          VARCHAR(64),
    started_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    -- Most recent paid period end. Refreshed by PAYMENT.SALE.COMPLETED.
    current_period_end      TIMESTAMPTZ,
    -- Hard cutoff. NULL = no expiry (admin grant w/o duration).
    expires_at              TIMESTAMPTZ,
    cancelled_at            TIMESTAMPTZ,
    -- Discord user_id of the admin that ran ,admin premium grant.
    granted_by              BIGINT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guild_premium_status
    ON guild_premium (status);

CREATE INDEX IF NOT EXISTS idx_guild_premium_expires
    ON guild_premium (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_premium_paypal_sub
    ON guild_premium (paypal_subscription_id)
    WHERE paypal_subscription_id IS NOT NULL;

-- PayPal webhook event ledger. Used for idempotent processing (PayPal
-- retries deliveries until it gets a 2xx) and as an audit log when a
-- subscription state looks wrong.
CREATE TABLE IF NOT EXISTS paypal_webhook_events (
    event_id        VARCHAR(64)     PRIMARY KEY,
    event_type      VARCHAR(64)     NOT NULL,
    -- Subscription / sale / plan id from event.resource.id.
    resource_id     VARCHAR(64),
    payload         JSONB           NOT NULL,
    received_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_paypal_webhook_resource
    ON paypal_webhook_events (resource_id);

CREATE INDEX IF NOT EXISTS idx_paypal_webhook_unprocessed
    ON paypal_webhook_events (received_at)
    WHERE processed_at IS NULL;
