-- 0265_rugpull_gender_defense.sql
-- Adds:
--   * rugpull_gender table: caches inferred-or-self-declared gender per (user_id, guild_id)
--     so the rug minigame can award the King of Rugs vs Queen of Rugs role appropriately.
--   * rugpull_king.active_defense_until + active_defense_bonus columns: support the new
--     ,rugdefend command (the monarch spends USD for a temporary success-chance debuff
--     on all challengers, similar to the ,defend shield in the exploit minigame).
--   * rugpull_king.defense_last_used_at: cooldown clock for ,rugdefend, kept on the DB
--     side so it cannot drift with container time.

CREATE TABLE IF NOT EXISTS rugpull_gender (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    gender       TEXT   NOT NULL CHECK (gender IN ('male', 'female')),
    source       TEXT   NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_rugpull_gender_user ON rugpull_gender (user_id);

ALTER TABLE rugpull_king
    ADD COLUMN IF NOT EXISTS active_defense_until  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS active_defense_bonus  NUMERIC(6,4) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS defense_last_used_at  TIMESTAMPTZ;
