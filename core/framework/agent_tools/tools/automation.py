"""
core/framework/agent_tools/tools/automation.py -- agent-facing automation tools.

These tools expose the existing automation substrate (task queue, event
triggers, multi-step chains) to the agent tool-call loop so the AI can set
up background jobs, price-gated triggers, and multi-step plans on the
user's behalf instead of stalling on a single request/response turn.

Risk posture
------------
Everything here is SAFE because the work being enqueued still has to go
through the normal :func:`run_tool` approval gate when it eventually runs.
A user can queue ``economy.transfer`` but the queue worker will refuse to
execute it without an approval row because transfer is MUTATE; same for
triggers. So creating a queue row / trigger / chain plan is a low-risk
"intent" action, and the real approval happens at execute time.

Tools
-----
``automation.queue_enqueue``   -- enqueue a tool call for later execution.
``automation.queue_list``      -- inspect a user's recent queue rows.
``automation.queue_cancel``    -- cancel a pending queue row.
``automation.trigger_create``  -- register an event trigger.
``automation.trigger_list``    -- list a user's triggers.
``automation.trigger_delete``  -- delete a trigger.
``automation.chain_run``       -- execute a short multi-step plan now.
"""
from __future__ import annotations

import logging

from ..chain import ChainExecutor, parse_chain_plan
from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool
from ..queue import cancel_task, enqueue_task, list_user_tasks
from ..triggers import create_trigger, delete_trigger, list_triggers

log = logging.getLogger("discoin.agent_tools.tools.automation")


# ── Task queue ────────────────────────────────────────────────────────────────

@tool(
    name="automation.queue_enqueue",
    summary="Enqueue a tool call for background execution. Approvals still run at execute time.",
    risk=RiskLevel.SAFE,
    category="automation",
    params=[
        ParamSpec("tool",      "str",   description="Fully-qualified agent tool name."),
        ParamSpec("args",      "json",  description="Args dict for the enqueued tool."),
        ParamSpec("delay_s",   "int",   required=False, default=0, min=0, max=604800,
                  description="Seconds to wait before running (max 7 days)."),
        ParamSpec("max_attempts", "int", required=False, default=3, min=1, max=10,
                  description="How many times to retry transient failures."),
    ],
)
async def _queue_enqueue(ctx: ToolContext, args: dict) -> ToolResult:
    import time as _time
    from ..core import ToolRegistry
    tool_name = str(args.get("tool") or "").strip()
    payload = args.get("args") or {}
    if not isinstance(payload, dict):
        return ToolResult.fail("'args' must be an object")
    spec = ToolRegistry.get(tool_name)
    if spec is None:
        return ToolResult.fail(f"unknown tool: {tool_name}")
    delay = int(args.get("delay_s") or 0)
    run_after = _time.time() + max(0, delay)
    task_id = await enqueue_task(
        ctx.db,
        guild_id=int(ctx.guild_id),
        user_id=int(ctx.user_id),
        tool=tool_name,
        args=payload,
        run_after=run_after,
        actor="queue",
        max_attempts=int(args.get("max_attempts") or 3),
    )
    return ToolResult.success({
        "task_id": task_id,
        "tool": tool_name,
        "run_after": run_after,
        "delay_s": delay,
    })


@tool(
    name="automation.queue_list",
    summary="List the caller's recent queued tasks.",
    risk=RiskLevel.READ,
    category="automation",
    params=[
        ParamSpec("status", "str", required=False, default=None,
                  choices=["pending", "running", "done", "failed", "cancelled"],
                  description="Filter by status."),
        ParamSpec("limit",  "int", required=False, default=10, min=1, max=25),
    ],
)
async def _queue_list(ctx: ToolContext, args: dict) -> ToolResult:
    rows = await list_user_tasks(
        ctx.db,
        guild_id=int(ctx.guild_id),
        user_id=int(ctx.user_id),
        status=args.get("status"),
        limit=int(args.get("limit") or 10),
    )
    tasks = [
        {
            "id":       int(r["id"]),
            "tool":     r.get("tool"),
            "status":   r.get("status"),
            "attempts": int(r.get("attempts") or 0),
        }
        for r in rows
    ]
    return ToolResult.success({"tasks": tasks, "count": len(tasks)})


@tool(
    name="automation.queue_cancel",
    summary="Cancel a pending queued task you own.",
    risk=RiskLevel.SAFE,
    category="automation",
    params=[
        ParamSpec("task_id", "int", min=1),
    ],
)
async def _queue_cancel(ctx: ToolContext, args: dict) -> ToolResult:
    task_id = int(args["task_id"])
    # Only allow cancelling tasks that belong to the caller so one user
    # can't drop another user's queued background job through the AI.
    row = await ctx.db.fetch_one(
        "SELECT user_id, status FROM agent_task_queue WHERE id=$1",
        task_id,
    )
    if row is None:
        return ToolResult.fail("task not found")
    if int(row["user_id"]) != int(ctx.user_id):
        return ToolResult.fail("not your task")
    if row["status"] != "pending":
        return ToolResult.fail(f"task is {row['status']}, cannot cancel")
    ok = await cancel_task(ctx.db, task_id)
    return ToolResult.success({"task_id": task_id, "cancelled": bool(ok)})


# ── Triggers ──────────────────────────────────────────────────────────────────

