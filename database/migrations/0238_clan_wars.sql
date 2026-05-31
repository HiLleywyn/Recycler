-- V3 Pillar 3: Clan Wars -- inter-guild PvP layer on top of cogs/groups.py
--
-- Each match pairs two guild groups for one week. Members earn
-- contribution points by doing literally any economic action that
-- ties to a node: mining a block (Mine node), catching a legendary
-- fish (Reef), an auction sale (Bazaar), etc. The winning group
-- captures the most nodes by the deadline.

CREATE TABLE IF NOT EXISTS clan_war_matches (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    group_a_id    BIGINT       NOT NULL,
    group_b_id    BIGINT       NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ends_at       TIMESTAMPTZ  NOT NULL,
    settled_at    TIMESTAMPTZ,
    winner_group  BIGINT,
    entry_pool_raw NUMERIC(36,0) NOT NULL DEFAULT 0,
    status        TEXT         NOT NULL DEFAULT 'live'
                               CHECK (status IN ('live', 'settled', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS clan_war_matches_window_idx
    ON clan_war_matches (guild_id, status, ends_at);

CREATE TABLE IF NOT EXISTS clan_war_nodes (
    match_id      BIGINT       NOT NULL,
    node_id       TEXT         NOT NULL,
    -- group_a score and group_b score (raw points). The leading group
    -- owns the node for settlement purposes; ties go to whichever group
    -- crossed the lead-line first via last_lead_change.
    a_score       BIGINT       NOT NULL DEFAULT 0,
    b_score       BIGINT       NOT NULL DEFAULT 0,
    last_lead_change TIMESTAMPTZ,
    PRIMARY KEY (match_id, node_id)
);

CREATE INDEX IF NOT EXISTS clan_war_nodes_match_idx
    ON clan_war_nodes (match_id);

CREATE TABLE IF NOT EXISTS clan_war_contributions (
    id            BIGSERIAL    PRIMARY KEY,
    match_id      BIGINT       NOT NULL,
    user_id       BIGINT       NOT NULL,
    group_id      BIGINT       NOT NULL,
    node_id       TEXT         NOT NULL,
    points        INTEGER      NOT NULL,
    contributed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS clan_war_contributions_match_idx
    ON clan_war_contributions (match_id, contributed_at DESC);

CREATE INDEX IF NOT EXISTS clan_war_contributions_user_idx
    ON clan_war_contributions (user_id, contributed_at DESC);

CREATE TABLE IF NOT EXISTS clan_war_queue (
    guild_id      BIGINT       NOT NULL,
    group_id      BIGINT       NOT NULL,
    queued_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entry_paid_raw NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, group_id)
);
