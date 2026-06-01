"""database/templates.py -- persistence for community templates."""
from __future__ import annotations

import json
import logging
from typing import Any

from database.base import PgBaseRepo

log = logging.getLogger(__name__)


class TemplateRepo(PgBaseRepo):
    async def create(
        self,
        *,
        template_id: str,
        name: str,
        description: str,
        owner_id: int,
        data: dict[str, Any],
    ) -> None:
        await self.execute(
            "INSERT INTO templates (id, name, description, owner_id, data) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            template_id, name, description, int(owner_id), json.dumps(data),
        )

    async def get(self, template_id: str) -> dict | None:
        row = await self.fetch_one("SELECT * FROM templates WHERE id = $1", template_id)
        return _decode(row)

    async def delete(self, template_id: str, owner_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM templates WHERE id = $1 AND owner_id = $2",
            template_id, int(owner_id),
        )
        return self._row_count(status) > 0

    async def increment_uses(self, template_id: str) -> None:
        await self.execute(
            "UPDATE templates SET uses = uses + 1 WHERE id = $1", template_id
        )

    async def list_for_owner(self, owner_id: int, limit: int = 25) -> list[dict]:
        return await self.fetch_all(
            "SELECT id, name, description, uses, featured, created_at "
            "FROM templates WHERE owner_id = $1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), int(limit),
        )

    async def browse(self, *, query: str = "", limit: int = 25) -> list[dict]:
        """List listed templates, most-used first, optional name/description filter."""
        if query:
            like = f"%{query}%"
            return await self.fetch_all(
                "SELECT id, name, description, uses, featured, created_at "
                "FROM templates WHERE listed AND (name ILIKE $1 OR description ILIKE $1) "
                "ORDER BY featured DESC, uses DESC LIMIT $2",
                like, int(limit),
            )
        return await self.fetch_all(
            "SELECT id, name, description, uses, featured, created_at "
            "FROM templates WHERE listed ORDER BY featured DESC, uses DESC LIMIT $1",
            int(limit),
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
