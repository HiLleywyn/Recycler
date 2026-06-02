"""Recycler entrypoint.

Boots from ``auren.json`` (the manifest's ``features`` is the cog list and
``bot.name`` is the app name) via the shared framework runtime. If the
manifest is missing or invalid the bot still starts from the fallback cog
list below, so a packaging slip can never make a working bot refuse to boot.
"""
import hashlib
import os

# The framework refuses to boot the embedded dashboard with the default
# ``JWT_SECRET`` -- but that secret only signs the framework's JWT dashboard
# sessions, which Recycler does not use (its API authenticates with
# ``CLANK_API_KEY``). Rather than make operators set an unused secret, derive a
# stable, non-default value from a per-deployment input so the guard passes and
# restarts stay consistent. If an operator does set ``JWT_SECRET``, theirs wins.
# This MUST run before importing the framework: ``core.config.Config`` reads
# ``JWT_SECRET`` from the environment at import time.
if not os.getenv("JWT_SECRET"):
    _seed = os.getenv("DATABASE_URL") or os.getenv("DISCORD_TOKEN") or "recycler"
    os.environ["JWT_SECRET"] = hashlib.sha256(_seed.encode("utf-8")).hexdigest()

from core.framework.run import run_manifest  # noqa: E402  (must follow the JWT_SECRET default above)

# Mirrors auren.json -> features. Kept in sync as a safety net only; the
# manifest is the source of truth at runtime.
_FALLBACK_COGS = [
    "cogs.meta",
    "cogs.settings",
    "cogs.backups",
    "cogs.templates",
    "cogs.chatlog",
    "cogs.sync",
    "cogs.importexport",
]


if __name__ == "__main__":
    run_manifest(fallback_cogs=_FALLBACK_COGS, fallback_app_name="Recycler")
