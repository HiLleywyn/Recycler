"""Recycler entrypoint.

The whole process lifecycle lives in the framework. Recycler boots from its
``sojourns.json`` manifest: the framework reads ``features`` (the single
``cogs.clanktank`` cog) and the bot identity from it, and exposes the declared
settings as ``bot.settings``. ``bot_manifest`` (``COGS`` + ``APP_NAME``) stays
as a fallback so the bot still starts as before if the manifest is missing or
invalid.
"""
from core.framework.run import run_manifest
from bot_manifest import COGS, APP_NAME

if __name__ == "__main__":
    run_manifest(fallback_cogs=COGS, fallback_app_name=APP_NAME)
