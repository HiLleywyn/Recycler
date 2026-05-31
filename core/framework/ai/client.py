"""Async OpenRouter + Ollama chat completion client."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from typing import Awaitable, Callable, AsyncIterator

import aiohttp

from core.config import Config
from .queue import ChatQueue
from .safety import _BASE_SYSTEM_INSTRUCTIONS, sanitize_output

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=100, connect=6, sock_connect=6, sock_read=90)
_REQUEST_TIMEOUT_VISION = aiohttp.ClientTimeout(total=130, connect=6, sock_connect=6, sock_read=120)
# Cap concurrent HTTP calls against the AI backend at the Python layer.
# The previous value (8) was a hard bottleneck: a multi-iteration tool loop
# uses 3-4 sequential slots, so 2-3 active users were enough to queue everyone
# else behind a semaphore that sat OUTSIDE the per-request asyncio.wait_for
# budget in cogs/help.py. New requests would never even start their HTTP
# call before the 40s outer timeout fired, producing the "AI timed out"
# message that looked like a connection drop. 32 leaves plenty of headroom
# for bursty AI replies while still letting aiohttp's TCPConnector (limit=64,
# limit_per_host=32) act as the real flow-control point.
_MAX_CONCURRENT_REQUESTS = 32
_MAX_HISTORY_MESSAGES = 20
_MAX_MESSAGE_CHARS = 2400


def _has_image_blocks(messages: list[dict]) -> bool:
    """Return True if any message contains multimodal image_url content blocks."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def _strip_image_blocks(messages: list[dict]) -> list[dict]:
    """Replace multimodal ``image_url`` blocks with ``[ATTACHMENT: <url>]`` text.

    Used by the tool-calling path: hosted Ollama Turbo rejects OpenAI-style
    ``image_url`` blocks that carry remote URLs, and the cheap OpenRouter
    chat model (Gemma 4b) doesn't actually read them either. Instead we
    hand the tool loop a text-only convo where attachments are tagged with
    ``[ATTACHMENT: <url>]`` markers, and let the model invoke the
    ``vision.describe_image`` tool to fetch a real description via the
    vision-capable Ollama backend.

    Text-only messages round-trip unchanged. A message that had nothing but
    images becomes a plain text message whose content is just the newline-
    joined ``[ATTACHMENT: ...]`` markers.
    """
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        text_parts: list[str] = []
        markers: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = str(block.get("text") or "")
                if t:
                    text_parts.append(t)
            elif block.get("type") == "image_url":
                img = block.get("image_url") or {}
                url = img.get("url") if isinstance(img, dict) else None
                if url:
                    markers.append(f"[ATTACHMENT: {url}]")
        merged = " ".join(text_parts).strip()
        if markers:
            merged = (merged + "\n" if merged else "") + "\n".join(markers)
        new_msg = dict(m)
        new_msg["content"] = merged
        out.append(new_msg)
    return out

def _backend_base() -> str:
    """OpenAI-compatible base URL of the AI backend.

    Recycler ships no built-in model brain: inference is served by the
    Sojourns platform (Disco / AI Chat, the clanker pattern controller,
    memory and tools all live there). Operators point ``AI_BACKEND_URL``
    at their Sojourns deploy; the default keeps a standalone OpenRouter
    path working so the bot is never hard-wired to a single host.
    """
    return Config.AI_BACKEND_URL or "https://openrouter.ai/api/v1"


def _backend_chat_url() -> str:
    return f"{_backend_base()}/chat/completions"


def _backend_key() -> str:
    return Config.AI_BACKEND_KEY or Config.OPENROUTER_API_KEY


_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

# Per-backend chat queue with per-user serialization. Replaces the previous
# single ``asyncio.Semaphore(32)`` (kept above as the absolute ceiling, but
# the queue's per-backend caps are the real flow-control point now). See
# ``core/framework/ai/queue.py`` for the algorithm.
chat_queue: ChatQueue = ChatQueue.from_config()

# Type alias for the position-change callback the streaming bridge wires
# through so the placeholder UI can render "queued (#3)".
_PosCb = Callable[[int], Awaitable[None]]


def _inject_base_instructions(messages: list[dict]) -> list[dict]:
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            new_messages = list(messages)
            new_messages[i] = {
                "role": "system",
                "content": m["content"].rstrip() + " " + _BASE_SYSTEM_INSTRUCTIONS,
            }
            return new_messages
    return [{"role": "system", "content": _BASE_SYSTEM_INSTRUCTIONS}] + list(messages)


