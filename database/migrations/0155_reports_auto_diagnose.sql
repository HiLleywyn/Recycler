-- Reports auto-diagnose toggle.
--
-- When TRUE, every new ,report submit fires the same AI realness check
-- that ,admin reports diagnose <id> runs and appends the verdict to the
-- admin DM that triages the report. Default NULL (treated as FALSE) so
-- existing servers don't start spending OpenRouter / Ollama credits
-- without the admin opting in.
--
-- Config lives on guild_settings to match the rest of the per-guild
-- module / feed / channel toggles.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS reports_auto_diagnose BOOLEAN;
