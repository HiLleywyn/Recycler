"""
core/framework/agent_tools/executor.py -- the single choke point for running tools.

run_tool(name, ctx, args) validates the args, enforces cooldowns, checks the
risk-based approval policy, runs the handler, records an audit row, and
returns a structured ToolResult envelope. A disabled tool (per registry_state)
short-circuits here so every execution path -- direct, chain, queue,
trigger -- sees the same gating.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import registry_state
from .core import RiskLevel, ToolContext, ToolRegistry, ToolResult
from .validation import ToolValidationError, validate_args

log = logging.getLogger("discoin.agent_tools.exec")

# Per (tool, guild, user, actor) monotonic timestamp of last successful call.
# guild_id keeps cooldowns from leaking across servers; actor keeps a queued
# /triggered call from blocking a user's interactive call (and vice versa).
_last_call_ts: dict[tuple[str, int, int, str], float] = {}


def _cooldown_key(name: str, ctx: ToolContext) -> tuple[str, int, int, str]:
    return (name, int(ctx.guild_id), int(ctx.user_id), str(ctx.actor or "user"))


async def run_tool(name: str, ctx: ToolContext, raw_args: dict | None) -> ToolResult:
    """Validated, audited, guardrailed tool execution."""
    spec = ToolRegistry.get(name)
    if spec is None:
        return ToolResult.fail(f"unknown tool: {name}")

    if not registry_state.is_enabled("tool", name, default=True):
        # Short-circuit before validation / cooldowns / approval so disabled
        # tools are completely inert regardless of how they were invoked.
        return ToolResult.fail(
            f"tool_disabled: {name} (enable with ,ai tools enable {name})",
            disabled=True,
        )

    try:
        args = validate_args(spec, raw_args or {})
    except ToolValidationError as exc:
        log.info("[agent_tools] validation fail %s: %s", name, exc)
        return ToolResult.fail(f"validation_error: {exc}")

    if spec.cooldown_s > 0:
        key = _cooldown_key(name, ctx)
        now = time.monotonic()
        wait = spec.cooldown_s - (now - _last_call_ts.get(key, 0.0))
        if wait > 0:
            return ToolResult.fail(
                f"cooldown: wait {int(wait)}s",
                wait_s=int(wait),
            )

    if spec.risk == RiskLevel.DANGER and not ctx.approved:
        return ToolResult.needs_approval(
            reason=(
                f"{spec.name!r} is marked {spec.risk.value} -- explicit user "
                f"approval required before execution."
            ),
            preview={"tool": spec.name, "risk": spec.risk.value, "args": args},
        )

    if spec.risk == RiskLevel.MUTATE and ctx.actor not in ("user", "chain"):
        # Agents / queued / triggered flows must not autonomously mutate
        # player-visible state without running through an approved chain.
        return ToolResult.needs_approval(
            reason=(
                f"{spec.name!r} mutates state; actor={ctx.actor!r} may not run "
                f"it without an approved chain plan."
            ),
            preview={"tool": spec.name, "risk": spec.risk.value, "args": args},
        )

    t0 = time.monotonic()
    try:
        result = await spec.handler(ctx, args)
    except Exception as exc:
        log.exception("[agent_tools] handler crashed %s", name)
        result = ToolResult.fail(f"handler_error: {type(exc).__name__}: {exc}")
    duration_ms = int((time.monotonic() - t0) * 1000)

    if not isinstance(result, ToolResult):
        log.error(
            "[agent_tools] %s returned %r instead of ToolResult", name, type(result),
        )
        result = ToolResult.fail("internal_error: handler returned non-ToolResult")

    result.meta.setdefault("tool", spec.name)
    result.meta.setdefault("risk", spec.risk.value)
    result.meta.setdefault("actor", ctx.actor)
    result.meta.setdefault("duration_ms", duration_ms)

    if result.ok and spec.cooldown_s > 0:
        _last_call_ts[_cooldown_key(name, ctx)] = time.monotonic()

    try:
        await _audit(ctx, spec.name, spec.risk.value, args, result)
    except Exception:
        log.warning("[agent_tools] audit write failed for %s", name)

    return result


async def _audit(
    ctx: ToolContext,
    name: str,
    risk: str,
    args: dict,
    result: ToolResult,
) -> None:
    db = getattr(ctx, "db", None)
    if db is None:
        return
    await db.execute(
        """
        INSERT INTO agent_tool_audit
            (guild_id, user_id, actor, tool, risk, args, ok, error, duration_ms, created_at)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,NOW())
        """,
        int(ctx.guild_id),
        int(ctx.user_id),
        ctx.actor,
        name,
        risk,
        json.dumps(args, default=str),
        bool(result.ok),
        (result.error or "")[:500],
        int(result.meta.get("duration_ms", 0)),
    )


async def request_approval(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    tool: str,
    args: dict,
    reason: str,
    expires_in_s: int = 600,
) -> int:
    """Create a pending approval row. Returns its id."""
    row = await db.fetch_one(
        """
        INSERT INTO agent_approvals
            (guild_id, user_id, tool, args, reason, expires_at)
        VALUES ($1,$2,$3,$4::jsonb,$5, NOW() + ($6 || ' seconds')::interval)
        RETURNING id
        """,
        int(guild_id), int(user_id), tool,
        json.dumps(args, default=str), reason[:500], str(int(expires_in_s)),
    )
    return int(row["id"])


async def decide_approval(
    db: Any,
    *,
    approval_id: int,
    decider_id: int,
    approve: bool,
) -> bool:
    """Mark an approval row approved or denied. Returns True on state change."""
    res = await db.execute(
        """
        UPDATE agent_approvals
        SET status = $3,
            decided_by = $2,
            decided_at = NOW()
        WHERE id = $1
          AND status = 'pending'
          AND expires_at > NOW()
        """,
        int(approval_id), int(decider_id), "approved" if approve else "denied",
    )
    return "UPDATE 1" in str(res)