def _clamp_messages(messages: list[dict]) -> list[dict]:
    """Bound prompt size so AI calls stay fast and predictable.

    Preserves ``tool_calls`` on assistant turns and ``tool_call_id`` /
    ``name`` on tool turns so multi-iteration tool-calling loops round-trip
    correctly. An assistant turn with ``content=None`` is allowed (OpenAI
    spec) and kept as None rather than stringified.
    """
    if not messages:
        return messages
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    tail = messages[1:] if system else messages
    trimmed_tail = []
    for m in tail[-_MAX_HISTORY_MESSAGES:]:
        content = m.get("content", "")
        role = m.get("role", "user")
        if isinstance(content, list):
            # Multimodal content blocks  -  truncate text parts only, keep image_url blocks intact
            trimmed_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    trimmed_content.append({"type": "text", "text": block["text"][:_MAX_MESSAGE_CHARS]})
                else:
                    trimmed_content.append(block)
            entry: dict = {"role": role, "content": trimmed_content}
        elif content is None:
            entry = {"role": role, "content": None}
        else:
            entry = {"role": role, "content": str(content)[:_MAX_MESSAGE_CHARS]}
        # Preserve tool-calling fields so multi-turn tool loops round-trip.
        if "tool_calls" in m:
            entry["tool_calls"] = m["tool_calls"]
        if "tool_call_id" in m:
            entry["tool_call_id"] = m["tool_call_id"]
        if "name" in m:
            entry["name"] = m["name"]
        trimmed_tail.append(entry)
    if system:
        return [{"role": "system", "content": str(system.get("content", ""))}] + trimmed_tail
    return trimmed_tail


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=_REQUEST_TIMEOUT,
                connector=aiohttp.TCPConnector(
                    limit=64,
                    limit_per_host=32,
                    ttl_dns_cache=300,
                    keepalive_timeout=75,
                    enable_cleanup_closed=True,
                ),
            )
        return _session


async def close_client() -> None:
    """Close the shared HTTP session used for AI requests."""
    global _session
    async with _session_lock:
        if _session is not None and not _session.closed:
            await _session.close()
        _session = None


