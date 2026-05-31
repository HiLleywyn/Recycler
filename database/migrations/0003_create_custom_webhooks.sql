-- Custom webhooks table for guild webhook management.
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
