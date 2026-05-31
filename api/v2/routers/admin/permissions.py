"""Admin permissions controller  -  manage user permissions, roles, security exemptions,
and per-guild bot manager auto-exempt configuration from the dashboard."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.routers.admin._helpers import audit_log
from api.v2.schemas.common import SuccessResponse

router = APIRouter()


# ── Request / Response Models ────────────────────────────────────────────────

class PermissionGrant(BaseModel):
    """Grant or revoke a permission for a user or role."""
    target_type: str = Field(..., description="'user' or 'role'")
    target_id: str = Field(..., description="Discord user ID or role ID")
    permission: str = Field(..., description="Permission key to grant")
    granted: bool = Field(True, description="True to grant, False to revoke")


class PermissionEntry(BaseModel):
    """A single permission assignment."""
    id: int | None = None
    target_type: str
    target_id: str
    permission: str
    granted_by: str | None = None
    created_at: str | None = None


class AdminRoleAssignment(BaseModel):
    """Assign or remove admin role for a user."""
    user_id: str = Field(..., description="Discord user ID")
    is_admin: bool = Field(..., description="Grant or revoke admin status")
    notes: str | None = Field(None, description="Optional reason/notes")


class SecurityExemptionRequest(BaseModel):
    """Add or remove a security exemption."""
    target_type: str = Field(..., description="'user' or 'role'")
    target_id: str = Field(..., description="Discord user ID or role ID")
    notes: str | None = Field(None, description="Optional note about the exemption")


class BotManagerConfig(BaseModel):
    """Configuration for the auto-exempt bot manager."""
    bot_manager_id: int | None = Field(None, description="Discord user ID for bot manager")
    auto_exempt: bool = Field(True, description="Whether to auto-exempt from security")
    all_permissions: bool = Field(True, description="Whether to grant all permissions")


class PermissionOverview(BaseModel):
    """Overview of the permissions system state."""
    total_admins: int = 0
    total_exemptions: int = 0
    total_permission_overrides: int = 0
    bot_manager_id: str | None = None
    bot_manager_exempt: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_security_db(request: Request):
    """Get SecurityRepository from app state."""
    db = getattr(request.app.state, "security_db", None)
    if db is None:
        raise HTTPException(503, "Security database not initialized")
    return db


async def _ensure_permissions_table(db) -> None:
    """Create the permission_overrides table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS permission_overrides (
            id          BIGSERIAL PRIMARY KEY,
            guild_id    BIGINT    NOT NULL,
            target_type TEXT      NOT NULL CHECK (target_type IN ('user', 'role')),
            target_id   BIGINT    NOT NULL,
            permission  TEXT      NOT NULL,
            granted_by  BIGINT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (guild_id, target_type, target_id, permission)
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_perm_overrides_guild
            ON permission_overrides (guild_id)
    """)


async def _ensure_admin_users_table(db) -> None:
    """Create the admin_users table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id          BIGSERIAL PRIMARY KEY,
            guild_id    BIGINT    NOT NULL,
            user_id     BIGINT    NOT NULL,
            granted_by  BIGINT,
            notes       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (guild_id, user_id)
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_admin_users_guild
            ON admin_users (guild_id)
    """)


# ── Overview ─────────────────────────────────────────────────────────────────

@router.get("/permissions/overview", summary="Permissions system overview")
async def permissions_overview(
    request: Request,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Get an overview of the permissions system: admin count, exemptions,
    overrides, and report user status."""
    gid = int(admin["guild_id"])

    await _ensure_admin_users_table(db)
    await _ensure_permissions_table(db)

    admin_count = await db.fetchval(
        "SELECT COUNT(*) FROM admin_users WHERE guild_id = $1", gid,
    ) or 0

    exempt_count = 0
    sec_db = getattr(request.app.state, "security_db", None)
    if sec_db:
        exemptions = await sec_db.get_exemptions(gid)
        exempt_count = len(exemptions)

    override_count = await db.fetchval(
        "SELECT COUNT(*) FROM permission_overrides WHERE guild_id = $1", gid,
    ) or 0

    # Check bot manager config
    row = await db.fetchrow(
        "SELECT bot_manager_id, bot_manager_auto_exempt FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    bot_manager_id = None
    bot_manager_exempt = False
    if row:
        rid = row.get("bot_manager_id")
        if rid:
            bot_manager_id = str(rid)
        bot_manager_exempt = bool(row.get("bot_manager_auto_exempt", False))

    return PermissionOverview(
        total_admins=admin_count,
        total_exemptions=exempt_count,
        total_permission_overrides=override_count,
        bot_manager_id=bot_manager_id,
        bot_manager_exempt=bot_manager_exempt,
    )


# ── Admin User Management ───────────────────────────────────────────────────

@router.get("/permissions/admins", summary="List admin users")
async def list_admin_users(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """List all users with admin privileges for this guild."""
    gid = int(admin["guild_id"])
    await _ensure_admin_users_table(db)
    rows = await db.fetch(
        "SELECT id, user_id, granted_by, notes, created_at FROM admin_users WHERE guild_id = $1 ORDER BY created_at",
        gid,
    )
    return {
        "admins": [
            {
                "id": r["id"],
                "user_id": str(r["user_id"]),
                "granted_by": str(r["granted_by"]) if r["granted_by"] else None,
                "notes": r["notes"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/permissions/admins", response_model=SuccessResponse, summary="Add or remove admin user")
async def set_admin_user(
    body: AdminRoleAssignment,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Grant or revoke admin status for a user."""
    gid = int(admin["guild_id"])
    target_uid = int(body.user_id)
    await _ensure_admin_users_table(db)

    if body.is_admin:
        await db.execute(
            """INSERT INTO admin_users (guild_id, user_id, granted_by, notes)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, user_id) DO UPDATE SET granted_by = $3, notes = $4""",
            gid, target_uid, int(admin["user_id"]), body.notes,
        )
        await audit_log(db, gid, int(admin["user_id"]), "grant_admin",
                        {"target_user_id": body.user_id, "notes": body.notes})
        return SuccessResponse(message=f"Admin granted to user {body.user_id}.")
    else:
        await db.execute(
            "DELETE FROM admin_users WHERE guild_id = $1 AND user_id = $2",
            gid, target_uid,
        )
        await audit_log(db, gid, int(admin["user_id"]), "revoke_admin",
                        {"target_user_id": body.user_id})
        return SuccessResponse(message=f"Admin revoked from user {body.user_id}.")


@router.delete("/permissions/admins/{user_id}", response_model=SuccessResponse, summary="Remove admin user")
async def remove_admin_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Remove admin status from a user."""
    gid = int(admin["guild_id"])
    await _ensure_admin_users_table(db)
    await db.execute(
        "DELETE FROM admin_users WHERE guild_id = $1 AND user_id = $2",
        gid, int(user_id),
    )
    await audit_log(db, gid, int(admin["user_id"]), "revoke_admin",
                    {"target_user_id": user_id})
    return SuccessResponse(message=f"Admin revoked from user {user_id}.")


# ── Permission Overrides ────────────────────────────────────────────────────

@router.get("/permissions/overrides", summary="List permission overrides")
async def list_permission_overrides(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """List all per-user and per-role permission overrides."""
    gid = int(admin["guild_id"])
    await _ensure_permissions_table(db)
    rows = await db.fetch(
        """SELECT id, target_type, target_id, permission, granted_by, created_at
           FROM permission_overrides
           WHERE guild_id = $1
           ORDER BY created_at DESC""",
        gid,
    )
    return {
        "overrides": [
            {
                "id": r["id"],
                "target_type": r["target_type"],
                "target_id": str(r["target_id"]),
                "permission": r["permission"],
                "granted_by": str(r["granted_by"]) if r["granted_by"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/permissions/overrides", response_model=SuccessResponse, summary="Set permission override")
async def set_permission_override(
    body: PermissionGrant,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Grant or revoke a specific permission for a user or role."""
    gid = int(admin["guild_id"])
    await _ensure_permissions_table(db)

    if body.target_type not in ("user", "role"):
        raise ValidationError("target_type must be 'user' or 'role'")

    target = int(body.target_id)

    if body.granted:
        await db.execute(
            """INSERT INTO permission_overrides (guild_id, target_type, target_id, permission, granted_by)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (guild_id, target_type, target_id, permission) DO NOTHING""",
            gid, body.target_type, target, body.permission, int(admin["user_id"]),
        )
        await audit_log(db, gid, int(admin["user_id"]), "grant_permission", {
            "target_type": body.target_type,
            "target_id": body.target_id,
            "permission": body.permission,
        })
        return SuccessResponse(message=f"Permission '{body.permission}' granted to {body.target_type} {body.target_id}.")
    else:
        await db.execute(
            """DELETE FROM permission_overrides
               WHERE guild_id = $1 AND target_type = $2 AND target_id = $3 AND permission = $4""",
            gid, body.target_type, target, body.permission,
        )
        await audit_log(db, gid, int(admin["user_id"]), "revoke_permission", {
            "target_type": body.target_type,
            "target_id": body.target_id,
            "permission": body.permission,
        })
        return SuccessResponse(message=f"Permission '{body.permission}' revoked from {body.target_type} {body.target_id}.")


@router.delete("/permissions/overrides/{override_id}", response_model=SuccessResponse, summary="Delete permission override")
async def delete_permission_override(
    override_id: int,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a specific permission override by ID."""
    gid = int(admin["guild_id"])
    await _ensure_permissions_table(db)
    result = await db.execute(
        "DELETE FROM permission_overrides WHERE id = $1 AND guild_id = $2",
        override_id, gid,
    )
    await audit_log(db, gid, int(admin["user_id"]), "delete_permission_override", {
        "override_id": override_id,
    })
    return SuccessResponse(message=f"Permission override #{override_id} deleted.")


# ── Security Exemptions (admin-friendly wrapper) ────────────────────────────

@router.get("/permissions/exemptions", summary="List security exemptions")
async def list_security_exemptions(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """List all security-exempt users and roles for this guild."""
    gid = int(admin["guild_id"])
    sec_db = _get_security_db(request)
    exemptions = await sec_db.get_exemptions(gid)
    return {
        "exemptions": [
            {
                "id": e.get("id"),
                "target_type": e.get("target_type"),
                "target_id": str(e.get("target_id")),
                "granted_by": str(e.get("granted_by")) if e.get("granted_by") else None,
                "notes": e.get("notes"),
                "created_at": e["created_at"].isoformat() if e.get("created_at") else None,
            }
            for e in exemptions
        ],
    }


@router.post("/permissions/exemptions", response_model=SuccessResponse, summary="Add security exemption")
async def add_security_exemption(
    body: SecurityExemptionRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Add a user or role to the security exemption list."""
    gid = int(admin["guild_id"])
    if body.target_type not in ("user", "role"):
        raise ValidationError("target_type must be 'user' or 'role'")

    sec_db = _get_security_db(request)
    exemption_id = await sec_db.add_exempt(
        gid, body.target_type, int(body.target_id), int(admin["user_id"]), body.notes,
    )
    return SuccessResponse(
        message=f"{body.target_type.capitalize()} {body.target_id} added to security exemptions (ID: {exemption_id}).",
    )


@router.delete("/permissions/exemptions/{target_type}/{target_id}", response_model=SuccessResponse,
               summary="Remove security exemption")
async def remove_security_exemption(
    target_type: str,
    target_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Remove a user or role from the security exemption list."""
    gid = int(admin["guild_id"])
    if target_type not in ("user", "role"):
        raise ValidationError("target_type must be 'user' or 'role'")

    sec_db = _get_security_db(request)
    removed = await sec_db.remove_exempt(gid, target_type, int(target_id))
    if not removed:
        raise NotFoundError("Exemption not found")
    return SuccessResponse(message=f"{target_type.capitalize()} {target_id} removed from security exemptions.")


# ── Bot Manager Auto-Exempt ─────────────────────────────────────────────────

@router.get("/permissions/bot-manager", summary="Get bot manager config")
async def get_bot_manager_config(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Get the bot manager user configuration (auto-exempt + all-perms)."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        """SELECT bot_manager_id, bot_manager_auto_exempt, bot_manager_all_perms
           FROM guild_settings WHERE guild_id = $1""",
        gid,
    )
    if not row:
        return BotManagerConfig()

    return BotManagerConfig(
        bot_manager_id=row["bot_manager_id"] if row.get("bot_manager_id") else None,
        auto_exempt=bool(row.get("bot_manager_auto_exempt", True)),
        all_permissions=bool(row.get("bot_manager_all_perms", True)),
    )


@router.patch("/permissions/bot-manager", response_model=SuccessResponse, summary="Update bot manager config")
async def update_bot_manager_config(
    body: BotManagerConfig,
    request: Request,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Set the bot manager user ID and configure auto-exemption and all-permissions.

    When auto_exempt is True, this user automatically bypasses security enforcement.
    When all_permissions is True, this user is treated as having all permissions.
    """
    gid = int(admin["guild_id"])
    bot_mgr_uid = body.bot_manager_id  # already int | None due to Pydantic model

    # Fetch the current config so we can clean up any stale exemption
    prev_row = await db.fetchrow(
        "SELECT bot_manager_id, bot_manager_auto_exempt FROM guild_settings WHERE guild_id = $1",
        gid,
    )
    prev_bot_mgr_uid = prev_row["bot_manager_id"] if prev_row else None
    prev_auto_exempt = bool(prev_row["bot_manager_auto_exempt"]) if prev_row else False

    await db.execute(
        """UPDATE guild_settings
           SET bot_manager_id = $2,
               bot_manager_auto_exempt = $3,
               bot_manager_all_perms = $4
           WHERE guild_id = $1""",
        gid, bot_mgr_uid, body.auto_exempt, body.all_permissions,
    )

    sec_db = getattr(request.app.state, "security_db", None)
    if sec_db:
        # Remove the old auto-exempt entry if it was enabled and the user changed or auto_exempt was turned off
        if prev_bot_mgr_uid and prev_auto_exempt and (not body.auto_exempt or prev_bot_mgr_uid != bot_mgr_uid):
            await sec_db.remove_exempt(gid, "user", prev_bot_mgr_uid)

        # Add the new auto-exempt entry when enabled
        if body.auto_exempt and bot_mgr_uid:
            await sec_db.add_exempt(
                gid, "user", bot_mgr_uid, int(admin["user_id"]),
                "Auto-exempt: bot manager (all permissions)",
            )

    await audit_log(db, gid, int(admin["user_id"]), "update_bot_manager", {
        "bot_manager_id": body.bot_manager_id,
        "auto_exempt": body.auto_exempt,
        "all_permissions": body.all_permissions,
    })
    return SuccessResponse(message="Bot manager configuration updated.")


# ── Audit Log (permissions-specific) ────────────────────────────────────────

@router.get("/permissions/audit", summary="Permissions audit log")
async def permissions_audit_log(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get the audit log filtered to permission-related actions."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        """SELECT id, admin_user_id, action, details, created_at
           FROM audit_log
           WHERE guild_id = $1
             AND action IN (
                 'grant_admin', 'revoke_admin',
                 'grant_permission', 'revoke_permission', 'delete_permission_override',
                 'update_bot_manager'
             )
           ORDER BY created_at DESC
           LIMIT $2 OFFSET $3""",
        gid, limit, offset,
    )
    return {
        "entries": [
            {
                "id": r["id"],
                "admin_user_id": str(r["admin_user_id"]),
                "action": r["action"],
                "details": json.loads(r["details"]) if r["details"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }
