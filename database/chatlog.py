"""database/chatlog.py -- persistence for channel chatlog archives."""
from __future__ import annotations

import json
import logging
from typing import Any

from database.base import PgBaseRepo

log = logging.getLogger(__name__)


class ChatlogRepo(PgBaseRepo):
    async def create(
        self,
        *,
        chatlog_id: str,
        owner_id: int,
        guild_id: int,
        channel_id: int | None,
        channel_name: str | None,
        messages: list[dict[str, Any]],
    ) -> None:
        await self.execute(
            "INSERT INTO chatlogs "
            "(id, owner_id, guild_id, channel_id, channel_name, message_count, data) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
            chatlog_id, int(owner_id), int(guild_id),
            int(channel_id) if channel_id else None, channel_name,
            len(messages), json.dumps(messages),
        )

    async def get(self, chatlog_id: str) -> dict | None:
        row = await self.fetch_one("SELECT * FROM chatlogs WHERE id = $1", chatlog_id)
        if row is None:
            return None
        data = row.get("data")
        if isinstance(data, str):
            try:
                row["data"] = json.loads(data)
            except (TypeError, ValueError):
                row["data"] = []
        return row

    async def list_for_owner(self, owner_id: int, limit: int = 25) -> list[dict]:
        return await self.fetch_all(
            "SELECT id, guild_id, channel_id, channel_name, message_count, created_at "
            "FROM chatlogs WHERE owner_id = $1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), int(limit),
        )

    async def delete(self, chatlog_id: str, owner_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM chatlogs WHERE id = $1 AND owner_id = $2",
            chatlog_id, int(owner_id),
        )
        return self._row_count(status) > 0
