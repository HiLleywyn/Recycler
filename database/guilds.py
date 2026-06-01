"""database/guilds.py -- guild-scoped lookups the framework runtime expects.

The framework's prefix/permission paths call ``db.guilds.get_bot_channels``
and ``db.guilds.get_command_allowed_roles``. Recycler does not restrict
commands by channel or role out of the box, so both return empty (meaning
"no restriction"); the settings cog can layer real restrictions on later.
"""
from __future__ import annotations

import logging

from database.base import PgBaseRepo

log = logging.getLogger(__name__)


class GuildRepo(PgBaseRepo):
    async def get_bot_channels(self, guild_id: int) -> list[int]:
        """Channel ids commands are restricted to ([] = anywhere)."""
        row = await self.fetch_one(
            "SELECT bot_channels FROM guild_settings WHERE guild_id = $1",
            int(guild_id),
        )
        raw = (row or {}).get("bot_channels") or ""
        out: list[int] = []
        for part in str(raw).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out

    async def get_command_allowed_roles(self, guild_id: int, command: str) -> list[int]:
        """Role ids allowed to run ``command`` ([] = everyone)."""
        return []
