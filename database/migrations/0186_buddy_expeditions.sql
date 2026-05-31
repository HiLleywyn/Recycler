-- 0186_buddy_expeditions.sql
--
-- AI Buddy Expedition table. The active buddy is "deployed" on a timed
-- run to a destination (forest / reef / mine / ruins); after the timer
-- expires the player collects the result -- a procedural story log + a
-- weighted loot drop pulled from fishing / farming / delve pools and
-- scaled by the buddy's species affinity, rarity tier, and run length.
--
-- The buddy is NOT removed from the player's collection while running;
-- the expedition just records "who was sent at start time" so subsequent
-- buddy changes don't affect the run. A buddy can only be on ONE active
-- expedition at a time -- the partial-unique index below enforces that.
--
-- Rows are append-only. ``status`` transitions: running -> collected
-- (player ran ,expedition collect) or running -> expired (sweep job
-- found a run that exceeded its grace window). ``loot_json`` /
-- ``story_json`` are populated on collect; until then they're empty.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS buddy_expeditions (
    expedition_id    BIGSERIAL    PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    user_id          BIGINT       NOT NULL,
    buddy_id         BIGINT       NOT NULL REFERENCES cc_buddies (id) ON DELETE CASCADE,
    destination      TEXT         NOT NULL,
    duration_seconds INTEGER      NOT NULL,
    started_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ends_at          TIMESTAMPTZ  NOT NULL,
    collected_at     TIMESTAMPTZ,
    story_json       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    loot_json        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    xp_gained        INTEGER      NOT NULL DEFAULT 0,
    happiness_delta  INTEGER      NOT NULL DEFAULT 0,
    status           TEXT         NOT NULL DEFAULT 'running',
    -- Buddy snapshot at deploy time. Lets the collect path apply the
    -- right affinity / rarity bonuses even if the player swaps active
    -- buddies before collecting.
    species_at_start TEXT         NOT NULL,
    rarity_at_start  INTEGER      NOT NULL DEFAULT 1,
    level_at_start   INTEGER      NOT NULL DEFAULT 1,
    CONSTRAINT buddy_expeditions_status_chk CHECK (
        status IN ('running', 'collected', 'expired')
    ),
    CONSTRAINT buddy_expeditions_duration_chk CHECK (duration_seconds > 0),
    CONSTRAINT buddy_expeditions_destination_chk CHECK (
        destination IN ('forest', 'reef', 'mine', 'ruins')
    )
);

-- One running expedition per buddy. Partial unique index so the same
-- buddy can have many historical (collected / expired) rows.
CREATE UNIQUE INDEX IF NOT EXISTS buddy_expeditions_one_active_per_buddy
    ON buddy_expeditions (buddy_id)
    WHERE status = 'running';

-- Hot lookup paths.
CREATE INDEX IF NOT EXISTS buddy_expeditions_user_status_idx
    ON buddy_expeditions (guild_id, user_id, status);

CREATE INDEX IF NOT EXISTS buddy_expeditions_ends_at_idx
    ON buddy_expeditions (ends_at)
    WHERE status = 'running';
