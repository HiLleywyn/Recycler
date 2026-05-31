"""Shared helpers for admin sub-routers."""
from __future__ import annotations

import json
from typing import Any


async def audit_log(
    db,
    guild_id: int,
    admin_user_id: int,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Insert a row into the audit_log table."""
    await db.execute(
        """
        INSERT INTO audit_log (guild_id, admin_user_id, action, details)
        VALUES ($1, $2, $3, $4)
        """,
        guild_id,
        admin_user_id,
        action,
        json.dumps(details) if details is not None else None,
    )
