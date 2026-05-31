"""Notifications router  -  5 endpoints."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db
from api.v2.schemas.common import PaginatedResponse, SuccessResponse
from api.v2.utils import to_iso
from api.v2.schemas.notifications import (
    MarkReadRequest,
    Notification,
    NotificationPreferences,
    NotificationPreferencesUpdate,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _safe_json_loads(value: str) -> Any | None:
    """Parse JSON string, returning None on malformed input.

    Note: may return any JSON type (dict, list, str, int, etc.).
    Callers that require a dict should check ``isinstance(result, dict)``.
    """
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None


@router.get("", response_model=PaginatedResponse, summary="Notification history")
async def list_notifications(
    unread: bool | None = Query(None, description="Filter by unread status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the user's notification history with optional unread filter."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    where = "WHERE user_id = $1 AND guild_id = $2"
    params: list = [uid, gid]

    if unread is not None:
        where += " AND is_read = $3"
        params.append(not unread)

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM notifications {where}",
        *params,
    )

    rows = await db.fetch(
        f"""
        SELECT id, type, title, body, data, is_read, created_at
        FROM notifications
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params, limit, offset,
    )

    items = [
        Notification(
            id=r["id"],
            type=r["type"],
            title=r["title"],
            body=r["body"],
            data=(
                _safe_json_loads(r["data"])
                if isinstance(r["data"], str)
                else r["data"]
            ),
            is_read=r["is_read"],
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]

    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.patch("/read", response_model=SuccessResponse, summary="Mark notifications read")
async def mark_read(
    body: MarkReadRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Mark specific notifications as read by ID."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    await db.execute(
        """
        UPDATE notifications
        SET is_read = TRUE
        WHERE user_id = $1 AND guild_id = $2 AND id = ANY($3::bigint[])
        """,
        uid, gid, body.ids,
    )
    return SuccessResponse(message=f"Marked {len(body.ids)} notification(s) as read.")


@router.patch("/read-all", response_model=SuccessResponse, summary="Mark all notifications read")
async def mark_all_read(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Mark all notifications as read."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    await db.execute(
        "UPDATE notifications SET is_read = TRUE WHERE user_id = $1 AND guild_id = $2 AND is_read = FALSE",
        uid, gid,
    )
    return SuccessResponse(message="All notifications marked as read.")


@router.get("/preferences", response_model=NotificationPreferences, summary="Notification preferences")
async def get_preferences(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the user's notification preference settings."""
    row = await db.fetchrow(
        "SELECT dm_mining, dm_transfer, dm_validator, dm_staking, dm_2fa "
        "FROM user_prefs WHERE user_id = $1 AND guild_id = $2",
        int(user["user_id"]),
        int(user["guild_id"]),
    )
    if not row:
        return NotificationPreferences()
    return NotificationPreferences(**dict(row))


@router.patch("/preferences", response_model=NotificationPreferences, summary="Update notification preferences")
async def update_preferences(
    body: NotificationPreferencesUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update notification settings."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return await get_preferences(user, db)

    set_parts = []
    values: list = [uid, gid]
    idx = 3
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    insert_cols = ", ".join(updates.keys())
    insert_vals = ", ".join(f"${i}" for i in range(3, idx))
    set_clause = ", ".join(set_parts)

    await db.execute(
        f"""
        INSERT INTO user_prefs (user_id, guild_id, {insert_cols})
        VALUES ($1, $2, {insert_vals})
        ON CONFLICT (user_id, guild_id) DO UPDATE SET {set_clause}
        """,
        *values,
    )
    return await get_preferences(user, db)
