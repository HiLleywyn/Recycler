"""Admin validator management endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.schemas.admin import ValidatorCreate, ValidatorInfo, ValidatorUpdate
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()


@router.get("/validators", response_model=list[ValidatorInfo], summary="List validators")
async def list_validators(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all NPC validators for this guild."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        "SELECT validator_id, name, emoji, uptime_rate, reward_rate, slash_rate "
        "FROM validators WHERE guild_id = $1 ORDER BY name",
        gid,
    )
    return [
        ValidatorInfo(
            validator_id=r["validator_id"],
            name=r["name"],
            emoji=r["emoji"],
            uptime_rate=float(r["uptime_rate"]),
            reward_rate=float(r["reward_rate"]),
            slash_rate=float(r["slash_rate"]),
        )
        for r in rows
    ]


@router.post("/validators", response_model=SuccessResponse, summary="Create validator")
async def create_validator(
    body: ValidatorCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create a new NPC validator."""
    gid = int(admin["guild_id"])
    existing = await db.fetchrow(
        "SELECT validator_id FROM validators WHERE guild_id = $1 AND validator_id = $2",
        gid, body.validator_id,
    )
    if existing:
        raise ValidationError(f"Validator {body.validator_id} already exists.")

    await db.execute(
        """
        INSERT INTO validators (validator_id, guild_id, name, emoji, uptime_rate, reward_rate, slash_rate)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        body.validator_id, gid, body.name, body.emoji,
        body.uptime_rate, body.reward_rate, body.slash_rate,
    )
    await audit_log(db, gid, int(admin["user_id"]), "create_validator",
                    {"validator_id": body.validator_id, "name": body.name})
    return SuccessResponse(message=f"Validator {body.name} created.")


@router.patch("/validators/{validator_id}", response_model=SuccessResponse, summary="Update validator")
async def update_validator(
    validator_id: str,
    body: ValidatorUpdate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update a validator's properties."""
    gid = int(admin["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SuccessResponse(message="No changes.")

    set_parts = []
    values: list[Any] = [validator_id, gid]
    idx = 3
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    result = await db.execute(
        f"UPDATE validators SET {', '.join(set_parts)} "
        f"WHERE validator_id = $1 AND guild_id = $2",
        *values,
    )
    if result == "UPDATE 0":
        raise NotFoundError(f"Validator {validator_id} not found.")
    await audit_log(db, gid, int(admin["user_id"]), "update_validator",
                    {"validator_id": validator_id, **updates})
    return SuccessResponse(message=f"Validator {validator_id} updated.")


@router.delete("/validators/{validator_id}", response_model=SuccessResponse, summary="Delete validator")
async def delete_validator(
    validator_id: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a validator."""
    gid = int(admin["guild_id"])
    result = await db.execute(
        "DELETE FROM validators WHERE validator_id = $1 AND guild_id = $2",
        validator_id, gid,
    )
    if result == "DELETE 0":
        raise NotFoundError(f"Validator {validator_id} not found.")
    await audit_log(db, gid, int(admin["user_id"]), "delete_validator",
                    {"validator_id": validator_id})
    return SuccessResponse(message=f"Validator {validator_id} deleted.")
