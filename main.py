"""Recycler entrypoint.

The whole process lifecycle lives in the framework. Recycler just hands it
the cog list + identity from ``bot_manifest`` and starts.
"""
from core.framework.run import run
from bot_manifest import COGS, APP_NAME

if __name__ == "__main__":
    run(cogs=COGS, app_name=APP_NAME)