_VALID_TRIGGER_KINDS = {
    "price_above", "price_below", "event", "token_halted", "loan_liquidated",
}


@tool(
    name="automation.trigger_create",
    summary="Create an event trigger that fires a tool when a condition matches.",
    risk=RiskLevel.SAFE,
    category="automation",
    params=[
        ParamSpec("kind", "str",
                  choices=sorted(_VALID_TRIGGER_KINDS),
                  description="Event kind: price_above, price_below, event, token_halted, loan_liquidated."),
        ParamSpec("condition", "json",
                  description="Condition shape (e.g. {'symbol':'MTA','threshold':50000})."),
        ParamSpec("tool", "str", description="Tool to run when the trigger fires."),
        ParamSpec("args", "json",
                  description="Args dict passed to the fired tool."),
        ParamSpec("name", "str", required=False, default="",
                  description="Label shown in ,ai trigger list."),
        ParamSpec("one_shot", "bool", required=False, default=True,
                  description="Delete the trigger after it fires once."),
    ],
)
async def _trigger_create(ctx: ToolContext, args: dict) -> ToolResult:
    from ..core import ToolRegistry
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in _VALID_TRIGGER_KINDS:
        return ToolResult.fail(f"invalid kind: {kind}")
    tool_name = str(args.get("tool") or "").strip()
    if ToolRegistry.get(tool_name) is None:
        return ToolResult.fail(f"unknown fired tool: {tool_name}")
    condition = args.get("condition") or {}
    if not isinstance(condition, dict):
        return ToolResult.fail("'condition' must be an object")
    payload = args.get("args") or {}
    if not isinstance(payload, dict):
        return ToolResult.fail("'args' must be an object")
    trigger_id = await create_trigger(
        ctx.db,
        guild_id=int(ctx.guild_id),
        user_id=int(ctx.user_id),
        kind=kind,
        condition=condition,
        tool=tool_name,
        args=payload,
        name=str(args.get("name") or "")[:80],
        one_shot=bool(args.get("one_shot", True)),
    )
    return ToolResult.success({
        "trigger_id": trigger_id,
        "kind": kind,
        "tool": tool_name,
    })


@tool(
    name="automation.trigger_list",
    summary="List the caller's active event triggers.",
    risk=RiskLevel.READ,
    category="automation",
    params=[],
)
async def _trigger_list(ctx: ToolContext, args: dict) -> ToolResult:
    rows = await list_triggers(
        ctx.db, guild_id=int(ctx.guild_id), user_id=int(ctx.user_id),
    )
    items = [
        {
            "id":        int(r["id"]),
            "kind":      r.get("kind"),
            "tool":      r.get("tool"),
            "name":      r.get("name"),
            "enabled":   bool(r.get("enabled")),
            "fire_count": int(r.get("fire_count") or 0),
        }
        for r in rows
    ]
    return ToolResult.success({"triggers": items, "count": len(items)})


@tool(
    name="automation.trigger_delete",
    summary="Delete a trigger you own.",
    risk=RiskLevel.SAFE,
    category="automation",
    params=[
        ParamSpec("trigger_id", "int", min=1),
    ],
)
async def _trigger_delete(ctx: ToolContext, args: dict) -> ToolResult:
    ok = await delete_trigger(
        ctx.db,
        trigger_id=int(args["trigger_id"]),
        user_id=int(ctx.user_id),
    )
    if not ok:
        return ToolResult.fail("trigger not found or not yours")
    return ToolResult.success({"trigger_id": int(args["trigger_id"])})


# ── Chains ────────────────────────────────────────────────────────────────────

@tool(
    name="automation.chain_run",
    summary="Run a short multi-step tool chain (max 8 steps).",
    risk=RiskLevel.SAFE,
    category="automation",
    params=[
        ParamSpec("steps", "json",
                  description="List of {tool, args, on, pipe_in} step objects."),
    ],
)
async def _chain_run(ctx: ToolContext, args: dict) -> ToolResult:
    # Pull the bot off the context's db handle if we can -- ChainExecutor
    # needs bus access. Fall back to a lightweight stub if not available.
    raw = args.get("steps") or []
    if not isinstance(raw, list) or not raw:
        return ToolResult.fail("'steps' must be a non-empty list")
    try:
        steps = parse_chain_plan(raw)
    except ValueError as exc:
        return ToolResult.fail(f"invalid plan: {exc}")
    bot = _get_bot_from_ctx(ctx)
    if bot is None:
        return ToolResult.fail("chain executor unavailable (no bot ref)")
    chain = ChainExecutor(bot)
    try:
        run = await chain.run(
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            steps=steps,
            approved_tools=set(),
            actor="chain",
        )
    except Exception as exc:
        return ToolResult.fail(f"chain crashed: {exc}")
    return ToolResult.success({
        "run_id": run.id,
        "status": run.status,
        "step_results": run.step_results,
    })


def _get_bot_from_ctx(ctx: ToolContext):
    db = getattr(ctx, "db", None)
    bot = getattr(db, "_bot", None)
    if bot is not None:
        return bot
    # Fall back to module-level reference if main cog stashed one.
    try:
        from core.framework import agent_tools as _agent_tools  # noqa
        # No bot exposed at module level by default; callers should set
        # ctx.db._bot from the cog if they want chain.run support.
    except Exception:
        pass
    return None
