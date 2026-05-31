"""Admin AI feature toggle endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.schemas.admin import AIFeatureStatus, AIFeatureToggle
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()

AI_FEATURES = [
    "ai_mm_enabled",
    "ai_chat_enabled",
    "ai_commentary_enabled",
    "ai_flavor_enabled",
    "ai_events_enabled",
]


@router.get("/ai/{feature}", response_model=AIFeatureStatus, summary="Get AI feature status")
async def get_ai_feature(
    feature: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return the enabled/disabled status of an AI feature."""
    col = f"ai_{feature}_enabled" if not feature.startswith("ai_") else feature
    if col not in AI_FEATURES:
        raise ValidationError(f"Unknown AI feature: {feature}")

    gid = int(admin["guild_id"])
    val = await db.fetchval(
        f"SELECT {col} FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    if val is None:
        raise NotFoundError("Guild settings not found.")
    return AIFeatureStatus(feature=col, enabled=bool(val))


@router.patch("/ai/{feature}", response_model=SuccessResponse, summary="Toggle AI feature")
async def toggle_ai_feature(
    feature: str,
    body: AIFeatureToggle,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Enable or disable an AI feature."""
    col = f"ai_{feature}_enabled" if not feature.startswith("ai_") else feature
    if col not in AI_FEATURES:
        raise ValidationError(f"Unknown AI feature: {feature}")

    gid = int(admin["guild_id"])
    await db.execute(
        f"UPDATE guild_settings SET {col} = $2 WHERE guild_id = $1",
        gid, body.enabled,
    )
    await audit_log(db, gid, int(admin["user_id"]), "toggle_ai_feature",
                    {"feature": feature, "enabled": body.enabled})
    return SuccessResponse(message=f"AI feature {feature} {'enabled' if body.enabled else 'disabled'}.")
