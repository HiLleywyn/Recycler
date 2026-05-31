-- 0274_nitro_lottery.sql
--
-- Nitro Lottery: a sniper-safe way for players to share Discord Nitro gifts.
--
--   * Pasting a Nitro gift link into chat is hopeless -- auto-claim "Nitro
--     bots" grab it in milliseconds. Here the host's gift code is collected
--     through a private modal and stored on nitro_lotteries.gift_code; it
--     never appears in any channel. The winner is drawn at RANDOM from the
--     entrants, so raw speed buys a sniper nothing.
--   * nitro_type distinguishes 'nitro' from 'nitro_basic' so every embed and
--     reply can label the prize tier correctly.
--   * nitro_lottery_entries holds one row per entrant; the composite primary
--     key (lottery_id, user_id) makes a double-entry a harmless no-op.

CREATE TABLE IF NOT EXISTS nitro_lotteries (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    channel_id  BIGINT       NOT NULL,
    message_id  BIGINT,
    host_id     BIGINT       NOT NULL,
    nitro_type  TEXT         NOT NULL,
    gift_code   TEXT         NOT NULL,
    note        TEXT,
    status      TEXT         NOT NULL DEFAULT 'open',
    winner_id   BIGINT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ends_at     TIMESTAMPTZ  NOT NULL,
    drawn_at    TIMESTAMPTZ,
    CONSTRAINT chk_nitro_type
        CHECK (nitro_type IN ('nitro', 'nitro_basic')),
    CONSTRAINT chk_nitro_status
        CHECK (status IN ('open', 'drawn', 'expired', 'cancelled'))
);

-- Background draw loop scans for open lotteries whose timer has elapsed.
CREATE INDEX IF NOT EXISTS idx_nitro_lotteries_due
    ON nitro_lotteries (ends_at)
    WHERE status = 'open';

-- .nitro list + per-host open-lottery cap.
CREATE INDEX IF NOT EXISTS idx_nitro_lotteries_guild
    ON nitro_lotteries (guild_id, status);

CREATE TABLE IF NOT EXISTS nitro_lottery_entries (
    lottery_id  BIGINT       NOT NULL
        REFERENCES nitro_lotteries (id) ON DELETE CASCADE,
    user_id     BIGINT       NOT NULL,
    entered_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (lottery_id, user_id)
);
