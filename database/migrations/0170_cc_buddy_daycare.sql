-- Buddy breeding daycare. One slot per user per guild: the user deposits
-- two of their owned buddies as parents, the daycare timer counts down on
-- the DB clock, and once egg_ready_at <= NOW() the user can collect a
-- pre-rolled egg (species + rarity tier rolled at deposit time so the
-- player can plan around the result while it incubates). Parents are
-- detected as in-daycare via the cc_buddy_daycare row reference -- no
-- redundant flag on cc_buddies, no migration churn if the breeding rules
-- ever change.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS cc_buddy_daycare (
    guild_id        BIGINT      NOT NULL,
    user_id         BIGINT      NOT NULL,
    parent1_id      BIGINT      NOT NULL REFERENCES cc_buddies (id) ON DELETE CASCADE,
    parent2_id      BIGINT      NOT NULL REFERENCES cc_buddies (id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    egg_ready_at    TIMESTAMPTZ NOT NULL,
    egg_species     TEXT        NOT NULL,
    egg_rarity_tier INTEGER     NOT NULL DEFAULT 1,
    egg_collected   BOOLEAN     NOT NULL DEFAULT FALSE,
    fee_paid_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    fee_currency    TEXT        NOT NULL DEFAULT 'BUD',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT cc_buddy_daycare_distinct_parents
        CHECK (parent1_id <> parent2_id),
    CONSTRAINT cc_buddy_daycare_rarity_chk
        CHECK (egg_rarity_tier BETWEEN 1 AND 5)
);

-- Lookup so the panel can quickly check whether any given buddy is busy
-- as a daycare parent without scanning the whole table.
CREATE INDEX IF NOT EXISTS cc_buddy_daycare_parent1_idx
    ON cc_buddy_daycare (parent1_id);
CREATE INDEX IF NOT EXISTS cc_buddy_daycare_parent2_idx
    ON cc_buddy_daycare (parent2_id);
