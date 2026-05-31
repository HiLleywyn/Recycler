-- V3 Pillars 8-10 indices pass.
--
-- Hot read paths added by:
--   - apex_events.modifier() lookups
--   - cwe.tax_on_credit's recent-payer pick
--   - wealth_lp_restoration historical lookups
--   - cwe_user_tx_state per-day reset queries

CREATE INDEX IF NOT EXISTS apex_events_active_event_idx
    ON apex_events_active (guild_id, event_id);

CREATE INDEX IF NOT EXISTS cwe_controller_log_target_idx
    ON cwe_controller_log (guild_id, ts DESC);

CREATE INDEX IF NOT EXISTS cwe_user_tx_state_reset_idx
    ON cwe_user_tx_state (last_reset);
