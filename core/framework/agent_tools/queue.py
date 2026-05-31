"""
core/framework/agent_tools/queue.py -- persistent Postgres-backed task queue.

Tasks are rows in agent_task_queue. A background worker claims due rows
(run_after <= NOW()) with SELECT ... FOR UPDATE SKIP LOCKED and routes each
one through run_tool(), so all tasks inherit the framework's validation,
cooldown, audit, and approval guardrails.

Rationale:
  - Tasks survive restarts (the task table lives in Postgres).
  - Claims are atomic, so multiple bot instances cannot double-run.
  - Retries use exponential backoff and only fire for transient errors;
    validation failures are not retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .core import ToolContext
from .executor import run_tool

log = logging.getLogger("discoin.agent_tools.queue")


async def enqueue_task(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    tool: str,
    args: dict,
    run_after: float | None = None,
    actor: str = "queue",
    max_attempts: int = 3,
    approval_id: int | None = None,
) -> int:
    """Persist a task for later execution. Returns the task id.

    ``approval_id`` references a row in ``agent_approvals``. The queue worker
    will only mark the task as approved (so DANGER tools can run) if the
    referenced approval is in ``status='approved'`` and not expired. Without
    an ``approval_id``, queued execution always goes through ``run_tool()``
    with ``approved=False`` -- DANGER tools cannot bypass the approval gate
    just because they were enqueued.
    """
    row = await db.fetch_one(
        """
        INSERT INTO agent_task_queue
            (guild_id, user_id, actor, tool, args, status, run_after,
             max_attempts, attempts, approval_id, created_at)
        VALUES ($1,$2,$3,$4,$5::jsonb,'pending',
                to_timestamp($6),$7,0,$8,NOW())
        RETURNING id
        """,
        int(guild_id), int(user_id), actor, tool,
        json.dumps(args, default=str),
        float(run_after or time.time()),
        int(max_attempts),
        int(approval_id) if approval_id is not None else None,
    )
    return int(row["id"])


async def cancel_task(db: Any, task_id: int) -> bool:
    res = await db.execute(
        "UPDATE agent_task_queue SET status='cancelled' "
        "WHERE id=$1 AND status='pending'",
        int(task_id),
    )
    return "UPDATE 1" in str(res)


async def list_user_tasks(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    status: str | None = None,
    limit: int = 25,
) -> list[dict]:
    if status:
        return await db.fetch_all(
            """
            SELECT * FROM agent_task_queue
            WHERE guild_id=$1 AND user_id=$2 AND status=$3
            ORDER BY id DESC LIMIT $4
            """,
            int(guild_id), int(user_id), status, int(limit),
        )
    return await db.fetch_all(
        """
        SELECT * FROM agent_task_queue
        WHERE guild_id=$1 AND user_id=$2
        ORDER BY id DESC LIMIT $3
        """,
        int(guild_id), int(user_id), int(limit),
    )


class TaskQueueWorker:
    """Background worker that drains the agent_task_queue."""

    def __init__(self, bot: Any, poll_interval: float = 5.0) -> None:
        self.bot = bot
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="agent_tools.queue")
        log.info("[agent_tools.queue] worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            processed = 0
            try:
                processed = await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[agent_tools.queue] drain crashed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=0.5 if processed else self.poll_interval,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

    async def _drain_once(self) -> int:
        db = getattr(self.bot, "db", None)
        if db is None:
            return 0
        row = await db.fetch_one(
            """
            UPDATE agent_task_queue
            SET status='running',
                claimed_at=NOW(),
                attempts=attempts+1
            WHERE id = (
                SELECT id FROM agent_task_queue
                WHERE status='pending' AND run_after <= NOW()
                ORDER BY run_after ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """,
        )
        if row is None:
            return 0

        task_id = int(row["id"])
        try:
            raw_args = row.get("args") or {}
            if isinstance(raw_args, str):
                args = json.loads(raw_args)
            else:
                args = dict(raw_args)
        except Exception:
            args = {}

        # Default to NOT approved -- queued tasks must satisfy run_tool()'s
        # approval gate. Tasks that need to invoke a DANGER tool must carry an
        # ``approval_id`` referencing an ``agent_approvals`` row that is still
        # in ``status='approved'`` and not expired.
        approved = False
        approval_id = row.get("approval_id")
        if approval_id is not None:
            approval = await db.fetch_one(
                """
                SELECT status, tool FROM agent_approvals
                WHERE id = $1
                  AND status = 'approved'
                  AND expires_at > NOW()
                """,
                int(approval_id),
            )
            if approval and str(approval.get("tool") or "") == str(row.get("tool") or ""):
                approved = True

        ctx = ToolContext(
            user_id=int(row["user_id"]),
            guild_id=int(row["guild_id"]),
            db=db,
            bus=getattr(self.bot, "bus", None),
            actor=row.get("actor") or "queue",
            approved=approved,
        )
        tool_name = str(row.get("tool") or "")
        result = await run_tool(tool_name, ctx, args)

        attempts = int(row.get("attempts") or 1)
        max_attempts = int(row.get("max_attempts") or 1)
        retry = (
            (not result.ok)
            and attempts < max_attempts
            and result.error
            and not result.error.startswith("validation_error")
            and not result.error.startswith("unknown tool")
            and not result.error.startswith("approval_required")
        )

        if result.ok:
            await db.execute(
                "UPDATE agent_task_queue SET status='done', result=$2::jsonb, "
                "finished_at=NOW() WHERE id=$1",
                task_id, result.to_json(),
            )
        elif retry:
            # Backoff: 10s, 20s, 40s...
            backoff_s = min(600, 10 * (2 ** max(0, attempts - 1)))
            await db.execute(
                """
                UPDATE agent_task_queue
                SET status='pending',
                    run_after = NOW() + ($3 || ' seconds')::interval,
                    result=$2::jsonb
                WHERE id=$1
                """,
                task_id, result.to_json(), str(backoff_s),
            )
        else:
            await db.execute(
                "UPDATE agent_task_queue SET status='failed', result=$2::jsonb, "
                "finished_at=NOW() WHERE id=$1",
                task_id, result.to_json(),
            )
        return 1
