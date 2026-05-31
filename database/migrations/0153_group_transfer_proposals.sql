-- Group ownership transfer handshake.
--
-- The original ,group transfer command flipped founder_id immediately so
-- a player could be saddled with founder rights they never asked for. The
-- two-sided flow stores a pending proposal here; the target then runs
-- ,group transfer accept (or decline) to complete the handover, and the
-- founder can ,group transfer cancel before that.
--
-- One pending proposal per (guild, group) at a time -- the founder must
-- cancel an existing proposal before opening a new one. UNIQUE on
-- (guild_id, group_id) enforces that without a separate status column.

CREATE TABLE IF NOT EXISTS group_transfer_proposals (
    id              BIGSERIAL   PRIMARY KEY,
    guild_id        BIGINT      NOT NULL,
    group_id        TEXT        NOT NULL,
    from_user_id    BIGINT      NOT NULL,             -- current founder at proposal time
    to_user_id      BIGINT      NOT NULL,             -- proposed new founder
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, group_id),
    CONSTRAINT chk_gtp_distinct CHECK (from_user_id <> to_user_id)
);

CREATE INDEX IF NOT EXISTS idx_gtp_target
    ON group_transfer_proposals (guild_id, to_user_id);


-- Module toggle column for the crafting minigame. Mirrors module_fishing
-- and module_farming. NULL = enabled by default; FALSE = disabled (admins
-- still bypass via module_cog_check). Folded into this migration so the
-- ,admin crafting enable/disable command lands ready to flip.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS module_crafting BOOLEAN;
