"""Admin MM persona management endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_db, require_admin
from api.v2.schemas.admin import PersonaCreate, PersonaInfo, PersonaUpdate
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log
from api.v2.utils import to_iso

router = APIRouter()


@router.get("/personas", response_model=list[PersonaInfo], summary="List personas")
async def list_personas(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all MM personas for this guild."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        """
        SELECT id, name, system_prompt, avatar_url, trade_bias, emoji, active, created_at
        FROM mm_personas
        WHERE guild_id = $1
        ORDER BY name
        """,
        gid,
    )
    return [
        PersonaInfo(
            id=r["id"],
            name=r["name"],
            system_prompt=r["system_prompt"],
            avatar_url=r["avatar_url"],
            trade_bias=r["trade_bias"],
            emoji=r["emoji"],
            active=r["active"],
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]


@router.post("/personas", response_model=SuccessResponse, summary="Create persona")
async def create_persona(
    body: PersonaCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create a new MM persona."""
    gid = int(admin["guild_id"])
    await db.execute(
        """
        INSERT INTO mm_personas (guild_id, name, system_prompt, avatar_url, trade_bias, emoji)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        gid, body.name, body.system_prompt, body.avatar_url, body.trade_bias, body.emoji,
    )
    await audit_log(db, gid, int(admin["user_id"]), "create_persona",
                    {"name": body.name})
    return SuccessResponse(message=f"Persona '{body.name}' created.")


@router.patch("/personas/{persona_id}", response_model=SuccessResponse, summary="Update persona")
async def update_persona(
    persona_id: int,
    body: PersonaUpdate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update an existing persona."""
    gid = int(admin["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SuccessResponse(message="No changes.")

    set_parts = []
    values: list[Any] = [persona_id, gid]
    idx = 3
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    result = await db.execute(
        f"UPDATE mm_personas SET {', '.join(set_parts)} WHERE id = $1 AND guild_id = $2",
        *values,
    )
    await audit_log(db, gid, int(admin["user_id"]), "update_persona",
                    {"persona_id": persona_id, **updates})
    return SuccessResponse(message=f"Persona {persona_id} updated.")


@router.delete("/personas/{persona_id}", response_model=SuccessResponse, summary="Delete persona")
async def delete_persona(
    persona_id: int,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a persona."""
    gid = int(admin["guild_id"])
    await db.execute(
        "DELETE FROM mm_personas WHERE id = $1 AND guild_id = $2",
        persona_id, gid,
    )
    await audit_log(db, gid, int(admin["user_id"]), "delete_persona",
                    {"persona_id": persona_id})
    return SuccessResponse(message=f"Persona {persona_id} deleted.")
