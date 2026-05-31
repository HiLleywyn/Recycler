"""
core/framework/agent_tools/tools/groups.py -- mining group read tool.

    groups.summary   caller's current mining group (if any) with member
                     roster, founder, reserve balance, and hall upgrades.

READ-only. Uses ``db.get_user_mining_group``, ``db.get_group_members``,
and ``db.get_group_hall_upgrades`` (when available).
"""
from __future__ import annotations

import logging

from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.groups")


@tool(
    name="groups.summary",
    summary=(
        "Return the caller's current mining group (if any). Includes "
        "founder, member roster with join timestamps, reserve balance, "
        "and any purchased hall upgrades."
    ),
    risk=RiskLevel.READ,
    category="groups",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def summary(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    try:
        group = await ctx.db.get_user_mining_group(target, gid)
    except Exception as exc:
        log.warning("[groups.summary] read failed: %s", exc)
        return ToolResult.fail(f"group_read_failed: {exc}")

    if not group:
        return ToolResult.success({
            "target_id": target,
            "has_group": False,
        })

    group_id = str(group.get("group_id") or "")
    name     = str(group.get("name") or "")
    founder  = int(group.get("founder_id") or 0)
    reserve_raw = int(group.get("reserve") or group.get("treasury") or 0)
    reserve  = to_human(reserve_raw) if reserve_raw else 0.0
    max_members = int(group.get("max_members") or 0)

    members: list[dict] = []
    get_members = getattr(ctx.db, "get_group_members", None)
    if get_members is not None:
        try:
            rows = await get_members(gid, group_id)
            for r in rows or []:
                members.append({
                    "user_id":  int(r.get("user_id") or 0),
                    "joined_at": r.get("joined_at"),
                })
        except Exception as exc:
            log.warning("[groups.summary] members read failed: %s", exc)

    upgrades: list[str] = []
    get_upgrades = getattr(ctx.db, "get_group_hall_upgrades", None)
    if get_upgrades is not None:
        try:
            rows = await get_upgrades(gid, group_id)
            upgrades = [str(r.get("upgrade_key") or "") for r in rows or []]
        except Exception as exc:
            log.warning("[groups.summary] upgrades read failed: %s", exc)

    return ToolResult.success({
        "target_id":    target,
        "has_group":    True,
        "group_id":     group_id,
        "name":         name,
        "founder_id":   founder,
        "reserve_usd":  round(reserve, 2),
        "member_count": len(members),
        "max_members":  max_members,
        "members":      members,
        "upgrades":     upgrades,
    })
