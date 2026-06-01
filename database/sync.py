"""database/sync.py -- persistence for sync links (message + ban mirroring)."""
from __future__ import annotations

import logging

from database.base import PgBaseRepo

log = logging.getLogger(__name__)


class SyncRepo(PgBaseRepo):
    async def create(
        self,
        *,
        kind: str,
        source_id: int,
        target_id: int,
        owner_id: int,
        guild_id: int,
        target_webhook: str | None = None,
    ) -> int:
        row = await self.fetch_one(
            "INSERT INTO sync_links "
            "(kind, source_id, target_id, target_webhook, owner_id, guild_id) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            kind, int(source_id), int(target_id), target_webhook,
            int(owner_id), int(guild_id),
        )
        return int((row or {}).get("id") or 0)

    async def links_for_source(self, kind: str, source_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM sync_links WHERE enabled AND kind = $1 AND source_id = $2",
            kind, int(source_id),
        )

    async def list_for_guild(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM sync_links WHERE guild_id = $1 ORDER BY created_at DESC",
            int(guild_id),
        )

    async def delete(self, link_id: int, guild_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM sync_links WHERE id = $1 AND guild_id = $2",
            int(link_id), int(guild_id),
        )
        return self._row_count(status) > 0

    async def set_enabled(self, link_id: int, guild_id: int, enabled: bool) -> bool:
        status = await self.execute(
            "UPDATE sync_links SET enabled = $3 WHERE id = $1 AND guild_id = $2",
            int(link_id), int(guild_id), bool(enabled),
        )
        return self._row_count(status) > 0
