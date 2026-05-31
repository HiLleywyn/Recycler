-- Migration 0016: Rename report_user fields to bot_manager
-- This migration renames the report_user_* columns to bot_manager_* for clarity.
-- If the columns were already created as bot_manager_* (fresh install from
-- current schema.sql), the renames are skipped and we just ensure the columns
-- exist.

DO $$
BEGIN
    -- Rename report_user_id -> bot_manager_id (if the old column exists)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'report_user_id'
    ) THEN
        ALTER TABLE guild_settings RENAME COLUMN report_user_id TO bot_manager_id;
    END IF;

    -- Rename report_user_auto_exempt -> bot_manager_auto_exempt
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'report_user_auto_exempt'
    ) THEN
        ALTER TABLE guild_settings RENAME COLUMN report_user_auto_exempt TO bot_manager_auto_exempt;
    END IF;

    -- Rename report_user_all_perms -> bot_manager_all_perms
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'report_user_all_perms'
    ) THEN
        ALTER TABLE guild_settings RENAME COLUMN report_user_all_perms TO bot_manager_all_perms;
    END IF;

    -- Ensure the bot_manager columns exist (for fresh installs where schema.sql
    -- already defines them, this is a no-op)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'bot_manager_id'
    ) THEN
        ALTER TABLE guild_settings ADD COLUMN bot_manager_id BIGINT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'bot_manager_auto_exempt'
    ) THEN
        ALTER TABLE guild_settings ADD COLUMN bot_manager_auto_exempt BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'bot_manager_all_perms'
    ) THEN
        ALTER TABLE guild_settings ADD COLUMN bot_manager_all_perms BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;
END $$;
