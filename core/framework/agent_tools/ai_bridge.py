"""
core/framework/agent_tools/ai_bridge.py -- glue between the chat pipeline and
the agent tool registry.

``complete_with_agent_tools`` runs a multi-iteration tool-calling loop
against the configured tool backend, using every registered non-DANGER
tool as a function schema. Each iteration:

  1. Posts the current message buffer + tool schemas to the model.
  2. If the model returns text, returns it immediately.
  3. If the model returns ``tool_calls``, executes each one through
     :func:`run_tool` (which enforces validation, cooldowns, approval
     policy, and audit), and appends a ``role=tool`` turn carrying the
     ``ToolResult`` JSON so the model can see the answer on the next turn.
  4. Stops after ``max_iter - 1`` tool-calling iterations. The final text
     pass always uses ``complete()`` with a collapsed plain-text convo so
     the model is never sent tool_calls history without a tools schema
     (which many OpenRouter models reject with a 400).

Backend dispatch:
  When ``Config.TOOLS_BACKEND == "ollama"`` the loop runs against the
  local Ollama OpenAI-compat endpoint using ``Config.TOOLS_MODEL`` (e.g.
  ``gemma3:27b``, which supports function calling and vision). Otherwise
  it talks to OpenRouter using the orchestrator model. The fallback text
  completion always goes through :func:`complete` (OpenRouter) so the
  user still gets a reply even if the tool-capable model fails.

The caller owns the ``ToolContext``. DANGER tools are never exposed to the
model via this loop; they must go through approval flows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Awaitable, Callable

from core.config import Config
from core.framework.ai import get_guild_default
from core.framework.ai.client import (
    _strip_image_blocks,
    complete,
    complete_ollama,
    complete_with_tool_calls,
    complete_with_tool_calls_ollama,
)

# Type alias for the queue-position callback. The placeholder UI subscribes
# and renders "queued (#N)" while the request waits behind other tickets.
_PosCb = Callable[[int], Awaitable[None]]

from .core import ToolContext, ToolRegistry, ToolResult
from .executor import request_approval, run_tool

log = logging.getLogger("discoin.agent_tools.ai_bridge")

# Hard cap on loop iterations. Each iteration is a full OpenRouter call plus
# N local tool executions. Keeping this low bounds latency and cost.
_MAX_ITER = 3
_MAX_TOOL_CALLS_PER_TURN = 4

# Maps ToolSpec.category values to ai_model_defaults category keys for the
# domain categories whose guild models are NOT the generic "tools" model.
# When tools from these categories fire during a loop, the final text pass
# uses the corresponding guild category model (if set) instead of "tools".
# vision/image/search are handled at the individual tool level so excluded.
_TOOL_CAT_TO_MODEL_CAT: dict[str, str] = {
    "risk":        "reason",
    "automation":  "automation",
    "defi":        "defi",
    "economy_sim": "economy_sim",
}
# Tool result JSON is truncated before being fed back to the model so a
# runaway data.web_fetch can't blow the context window.
_MAX_TOOL_RESULT_CHARS = 1800


_TRUNCATION_SUFFIX = '..."_truncated":true}'


async def _race_queue_events(
    call_factory: Callable[[_PosCb], Awaitable],
) -> AsyncIterator[dict]:
    """Yield ``{"type":"queued","position":N}`` events while *call_factory* waits.

    ``call_factory`` receives an ``on_queue_update`` callback and returns the
    awaitable that drives the actual HTTP call. The generator yields one
    ``queued`` event per position change reported by the chat queue, then
    yields one final ``{"_result": <returned value>}`` so the caller can
    distinguish queue updates from the wrapped call's return value.

    Exists so the agent bridge's async generator can surface live queue
    position to the placeholder UI without blocking the HTTP call on the
    Discord-side render. Without this race, queue updates would only land
    AFTER the HTTP call returned, defeating the point of live feedback.
    """
    q: asyncio.Queue[dict] = asyncio.Queue()

    async def _cb(pos: int) -> None:
        # Only surface "I'm waiting" positions; the in-flight (pos=0) wake
        # is implicit -- the next tool-loop status event covers it.
        if pos > 0:
            await q.put({"type": "queued", "position": pos})

    task = asyncio.ensure_future(call_factory(_cb))
    try:
        while not task.done():
            getter = asyncio.create_task(q.get())
            done, _pending = await asyncio.wait(
                [getter, task], return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                try:
                    yield getter.result()
                except Exception:
                    pass
            else:
                getter.cancel()
                try:
                    await getter
                except (asyncio.CancelledError, Exception):
                    pass
    except asyncio.CancelledError:
        task.cancel()
        raise

    # Drain any tail events that landed between the task finishing and now.
    while not q.empty():
        try:
            yield q.get_nowait()
        except asyncio.QueueEmpty:
            break
    yield {"_result": task.result()}


def _collapse_tool_turns(convo: list[dict]) -> list[dict]:
    """Rewrite a tool-calling convo into a plain messages list.

    The fallback ``complete()`` call sends no ``tools`` schema. Many
    OpenRouter models reject conversations that contain ``role=tool``
    messages or ``assistant`` messages with ``tool_calls`` when the schema
    is absent, returning a 400 that wastes an API round-trip and burns
    precious timeout budget. This collapses each (assistant+tool_calls,
    tool-result*) pair into a single assistant message that embeds the
    results as plain text so any model can read and respond to them.
    """
    out: list[dict] = []
    i = 0
    while i < len(convo):
        m = convo[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls") and not m.get("content"):
            calls = m.get("tool_calls") or []
            tool_names = [
                (tc.get("function") or {}).get("name") or "?"
                for tc in calls
            ]
            # Absorb the immediately following tool-result turns.
            results: list[str] = []
            j = i + 1
            while j < len(convo) and convo[j].get("role") == "tool":
                results.append((convo[j].get("content") or "")[:400])
                j += 1
            parts = [f"[Used tools: {', '.join(tool_names)}]"]
            for r in results:
                parts.append(f"[Result: {r}]")
            out.append({"role": "assistant", "content": "\n".join(parts)})
            i = j
        elif role == "tool":
            # Orphaned tool result (no preceding tool_call turn) -- skip.
            i += 1
        else:
            entry = {k: v for k, v in m.items() if k not in ("tool_calls", "tool_call_id")}
            out.append(entry)
            i += 1
    return out


def _parse_tool_call_args(tc: dict) -> tuple[str, dict]:
    """Extract ``(name, args)`` from a single OpenAI tool_call envelope.

    Robust to the three shapes models actually emit: stringified JSON args
    (the spec), already-parsed dict args (some Ollama models), or
    missing/garbage args (falls back to empty dict).
    """
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "")
    raw_args = fn.get("arguments")
    try:
        if isinstance(raw_args, str):
            args = json.loads(raw_args or "{}")
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = {}
    except Exception as exc:
        log.info("[ai_bridge] bad args for %s: %s", name, exc)
        args = {}
    return name, args


async def _execute_one_tool_call(
    tc: dict,
    ctx: ToolContext,
) -> tuple[dict, str, dict, ToolResult]:
    """Parse and execute a single tool_call. Returns (tc, name, args, result).

    The return tuple preserves the original tool_call envelope so callers
    can rebuild the assistant/tool turn pair with the right ``tool_call_id``
    without re-walking ``tool_calls`` separately. Never raises -- crashes
    become ``ToolResult.fail`` so ``asyncio.gather`` doesn't tear down the
    whole round when one tool blows up.
    """
    name, args = _parse_tool_call_args(tc)
    if not name:
        return tc, name, args, ToolResult.fail("empty_tool_name")
    try:
        result = await run_tool(name, ctx, args)
    except Exception as exc:
        log.exception("[ai_bridge] run_tool crashed for %s", name)
        result = ToolResult.fail(
            f"run_tool_crashed: {type(exc).__name__}: {exc}"
        )
    return tc, name, args, result


def _summarise_result(result: ToolResult) -> str:
    """Serialise a ToolResult for the ``role=tool`` turn, length-capped.

    The returned string is guaranteed to be at most ``_MAX_TOOL_RESULT_CHARS``
    characters long so a runaway data.web_fetch can't blow the context
    window on the next turn.
    """
    try:
        raw = result.to_json()
    except Exception:
        raw = json.dumps({"ok": False, "error": "serialize_failed"})
    if len(raw) > _MAX_TOOL_RESULT_CHARS:
        head_len = _MAX_TOOL_RESULT_CHARS - len(_TRUNCATION_SUFFIX)
        raw = raw[:head_len] + _TRUNCATION_SUFFIX
    return raw


async def _complete_for_provider(
    provider: str,
    messages: list[dict],
    *,
    model: str | None,
    max_tokens: int,
    temperature: float,
    usage_out: list | None = None,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
) -> str | None:
    """Non-streaming text completion routed to the matching backend.

    Used by the post-streaming fallback in the bridge so a hiccup on the
    SSE side doesn't accidentally fall through to OpenRouter for an
    Ollama-only deployment. Returns ``None`` on hard failure; callers
    handle the empty-response surface.

    ``user_id`` + ``on_queue_update`` route through the per-user chat queue
    so the placeholder UI can render queue position as the request waits.
    """
    if provider == "ollama":
        return await complete_ollama(
            messages,
            model=model or Config.TOOLS_MODEL or "llama3.2",
            max_tokens=max_tokens,
            temperature=temperature,
            _usage_out=usage_out,
            user_id=user_id,
            on_queue_update=on_queue_update,
        )
    return await complete(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        _usage_out=usage_out,
        user_id=user_id,
        on_queue_update=on_queue_update,
    )


async def _dispatch_tool_call(
    convo: list[dict],
    *,
    tools: list[dict],
    provider: str,
    model: str | None,
    max_tokens: int,
    temperature: float,
    tool_choice: str = "auto",
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
) -> tuple[str | None, list[dict] | None, dict | None]:
    """Pick the right backend for a single tool-calling round.

    Returns ``(text, tool_calls, usage)`` where ``usage`` is the OpenAI-format
    ``{"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}`` dict
    from the model response, or ``None`` if the backend doesn't report it.
    Exactly one of ``text`` / ``tool_calls`` is non-None on success.
    """
    if provider == "ollama" or (
        provider == "openrouter"
        and not model
        and Config.TOOLS_BACKEND == "ollama"
    ):
        return await complete_with_tool_calls_ollama(
            convo,
            tools=tools,
            model=model or Config.TOOLS_MODEL or None,
            max_tokens=max_tokens,
            temperature=temperature,
            inject_base_instructions=True,
            tool_choice=tool_choice,
            user_id=user_id,
            on_queue_update=on_queue_update,
        )
    return await complete_with_tool_calls(
        convo,
        tools=tools,
        model=model or None,
        max_tokens=max_tokens,
        temperature=temperature,
        inject_base_instructions=True,
        tool_choice=tool_choice,
        user_id=user_id,
        on_queue_update=on_queue_update,
    )


def _chat_env_provider() -> str:
    """Return the configured backend for casual chat (no tool match).

    Reads ``CHAT_BACKEND`` first so operators can split casual chat off the
    slow tool-loop backend. Falls back to ``TOOLS_BACKEND``. Anything other
    than "ollama" resolves to "openrouter" (the historical default).
    """
    chat = (Config.CHAT_BACKEND or "").strip().lower()
    if chat in ("ollama", "openrouter"):
        return chat
    return "ollama" if (Config.TOOLS_BACKEND or "").lower() == "ollama" else "openrouter"


def _tools_env_provider() -> str:
    """Return the configured backend for the agent tool loop."""
    return "ollama" if (Config.TOOLS_BACKEND or "").lower() == "ollama" else "openrouter"


def _env_model_for(provider: str) -> str | None:
    """Return the env-default model for ``provider`` ("" treated as None)."""
    if provider == "ollama":
        return Config.TOOLS_MODEL or None
    return Config.OPENROUTER_MODEL or None


async def _resolve_tools_pick(
    ctx: ToolContext | None,
    model_override: str | None,
    *,
    has_tools: bool = True,
) -> tuple[str, str | None]:
    """Return ``(provider, model)`` for an agent call.

    ``has_tools=False`` means casual chat (no tool schemas in scope). That
    path follows ``CHAT_BACKEND`` (or ``TOOLS_BACKEND`` if unset). When
    ``has_tools=True`` the agent tool loop runs and follows ``TOOLS_BACKEND``.

    Priority (env-wins, mirrors :func:`core.framework.ai.resolve_model`):
      1. ``model_override`` from a direct caller, routed to the env backend.
      2. ``CHAT_BACKEND`` / ``TOOLS_BACKEND`` env var picks the provider. A
         guild row in ``ai_model_defaults`` pointing at a different provider
         is ignored -- the operator's deployment env is canonical, otherwise
         a stale ``,ai model set tools openrouter:...`` row silently bills
         OpenRouter on a deployment that explicitly opted into Ollama.
      3. Guild row matching the env backend supplies the model.
      4. Otherwise the env model (``TOOLS_MODEL`` / ``OPENROUTER_MODEL``).
    """
    db = getattr(ctx, "db", None) if ctx is not None else None
    gid = getattr(ctx, "guild_id", None) if ctx is not None else None
    gid_int = int(gid) if gid else None

    env_provider = _tools_env_provider() if has_tools else _chat_env_provider()

    # Only consult a per-guild override when its provider matches the env
    # backend. Cross-provider guild overrides are silently ignored so a
    # stale ``,ai model set tools openrouter:...`` row can't bounce a
    # deployment that explicitly opted into Ollama back onto OpenRouter.
    guild_pick = None
    if db and gid_int:
        # has_tools=True uses the "tools" category; has_tools=False uses
        # "chat". Casual chat picking up the "tools" override was a major
        # source of "the bot called 3 different models per reply".
        cat_key = "tools" if has_tools else "chat"
        try:
            guild_pick = await get_guild_default(db, gid_int, cat_key)
        except Exception as exc:
            log.warning("[ai_bridge] get_guild_default(%s) failed: %s", cat_key, exc)

    if model_override:
        return env_provider, model_override

    if guild_pick and guild_pick.model and guild_pick.provider == env_provider:
        return env_provider, guild_pick.model

    return env_provider, _env_model_for(env_provider)


def _model_cats_from_calls(tool_calls: list[dict]) -> set[str]:
    """Return the set of ai_model_defaults category keys touched by *tool_calls*.

    Used after each loop iteration to detect whether domain-specific tools ran
    (risk -> reason, defi -> defi, etc.) so the final text pass can pick a
    more appropriate category model instead of the generic "tools" one.
    """
    cats: set[str] = set()
    for tc in tool_calls:
        name = str((tc.get("function") or {}).get("name") or "")
        spec = ToolRegistry.get(name)
        if spec:
            mc = _TOOL_CAT_TO_MODEL_CAT.get(spec.category)
            if mc:
                cats.add(mc)
    return cats


async def _resolve_domain_model(
    ctx: ToolContext | None,
    called_cats: set[str],
    fallback_model: str | None,
) -> str | None:
    """Return the guild category model for the dominant domain that ran.

    If no domain-specific category override is set, returns *fallback_model*.
    Guild rows whose provider doesn't match the env backend are ignored so
    a stale override can't bounce the final pass to OpenRouter on an
    Ollama-only deployment.
    """
    if not called_cats or ctx is None:
        return fallback_model
    db = getattr(ctx, "db", None)
    gid = getattr(ctx, "guild_id", None)
    if not db or not gid:
        return fallback_model
    env_provider = "ollama" if (Config.TOOLS_BACKEND or "").lower() == "ollama" else "openrouter"
    for cat in called_cats:
        try:
            pick = await get_guild_default(db, int(gid), cat)
            if pick and pick.model and pick.provider == env_provider:
                return pick.model
        except Exception as exc:
            log.warning("[ai_bridge] get_guild_default(%s) failed: %s", cat, exc)
    return fallback_model


async def complete_with_agent_tools(
    messages: list[dict],
    ctx: ToolContext,
    *,
    max_tokens: int = 400,
    temperature: float = 0.7,
    model: str | None = None,
    max_iter: int = _MAX_ITER,
) -> str | None:
    """Run an agent-style tool-calling loop and return the final assistant text.

    Falls back to a plain :func:`complete` call when no non-DANGER tools are
    registered or when the backend is not configured.
    """
    schemas = ToolRegistry.openai_tool_schemas(exclude_danger=True)
    has_tools = bool(schemas)
    provider, resolved_model = await _resolve_tools_pick(ctx, model, has_tools=has_tools)
    if not has_tools:
        # No tools registered -- casual chat path. Single call to the
        # configured chat backend. No retry-on-empty, no cross-provider
        # rescue -- those used to fan one chat turn out into 3 different
        # model calls (Ollama -> OpenRouter env model -> OpenRouter guild
        # override) which is exactly what operators set CHAT_BACKEND to
        # avoid. AI_CROSS_PROVIDER_RESCUE=1 re-enables the fallback for
        # operators who want it back.
        log.info(
            "[ai_bridge] casual chat -> provider=%s model=%s",
            provider, resolved_model or "(env default)",
        )
        final = await _complete_for_provider(
            provider, messages,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if (
            not final
            and Config.AI_CROSS_PROVIDER_RESCUE
            and provider == "ollama"
            and (Config.OPENROUTER_API_KEY or "").strip()
        ):
            log.warning("[ai_bridge] ollama empty, cross-provider rescue -> openrouter")
            final = await _complete_for_provider(
                "openrouter", messages,
                model=Config.OPENROUTER_MODEL or None,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        return final

    # Strip multimodal image blocks and replace them with text
    # ``[ATTACHMENT: <url>]`` markers. The chat model can still SEE that
    # an attachment exists and react to it by calling
    # ``vision.describe_image`` against the URL; that tool downloads the
    # image, base64-encodes it, and routes through the Ollama vision
    # backend for the actual description. This keeps raw Discord image
    # URLs out of the tool-call HTTP path entirely, which is what hosted
    # Ollama Turbo (and tool-only chat models on OpenRouter) insist on.
    convo: list[dict] = _strip_image_blocks(messages)
    _called_model_cats: set[str] = set()  # track domain categories for final-pass model

    # Run up to (max_iter - 1) tool-calling iterations. The final text pass
    # is always a direct complete() on a collapsed convo (see below) so we
    # never send tool_calls history to a model without a tools schema.
    for iteration in range(max_iter):
        try:
            text, tool_calls, _usage = await _dispatch_tool_call(
                convo,
                tools=schemas,
                provider=provider,
                model=resolved_model,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_choice="auto",
            )
        except Exception as exc:
            log.warning("[ai_bridge] tool-call HTTP error: %s", exc)
            break

        if text:
            return text

        if not tool_calls:
            # Model returned no text AND no tool calls. Fall through to the
            # clean plain-completion below.
            break

        # Only let the model fan out a bounded number of parallel calls.
        tool_calls = list(tool_calls)[:_MAX_TOOL_CALLS_PER_TURN]
        _called_model_cats |= _model_cats_from_calls(tool_calls)

        # Record the assistant turn that asked for these calls so the model
        # can see its own request on the follow-up.
        convo.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        })

        # Run the bounded fan-out of tool calls concurrently. The model often
        # asks for (wallet.portfolio + market.snapshot + data.web_search) in
        # the same round; running these serially used to add ~1-3s per round.
        # ``asyncio.gather`` keeps the original call order in the returned
        # list so the appended tool turns line up with the assistant's
        # ``tool_calls`` array (the OpenAI spec requires same ordering).
        executions = await asyncio.gather(
            *(_execute_one_tool_call(tc, ctx) for tc in tool_calls)
        )
        for tc, name, args, result in executions:
            # Capture generated image URLs so the Discord layer can attach
            # them to the reply without relying on the AI to repeat the URL
            # (sanitize_output strips URLs from AI text).
            if name == "image.generate" and result.ok and result.data:
                img_url = str(result.data.get("url") or "").strip()
                if img_url:
                    ctx.generated_images.append(img_url)

            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name or "call",
                "name": name,
                "content": _summarise_result(result),
            })

    # Final text pass: collapse tool_calls/tool turns into plain assistant
    # text so complete() never receives a convo with tool_calls history
    # but no tools schema (which causes 400 errors on most OpenRouter
    # models and wastes the entire timeout budget on a guaranteed failure).
    # If domain tools ran (risk/defi/automation/economy_sim), prefer the
    # guild-configured category model over the generic "tools" model so the
    # final response is authored by the best model for that domain.
    _final_model = await _resolve_domain_model(ctx, _called_model_cats, resolved_model)
    collapsed = _collapse_tool_turns(convo)
    final = await _complete_for_provider(
        provider, collapsed,
        model=_final_model,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    # Cross-provider rescue is OFF by default. Set AI_CROSS_PROVIDER_RESCUE=1
    # to re-enable Ollama->OpenRouter fall-over on empty responses.
    if (
        not final
        and Config.AI_CROSS_PROVIDER_RESCUE
        and provider == "ollama"
        and (Config.OPENROUTER_API_KEY or "").strip()
    ):
        log.warning("[ai_bridge] ollama final pass empty, cross-provider rescue -> openrouter")
        final = await _complete_for_provider(
            "openrouter", collapsed,
            model=Config.OPENROUTER_MODEL or None,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return final


# ── streaming variant ───────────────────────────────────────────────────────

async def complete_with_agent_tools_stream(
    messages: list[dict],
    ctx: ToolContext,
    *,
    max_tokens: int = 400,
    temperature: float = 0.7,
    model: str | None = None,
    max_iter: int = _MAX_ITER,
    tools_override: list[dict] | None = None,
    user_id: int | None = None,
) -> AsyncIterator[dict]:
    """Yield events for a streaming agent tool-calling loop.

    ``tools_override``: when provided, use this list of OpenAI-format tool
    schemas instead of the full registry.  Callers can pass a filtered subset
    to reduce prompt tokens for queries that only need a few tools.

    Why this exists: the non-streaming bridge makes users wait 5-15s in
    silence while the tool loop runs (wallet lookup, market snapshot,
    vision, etc.), then dumps the full reply in one shot. This variant
    surfaces progress so the Discord side can render a live "typing"
    message -- a placeholder that gets edited as events arrive.

    Event shape (all are plain dicts so callers don't import anything):

        {"type": "status", "text": "thinking..."}
          emitted when the loop starts, whenever a tool batch kicks off,
          and when the bridge falls through to a plain completion.

        {"type": "tool_call", "name": "wallet.portfolio", "ok": True}
          emitted once per tool the model invoked, with the success flag
          of the executed ToolResult so the UI can show a green check or
          a red cross per step.

        {"type": "approval_required", "approval_id": 42,
         "tool": "trade.execute", "args": {...}, "reason": "..."}
          emitted when a MUTATE/DANGER tool returned ``approval_required``
          AND we successfully persisted an agent_approvals row. The UI
          uses this to post an approve/deny button card. The row id is
          what ``cogs/approvals.py`` consumes.

        {"type": "delta", "text": "next "}
          emitted repeatedly once the loop produces final text. The final
          text is chunked into word groups and released with a small
          sleep between chunks so the Discord side can paint a live
          typing animation. Concatenating every delta reproduces the
          full reply.

        {"type": "done", "text": "<full answer>"}
          emitted once at the end. ``text`` is the canonical final
          answer; callers may use this to do one last message edit
          with the complete content instead of relying on accumulated
          deltas.

        {"type": "error", "error": "..."}
          emitted if the whole pipeline produces nothing. Callers should
          surface a generic "AI took a nap" style reply to the user.

    The function NEVER raises for backend errors -- it degrades through
    the same plain-complete() fallback path the non-streaming bridge
    uses, so the user always gets something. It only raises for
    programmer errors (bad argument types, etc).
    """
    if tools_override is not None:
        # Re-filter override to preserve the module's safety invariant:
        # DANGER and disabled tools must never reach the model.
        _safe_names = {
            s["function"]["name"]
            for s in ToolRegistry.openai_tool_schemas(exclude_danger=True)
        }
        schemas = [
            s for s in tools_override
            if s["function"]["name"] in _safe_names
        ]
    else:
        schemas = ToolRegistry.openai_tool_schemas(exclude_danger=True)
    has_tools = bool(schemas)
    provider, resolved_model = await _resolve_tools_pick(ctx, model, has_tools=has_tools)
    log.info(
        "[ai_bridge/stream] %s -> provider=%s model=%s",
        "tool loop" if has_tools else "casual chat",
        provider, resolved_model or "(env default)",
    )
    _start = time.monotonic()

    def _meta(text: str, usage: dict | None = None) -> dict:
        """Build a 'done' event with timing/model/usage metadata."""
        model_label = (resolved_model or "").rsplit("/", 1)[-1] or resolved_model or "ai"
        return {
            "type": "done",
            "text": text,
            "model": model_label,
            "elapsed_ms": int((time.monotonic() - _start) * 1000),
            "usage": usage or {},
            # Surfaced from the underlying chat completion's
            # ``choices[0].finish_reason``. The cog uses
            # ``finish_reason == "length"`` to know the model hit its
            # max_tokens cap, which is the signal for offering a
            # Continue button on the reply view.
            "finish_reason": (usage or {}).get("_finish_reason", ""),
        }

    # No tools registered -- fetch a plain completion synchronously, then
    # paint it in via _fake_stream_text. We deliberately do NOT yield raw
    # SSE deltas to the help.py layer: each OpenRouter token is typically
    # 2-3 chars, and Discord's per-message edit throttle (~0.85s) means
    # the user would see ~2-3 chars typed per second, which feels much
    # slower than the underlying generation actually is. Buffering to a
    # full string and then chunking at word boundaries gives the polished
    # "thinking spinner -> chunked paint-in" UX users expect, and the
    # accompanying _meta footer reliably has the usage block (the SSE
    # stream's usage chunk arrives last and was sometimes dropped on
    # provider-side hiccups).
    if not schemas:
        yield {"type": "status", "text": "thinking..."}
        _fallback_usage: list[dict] = []
        final = ""
        async for ev in _race_queue_events(
            lambda cb: _complete_for_provider(
                provider, messages,
                model=resolved_model,
                max_tokens=max_tokens,
                temperature=temperature,
                usage_out=_fallback_usage,
                user_id=user_id,
                on_queue_update=cb,
            )
        ):
            if "_result" in ev:
                final = ev["_result"] or ""
            else:
                yield ev
        # Cross-provider rescue is OFF by default. The legacy fallback chain
        # (same-backend env-default retry + Ollama->OpenRouter fall-over)
        # turned one casual chat into 3 different model calls, which is the
        # exact "bot called 3 models per reply" issue operators on Ollama
        # were trying to escape. AI_CROSS_PROVIDER_RESCUE=1 re-enables it.
        if (
            not final
            and Config.AI_CROSS_PROVIDER_RESCUE
            and provider == "ollama"
            and (Config.OPENROUTER_API_KEY or "").strip()
        ):
            log.warning(
                "[ai_bridge/stream] ollama empty, cross-provider rescue -> openrouter",
            )
            _fallback_usage.clear()
            async for ev in _race_queue_events(
                lambda cb: _complete_for_provider(
                    "openrouter", messages,
                    model=Config.OPENROUTER_MODEL or None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    usage_out=_fallback_usage,
                    user_id=user_id,
                    on_queue_update=cb,
                )
            ):
                if "_result" in ev:
                    final = ev["_result"] or ""
                else:
                    yield ev
        if final:
            async for chunk in _fake_stream_text(final):
                yield {"type": "delta", "text": chunk}
            yield _meta(final, _fallback_usage[0] if _fallback_usage else None)
        else:
            log.warning(
                "[ai_bridge/stream] no-schemas path produced no text "
                "(provider=%s, model=%s, prompt_msgs=%d)",
                provider, resolved_model, len(messages),
            )
            yield {"type": "error", "error": "empty_response"}
        return

    yield {"type": "status", "text": "thinking..."}

    # Strip multimodal image blocks and replace them with [ATTACHMENT: <url>]
    # markers so the text-only tool backends can handle the convo. The
    # vision.describe_image tool is what actually looks at the image.
    convo: list[dict] = _strip_image_blocks(messages)
    _accumulated_usage: dict = {}  # running token totals across all iterations
    _called_model_cats: set[str] = set()  # domain categories for final-pass model

    for iteration in range(max_iter):
        try:
            text: str | None = None
            tool_calls: list[dict] | None = None
            _iter_usage: dict | None = None
            async for ev in _race_queue_events(
                lambda cb: _dispatch_tool_call(
                    convo,
                    tools=schemas,
                    provider=provider,
                    model=resolved_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tool_choice="auto",
                    user_id=user_id,
                    on_queue_update=cb,
                )
            ):
                if "_result" in ev:
                    text, tool_calls, _iter_usage = ev["_result"]
                else:
                    yield ev
        except Exception as exc:
            log.warning("[ai_bridge/stream] dispatch error: %s", exc)
            break

        # Accumulate token counts across all iterations.
        if _iter_usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                _accumulated_usage[k] = _accumulated_usage.get(k, 0) + (_iter_usage.get(k) or 0)
            # finish_reason isn't summable; keep the LATEST iteration's
            # value so the done event reflects how the final text turn
            # ended (length / stop / etc).
            if _iter_usage.get("_finish_reason"):
                _accumulated_usage["_finish_reason"] = _iter_usage["_finish_reason"]

        if text:
            # We already have the final text -- fake-stream it so the
            # Discord side still gets a progressive typing animation.
            async for chunk in _fake_stream_text(text):
                yield {"type": "delta", "text": chunk}
            yield _meta(text, _accumulated_usage or None)
            return

        if not tool_calls:
            # Model returned neither text nor tool calls -- break out and
            # fall through to the plain-complete fallback.
            break

        # Cap the fan-out so a runaway model can't stall the loop.
        tool_calls = list(tool_calls)[:_MAX_TOOL_CALLS_PER_TURN]
        _called_model_cats |= _model_cats_from_calls(tool_calls)

        # Surface which tools we're about to invoke so the UI can
        # swap the typing indicator for "running X, Y..." text.
        names = [str((tc.get("function") or {}).get("name") or "") for tc in tool_calls]
        visible = ", ".join(n for n in names if n) or "tools"
        yield {"type": "status", "text": f"running {visible}..."}

        # Record the assistant turn so the model sees its own request
        # when we round-trip the tool results.
        convo.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        })

        # Run the bounded fan-out of tool calls concurrently. See the
        # non-streaming branch above for the rationale -- same win, same
        # ordering guarantee. Side-effect yields (tool_call / image_generated
        # / search_sources / approval_required) still fire in the original
        # call order so the UI renders the spinner subtext deterministically.
        executions = await asyncio.gather(
            *(_execute_one_tool_call(tc, ctx) for tc in tool_calls)
        )
        for tc, name, args, result in executions:
            yield {"type": "tool_call", "name": name, "ok": bool(result.ok)}

            # Surface generated image URLs so the Discord layer can attach
            # them to the reply message directly.
            if name == "image.generate" and result.ok and result.data:
                img_url = str(result.data.get("url") or "").strip()
                prompt = str(result.data.get("prompt") or "").strip()
                if img_url:
                    ctx.generated_images.append(img_url)
                    yield {"type": "image_generated", "url": img_url, "prompt": prompt}

            # Surface web search sources so the Discord layer can show a
            # "Sources" button alongside the AI reply.
            if name == "data.web_search" and result.ok and result.data:
                sources = result.data.get("results") or []
                if sources:
                    ctx.search_sources.extend(sources)
                    yield {"type": "search_sources", "results": sources}

            # If the tool wanted explicit approval, persist an approval
            # row and surface it so the Discord UI can post a card.
            # The AI still sees a normal tool turn saying
            # "approval_required, awaiting user" so it can compose a
            # sensible reply.
            if (not result.ok) and result.error == "approval_required":
                approval_id = await _maybe_request_approval(ctx, name, args, result)
                if approval_id is not None:
                    yield {
                        "type": "approval_required",
                        "approval_id": approval_id,
                        "tool": name,
                        "args": args,
                        "reason": str(result.meta.get("reason") or ""),
                    }

            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name or "call",
                "name": name,
                "content": _summarise_result(result),
            })

    # Fallback path: the loop produced no final text (e.g. tool storm,
    # exception during dispatch, model returned empty). Real-stream a plain
    # completion so the user sees tokens as the model produces them instead
    # of waiting for the entire reply and then watching it animate in via
    # _fake_stream_text. After a multi-iteration tool loop the user has
    # already been staring at a spinner for several seconds, so cutting the
    # time-to-first-token here is the single biggest UX win in this file.
    # Use _collapse_tool_turns so complete_stream() never sees tool_calls
    # history without a tools schema -- that causes 400 errors on most models.
    # If domain tools ran, prefer the guild category model for the final reply.
    # Final-pass: fetch the assistant text synchronously, then chunk-stream
    # it via _fake_stream_text. We tried real-streaming this for a release
    # but each ~2-3 char OpenRouter delta becoming a throttled Discord edit
    # made replies feel CHOPPY (~2-3 chars typed per second under the
    # 0.85s edit throttle), which was worse UX than the polished
    # "spinner-then-chunks" flow even though TTFB was technically faster.
    # Going back to the synchronous complete() also restores the token
    # counts in the footer reliably (the SSE usage chunk was sometimes
    # dropped on provider-side hiccups, leaving the footer empty).
    yield {"type": "status", "text": "wrapping up..."}
    _final_model = await _resolve_domain_model(ctx, _called_model_cats, resolved_model)
    _collapsed = _collapse_tool_turns(convo)
    _final_usage: list[dict] = []
    final: str | None = None
    async for ev in _race_queue_events(
        lambda cb: _complete_for_provider(
            provider, _collapsed,
            model=_final_model,
            max_tokens=max_tokens,
            temperature=temperature,
            usage_out=_final_usage,
            user_id=user_id,
            on_queue_update=cb,
        )
    ):
        if "_result" in ev:
            final = ev["_result"]
        else:
            yield ev
    # Cross-provider rescue is OFF by default. Set AI_CROSS_PROVIDER_RESCUE=1
    # to re-enable Ollama->OpenRouter fall-over on empty responses.
    if (
        not final
        and Config.AI_CROSS_PROVIDER_RESCUE
        and provider == "ollama"
        and (Config.OPENROUTER_API_KEY or "").strip()
    ):
        log.warning(
            "[ai_bridge/stream] ollama final pass empty, cross-provider rescue -> openrouter",
        )
        _final_usage.clear()
        async for ev in _race_queue_events(
            lambda cb: _complete_for_provider(
                "openrouter", _collapsed,
                model=Config.OPENROUTER_MODEL or None,
                max_tokens=max_tokens,
                temperature=temperature,
                usage_out=_final_usage,
                user_id=user_id,
                on_queue_update=cb,
            )
        ):
            if "_result" in ev:
                final = ev["_result"]
            else:
                yield ev
    if _final_usage:
        u = _final_usage[0]
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            _accumulated_usage[k] = _accumulated_usage.get(k, 0) + (u.get(k) or 0)
        # Propagate finish_reason from the fallback completion so the
        # Continue button signal reaches the cog.
        if u.get("_finish_reason"):
            _accumulated_usage["_finish_reason"] = u["_finish_reason"]
    if final:
        async for chunk in _fake_stream_text(final):
            yield {"type": "delta", "text": chunk}
        yield _meta(final, _accumulated_usage or None)
    else:
        log.warning(
            "[ai_bridge/stream] final pass produced no text "
            "(provider=%s, model=%s)",
            provider, _final_model,
        )
        yield {"type": "error", "error": "empty_response"}


# Chunk granularity for the FAKE-stream path. The full text is already in
# hand by the time _fake_stream_text runs (it's invoked AFTER the synchronous
# complete() returns), so this controls how the polished "typing" animation
# paces out. 24 chars per chunk + 0.04s per chunk gives a ~600 chars/sec
# emit rate; combined with the help.py-side edit throttle (~0.85s), the
# user sees the spinner end, then 1-2 visible chunk edits, then the full
# reply land. We tried sleep=0 + chunks=48 for a release but the throttle
# absorbed all the chunks into a single edit, so the typing animation
# disappeared and the reply popped in all at once.
_FAKE_STREAM_CHUNK_CHARS = 24
_FAKE_STREAM_CHUNK_SLEEP = 0.04


async def _fake_stream_text(text: str) -> AsyncIterator[str]:
    """Yield ``text`` in small chunks on word boundaries with a tiny sleep.

    Used for the non-tool-calling iteration text which we already have in
    hand (we had to parse ``tool_calls`` in one piece, so we couldn't
    real-stream through the tool loop). Emitting the text progressively
    lets the Discord side still paint a "typing" animation instead of
    popping the whole reply in at once.
    """
    import asyncio as _aio

    if not text:
        return
    words = text.split(" ")
    buf: list[str] = []
    buf_len = 0
    for w in words:
        add_len = len(w) + (1 if buf else 0)
        if buf and buf_len + add_len > _FAKE_STREAM_CHUNK_CHARS:
            yield (" ".join(buf) + " ")
            await _aio.sleep(_FAKE_STREAM_CHUNK_SLEEP)
            buf = [w]
            buf_len = len(w)
        else:
            buf.append(w)
            buf_len += add_len
    if buf:
        yield " ".join(buf)


async def _maybe_request_approval(
    ctx: ToolContext,
    tool_name: str,
    args: dict,
    result: ToolResult,
) -> int | None:
    """Persist an agent_approvals row for a tool that demanded approval.

    Returns the row id on success, ``None`` if persistence failed (in
    which case the caller just surfaces the approval_required message
    without a follow-up approval card).
    """
    db = getattr(ctx, "db", None)
    if db is None:
        return None
    try:
        return await request_approval(
            db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            tool=tool_name,
            args=args,
            reason=str(result.meta.get("reason") or "approval required"),
        )
    except Exception as exc:
        log.warning("[ai_bridge/stream] request_approval failed: %s", exc)
        return None
