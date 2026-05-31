"""Groups router -- mining group management endpoints for Discoin v2."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends

from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import (
    ForbiddenError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from core.framework.scale import to_human
from api.v2.schemas.mining import MinerInfo, MiningGroupDetail, MiningGroupInfo
from core.config import Config

router = APIRouter(prefix="/groups", tags=["groups"], dependencies=[require_module("groups")], redirect_slashes=False)

# ---------------------------------------------------------------------------
# Catalogues  -  sourced from Config to stay in sync with bot
# ---------------------------------------------------------------------------
DEFAULT_RIGS: list[dict] = [
    {"rig_id": rid, "name": r["name"], "hashrate": r["hashrate"], "power": r["power"], "price": r["price"]}
    for rid, r in Config.MINING_RIGS.items()
]

# Hall upgrade catalogue
GROUP_HALL_UPGRADES: dict[str, dict] = Config.GROUP_HALL_UPGRADES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_user_hashrate(conn: asyncpg.Connection, user_id: int, guild_id: int) -> float:
    """Calculate total hashrate for a user from their rigs."""
    rows = await conn.fetch(
        "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
        user_id, guild_id,
    )
    total = 0.0
    for r in rows:
        rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == r["rig_id"]), None)
        if rig_def:
            total += rig_def["hashrate"] * r["quantity"]
    return total


async def _get_group_or_404(conn: asyncpg.Connection, group_id: str, guild_id: int) -> dict:
    """Fetch a mining group or raise NotFoundError."""
    row = await conn.fetchrow(
        "SELECT * FROM mining_groups WHERE group_id = $1 AND guild_id = $2",
        group_id, guild_id,
    )
    if not row:
        raise NotFoundError("Mining group not found.")
    return dict(row)


async def _require_founder(group: dict, user_id: int) -> None:
    """Raise ForbiddenError if user is not the group founder."""
    if group["founder_id"] != user_id:
        raise ForbiddenError("Only the group founder can perform this action.")


# ---------------------------------------------------------------------------
# 1. GET /groups  -  list all groups for the guild
# ---------------------------------------------------------------------------

@router.get("", response_model=list[MiningGroupInfo], summary="List all mining groups")
async def list_groups(
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all mining groups for the authenticated user's guild."""
    guild_id = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT mg.group_id, mg.name, mg.description, mg.tag, mg.founder_id,
                  COUNT(mgm.user_id) as member_count
           FROM mining_groups mg
           LEFT JOIN mining_group_members mgm ON mgm.group_id = mg.group_id AND mgm.guild_id = mg.guild_id
           WHERE mg.guild_id = $1
           GROUP BY mg.group_id, mg.name, mg.description, mg.tag, mg.founder_id
           ORDER BY member_count DESC""",
        guild_id,
    )

    results = []
    for r in rows:
        member_rows = await conn.fetch(
            """SELECT mr.rig_id, SUM(mr.quantity) as total_qty
               FROM mining_rigs mr
               JOIN mining_group_members mgm ON mgm.user_id = mr.user_id AND mgm.guild_id = mr.guild_id
               WHERE mgm.group_id = $1 AND mgm.guild_id = $2
               GROUP BY mr.rig_id""",
            r["group_id"], guild_id,
        )
        total_hashrate = 0.0
        for mr in member_rows:
            rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == mr["rig_id"]), None)
            if rig_def:
                total_hashrate += rig_def["hashrate"] * mr["total_qty"]

        results.append(MiningGroupInfo(
            group_id=r["group_id"],
            name=r["name"],
            description=r["description"] or "",
            tag=r["tag"] or "",
            founder_id=r["founder_id"],
            member_count=r["member_count"],
            total_hashrate=total_hashrate,
        ))
    return results


# ---------------------------------------------------------------------------
# 2. GET /groups/{group_id}  -  group details + members
# ---------------------------------------------------------------------------

@router.get("/{group_id}", response_model=MiningGroupDetail, summary="Group details")
async def get_group(
    group_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get detailed information about a mining group including member list."""
    guild_id = int(user["guild_id"])
    group = await _get_group_or_404(conn, group_id, guild_id)

    members_rows = await conn.fetch(
        """SELECT mgm.user_id, COALESCE(u.username, '') as username
           FROM mining_group_members mgm
           LEFT JOIN users u ON u.user_id = mgm.user_id AND u.guild_id = mgm.guild_id
           WHERE mgm.group_id = $1 AND mgm.guild_id = $2
           ORDER BY mgm.joined_at""",
        group_id, guild_id,
    )

    members = []
    total_hashrate = 0.0
    for mr in members_rows:
        rig_rows = await conn.fetch(
            "SELECT rig_id, quantity FROM mining_rigs WHERE user_id = $1 AND guild_id = $2 AND quantity > 0",
            mr["user_id"], guild_id,
        )
        user_hashrate = 0.0
        rig_count = 0
        for rr in rig_rows:
            rig_def = next((d for d in DEFAULT_RIGS if d["rig_id"] == rr["rig_id"]), None)
            if rig_def:
                user_hashrate += rig_def["hashrate"] * rr["quantity"]
            rig_count += rr["quantity"]

        blocks = await conn.fetchval(
            "SELECT COUNT(*) FROM mining_blocks WHERE guild_id = $1 AND miner_id = $2",
            guild_id, mr["user_id"],
        )

        total_hashrate += user_hashrate
        uname = mr["username"] or f"User {str(mr['user_id'])[:8]}"
        members.append(MinerInfo(
            user_id=mr["user_id"],
            username=uname,
            total_hashrate=user_hashrate,
            rig_count=rig_count,
            blocks_mined=blocks or 0,
        ))

    return MiningGroupDetail(
        group_id=group["group_id"],
        name=group["name"],
        description=group.get("description") or "",
        tag=group.get("tag") or "",
        founder_id=group["founder_id"],
        member_count=len(members),
        total_hashrate=total_hashrate,
        members=members,
    )


