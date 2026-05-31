-- Buddy Network economy state.
--
-- One row per (guild_id, owner_user_id) tracking:
--   * fren_staked_raw       -- FREN locked into the Buddy stake position
--   * bud_yield_pending_raw -- accrued BUD yield not yet claimed
--   * last_yield_at         -- DB-side clock anchor for yield accrual
--   * bud_slots_purchased   -- extra shelter slots bought via ,buddy shop
--                              (cumulative; capped at MAX_BUDDY_SLOTS_TOTAL)
--   * attractor_until       -- TIMESTAMPTZ; while NOW() < this, the user
--                              gets a buffed escape-event roll rate
--   * lifetime totals       -- analytics + leaderboard surface
--
-- Generic pattern mirrored from user_dungeon's stake state, so the same
-- _accrue_pending math from services/dungeon.py composes cleanly.

CREATE TABLE IF NOT EXISTS user_buddy_economy (
    guild_id                BIGINT       NOT NULL,
    user_id                 BIGINT       NOT NULL,
    fren_staked_raw         NUMERIC(36, 0) NOT NULL DEFAULT 0,
    bud_yield_pending_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    last_yield_at           TIMESTAMPTZ,
    bud_slots_purchased     INTEGER      NOT NULL DEFAULT 0,
    attractor_until         TIMESTAMPTZ,
    total_bud_earned_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_bud_burned_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT user_buddy_economy_slots_chk     CHECK (bud_slots_purchased >= 0),
    CONSTRAINT user_buddy_economy_fren_chk      CHECK (fren_staked_raw >= 0),
    CONSTRAINT user_buddy_economy_pending_chk   CHECK (bud_yield_pending_raw >= 0)
);

-- Per-guild slot-purchase analytic so admin panels can see the lifetime
-- BUD sink from slot purchases at a glance without scanning every row.
CREATE INDEX IF NOT EXISTS user_buddy_economy_slots_idx
    ON user_buddy_economy (guild_id, bud_slots_purchased DESC)
    WHERE bud_slots_purchased > 0;

-- Active-attractor lookup so the escape-event tick can filter to only
-- users with a live buff in one query.
CREATE INDEX IF NOT EXISTS user_buddy_economy_attractor_idx
    ON user_buddy_economy (guild_id, attractor_until)
    WHERE attractor_until IS NOT NULL;