async def complete(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.8,
    _usage_out: list | None = None,
    *,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> str | None:
    """Call OpenRouter chat completion. Returns content string or None on error/disabled.

    If ``_usage_out`` is a list it will have ``{"prompt_tokens": N,
    "completion_tokens": N, "total_tokens": N}`` appended after a successful
    call so callers can accumulate token counts across multiple rounds.

    ``user_id`` and ``on_queue_update`` route the call through the per-user
    chat queue. Background / system callers pass ``kind="system"`` so they
    can share a small reserved sub-pool that user chat can't deplete.
    """
    messages = _clamp_messages(_inject_base_instructions(messages))
    if not Config.AI_ENABLED:
        return None
    key = _backend_key()
    if not key:
        return None
    vision = _has_image_blocks(messages)
    timeout = _REQUEST_TIMEOUT_VISION if vision else _REQUEST_TIMEOUT
    for attempt in range(2):
        try:
            async with chat_queue.acquire(
                backend="openrouter", user_id=user_id,
                kind="system" if kind == "system" else "chat",
                on_position_change=on_queue_update,
            ):
                sess = await _get_session()
                async with sess.post(
                    _backend_chat_url(),
                    json={
                        "model": model or Config.OPENROUTER_MODEL,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://econbot",
                        "X-Title": "Recycler",
                    },
                    timeout=timeout,
                ) as r:
                    if r.status == 429:
                        if attempt < 1:
                            retry_after = float(r.headers.get("retry-after", "1.0"))
                            await asyncio.sleep(min(retry_after, 10.0))
                            continue
                        return None
                    if r.status >= 500 and attempt < 1:
                        await asyncio.sleep(0.5)
                        continue
                    if r.status != 200:
                        body = await r.text()
                        logging.warning("[ai] OpenRouter HTTP %s: %.200s", r.status, body)
                        return None
                    data = await r.json()
                    usage = data.get("usage")
                    choices = data.get("choices", [{}])
                    finish = choices[0].get("finish_reason") if choices else None
                    if finish:
                        usage = dict(usage or {})
                        usage["_finish_reason"] = finish
                    if usage and _usage_out is not None:
                        _usage_out.append(usage)
                    message = choices[0].get("message", {}) if choices else {}
                    content = message.get("content")
                    if content is None:
                        if finish == "length":
                            logging.debug("[ai] Response truncated (finish_reason=length), returning partial")
                            # .get() returns None (not "") when key exists with null value;
                            # use `or ""` to handle both absent-key and explicit-null cases.
                            content = message.get("content") or ""
                            if not content:
                                return None
                        else:
                            logging.debug("[ai] Empty response from model (finish_reason=%s)", finish)
                            return None
                    return sanitize_output(content.strip())
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
            if attempt < 1:
                logging.warning("[ai] Network error on attempt %d, retrying: %s", attempt + 1, exc)
            else:
                logging.warning("[ai] Network error after retries: %s", exc)
        except Exception:
            logging.exception("[ai] Unexpected error during OpenRouter call")
            return None
    return None


async def complete_default(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.8,
    _usage_out: list | None = None,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> str | None:
    """Backend-aware non-streaming completion. Routes to the configured backend.

    When ``Config.TOOLS_BACKEND == "ollama"`` this goes to Ollama using
    ``Config.TOOLS_MODEL`` (so background side-effects like memory refresh
    and passive trait extraction don't bypass the operator's chosen backend
    and silently bill OpenRouter on every chat turn). Otherwise routes to
    OpenRouter using ``Config.OPENROUTER_MODEL``. Falls over to the other
    backend if the primary returns empty AND an API key is available, so a
    transient Ollama Cloud hiccup doesn't drop the call entirely.
    """
    backend = (Config.TOOLS_BACKEND or "openrouter").lower()
    if backend == "ollama":
        resolved = model or Config.TOOLS_MODEL or "llama3.2"
        out = await complete_ollama(
            messages,
            model=resolved,
            max_tokens=max_tokens,
            temperature=temperature,
            _usage_out=_usage_out,
            user_id=user_id,
            on_queue_update=on_queue_update,
            kind=kind,
        )
        # Cross-provider rescue is opt-in via AI_CROSS_PROVIDER_RESCUE. The
        # default is OFF: operators on Ollama-only deployments explicitly
        # chose Ollama and shouldn't see silent OpenRouter charges every
        # time Ollama Cloud burps an empty response. Set
        # AI_CROSS_PROVIDER_RESCUE=1 to re-enable the old behaviour.
        if out or not Config.AI_CROSS_PROVIDER_RESCUE:
            return out
        if not (Config.OPENROUTER_API_KEY or "").strip():
            return out
        return await complete(
            messages,
            model=Config.OPENROUTER_MODEL or None,
            max_tokens=max_tokens,
            temperature=temperature,
            _usage_out=_usage_out,
            user_id=user_id,
            on_queue_update=on_queue_update,
            kind=kind,
        )
    return await complete(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        _usage_out=_usage_out,
        user_id=user_id,
        on_queue_update=on_queue_update,
        kind=kind,
    )


async def complete_stream(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.8,
    _usage_out: list | None = None,
    include_usage: bool = True,
    _error_out: list | None = None,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> AsyncIterator[str]:
    """Stream OpenRouter chat completion tokens as ``str`` deltas.

    Yields each content-delta as soon as it arrives on the SSE stream,
    so callers can update a Discord message progressively (with their
    own rate-limit throttling). On any error the generator simply ends
    without raising; callers should have a non-streaming fallback.

    When ``_usage_out`` is provided, the final SSE chunk's ``usage`` block
    (enabled via OpenRouter's ``stream_options.include_usage``) is appended
    as ``{"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}``
    so callers can still surface token counts in the reply footer even
    though they consumed the body via streaming.

    ``include_usage`` controls whether we send ``stream_options`` at all.
    Some upstream providers behind OpenRouter (notably certain Gemini and
    Llama routes) reject the field with a 400, in which case the caller
    can retry the same request with ``include_usage=False`` to get raw
    streaming back. ``_error_out`` (when supplied) accumulates a short
    string describing why the stream produced no tokens so the caller can
    surface a useful reason on the fallback path.

    This is wired by :func:`core.framework.agent_tools.ai_bridge.complete_with_agent_tools_stream`
    for the final (text-producing) iteration of the agent tool loop.
    Intermediate tool-calling iterations run non-streaming so the
    ``tool_calls`` envelope can be parsed in one piece.
    """
    def _err(msg: str) -> None:
        if _error_out is not None:
            _error_out.append(msg)
    key = _backend_key() if Config.AI_ENABLED else ""
    if not key:
        _err("no_api_key")
        return
    messages = _clamp_messages(_inject_base_instructions(messages))
    timeout = _REQUEST_TIMEOUT_VISION if _has_image_blocks(messages) else _REQUEST_TIMEOUT
    payload: dict = {
        "model": model or Config.OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if include_usage:
        # Ask OpenRouter to emit a final SSE chunk with the usage block so
        # the streaming reply can still report prompt/completion token totals
        # in the message footer. Providers that don't honour this just skip
        # the extra chunk -- but a few reject the request outright, which
        # the caller mitigates by retrying with include_usage=False.
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://econbot",
        "X-Title": "Recycler",
        "Accept": "text/event-stream",
    }
    try:
        async with chat_queue.acquire(
            backend="openrouter", user_id=user_id,
            kind="system" if kind == "system" else "chat",
            on_position_change=on_queue_update,
        ):
            sess = await _get_session()
            async with sess.post(
                _backend_chat_url(),
                json=payload, headers=headers, timeout=timeout,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logging.warning(
                        "[ai/stream] OpenRouter HTTP %s (model=%s, include_usage=%s): %.200s",
                        r.status, payload["model"], include_usage, body,
                    )
                    _err(f"http_{r.status}")
                    return
                async for line in r.content:
                    if not line:
                        continue
                    try:
                        raw = line.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        continue
                    if not raw or not raw.startswith("data:"):
                        continue
                    payload_str = raw[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        return
                    try:
                        obj = _json.loads(payload_str)
                    except Exception:
                        continue
                    # The usage-only chunk has choices=[] (or omits choices)
                    # and carries a top-level "usage" block. Capture it and
                    # keep iterating in case more data follows.
                    usage = obj.get("usage")
                    if usage and _usage_out is not None:
                        _usage_out.append(usage)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
    except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
        logging.warning("[ai/stream] network error: %s", exc)
        _err(f"network_{type(exc).__name__}")
    except Exception:
        logging.warning("[ai/stream] stream crashed", exc_info=True)
        _err("exception")


async def complete_tools(
    messages: list[dict],
    max_tokens: int = 300,
    temperature: float = 0.8,
) -> str | None:
    """Run a tool-augmented AI call.

    Routes to Ollama when TOOLS_BACKEND=ollama, otherwise falls back to
    OpenRouter using TOOLS_MODEL (or OPENROUTER_MODEL if unset).

    Historical note: TOOLS_MODEL used to default to "llama3.2" even on the
    OpenRouter path, which silently routed tool-matched chat turns (including
    multimodal image_url blocks) to a text-only model. The default is now
    empty so the OpenRouter path uses OPENROUTER_MODEL, which is vision-capable.
    """
    if Config.TOOLS_BACKEND == "ollama":
        return await complete_ollama(
            messages,
            model=Config.TOOLS_MODEL or "llama3.2",
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return await complete(
        messages,
        model=Config.TOOLS_MODEL or None,
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def complete_with_tool_calls(
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    inject_base_instructions: bool = False,
    tool_choice: str = "auto",
    *,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> tuple[str | None, list[dict] | None, dict | None]:
    """Call OpenRouter with function/tool definitions.

    Returns ``(text, tool_calls)`` where exactly one is non-None on success.
    Returns ``(None, None)`` on error or when the AI key is not configured.

    ``tool_calls`` is the raw list of OpenAI-format tool call objects
    ``[{"id": str, "type": "function", "function": {"name": str, "arguments": str}}]``.

    When ``inject_base_instructions`` is True, the shared Discoin persona and
    safety rails are merged into the first system message (same behaviour as
    :func:`complete`). The diagnose AI flow leaves this off so its own system
    prompt is the single source of instructions.

    ``tool_choice`` maps directly to the OpenAI API field. Pass ``"required"``
    to force the model to call at least one tool on the first iteration of the
    agent loop so it cannot respond with a text acknowledgment ("I'll search
    for that!") instead of actually invoking the tool.
    """
    key = _backend_key() if Config.AI_ENABLED else ""
    if not key:
        return None, None, None
    if inject_base_instructions:
        messages = _inject_base_instructions(messages)
    messages = _clamp_messages(messages)
    timeout = _REQUEST_TIMEOUT_VISION if _has_image_blocks(messages) else _REQUEST_TIMEOUT
    for attempt in range(2):
        try:
            async with chat_queue.acquire(
                backend="openrouter", user_id=user_id,
                kind="system" if kind == "system" else "chat",
                on_position_change=on_queue_update,
            ):
                sess = await _get_session()
                payload: dict = {
                    "model": model or Config.OPENROUTER_MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                # Only include tool fields when tools are actually provided.
                # Some OpenRouter backends misbehave when tools=[] is sent.
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = tool_choice
                async with sess.post(
                    _backend_chat_url(),
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://econbot",
                        "X-Title": "Recycler",
                    },
                    timeout=timeout,
                ) as r:
                    if r.status == 429:
                        if attempt < 1:
                            retry_after = float(r.headers.get("retry-after", "1.0"))
                            await asyncio.sleep(min(retry_after, 10.0))
                            continue
                        return None, None, None
                    if r.status >= 500 and attempt < 1:
                        await asyncio.sleep(0.5)
                        continue
                    if r.status != 200:
                        body = await r.text()
                        logging.warning("[ai/tools] OpenRouter HTTP %s: %.200s", r.status, body)
                        return None, None, None
                    data = await r.json()
                    usage: dict | None = data.get("usage") or None
                    choices = data.get("choices", [{}])
                    if not choices:
                        return None, None, usage
                    message = choices[0].get("message", {})
                    tool_calls = message.get("tool_calls")
                    content = message.get("content")
                    # Capture finish_reason so the bridge can surface a
                    # Continue button when the model hit its max_tokens
                    # cap. We stash it on the usage dict (creating one
                    # if absent) to avoid changing the tuple signature
                    # that callers + test mocks already match.
                    finish_reason = (choices[0].get("finish_reason") or "") if choices else ""
                    if finish_reason:
                        if usage is None:
                            usage = {}
                        usage["_finish_reason"] = finish_reason
                    if tool_calls:
                        return None, tool_calls, usage
                    if content:
                        return sanitize_output(content.strip()), None, usage
                    return None, None, usage
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
            if attempt < 1:
                logging.warning("[ai/tools] Network error on attempt %d, retrying: %s", attempt + 1, exc)
            else:
                logging.warning("[ai/tools] Network error after retries: %s", exc)
        except Exception:
            logging.exception("[ai/tools] Unexpected error during tool-call request")
            return None, None, None
    return None, None, None


def _ollama_endpoint() -> tuple[str, dict[str, str]]:
    """Resolve ``(url, headers)`` for the configured Ollama server."""
    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1").rstrip("/")
    if base_url.endswith("/v1"):
        url = f"{base_url}/chat/completions"
    else:
        url = f"{base_url}/v1/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return url, headers


def _apply_ollama_keep_alive(payload: dict) -> dict:
    """Attach ``keep_alive`` to an Ollama OpenAI-compat request payload.

    Ollama keeps a model resident only for ``keep_alive`` seconds after the
    last request. Without it the cloud unloads ``gemma4:31b-cloud`` etc.
    after ~30s idle, so every "AI took a nap" gap costs a 5-15s cold reload.
    Setting the field on every request keeps the model warm without needing
    a separate ``/api/generate?keep_alive=...`` call.
    """
    ka = (Config.OLLAMA_KEEP_ALIVE or "").strip()
    if ka:
        payload["keep_alive"] = ka
    return payload


async def complete_with_tool_calls_ollama(
    messages: list[dict],
    tools: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    tool_choice: str = "auto",
    inject_base_instructions: bool = False,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> tuple[str | None, list[dict] | None, dict | None]:
    """Function-calling chat against an Ollama OpenAI-compat endpoint.

    Mirrors :func:`complete_with_tool_calls` but points at Ollama so local
    tool-capable models (gemma3:27b, llama3.1:70b, etc.) can run the
    agent loop. Returns ``(text, tool_calls, usage)`` with exactly one of
    text/tool_calls non-None on success.
    """
    if inject_base_instructions:
        messages = _inject_base_instructions(messages)
    messages = _clamp_messages(messages)

    url, headers = _ollama_endpoint()
    resolved_model = model or Config.TOOLS_MODEL or "llama3.2"
    timeout = _REQUEST_TIMEOUT_VISION if _has_image_blocks(messages) else _REQUEST_TIMEOUT

    payload: dict = _apply_ollama_keep_alive({
        "model": resolved_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    })
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    for attempt in range(2):
        try:
            async with chat_queue.acquire(
                backend="ollama", user_id=user_id,
                kind="system" if kind == "system" else "chat",
                on_position_change=on_queue_update,
            ):
                sess = await _get_session()
                async with sess.post(url, json=payload, headers=headers, timeout=timeout) as r:
                    if r.status >= 500 and attempt < 1:
                        await asyncio.sleep(0.5)
                        continue
                    if r.status != 200:
                        body = await r.text()
                        logging.warning(
                            "[ai/ollama/tools] HTTP %s: %.200s", r.status, body,
                        )
                        return None, None, None
                    data = await r.json()
                    usage: dict | None = data.get("usage") or None
                    choices = data.get("choices", [{}])
                    if not choices:
                        return None, None, usage
                    message = choices[0].get("message", {}) or {}
                    tool_calls = message.get("tool_calls")
                    content = message.get("content")
                    finish_reason = (choices[0].get("finish_reason") or "") if choices else ""
                    if finish_reason:
                        if usage is None:
                            usage = {}
                        usage["_finish_reason"] = finish_reason
                    if tool_calls:
                        return None, tool_calls, usage
                    if content:
                        return sanitize_output(str(content).strip()), None, usage
                    return None, None, usage
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
            if attempt < 1:
                logging.warning("[ai/ollama/tools] Network error on attempt %d, retrying: %s", attempt + 1, exc)
            else:
                logging.warning("[ai/ollama/tools] Network error after retries: %s", exc)
        except Exception:
            logging.exception("[ai/ollama/tools] Unexpected error")
            return None, None, None
    return None, None, None


async def complete_ollama_vision(
    prompt: str,
    image_data_uri: str,
    *,
    model: str | None = None,
    max_tokens: int = 350,
    temperature: float = 0.2,
) -> str | None:
    """Describe an image via Ollama's vision endpoint.

    ``image_data_uri`` must be a ``data:image/...;base64,<blob>`` URI.
    Hosted Ollama Turbo refuses remote URLs, so the caller (currently
    ``vision.describe_image``) is responsible for downloading and
    base64-encoding the image before calling this.

    Two-format strategy, because hosted Ollama Cloud quietly rejects
    OpenAI-compat multimodal on a lot of model routes (kimi-k2, qwen2-vl,
    most minicpm variants) with a generic HTTP 500:

      1. First try the OpenAI-compatible endpoint ``/v1/chat/completions``
         with ``content = [{"type": "text", ...}, {"type": "image_url",
         "image_url": {"url": data_uri}}]``. Local Ollama handles this
         fine and some cloud models do too, so it's the happy path.

      2. If that returns non-200 OR returns 200 with empty content, fall
         through to Ollama's NATIVE chat endpoint ``/api/chat`` with
         ``{"images": [<raw base64 without data: prefix>]}`` on the
         message. The native format is what Ollama's vision pipeline
         actually consumes under the hood -- the OpenAI-compat shim on
         cloud sometimes can't translate the image_url back to it,
         producing the 500s this function was returning None on.

    Returns the description string, or ``None`` on hard failure after
    both attempts.
    """
    resolved_model = model or Config.VISION_MODEL or Config.TOOLS_MODEL or "gemma3:27b"
    _img_kb = len(image_data_uri) // 1024

    # Attempt 1: OpenAI-compat /v1/chat/completions
    oai_url, headers = _ollama_endpoint()
    oai_payload = _apply_ollama_keep_alive({
        "model": resolved_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_uri}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    })
    oai_failure_reason: str | None = None
    for attempt in range(2):
        try:
            # Vision is always a tool-loop subroutine (the user is waiting on
            # the parent chat turn), so route through the Ollama backend's
            # queue lane. No user_id -- the parent chat's ticket already
            # bills against the user; this is the tool's own slot.
            async with chat_queue.acquire(
                backend="ollama", user_id=None, kind="system",
            ):
                sess = await _get_session()
                async with sess.post(
                    oai_url, json=oai_payload, headers=headers, timeout=_REQUEST_TIMEOUT_VISION,
                ) as r:
                    if r.status >= 500 and attempt < 1:
                        await asyncio.sleep(0.5)
                        continue
                    if r.status != 200:
                        body = await r.text()
                        logging.warning(
                            "[ai/ollama/vision] OpenAI-compat HTTP %s "
                            "(model=%s, img=%skB, endpoint=%s): %.300s -- "
                            "falling through to native /api/chat",
                            r.status, resolved_model, _img_kb, oai_url, body,
                        )
                        oai_failure_reason = f"http_{r.status}"
                        break
                    data = await r.json()
                    choices = data.get("choices", [{}])
                    message = choices[0].get("message", {}) if choices else {}
                    content = message.get("content")
                    if content:
                        return sanitize_output(str(content).strip())
                    oai_failure_reason = "empty_content"
                    break
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
            if attempt < 1:
                logging.warning(
                    "[ai/ollama/vision] OpenAI-compat network error on attempt %d, retrying: %s",
                    attempt + 1, exc,
                )
                continue
            logging.warning(
                "[ai/ollama/vision] OpenAI-compat network error after retries: %s", exc,
            )
            oai_failure_reason = f"network_{type(exc).__name__}"
            break
        except Exception:
            logging.exception("[ai/ollama/vision] OpenAI-compat unexpected error")
            oai_failure_reason = "exception"
            break

    # Attempt 2: Ollama native /api/chat with images[] on the message.
    # Rebuild the endpoint without the /v1 suffix since this path lives
    # under /api on the same host.
    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    native_url = f"{base_url}/api/chat"

    # Strip the data: prefix from the URI -- native format wants raw base64.
    if image_data_uri.startswith("data:") and ";base64," in image_data_uri:
        b64_only = image_data_uri.split(";base64,", 1)[1]
    else:
        b64_only = image_data_uri

    native_payload = _apply_ollama_keep_alive({
        "model": resolved_model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [b64_only],
            }
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    })
    logging.info(
        "[ai/ollama/vision] retrying via native /api/chat (model=%s, img=%skB, oai_failed=%s)",
        resolved_model, _img_kb, oai_failure_reason or "n/a",
    )
    try:
        async with chat_queue.acquire(
            backend="ollama", user_id=None, kind="system",
        ):
            sess = await _get_session()
            async with sess.post(
                native_url, json=native_payload, headers=headers, timeout=_REQUEST_TIMEOUT_VISION,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logging.warning(
                        "[ai/ollama/vision] native HTTP %s (model=%s, img=%skB, endpoint=%s): %.300s",
                        r.status, resolved_model, _img_kb, native_url, body,
                    )
                    return None
                data = await r.json()
                # Native /api/chat returns {"message": {"content": "..."}}
                # (not the OpenAI choices[] shape).
                message = data.get("message") or {}
                content = message.get("content")
                if not content:
                    logging.warning(
                        "[ai/ollama/vision] native returned empty content (model=%s)",
                        resolved_model,
                    )
                    return None
                return sanitize_output(str(content).strip())
    except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
        logging.warning("[ai/ollama/vision] native network error: %s", exc)
        return None
    except Exception:
        logging.exception("[ai/ollama/vision] native unexpected error")
        return None


async def complete_ollama(
    messages: list[dict],
    model: str = "llama3.2",
    max_tokens: int = 256,
    temperature: float = 0.8,
    _error_out: list | None = None,
    _usage_out: list | None = None,
    *,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> str | None:
    """Call local Ollama via its OpenAI-compatible endpoint.

    ``_error_out`` (when supplied) accumulates a short string describing
    why the call failed so callers can surface a useful reason instead
    of just logging a None. ``_usage_out`` (when supplied) appends the
    ``{"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}``
    block from a successful response so the streaming bridge can surface
    token counts in the reply footer for Ollama deployments too. Mirrors
    the same params on :func:`complete`.
    """
    def _err(msg: str) -> None:
        if _error_out is not None:
            _error_out.append(msg)
    messages = _clamp_messages(_inject_base_instructions(messages))
    url, headers = _ollama_endpoint()
    timeout = _REQUEST_TIMEOUT_VISION if _has_image_blocks(messages) else _REQUEST_TIMEOUT
    # Two attempts max (was three). The third retry just multiplied the
    # 60-90s wait on a slow gemma4:31b-cloud route into 180-270s, blowing
    # past the outer wait_for budget and surfacing "AI didn't respond"
    # even though the very first response was on its way. One retry on
    # 429/5xx is enough to ride out a transient hiccup.
    _backoffs = [0.5]
    for attempt in range(2):
        try:
            async with chat_queue.acquire(
                backend="ollama", user_id=user_id,
                kind="system" if kind == "system" else "chat",
                on_position_change=on_queue_update,
            ):
                sess = await _get_session()
                _payload = _apply_ollama_keep_alive({
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                })
                async with sess.post(
                    url,
                    json=_payload,
                    headers=headers,
                    timeout=timeout,
                ) as r:
                    if r.status == 429 and attempt < 1:
                        retry_after = float(r.headers.get("retry-after", "1.0"))
                        await asyncio.sleep(min(retry_after, 5.0))
                        continue
                    if r.status >= 500 and attempt < 1:
                        await asyncio.sleep(_backoffs[attempt])
                        continue
                    if r.status != 200:
                        body = await r.text()
                        logging.warning("[ai/ollama] HTTP %s: %.200s", r.status, body)
                        _err(f"HTTP {r.status}: {body[:200]}")
                        return None
                    data = await r.json()
                    usage = data.get("usage")
                    choices = data.get("choices", [{}])
                    # Surface finish_reason on the usage dict so the bridge
                    # can offer a Continue button when the model hit its
                    # max_tokens cap.
                    finish_reason = (choices[0].get("finish_reason") or "") if choices else ""
                    if finish_reason:
                        usage = dict(usage or {})
                        usage["_finish_reason"] = finish_reason
                    if usage and _usage_out is not None:
                        _usage_out.append(usage)
                    message = choices[0].get("message", {}) if choices else {}
                    content = message.get("content")
                    if not content:
                        # Capture the response shape so the caller can tell
                        # apart 'model returned empty' vs 'response missing
                        # the expected keys' (the latter usually means a
                        # provider-side schema mismatch).
                        keys = ", ".join(list(data.keys())[:6])
                        _err(
                            f"empty content; response keys: {keys or '(none)'}"
                        )
                        return None
                    return sanitize_output(content.strip())
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
            if attempt < 1:
                logging.warning("[ai/ollama] Network error on attempt %d, retrying: %s", attempt + 1, exc)
                await asyncio.sleep(_backoffs[attempt])
                continue
            else:
                logging.warning("[ai/ollama] Network error after retries: %s", exc)
                _err(f"network error after retries: {exc!r}")
        except Exception as exc:
            logging.exception("[ai/ollama] Unexpected error")
            _err(f"unexpected: {exc!r}")
            return None
    return None


async def complete_stream_ollama(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.8,
    _usage_out: list | None = None,
    _error_out: list | None = None,
    user_id: int | None = None,
    on_queue_update: _PosCb | None = None,
    kind: str = "chat",
) -> AsyncIterator[str]:
    """Stream Ollama chat completion tokens as ``str`` deltas.

    Mirrors :func:`complete_stream` but talks to the Ollama OpenAI-compat
    endpoint (``/v1/chat/completions``) so callers that resolved the
    provider as ``"ollama"`` (TOOLS_BACKEND=ollama, ``gemma4:31b-cloud``,
    etc.) can also benefit from real streaming. Without this, the
    streaming bridge sent every ``provider="ollama"`` chat through
    OpenRouter's ``complete_stream`` and got a hard 400 like
    ``"gemma4:31b-cloud is not a valid model ID"`` -- which silently
    produced no tokens, fell all the way through to the non-streaming
    fallback (also OpenRouter -> same 400), and surfaced the generic
    "AI didn't respond" card to the user.

    Same ``_usage_out`` / ``_error_out`` contract as ``complete_stream``
    so the bridge can hand either backend a uniform interface.
    """
    def _err(msg: str) -> None:
        if _error_out is not None:
            _error_out.append(msg)
    messages = _clamp_messages(_inject_base_instructions(messages))
    timeout = _REQUEST_TIMEOUT_VISION if _has_image_blocks(messages) else _REQUEST_TIMEOUT
    url, headers = _ollama_endpoint()
    resolved_model = model or Config.TOOLS_MODEL or "llama3.2"
    headers = {**headers, "Accept": "text/event-stream"}
    payload: dict = _apply_ollama_keep_alive({
        "model": resolved_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    })
    try:
        async with chat_queue.acquire(
            backend="ollama", user_id=user_id,
            kind="system" if kind == "system" else "chat",
            on_position_change=on_queue_update,
        ):
            sess = await _get_session()
            async with sess.post(url, json=payload, headers=headers, timeout=timeout) as r:
                if r.status != 200:
                    body = await r.text()
                    logging.warning(
                        "[ai/stream/ollama] HTTP %s (model=%s, endpoint=%s): %.200s",
                        r.status, resolved_model, url, body,
                    )
                    _err(f"http_{r.status}")
                    return
                async for line in r.content:
                    if not line:
                        continue
                    try:
                        raw = line.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        continue
                    if not raw or not raw.startswith("data:"):
                        continue
                    payload_str = raw[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        return
                    try:
                        obj = _json.loads(payload_str)
                    except Exception:
                        continue
                    usage = obj.get("usage")
                    if usage and _usage_out is not None:
                        _usage_out.append(usage)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
    except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError, asyncio.TimeoutError) as exc:
        logging.warning("[ai/stream/ollama] network error: %s", exc)
        _err(f"network_{type(exc).__name__}")
    except Exception:
        logging.warning("[ai/stream/ollama] stream crashed", exc_info=True)
        _err("exception")


