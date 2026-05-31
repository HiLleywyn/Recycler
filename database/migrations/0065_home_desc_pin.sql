-- Store the Discord message ID of the currently pinned description in each home.
-- Allows home_setdesc to unpin the old message before pinning the new one,
-- preventing unbounded pin accumulation.

ALTER TABLE player_homes
    ADD COLUMN IF NOT EXISTS desc_pin_msg_id BIGINT DEFAULT NULL;
