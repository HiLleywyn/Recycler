-- V3 Pillar 2: optional mastery seed. Idempotent; safe to re-run.
--
-- mastery_config.py is the canonical declarative source for tracks +
-- nodes -- the bot reads the node tree from there at runtime. This
-- migration only seeds a couple of indices that didn't exist before
-- so common queries stay fast.

CREATE INDEX IF NOT EXISTS user_mastery_track_level_idx
    ON user_mastery (guild_id, track, level DESC);

CREATE INDEX IF NOT EXISTS user_mastery_nodes_node_idx
    ON user_mastery_nodes (node_id);
