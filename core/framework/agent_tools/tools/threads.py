"""
core/framework/agent_tools/tools/threads.py -- thread memory graph tools.

    thread.context  inspect what is merged into the current thread (READ).
    thread.link     merge another thread's context into this one (MUTATE).
    thread.unlink   sever a merged thread link (MUTATE).

These tools mutate ONLY the Postgres conversation DAG. They never create,
close, or message Discord threads -- that lifecycle stays with the human
`,thread` commands. ``ctx.channel_id`` is a DAG lookup key extracted from
the invocation context; the AI never passes it and never sees a raw
Discord handle. Outside a chat thread the tools fail closed.
"""
from __future__ import annotations

import logging

import services.chat_threads as ct

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.threads")


async def _current_thread(ctx: ToolContext) -> dict | None:
    """The chat_threads row for the active thread, or None when not in one."""
    cid = getattr(ctx, "channel_id", None)
    if not cid:
        return None
    row = await ct.get_thread_row(ctx.db, int(cid))
    if row is None or row.get("status") != "active":
        return None
    return row


@tool(
    name="thread.context",
    summary=(
        "Inspect this conversation's memory graph: the threads and groups "
        "linked directly into the current thread, plus the full transitive "
        "set of past threads whose summaries reach your context and how "
        "many graph hops away each one is."
    ),
    risk=RiskLevel.READ,
    category="thread",
    params=[],
)
async def context(ctx: ToolContext, args: dict) -> ToolResult:
    row = await _current_thread(ctx)
    if row is None:
        return ToolResult.fail(
            "not_in_thread: this only works inside a Disco chat thread."
        )
    tid = int(row["thread_id"])
    direct = await ct.thread_link_rows(ctx.db, tid)
    groups = await ct.group_link_rows(ctx.db, tid)
    resolved = await ct.resolve_linked_thread_rows(ctx.db, tid)
    return ToolResult.success({
        "direct_thread_links": [
            {"code": r.get("linked_token"), "title": r.get("title")}
            for r in direct
        ],
        "group_links": [
            {
                "group_id": int(r["linked_group_id"]),
                "threads": int(r.get("member_count") or 0),
            }
            for r in groups
        ],
        "resolved_context": [
            {
                "code": r.get("token"),
                "title": r.get("title"),
                "hops_away": int(r.get("distance") or 1),
            }
            for r in resolved
        ],
    })


@tool(
    name="thread.link",
    summary=(
        "Merge another Disco thread's context into the current conversation, "
        "identified by its recall code, Discord thread id, or thread name. "
        "Creates a live, directional graph edge -- the linked thread's "
        "summary is folded into your context every turn until it is "
        "unlinked or the thread is closed. The identifier is a DAG lookup "
        "key, not a Discord handle: this never creates, closes, or messages "
        "a Discord thread."
    ),
    risk=RiskLevel.MUTATE,
    category="thread",
    params=[
        ParamSpec(
            "target", "str", required=True,
            description=(
                "The thread whose context to merge in: its 8-char recall "
                "code, its Discord thread id, or its thread name."
            ),
        ),
    ],
)
async def link(ctx: ToolContext, args: dict) -> ToolResult:
    row = await _current_thread(ctx)
    if row is None:
        return ToolResult.fail(
            "not_in_thread: run this inside a Disco chat thread."
        )
    target = str(args.get("target") or "").strip()
    recalled = await ct.resolve_link_target(ctx.db, int(ctx.guild_id), target)
    if recalled is None:
        return ToolResult.fail(
            f"no_thread: no Disco thread matches {target!r}."
        )
    ok, reason = await ct.apply_thread_link(
        ctx.db,
        source_thread_id=int(row["thread_id"]),
        guild_id=int(ctx.guild_id),
        recalled=recalled,
        user_id=int(ctx.user_id),
    )
    if not ok:
        return ToolResult.fail(f"link_failed: {reason}")
    return ToolResult.success({
        "linked_code": recalled.get("token"),
        "linked_title": recalled.get("title"),
    })


@tool(
    name="thread.unlink",
    summary=(
        "Sever a merged thread link by recall code, removing that thread's "
        "context from the current conversation. A pure graph-edge deletion: "
        "it never closes or deletes a Discord thread."
    ),
    risk=RiskLevel.MUTATE,
    category="thread",
    params=[
        ParamSpec(
            "target", "str", required=True,
            description="Recall code of the linked thread to remove.",
        ),
    ],
)
async def unlink(ctx: ToolContext, args: dict) -> ToolResult:
    row = await _current_thread(ctx)
    if row is None:
        return ToolResult.fail(
            "not_in_thread: run this inside a Disco chat thread."
        )
    target = str(args.get("target") or "").strip()
    removed = await ct.apply_thread_unlink(
        ctx.db, int(row["thread_id"]), target,
    )
    if not removed:
        return ToolResult.fail(
            f"not_linked: no thread linked here with code {target!r}."
        )
    return ToolResult.success({"unlinked_code": target})