# ---------------------------------------------------------------------------
# 3. POST /groups  -  create a new group
# ---------------------------------------------------------------------------

@router.post("", summary="Create a mining group")
async def create_group(
    body: dict,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Create a new mining group. The authenticated user becomes the founder."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    name = body.get("name", "").strip()
    private = body.get("private", False)

    if not name or len(name) > 32:
        raise ValidationError("Group name must be 1-32 characters.")

    # Check user not already in a group
    existing = await conn.fetchrow(
        "SELECT group_id FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    if existing:
        raise ValidationError("You are already in a mining group. Leave it first.")

    # Check name collision
    collision = await conn.fetchrow(
        "SELECT group_id FROM mining_groups WHERE guild_id = $1 AND LOWER(name) = LOWER($2)",
        guild_id, name,
    )
    if collision:
        raise ValidationError(f"A group named '{name}' already exists.")

    group_id = secrets.token_hex(4).upper()
    now_dt = datetime.now(timezone.utc)
    is_public = 0 if private else 1

    async with conn.transaction():
        await conn.execute(
            """INSERT INTO mining_groups (group_id, guild_id, name, founder_id, created_at, is_public)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            group_id, guild_id, name, user_id, now_dt, is_public,
        )
        # Auto-join founder
        await conn.execute(
            """INSERT INTO mining_group_members (user_id, guild_id, group_id, joined_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(user_id, guild_id)
               DO UPDATE SET group_id = EXCLUDED.group_id, joined_at = EXCLUDED.joined_at""",
            user_id, guild_id, group_id, now_dt,
        )

    return {
        "success": True,
        "group_id": group_id,
        "name": name,
        "founder_id": user_id,
        "private": private,
    }


# ---------------------------------------------------------------------------
# 4. POST /groups/{group_id}/join  -  join a group
# ---------------------------------------------------------------------------

@router.post("/{group_id}/join", summary="Join a mining group")
async def join_group(
    group_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Join a public mining group (or one you have an invite for)."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    group = await _get_group_or_404(conn, group_id, guild_id)

    # Check user not already in a group
    existing = await conn.fetchrow(
        "SELECT group_id FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    if existing:
        raise ValidationError("You are already in a mining group. Leave it first.")

    # Check privacy
    is_public = group.get("is_public", 1)
    if not is_public:
        invite = await conn.fetchrow(
            "SELECT 1 FROM group_invites WHERE guild_id = $1 AND group_id = $2 AND invitee_id = $3",
            guild_id, group_id, user_id,
        )
        if not invite:
            raise ForbiddenError("This group is invite-only.")
        # Consume invite
        await conn.execute(
            "DELETE FROM group_invites WHERE guild_id = $1 AND group_id = $2 AND invitee_id = $3",
            guild_id, group_id, user_id,
        )

    now_dt = datetime.now(timezone.utc)
    await conn.execute(
        """INSERT INTO mining_group_members (user_id, guild_id, group_id, joined_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT(user_id, guild_id)
           DO UPDATE SET group_id = EXCLUDED.group_id, joined_at = EXCLUDED.joined_at""",
        user_id, guild_id, group_id, now_dt,
    )

    return {"success": True, "message": f"Joined group '{group['name']}'."}


# ---------------------------------------------------------------------------
# 5. POST /groups/{group_id}/leave  -  leave a group
# ---------------------------------------------------------------------------

@router.post("/{group_id}/leave", summary="Leave a mining group")
async def leave_group(
    group_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Leave the specified mining group. Founders must disband instead."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    group = await _get_group_or_404(conn, group_id, guild_id)

    # Check membership
    membership = await conn.fetchrow(
        "SELECT group_id FROM mining_group_members WHERE user_id = $1 AND guild_id = $2 AND group_id = $3",
        user_id, guild_id, group_id,
    )
    if not membership:
        raise ValidationError("You are not a member of this group.")

    if group["founder_id"] == user_id:
        raise ValidationError("Founders cannot leave. Use disband instead.")

    await conn.execute(
        "DELETE FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )

    return {"success": True, "message": f"Left group '{group['name']}'."}


# ---------------------------------------------------------------------------
# 6. PUT /groups/{group_id}  -  update group settings (founder only)
# ---------------------------------------------------------------------------

@router.put("/{group_id}", summary="Update group settings")
async def update_group(
    group_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Update group settings. Founder only. Allowed keys: description, tag, image_url, weight_mode, is_public."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    group = await _get_group_or_404(conn, group_id, guild_id)
    await _require_founder(group, user_id)

    ALLOWED_KEYS = {"description", "tag", "image_url", "weight_mode", "is_public", "reserve_pct"}
    updated = []
    for key, val in body.items():
        if key not in ALLOWED_KEYS:
            continue
        if key == "tag" and isinstance(val, str) and len(val) > 5:
            raise ValidationError("Tag must be 5 characters or fewer.")
        if key == "description" and isinstance(val, str) and len(val) > 200:
            raise ValidationError("Description must be 200 characters or fewer.")
        if key == "weight_mode" and val not in ("hashrate", "equal", "custom"):
            raise ValidationError("Weight mode must be: hashrate, equal, or custom.")
        if key == "reserve_pct" and (not isinstance(val, (int, float)) or val < 0 or val > 100):
            raise ValidationError("Reserve cut must be between 0 and 100.")
        await conn.execute(
            f"UPDATE mining_groups SET {key} = $1 WHERE guild_id = $2 AND group_id = $3",
            val, guild_id, group_id,
        )
        updated.append(key)

    if not updated:
        raise ValidationError("No valid fields to update.")

    return {"success": True, "updated_fields": updated}


# ---------------------------------------------------------------------------
# 7. POST /groups/{group_id}/kick  -  kick a member (founder only)
# ---------------------------------------------------------------------------

@router.post("/{group_id}/kick", summary="Kick a member from the group")
async def kick_member(
    group_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Kick a member from the mining group. Founder only."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    target_id = body.get("user_id")

    if not target_id:
        raise ValidationError("Provide 'user_id' of the member to kick.")
    target_id = int(target_id)

    group = await _get_group_or_404(conn, group_id, guild_id)
    await _require_founder(group, user_id)

    if target_id == user_id:
        raise ValidationError("You cannot kick yourself. Use disband instead.")

    # Verify target is a member
    membership = await conn.fetchrow(
        "SELECT 1 FROM mining_group_members WHERE user_id = $1 AND guild_id = $2 AND group_id = $3",
        target_id, guild_id, group_id,
    )
    if not membership:
        raise NotFoundError("User is not a member of this group.")

    await conn.execute(
        "DELETE FROM mining_group_members WHERE user_id = $1 AND guild_id = $2",
        target_id, guild_id,
    )

    return {"success": True, "message": f"User {target_id} has been kicked from the group."}


# ---------------------------------------------------------------------------
# 8. GET /groups/{group_id}/upgrades  -  available upgrades
# ---------------------------------------------------------------------------

@router.get("/{group_id}/upgrades", summary="Available group upgrades")
async def list_upgrades(
    group_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all available group upgrades with purchase status."""
    guild_id = int(user["guild_id"])
    await _get_group_or_404(conn, group_id, guild_id)

    # Get purchased upgrades
    purchased_rows = await conn.fetch(
        "SELECT upgrade_id FROM group_upgrades WHERE guild_id = $1 AND group_id = $2",
        guild_id, group_id,
    )
    purchased_ids = {r["upgrade_id"] for r in purchased_rows}

    # Get member count
    member_count = await conn.fetchval(
        "SELECT COUNT(*) FROM mining_group_members WHERE guild_id = $1 AND group_id = $2",
        guild_id, group_id,
    ) or 0

    # Get reserve balance
    group_row = await conn.fetchrow(
        "SELECT reserve_usd FROM mining_groups WHERE guild_id = $1 AND group_id = $2",
        guild_id, group_id,
    )
    reserve_usd = to_human(int(group_row["reserve_usd"])) if group_row and group_row["reserve_usd"] else 0.0

    upgrades = []
    for uid, cfg in GROUP_HALL_UPGRADES.items():
        cost_usd = cfg.get("cost_usd", 0.0)
        purchased = uid in purchased_ids
        can_afford = reserve_usd >= cost_usd
        requires = cfg.get("requires", [])
        prereqs_met = all(r in purchased_ids for r in requires)
        upgrades.append({
            "upgrade_id": uid,
            "name": cfg["name"],
            "description": cfg["description"],
            "emoji": cfg.get("emoji", ""),
            "line": cfg.get("line", ""),
            "cost_usd": cost_usd,
            "tier": cfg.get("tier", 1),
            "requires": requires,
            "effect": cfg.get("effect", {}),
            "purchased": purchased,
            "can_afford": can_afford,
            "prereqs_met": prereqs_met,
        })

    return {"reserve_usd": reserve_usd, "member_count": member_count, "upgrades": upgrades}


# ---------------------------------------------------------------------------
# 9. POST /groups/{group_id}/upgrade  -  purchase an upgrade
# ---------------------------------------------------------------------------

@router.post("/{group_id}/upgrade", summary="Purchase a group upgrade")
async def purchase_upgrade(
    group_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Purchase a group upgrade using the group's SUN reserve. Founder only."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    raw_id = body.get("upgrade_id", "")

    group = await _get_group_or_404(conn, group_id, guild_id)
    await _require_founder(group, user_id)

    # Resolve Hall upgrade by ID
    upgrade_id = raw_id.lower().strip()
    cfg = GROUP_HALL_UPGRADES.get(upgrade_id)
    if not cfg:
        raise NotFoundError(f"Unknown Hall upgrade '{raw_id}'. See GET /{group_id}/upgrades for valid IDs.")

    # Check not already purchased
    existing = await conn.fetchrow(
        "SELECT 1 FROM group_upgrades WHERE guild_id = $1 AND group_id = $2 AND upgrade_id = $3",
        guild_id, group_id, upgrade_id,
    )
    if existing:
        raise ValidationError("This upgrade has already been purchased.")

    # Check tier prerequisites
    requires = cfg.get("requires", [])
    if requires:
        purchased_rows = await conn.fetch(
            "SELECT upgrade_id FROM group_upgrades WHERE guild_id = $1 AND group_id = $2",
            guild_id, group_id,
        )
        purchased_set = {r["upgrade_id"] for r in purchased_rows}
        for req in requires:
            if req not in purchased_set:
                req_name = GROUP_HALL_UPGRADES.get(req, {}).get("name", req)
                raise ValidationError(f"Requires '{req_name}' upgrade first.")

    # Check and deduct reserve
    cost_usd = cfg.get("cost_usd", 0.0)
    async with conn.transaction():
        row = await conn.fetchrow(
            """UPDATE mining_groups SET reserve_usd = reserve_usd - $1
               WHERE guild_id = $2 AND group_id = $3 AND reserve_usd >= $1
               RETURNING reserve_usd""",
            cost_usd, guild_id, group_id,
        )
        if row is None:
            raise InsufficientBalanceError("Insufficient group reserve for this upgrade.")

        now_dt = datetime.now(timezone.utc)
        await conn.execute(
            """INSERT INTO group_upgrades (guild_id, group_id, upgrade_id, purchased_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING""",
            guild_id, group_id, upgrade_id, now_dt,
        )

    return {
        "success": True,
        "upgrade_id": upgrade_id,
        "name": cfg["name"],
        "cost_usd": cost_usd,
        "new_reserve": to_human(int(row["reserve_usd"])),
    }
