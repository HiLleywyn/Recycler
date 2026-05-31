"""
core/framework/agent_tools/tools/alerts.py -- agent-facing alert tools.

Thin wrapper over the trigger engine so the AI can set a price alert or a
market event alert without knowing anything about agent_triggers internals.

One powerful CRUD tool rather than three separate commands:
  alerts.set       create a price-above, price-below, or event alert
  alerts.list      list caller's active alerts
  alerts.cancel    cancel a caller-owned alert by id

All are SAFE: idempotent local writes, no money moves.
"""
from __future__ import annotations

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool
from ..triggers import create_trigger, delete_trigger, list_triggers


@tool(
    name="alerts.set",
    summary=(
        "Create an alert trigger. kind=price_above/price_below: provide symbol "
        "and threshold. kind=event: provide event (or '*' for any). When the "
        "alert fires, the follow-up tool runs via the trigger engine."
    ),
    risk=RiskLevel.SAFE,
    category="alerts",
    params=[
        ParamSpec("kind", "str", choices=["price_above", "price_below", "event"],
                  description="Alert trigger kind."),
        ParamSpec("symbol", "symbol", required=False, default=None,
                  description="Token symbol for price alerts."),
        ParamSpec("threshold", "float", required=False, default=None,
                  description="Price threshold in USD (price alerts)."),
        ParamSpec("event", "str", required=False, default="*",
                  description="Market event name (event alerts)."),
        ParamSpec("follow_up_tool", "str", required=False, default="social.notice",
                  description="Tool to run when the alert fires."),
        ParamSpec("follow_up_args", "json", required=False, default=None,
                  description="JSON object of args to pass to the follow-up tool."),
        ParamSpec("one_shot", "bool", required=False, default=True,
                  description="Fire once then disable?"),
        ParamSpec("name", "str", required=False, default="",
                  description="Optional display name for the alert."),
    ],
)
async def alerts_set(ctx: ToolContext, args: dict) -> ToolResult:
    kind = args["kind"]
    condition: dict = {}

    if kind in ("price_above", "price_below"):
        sym = args.get("symbol")
        thr = args.get("threshold")
        if not sym:
            return ToolResult.fail("price alert requires a symbol")
        if thr is None:
            return ToolResult.fail("price alert requires a threshold")
        condition = {"symbol": sym, "threshold": float(thr)}
    elif kind == "event":
        condition = {"event": args.get("event") or "*"}
    else:
        return ToolResult.fail(f"unsupported alert kind: {kind}")

    follow_up_args = args.get("follow_up_args") or {}
    if not isinstance(follow_up_args, dict):
        return ToolResult.fail("follow_up_args must be a JSON object")

    trigger_id = await create_trigger(
        ctx.db,
        guild_id=int(ctx.guild_id),
        user_id=int(ctx.user_id),
        kind=kind,
        condition=condition,
        tool=args.get("follow_up_tool") or "social.notice",
        args=follow_up_args,
        name=args.get("name") or "",
        one_shot=bool(args.get("one_shot", True)),
    )
    return ToolResult.success({
        "id": trigger_id,
        "kind": kind,
        "condition": condition,
    })


@tool(
    name="alerts.list",
    summary="List the caller's active alerts.",
    risk=RiskLevel.READ,
    category="alerts",
)
async def alerts_list(ctx: ToolContext, args: dict) -> ToolResult:
    rows = await list_triggers(ctx.db, guild_id=int(ctx.guild_id), user_id=int(ctx.user_id))
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "kind": str(r["kind"]),
            "name": str(r.get("name") or ""),
            "enabled": bool(r.get("enabled")),
            "one_shot": bool(r.get("one_shot")),
            "fire_count": int(r.get("fire_count") or 0),
            "condition": r.get("condition") or {},
        })
    return ToolResult.success({"alerts": out, "count": len(out)})


@tool(
    name="alerts.cancel",
    summary="Cancel (delete) a caller-owned alert by id.",
    risk=RiskLevel.SAFE,
    category="alerts",
    params=[
        ParamSpec("alert_id", "int", min=1, description="Alert id to delete."),
    ],
)
async def alerts_cancel(ctx: ToolContext, args: dict) -> ToolResult:
    ok = await delete_trigger(
        ctx.db, trigger_id=int(args["alert_id"]), user_id=int(ctx.user_id)
    )
    if not ok:
        return ToolResult.fail("alert_not_found_or_not_owned")
    return ToolResult.success({"alert_id": int(args["alert_id"]), "deleted": True})
