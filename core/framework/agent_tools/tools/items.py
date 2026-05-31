"""
core/framework/agent_tools/tools/items.py -- item inventory read tool.

    items.inventory   aggregate stones + consumables the caller owns, with
                      levels, XP, staked amounts, and consumable stacks.

READ-only. Uses the existing ``db.get_hashstone/lockstone/vaultstone/liqstone``
getters and the consumable ``validator_guard_inventory``/``yield_guard_inventory``
tables. Returns one unified payload so the AI never needs to fire four
separate tool calls to answer "what do I have?".
"""
from __future__ import annotations

import logging

from core.config import Config

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.items")


_STONE_GETTERS: list[tuple[str, str]] = [
    ("hashstone",  "get_hashstone"),
    ("lockstone",  "get_lockstone"),
    ("vaultstone", "get_vaultstone"),
    ("liqstone",   "get_liqstone"),
]


@tool(
    name="items.inventory",
    summary=(
        "Return the caller's full item inventory: owned stones with level/"
        "XP/staked amount, plus consumable stacks (validator guards, yield "
        "guards). Use this instead of four separate stone tool calls."
    ),
    risk=RiskLevel.READ,
    category="items",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def inventory(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    stones: list[dict] = []
    for key, getter_name in _STONE_GETTERS:
        getter = getattr(ctx.db, getter_name, None)
        if getter is None:
            continue
        try:
            row = await getter(target, gid)
        except Exception as exc:
            log.warning("[items.inventory] %s failed: %s", getter_name, exc)
            row = None
        if not row:
            continue
        item_cfg = Config.SHOP_ITEMS.get(key, {})
        stones.append({
            "key":           key,
            "name":          str(item_cfg.get("name") or key),
            "emoji":         str(item_cfg.get("emoji") or ""),
            "level":         int(row.get("level") or 0),
            "xp":            float(row.get("xp") or 0.0),
            "staked_amount": row.h("staked_amount"),
            "max_level":     int(item_cfg.get("max_level", 0) or 0),
            "stats":         dict(item_cfg.get("stats") or {}),
        })

    consumables: list[dict] = []
    for key, counter in (
        ("validator_guard", "get_validator_guard_count"),
        ("yield_guard",     "get_yield_guard_count"),
    ):
        get_fn = getattr(ctx.db, counter, None)
        if get_fn is None:
            continue
        try:
            count = int(await get_fn(target, gid) or 0)
        except Exception as exc:
            log.warning("[items.inventory] %s failed: %s", counter, exc)
            continue
        if count <= 0:
            continue
        item_cfg = Config.SHOP_ITEMS.get(key, {})
        consumables.append({
            "key":     key,
            "name":    str(item_cfg.get("name") or key),
            "emoji":   str(item_cfg.get("emoji") or ""),
            "count":   count,
            "max_stack": int(item_cfg.get("max_stack", 0) or 0),
        })

    return ToolResult.success({
        "target_id":       target,
        "stone_count":     len(stones),
        "stones":          stones,
        "consumable_count": len(consumables),
        "consumables":     consumables,
    })
