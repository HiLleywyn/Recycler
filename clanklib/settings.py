"""clanklib/settings.py -- one place every cog reads runtime config from.

Precedence, highest first:

1. Per-guild override stored in ``guild_settings`` (set via ``.set ...``).
2. The bot's live ``Settings`` object (``bot.settings``), which the Sojourns
   control link refreshes on every heartbeat -- so a change made in the
   Sojourns UI takes effect without a redeploy.
3. The matching environment variable.
4. The caller's default.

Cogs call ``await cfg(self.bot).get(...)`` (guild-aware) or the cheap
``setting(self.bot, KEY, default)`` (global, no DB) instead of reading
``os.getenv`` at import time, so config is live rather than a boot snapshot.
"""
from __future__ import annotations

import os
from typing import Any, Optional


def setting(bot: Any, key: str, default: Any = None) -> Any:
    """Global (non-guild) resolution: live bot.settings -> env -> default.

    Cheap and synchronous; use for values with no per-guild override
    (``BACKUP_MAX_PER_USER``, ``CLANK_API_KEY`` ...)."""
    bs = getattr(bot, "settings", None)
    if bs is not None:
        try:
            val = bs.get(key)
            if val is not None and not (isinstance(val, str) and val == ""):
                return val
        except Exception:  # noqa: BLE001 - settings is best-effort
            pass
    env = os.getenv(key)
    if env is not None and env.strip() != "":
        return env
    return default


def setting_int(bot: Any, key: str, default: int) -> int:
    try:
        return int(setting(bot, key, default))
    except (TypeError, ValueError):
        return default


class GuildConfig:
    """Guild-aware resolution: guild_settings override -> global ``setting``."""

    def __init__(self, bot: Any, guild_id: Optional[int]) -> None:
        self._bot = bot
        self._gid = int(guild_id) if guild_id else None
        self._row: dict | None = None

    async def _settings_row(self) -> dict:
        if self._row is None and self._gid is not None:
            db = getattr(self._bot, "db", None)
            if db is not None and hasattr(db, "get_guild_settings"):
                try:
                    self._row = await db.get_guild_settings(self._gid)
                except Exception:  # noqa: BLE001
                    self._row = {}
            else:
                self._row = {}
        return self._row or {}

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._settings_row()
        val = row.get(key)
        if val is not None and not (isinstance(val, str) and val == ""):
            return val
        return setting(self._bot, key, default)

    def prefix(self) -> str:
        """Synchronous prefix for help text (global; per-guild prefix is applied
        by the framework's dynamic prefix at dispatch time)."""
        return str(setting(self._bot, "PREFIX", ".") or ".")


def cfg(bot: Any, guild_id: Optional[int] = None) -> GuildConfig:
    return GuildConfig(bot, guild_id)


def prefix(bot: Any) -> str:
    """The active command prefix from live settings (``.`` fallback)."""
    return str(setting(bot, "PREFIX", ".") or ".")
