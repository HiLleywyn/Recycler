"""Admin network management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import ValidationError
from api.v2.schemas.admin import NetworkCreate, NetworkInfo
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log
from api.v2.utils import to_iso

router = APIRouter()


@router.get("/networks", response_model=list[NetworkInfo], summary="List networks")
async def list_networks(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all networks configured for this guild."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        "SELECT network_name, stake_token, emoji, created_at "
        "FROM guild_networks WHERE guild_id = $1 ORDER BY network_name",
        gid,
    )
    return [
        NetworkInfo(
            network_name=r["network_name"],
            stake_token=r["stake_token"],
            emoji=r["emoji"],
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]


@router.post("/networks", response_model=SuccessResponse, summary="Create network")
async def create_network(
    body: NetworkCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create a new blockchain network."""
    gid = int(admin["guild_id"])
    existing = await db.fetchrow(
        "SELECT network_name FROM guild_networks WHERE guild_id = $1 AND network_name = $2",
        gid, body.network_name,
    )
    if existing:
        raise ValidationError(f"Network {body.network_name} already exists.")

    await db.execute(
        """
        INSERT INTO guild_networks (guild_id, network_name, stake_token, emoji)
        VALUES ($1, $2, $3, $4)
        """,
        gid, body.network_name, body.stake_token, body.emoji,
    )
    await audit_log(db, gid, int(admin["user_id"]), "create_network",
                    {"network": body.network_name, "stake_token": body.stake_token})
    return SuccessResponse(message=f"Network {body.network_name} created.")


@router.delete("/networks", response_model=SuccessResponse, summary="Delete network")
async def delete_network(
    network_name: str = Query(..., description="Network name to delete"),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a network from this guild."""
    gid = int(admin["guild_id"])
    await db.execute(
        "DELETE FROM guild_networks WHERE guild_id = $1 AND network_name = $2",
        gid, network_name,
    )
    await audit_log(db, gid, int(admin["user_id"]), "delete_network",
                    {"network": network_name})
    return SuccessResponse(message=f"Network {network_name} deleted.")
