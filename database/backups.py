"""database/backups.py -- persistence for server backups + auto-backup schedules."""
from __future__ import annotations

import json
import logging
from typing import Any

from database.base import PgBaseRepo

log = logging.getLogger(__name__)


class BackupRepo(PgBaseRepo):
    # -- backups ---------------------------------------------------------------

    async def create(
        self,
        *,
        backup_id: str,
        owner_id: int,
        guild_id: int,
        guild_name: str,
        data: dict[str, Any],
        message_count: int = 0,
        encrypted: bool = False,
    ) -> None:
        await self.execute(
            "INSERT INTO backups "
            "(id, owner_id, guild_id, guild_name, data, message_count, encrypted) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)",
            backup_id, int(owner_id), int(guild_id), guild_name,
            json.dumps(data), int(message_count), bool(encrypted),
        )

    async def get(self, backup_id: str) -> dict | None:
        row = await self.fetch_one("SELECT * FROM backups WHERE id = $1", backup_id)
        return _decode(row)

    async def list_for_owner(self, owner_id: int, limit: int = 25) -> list[dict]:
        rows = await self.fetch_all(
            "SELECT id, guild_id, guild_name, message_count, encrypted, created_at "
            "FROM backups WHERE owner_id = $1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), int(limit),
        )
        return rows

    async def delete(self, backup_id: str, owner_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM backups WHERE id = $1 AND owner_id = $2",
            backup_id, int(owner_id),
        )
        return self._row_count(status) > 0

    async def count_for_owner(self, owner_id: int) -> int:
        return int(await self.fetch_val(
            "SELECT COUNT(*) FROM backups WHERE owner_id = $1", int(owner_id)
        ) or 0)

    async def prune_oldest(self, guild_id: int, keep: int) -> int:
        """Keep the newest ``keep`` backups for a guild; delete the rest."""
        status = await self.execute(
            "DELETE FROM backups WHERE id IN ("
            "  SELECT id FROM backups WHERE guild_id = $1 "
            "  ORDER BY created_at DESC OFFSET $2"
            ")",
            int(guild_id), int(keep),
        )
        return self._row_count(status)

    # -- auto-backup intervals -------------------------------------------------

    async def set_interval(
        self, *, guild_id: int, owner_id: int, interval_hours: int, keep: int = 7
    ) -> None:
        await self.execute(
            "INSERT INTO backup_intervals (guild_id, owner_id, interval_hours, keep, next_run_at) "
            "VALUES ($1, $2, $3, $4, NOW() + ($3 || ' hours')::interval) "
            "ON CONFLICT (guild_id) DO UPDATE SET "
            "  owner_id = EXCLUDED.owner_id, interval_hours = EXCLUDED.interval_hours, "
            "  keep = EXCLUDED.keep, enabled = TRUE, "
            "  next_run_at = NOW() + (EXCLUDED.interval_hours || ' hours')::interval",
            int(guild_id), int(owner_id), int(interval_hours), int(keep),
        )

    async def clear_interval(self, guild_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM backup_intervals WHERE guild_id = $1", int(guild_id)
        )
        return self._row_count(status) > 0

    async def get_interval(self, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM backup_intervals WHERE guild_id = $1", int(guild_id)
        )

    async def due_intervals(self) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM backup_intervals WHERE enabled AND next_run_at <= NOW()"
        )

    async def mark_interval_ran(self, guild_id: int) -> None:
        await self.execute(
            "UPDATE backup_intervals SET last_run_at = NOW(), "
            "next_run_at = NOW() + (interval_hours || ' hours')::interval "
            "WHERE guild_id = $1",
            int(guild_id),
        )


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    data = row.get("data")
    if isinstance(data, str):
        try:
            row["data"] = json.loads(data)
        except (TypeError, ValueError):
            row["data"] = {}
    return row
