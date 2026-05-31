-- Bloomstone -- the Harvest Network's themed leveled gem.
--
-- Same row shape as the other themed stones (tidestones, heartstones,
-- cryptstones, bloodstones from migration 0146). The bot grants XP via
-- services/themed_stones.grant_bloomstone_xp and applies stat bonuses
-- (farm_yield_bonus, farm_seed_drop_bonus) inside services/farming.py
-- on every harvest.
--
-- Drops straight into the generic stone CRUD on database/users.py
-- (_get_stone / _create_stone / _add_stone_xp_delta / etc.) -- the
-- per-stone wrapper helpers in users.py just route to the table name.

CREATE TABLE IF NOT EXISTS bloomstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'DSD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_bloomstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_bloomstones_level CHECK (level >= 1),
    CONSTRAINT chk_bloomstones_xp    CHECK (xp >= 0)
);
