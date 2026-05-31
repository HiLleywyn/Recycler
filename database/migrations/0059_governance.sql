-- ============================================================================
-- 0059_governance.sql  -  Discoin Governance voting tables
--
-- Voting power = DSC held across all positions (CeFi + DeFi + staked + delegated)
-- This mirrors on-chain governance models (Compound/AAVE/Cardano style):
--   - Proposals have a quorum requirement (% of circulating DSC)
--   - Pass threshold: majority of YES+NO votes (abstain excluded from ratio)
--   - Supply snapshot taken at proposal creation to prevent manipulation
-- ============================================================================

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
    supply_snapshot NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    CONSTRAINT fk_gov_proposals_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gov_proposals_guild
    ON governance_proposals (guild_id, status);

CREATE TABLE IF NOT EXISTS governance_votes (
    proposal_id  INT           NOT NULL,
    user_id      BIGINT        NOT NULL,
    vote         TEXT          NOT NULL CHECK (vote IN ('yes', 'no', 'abstain')),
    voting_power NUMERIC(28,8) NOT NULL DEFAULT 0,
    voted_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (proposal_id, user_id),
    CONSTRAINT fk_gov_votes_proposal FOREIGN KEY (proposal_id)
        REFERENCES governance_proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gov_votes_proposal
    ON governance_votes (proposal_id);
