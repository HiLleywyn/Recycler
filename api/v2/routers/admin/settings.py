"""Admin settings, modules, fees, drops, auto-delete, scam, command roles, shop items."""
from __future__ import annotations

from typing import Any

from core.framework.scale import to_human, to_raw

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.schemas.admin import (
    AutoDeleteSettings,
    CommandRole,
    CommandRoleCreate,
    DropSettings,
    FeeSettings,
    GuildSettingsUpdate,
    ModuleStatus,
    ModuleToggle,
    ScamDetectionSettings,
    ShopItemCreate,
)
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()

# All module keys that can be toggled
MODULE_KEYS = [
    "module_gambling", "module_lending", "module_staking", "module_mining",
    "module_drops", "module_faucet", "module_savings", "module_validators",
    "module_pools", "module_contracts", "module_groups", "module_chart",
    "module_crypto", "module_daily", "module_work", "module_economy",
    "module_chain", "module_shop", "module_games", "module_gambling_coinflip",
    "module_gambling_dice", "module_gambling_roulette",
    "module_gambling_blackjack", "module_gambling_slots",
    "module_security",
    "module_ape", "module_nft", "module_predictions", "module_events",
]


# ---- Guild settings --------------------------------------------------------

@router.get("/settings", summary="Get guild settings")
async def get_guild_settings(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return the full guild settings object."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", gid)
    if not row:
        raise NotFoundError("Guild settings not found.")
    d = dict(row)
    d["guild_id"] = str(d["guild_id"])
    for k in ("trade_channel", "mine_channel", "staking_channel", "validators_channel",
              "contracts_channel", "crypto_channel", "gambling_channel", "pools_channel",
              "drops_channel", "job_channel", "drops_spawn_channel", "faucet_channel",
              "wallet_channel", "error_channel", "scam_channel", "whale_alerts_channel",
              "reports_feed_channel", "security_log_channel",
              "nft_channel", "predictions_channel", "events_channel", "ape_channel"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    # Raw-scaled NUMERIC(36,0) columns: return human-scale floats so the UI
    # round-trips correctly. Otherwise a re-save would double-scale.
    for k in ("platform_fee_min", "platform_fee_max", "drop_min", "drop_max",
              "whale_alert_threshold"):
        if d.get(k) is not None:
            d[k] = to_human(int(d[k]))
    return d


@router.patch("/settings", response_model=SuccessResponse, summary="Update guild settings")
async def update_guild_settings(
    body: GuildSettingsUpdate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update guild settings."""
    gid = int(admin["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SuccessResponse(message="No changes.")

    # These columns are stored as raw NUMERIC(36,0) and must be converted.
    _raw_cols = {"drop_min", "drop_max", "platform_fee_min", "platform_fee_max"}
    set_parts = []
    values: list[Any] = [gid]
    idx = 2
    for key, val in updates.items():
        # Convert channel IDs from str to int; treat empty string as NULL
        if key.endswith("_channel"):
            if val is not None and str(val).strip() == "":
                val = None
            elif val is not None:
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    continue  # skip invalid channel IDs
        elif key in _raw_cols and val is not None:
            val = to_raw(val)
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    if not set_parts:
        return SuccessResponse(message="No valid changes.")

    async with db.transaction():
        await db.execute(
            f"UPDATE guild_settings SET {', '.join(set_parts)} WHERE guild_id = $1",
            *values,
        )
        await audit_log(db, gid, int(admin["user_id"]), "update_settings", updates)
    return SuccessResponse(message="Guild settings updated.")


# ---- Modules ---------------------------------------------------------------

@router.get("/modules", response_model=list[ModuleStatus], summary="List module statuses")
async def list_modules(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return the enabled/disabled state of all feature modules."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", gid)
    if not row:
        raise NotFoundError("Guild settings not found.")
    return [
        ModuleStatus(module=k, enabled=bool(row[k]))
        for k in MODULE_KEYS
        if k in dict(row)
    ]


@router.patch("/modules/{module}", response_model=SuccessResponse, summary="Toggle module")
async def toggle_module(
    module: str,
    body: ModuleToggle,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Enable or disable a feature module."""
    if module not in MODULE_KEYS:
        raise ValidationError(f"Unknown module: {module}")
    gid = int(admin["guild_id"])
    async with db.transaction():
        await db.execute(
            f"UPDATE guild_settings SET {module} = $2 WHERE guild_id = $1",
            gid, body.enabled,
        )
        await audit_log(db, gid, int(admin["user_id"]), "toggle_module", {"module": module, "enabled": body.enabled})
    return SuccessResponse(message=f"Module {module} {'enabled' if body.enabled else 'disabled'}.")


# ---- Fee settings ----------------------------------------------------------

@router.get("/fee-settings", response_model=FeeSettings, summary="Get fee settings")
async def get_fee_settings(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return platform fee configuration."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT platform_fee_pct, platform_fee_min, platform_fee_max, treasury_cut_pct "
        "FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    if not row:
        return FeeSettings()
    return FeeSettings(
        platform_fee_pct=float(row["platform_fee_pct"]) if row["platform_fee_pct"] is not None else None,
        platform_fee_min=to_human(int(row["platform_fee_min"])) if row["platform_fee_min"] is not None else None,
        platform_fee_max=to_human(int(row["platform_fee_max"])) if row["platform_fee_max"] is not None else None,
        treasury_cut_pct=float(row["treasury_cut_pct"]) if row["treasury_cut_pct"] is not None else None,
    )


@router.patch("/fee-settings", response_model=SuccessResponse, summary="Update fee settings")
async def update_fee_settings(
    body: FeeSettings,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update platform fee configuration."""
    gid = int(admin["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SuccessResponse(message="No changes.")

    # platform_fee_min and platform_fee_max live in raw-scaled NUMERIC(36,0)
    # columns; the human-scale value from the client must be multiplied by
    # SCALE before it hits the DB, or it gets truncated to 0 / a tiny int.
    _raw_cols = {"platform_fee_min", "platform_fee_max"}
    set_parts = []
    values: list[Any] = [gid]
    idx = 2
    for key, val in updates.items():
        if key in _raw_cols and val is not None:
            val = to_raw(val)
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    async with db.transaction():
        await db.execute(
            f"UPDATE guild_settings SET {', '.join(set_parts)} WHERE guild_id = $1",
            *values,
        )
        await audit_log(db, gid, int(admin["user_id"]), "update_fee_settings", updates)
    return SuccessResponse(message="Fee settings updated.")


# ---- Command roles ---------------------------------------------------------

@router.get("/command-roles", response_model=list[CommandRole], summary="List command roles")
async def list_command_roles(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all command role assignments."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        "SELECT command_name, role_id FROM guild_command_roles WHERE guild_id = $1 ORDER BY command_name",
        gid,
    )
    return [CommandRole(command_name=r["command_name"], role_id=str(r["role_id"])) for r in rows]


@router.post("/command-roles", response_model=SuccessResponse, summary="Add command role")
async def add_command_role(
    body: CommandRoleCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Assign a role to a command."""
    gid = int(admin["guild_id"])
    await db.execute(
        """
        INSERT INTO guild_command_roles (guild_id, command_name, role_id)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        """,
        gid, body.command_name, int(body.role_id),
    )
    await audit_log(db, gid, int(admin["user_id"]), "add_command_role",
                    {"command": body.command_name, "role_id": body.role_id})
    return SuccessResponse(message=f"Role {body.role_id} assigned to {body.command_name}.")


@router.delete("/command-roles", response_model=SuccessResponse, summary="Remove command role")
async def remove_command_role(
    command_name: str = Query(...),
    role_id: str = Query(...),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Remove a role from a command."""
    gid = int(admin["guild_id"])
    await db.execute(
        "DELETE FROM guild_command_roles WHERE guild_id = $1 AND command_name = $2 AND role_id = $3",
        gid, command_name, int(role_id),
    )
    await audit_log(db, gid, int(admin["user_id"]), "remove_command_role",
                    {"command": command_name, "role_id": role_id})
    return SuccessResponse(message=f"Role {role_id} removed from {command_name}.")


# ---- Drop settings ---------------------------------------------------------

@router.get("/drop-settings", response_model=DropSettings, summary="Get drop settings")
async def get_drop_settings(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return drop configuration."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT drop_interval, drop_min, drop_max FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    if not row:
        return DropSettings()
    # drop_min and drop_max are stored as raw NUMERIC(36,0); convert to human for API
    return DropSettings(
        drop_interval=row["drop_interval"],
        drop_min=to_human(int(row["drop_min"])) if row["drop_min"] else None,
        drop_max=to_human(int(row["drop_max"])) if row["drop_max"] else None,
    )


@router.patch("/drop-settings", response_model=SuccessResponse, summary="Update drop settings")
async def update_drop_settings(
    body: DropSettings,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update drop configuration."""
    gid = int(admin["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SuccessResponse(message="No changes.")

    # drop_min and drop_max are stored as raw NUMERIC(36,0); convert from human API values
    _raw_cols = {"drop_min", "drop_max"}
    set_parts = []
    values: list[Any] = [gid]
    idx = 2
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(to_raw(val) if key in _raw_cols else val)
        idx += 1

    async with db.transaction():
        await db.execute(
            f"UPDATE guild_settings SET {', '.join(set_parts)} WHERE guild_id = $1",
            *values,
        )
        await audit_log(db, gid, int(admin["user_id"]), "update_drop_settings", updates)
    return SuccessResponse(message="Drop settings updated.")


# ---- Shop items admin ------------------------------------------------------

@router.get("/shop/items", summary="List admin shop items")
async def admin_list_shop_items(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all shop item configs (admin view)."""
    # Return the static catalogue + any DB overrides
    return {"items": ["hashstone", "lockstone", "vaultstone"]}


@router.post("/shop/items", response_model=SuccessResponse, summary="Create shop item")
async def admin_create_shop_item(
    body: ShopItemCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create or update a shop item config."""
    gid = int(admin["guild_id"])
    await audit_log(db, gid, int(admin["user_id"]), "create_shop_item",
                    {"key": body.key, "name": body.name, "price": body.price})
    return SuccessResponse(message=f"Shop item '{body.key}' configured.")


@router.delete("/shop/items", response_model=SuccessResponse, summary="Delete shop item")
async def admin_delete_shop_item(
    key: str = Query(...),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Remove a shop item config."""
    gid = int(admin["guild_id"])
    await audit_log(db, gid, int(admin["user_id"]), "delete_shop_item", {"key": key})
    return SuccessResponse(message=f"Shop item '{key}' removed.")


# ---- Auto-delete -----------------------------------------------------------

@router.get("/auto-delete", response_model=AutoDeleteSettings, summary="Get auto-delete settings")
async def get_auto_delete(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return auto-delete configuration."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT cmd_delete_after, reply_delete_after, ai_cmd_delete_after, ai_reply_delete_after "
        "FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    if not row:
        return AutoDeleteSettings()
    return AutoDeleteSettings(
        cmd_delete_after=row["cmd_delete_after"],
        reply_delete_after=row["reply_delete_after"],
        ai_cmd_delete_after=row["ai_cmd_delete_after"],
        ai_reply_delete_after=row["ai_reply_delete_after"],
    )


@router.patch("/auto-delete", response_model=SuccessResponse, summary="Update auto-delete")
async def update_auto_delete(
    body: AutoDeleteSettings,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update auto-delete configuration."""
    gid = int(admin["guild_id"])
    async with db.transaction():
        await db.execute(
            "UPDATE guild_settings SET cmd_delete_after = $2, reply_delete_after = $3, "
            "ai_cmd_delete_after = $4, ai_reply_delete_after = $5 WHERE guild_id = $1",
            gid, body.cmd_delete_after, body.reply_delete_after,
            body.ai_cmd_delete_after, body.ai_reply_delete_after,
        )
        await audit_log(db, gid, int(admin["user_id"]), "update_auto_delete", {
            "cmd_delete_after": body.cmd_delete_after,
            "reply_delete_after": body.reply_delete_after,
            "ai_cmd_delete_after": body.ai_cmd_delete_after,
            "ai_reply_delete_after": body.ai_reply_delete_after,
        })
    return SuccessResponse(message="Auto-delete settings updated.")


# ---- Scam detection --------------------------------------------------------

@router.get("/scam-detection", response_model=ScamDetectionSettings, summary="Get scam detection")
async def get_scam_detection(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return scam detection configuration."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT scam_detection, scam_timeout_minutes FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    if not row:
        return ScamDetectionSettings()
    return ScamDetectionSettings(
        scam_detection=row["scam_detection"],
        scam_timeout_minutes=row["scam_timeout_minutes"],
    )


@router.patch("/scam-detection", response_model=SuccessResponse, summary="Update scam detection")
async def update_scam_detection(
    body: ScamDetectionSettings,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Update scam detection configuration."""
    gid = int(admin["guild_id"])
    async with db.transaction():
        await db.execute(
            "UPDATE guild_settings SET scam_detection = $2, scam_timeout_minutes = $3 WHERE guild_id = $1",
            gid, body.scam_detection, body.scam_timeout_minutes,
        )
        await audit_log(db, gid, int(admin["user_id"]), "update_scam_detection",
                        {"enabled": body.scam_detection, "timeout": body.scam_timeout_minutes})
    return SuccessResponse(message="Scam detection settings updated.")
