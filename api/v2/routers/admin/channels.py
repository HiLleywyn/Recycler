"""Admin channel assignment endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError
from api.v2.schemas.admin import ChannelAssignment, ChannelUpdate
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()

CHANNEL_KEYS = [
    "trade_channel", "mine_channel", "staking_channel", "validators_channel",
    "contracts_channel", "crypto_channel", "gambling_channel", "pools_channel",
    "drops_channel", "job_channel", "drops_spawn_channel", "faucet_channel",
    "wallet_channel", "error_channel", "scam_channel", "whale_alerts_channel",
    "reports_feed_channel", "security_log_channel",
    "nft_channel", "predictions_channel", "events_channel", "ape_channel",
]


@router.get("/channels", response_model=list[ChannelAssignment], summary="List channel assignments")
async def list_channels(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all channel assignments for this guild."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", gid)
    if not row:
        raise NotFoundError("Guild settings not found.")

    d = dict(row)
    return [
        ChannelAssignment(
            channel_key=key,
            channel_id=str(d[key]) if d.get(key) is not None else None,
        )
        for key in CHANNEL_KEYS
    ]


@router.patch("/channels", response_model=SuccessResponse, summary="Update channel assignments")
async def update_channels(
    body: ChannelUpdate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update channel assignments. Pass a map of channel_key -> channel_id (or null to unset)."""
    gid = int(admin["guild_id"])

    set_parts = []
    values: list[Any] = [gid]
    idx = 2
    for key, val in body.assignments.items():
        if key not in CHANNEL_KEYS:
            continue
        set_parts.append(f"{key} = ${idx}")
        values.append(int(val) if val is not None else None)
        idx += 1

    if not set_parts:
        return SuccessResponse(message="No valid channel keys provided.")

    await db.execute(
        f"UPDATE guild_settings SET {', '.join(set_parts)} WHERE guild_id = $1",
        *values,
    )
    await audit_log(db, gid, int(admin["user_id"]), "update_channels", body.assignments)
    return SuccessResponse(message="Channel assignments updated.")
