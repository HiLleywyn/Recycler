"""
core/framework/agent_tools/chain.py -- multi-step agent tool chain executor.

A chain is a list of ChainSteps. Each step names a tool, its args, and a
policy for when to run:

    on="seq"   -- run only if the previous step succeeded (default).
    on="any"   -- run regardless of previous outcome.
    on="fail"  -- run only if the previous step failed (fallback / cleanup).

Optional ``pipe_in`` copies a field from the previous step's result.data into
the current step's args under the same name.

Guardrails:
  - Chains have a hard cap on the number of steps (MAX_STEPS).
  - Every step goes through run_tool(), which enforces validation, cooldowns,
    and the risk-based approval policy. Steps that require approval must be
    listed in ``approved_tools``; otherwise they get blocked.
  - Chain runs are persisted to agent_chain_runs with per-step results so
    failures are auditable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .core import ToolContext, ToolResult
from .executor import run_tool

log = logging.getLogger("discoin.agent_tools.chain")


@dataclass
class ChainStep:
    tool: str
    args: dict = field(default_factory=dict)
    on: str = "seq"          # "seq" | "any" | "fail"
    pipe_in: str | None = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args": self.args,
            "on": self.on,
            "pipe_in": self.pipe_in,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ChainStep":
        return cls(
            tool=str(raw.get("tool") or ""),
            args=dict(raw.get("args") or {}),
            on=str(raw.get("on") or "seq"),
            pipe_in=raw.get("pipe_in"),
        )


@dataclass
class ChainRun:
    id: int
    guild_id: int
    user_id: int
    steps: list[ChainStep]
    step_results: list[dict]
    status: str


class ChainExecutor:
    """Runs a chain of agent tool calls, persisting every step."""

    MAX_STEPS = 8

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def run(
        self,
        *,
        guild_id: int,
        user_id: int,
        steps: list[ChainStep],
        approved_tools: set[str] | None = None,
        actor: str = "chain",
    ) -> ChainRun:
        if not steps:
            raise ValueError("chain has no steps")
        if len(steps) > self.MAX_STEPS:
            raise ValueError(f"chain exceeds max steps ({self.MAX_STEPS})")

        db = self.bot.db
        row = await db.fetch_one(
            """
            INSERT INTO agent_chain_runs
                (guild_id, user_id, actor, steps, status, created_at)
            VALUES ($1,$2,$3,$4::jsonb,'running',NOW())
            RETURNING id
            """,
            int(guild_id), int(user_id), actor,
            json.dumps([s.to_dict() for s in steps], default=str),
        )
        run_id = int(row["id"])

        results: list[dict] = []
        prev: ToolResult | None = None
        approved = approved_tools or set()

        for idx, step in enumerate(steps):
            # Decide whether to run this step
            run_this = True
            if prev is not None:
                if step.on == "seq" and not prev.ok:
                    run_this = False
                elif step.on == "fail" and prev.ok:
                    run_this = False
                # "any" always runs
            if not run_this:
                results.append({
                    "step": idx,
                    "tool": step.tool,
                    "skipped": True,
                    "reason": f"policy={step.on}, prev_ok={prev.ok if prev else None}",
                })
                continue

            args = dict(step.args or {})
            if step.pipe_in and prev is not None and prev.ok and prev.data:
                piped = prev.data.get(step.pipe_in)
                if piped is not None:
                    args[step.pipe_in] = piped

            ctx = ToolContext(
                user_id=int(user_id),
                guild_id=int(guild_id),
                db=db,
                bus=getattr(self.bot, "bus", None),
                actor=actor,
                approved=(step.tool in approved),
            )
            prev = await run_tool(step.tool, ctx, args)
            results.append({
                "step": idx,
                "tool": step.tool,
                "ok": prev.ok,
                "error": prev.error,
                "data": prev.data,
                "meta": prev.meta,
            })
            if not prev.ok and step.on == "seq":
                break

        all_ok = all(
            r.get("ok", False) or r.get("skipped", False)
            for r in results
        )
        status = "done" if all_ok else "failed"
        await db.execute(
            """
            UPDATE agent_chain_runs
            SET status=$2, step_results=$3::jsonb, finished_at=NOW()
            WHERE id=$1
            """,
            run_id, status, json.dumps(results, default=str),
        )
        return ChainRun(
            id=run_id,
            guild_id=int(guild_id),
            user_id=int(user_id),
            steps=steps,
            step_results=results,
            status=status,
        )


_VALID_ON_POLICIES = {"seq", "any", "fail"}


def parse_chain_plan(plan: list[dict]) -> list[ChainStep]:
    """Convert a raw list-of-dicts plan (e.g. from an AI tool call) into steps."""
    if not isinstance(plan, list):
        raise ValueError("chain plan must be a list")
    steps: list[ChainStep] = []
    for idx, entry in enumerate(plan):
        if not isinstance(entry, dict):
            raise ValueError(f"chain step {idx} must be an object")
        if not entry.get("tool"):
            raise ValueError(f"chain step {idx} missing 'tool'")
        step = ChainStep.from_dict(entry)
        if step.on not in _VALID_ON_POLICIES:
            raise ValueError(
                f"chain step {idx}: 'on' must be one of "
                f"{sorted(_VALID_ON_POLICIES)}, got {step.on!r}"
            )
        steps.append(step)
    return steps
