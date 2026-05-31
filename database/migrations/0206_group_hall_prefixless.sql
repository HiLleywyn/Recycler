-- Per-group toggle: when set, the bot accepts bare commands (no prefix)
-- inside the group's Hall thread, the same way it does in admin-set
-- bot channels. Default off so existing groups keep the legacy
-- ,prefix-required behaviour until the founder opts in via
-- ``,group hall prefixless on``.
--
-- Read in framework/bot.py:_get_prefix on every message so the change
-- takes effect immediately when the founder toggles it.

ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS hall_prefixless BOOLEAN NOT NULL DEFAULT FALSE;
