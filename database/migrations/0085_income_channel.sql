-- 0085_income_channel.sql
-- Silent chat income feed channel.  Users posting in the configured channel
-- (or threads under it) earn small wallet credits on a cooldown, with a bonus
-- when replying to or reacting to the bot.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS income_channel BIGINT;
