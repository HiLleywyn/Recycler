"""Tests for the agent_tools framework: core registry, validation, executor,
and the ai_bridge multi-iteration tool-calling loop.

These tests exercise the framework end-to-end at the Python level without
talking to OpenRouter or Ollama. The bridge's HTTP dispatch is
monkey-patched so we can assert the full conversation shape the model
would have seen.
"""
from __future__ import annotations

import json

import pytest

from core.framework.agent_tools.core import (
    ParamSpec,
    RiskLevel,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    tool,
)
from core.framework.agent_tools.executor import run_tool
from core.framework.agent_tools.validation import ToolValidationError, validate_args
from core.framework.scale import to_human as _to_human


class PgRow(dict):
    """Test-local stand-in for ``core.database.PgRow``.

    Provides attribute access and the ``.h(col)`` helper that converts a raw
    NUMERIC(36,0) column to a human float - the same interface the production
    tool code calls on real DB rows. Defined here so the test suite does not
    have to import ``core.database`` (which pulls in asyncpg and the
    full database package at collection time).
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def h(self, col: str, default: int = 0) -> float:
        v = self.get(col, default)
        return _to_human(int(v) if v is not None else default)


# ── helpers ──────────────────────────────────────────────────────────────────

class _AuditDB:
    """Minimal DB double that captures agent_tool_audit inserts."""

    def __init__(self) -> None:
        self.inserts: list[tuple] = []

    async def execute(self, query: str, *args) -> str:
        if "agent_tool_audit" in query:
            self.inserts.append(args)
        return "INSERT 0 1"


def _fresh_registry() -> dict[str, ToolSpec]:
    """Snapshot and clear the global registry for an isolated test."""
    snap = dict(ToolRegistry._tools)
    ToolRegistry._tools.clear()
    return snap


def _restore_registry(snap: dict[str, ToolSpec]) -> None:
    ToolRegistry._tools.clear()
    ToolRegistry._tools.update(snap)


@pytest.fixture
def isolated_registry():
    """Yield a clean ToolRegistry, then restore the original on teardown.

    The built-in tools register themselves on import; these tests need an
    empty slate so they can register their own fixtures without colliding.
    """
    snap = _fresh_registry()
    try:
        yield ToolRegistry
    finally:
        _restore_registry(snap)


@pytest.fixture
def audit_ctx():
    return ToolContext(
        user_id=42,
        guild_id=99,
        db=_AuditDB(),
        actor="user",
        approved=False,
    )


# ── validation ────────────────────────────────────────────────────────────────

def test_validate_args_coerces_primitives_and_strips_unknown_keys():
    spec = ToolSpec(
        name="t.validate",
        summary="x",
        risk=RiskLevel.READ,
        params=[
            ParamSpec("amount", "float", required=True),
            ParamSpec("symbol", "symbol", required=True),
            ParamSpec("network", "network", required=False, default="arc"),
        ],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    out = validate_args(spec, {"amount": "12.5", "symbol": "arc"})
    assert out == {"amount": 12.5, "symbol": "ARC", "network": "arc"}


def test_validate_args_rejects_unknown_keys():
    spec = ToolSpec(
        name="t.noextra", summary="x", risk=RiskLevel.READ,
        params=[ParamSpec("x", "int")],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ToolValidationError, match="unknown params"):
        validate_args(spec, {"x": 1, "hallucinated": "os"})


def test_validate_args_enforces_choices_and_range():
    spec = ToolSpec(
        name="t.bounded", summary="x", risk=RiskLevel.READ,
        params=[
            ParamSpec("pick", "str", choices=["a", "b"]),
            ParamSpec("amt", "int", min=1, max=10),
        ],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ToolValidationError):
        validate_args(spec, {"pick": "c", "amt": 5})
    with pytest.raises(ToolValidationError):
        validate_args(spec, {"pick": "a", "amt": 99})
    assert validate_args(spec, {"pick": "b", "amt": 2}) == {"pick": "b", "amt": 2}


def test_validate_args_required_missing_raises():
    spec = ToolSpec(
        name="t.req", summary="x", risk=RiskLevel.READ,
        params=[ParamSpec("must", "str")],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ToolValidationError, match="missing required"):
        validate_args(spec, {})


def test_validate_args_preserves_underscore_metadata_keys():
    spec = ToolSpec(
        name="t.meta", summary="x", risk=RiskLevel.READ,
        params=[ParamSpec("x", "int")],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    out = validate_args(spec, {"x": 3, "_trigger_id": "evt-1"})
    assert out["_trigger_id"] == "evt-1"
    assert out["x"] == 3


# ── registry + schema export ─────────────────────────────────────────────────

def test_registry_exports_openai_schemas_without_danger(isolated_registry):
    @tool("t.read", "read tool", risk=RiskLevel.READ,
          params=[ParamSpec("q", "str", description="query")])
    async def _read(ctx, args):
        return ToolResult.success({"q": args["q"]})

    @tool("t.danger", "danger tool", risk=RiskLevel.DANGER,
          params=[ParamSpec("x", "int")])
    async def _danger(ctx, args):
        return ToolResult.success({"x": args["x"]})

    schemas = ToolRegistry.openai_tool_schemas(exclude_danger=True)
    names = [s["function"]["name"] for s in schemas]
    assert "t.read" in names
    assert "t.danger" not in names

    full = ToolRegistry.openai_tool_schemas(exclude_danger=False)
    assert "t.danger" in [s["function"]["name"] for s in full]


def test_to_openai_schema_shapes_parameters():
    spec = ToolSpec(
        name="t.shape", summary="check", risk=RiskLevel.READ,
        params=[
            ParamSpec("sym", "symbol", description="Token symbol"),
            ParamSpec("amt", "float", min=0.0, max=1000.0),
            ParamSpec("pick", "str", choices=["a", "b"], required=False, default="a"),
        ],
        handler=lambda ctx, args: None,  # type: ignore[arg-type]
    )
    s = spec.to_openai_schema()
    assert s["type"] == "function"
    props = s["function"]["parameters"]["properties"]
    assert props["sym"]["type"] == "string"
    assert props["amt"]["type"] == "number"
    assert props["amt"]["minimum"] == 0.0
    assert props["pick"]["enum"] == ["a", "b"]
    assert s["function"]["parameters"]["required"] == ["sym", "amt"]


# ── executor: risk policy, audit, result envelope ────────────────────────────

@pytest.mark.asyncio
async def test_run_tool_danger_without_approval_returns_needs_approval(isolated_registry, audit_ctx):
    @tool("t.danger2", "risky", risk=RiskLevel.DANGER,
          params=[ParamSpec("amount", "int")])
    async def _d(ctx, args):
        return ToolResult.success({"did": True})

    res = await run_tool("t.danger2", audit_ctx, {"amount": 1})
    assert res.ok is False
    assert res.error == "approval_required"
    assert res.data == {"tool": "t.danger2", "risk": "danger", "args": {"amount": 1}}


@pytest.mark.asyncio
async def test_run_tool_mutate_blocks_non_user_actor(isolated_registry):
    @tool("t.mut", "mutates", risk=RiskLevel.MUTATE,
          params=[ParamSpec("x", "int")])
    async def _m(ctx, args):
        return ToolResult.success({"x": args["x"]})

    ctx = ToolContext(
        user_id=1, guild_id=1, db=_AuditDB(), actor="queue",
    )
    res = await run_tool("t.mut", ctx, {"x": 5})
    assert res.ok is False
    assert res.error == "approval_required"


@pytest.mark.asyncio
async def test_run_tool_success_writes_audit_row(isolated_registry, audit_ctx):
    @tool("t.ok", "ok", risk=RiskLevel.READ,
          params=[ParamSpec("q", "str")])
    async def _ok(ctx, args):
        return ToolResult.success({"echo": args["q"]})

    res = await run_tool("t.ok", audit_ctx, {"q": "hi"})
    assert res.ok is True
    assert res.data == {"echo": "hi"}
    # meta is always decorated with tool/risk/actor/duration_ms
    assert res.meta["tool"] == "t.ok"
    assert res.meta["risk"] == "read"
    assert res.meta["actor"] == "user"
    assert "duration_ms" in res.meta
    # audit row was captured
    assert len(audit_ctx.db.inserts) == 1


@pytest.mark.asyncio
async def test_run_tool_handler_crash_is_wrapped(isolated_registry, audit_ctx):
    @tool("t.boom", "boom", risk=RiskLevel.READ, params=[])
    async def _boom(ctx, args):
        raise RuntimeError("kaboom")

    res = await run_tool("t.boom", audit_ctx, {})
    assert res.ok is False
    assert "handler_error" in (res.error or "")
    assert "kaboom" in (res.error or "")


@pytest.mark.asyncio
async def test_run_tool_unknown_name_returns_fail(isolated_registry, audit_ctx):
    res = await run_tool("t.nope", audit_ctx, {})
    assert res.ok is False
    assert "unknown tool" in (res.error or "")


@pytest.mark.asyncio
async def test_run_tool_validation_error_wrapped_as_failure(isolated_registry, audit_ctx):
    @tool("t.validate_fail", "v", risk=RiskLevel.READ,
          params=[ParamSpec("amt", "int", min=0, max=5)])
    async def _v(ctx, args):
        return ToolResult.success({"amt": args["amt"]})

    res = await run_tool("t.validate_fail", audit_ctx, {"amt": 999})
    assert res.ok is False
    assert "validation_error" in (res.error or "")


# ── ai_bridge: multi-iteration loop ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_with_agent_tools_falls_back_when_no_tools(isolated_registry, monkeypatch):
    """With an empty registry, complete_with_agent_tools just calls complete()."""
    from core.framework.agent_tools import ai_bridge

    called: dict = {}

    async def fake_complete(messages, max_tokens=256, temperature=0.8, model=None, **_kw):
        called["messages"] = messages
        return "fallback-answer"

    monkeypatch.setattr(ai_bridge, "complete", fake_complete)
    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "hi"}], ctx,
    )
    assert out == "fallback-answer"
    assert called["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_complete_with_agent_tools_runs_tool_and_returns_text(isolated_registry, monkeypatch):
    """Round-trip: tool_calls on turn 1, tool result fed back, text on turn 2."""
    from core.framework.agent_tools import ai_bridge

    @tool("t.loop_echo", "echo", risk=RiskLevel.READ,
          params=[ParamSpec("q", "str")])
    async def _echo(ctx, args):
        return ToolResult.success({"answer": f"got {args['q']}"})

    call_log: list[dict] = []

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        call_log.append({"convo": list(convo), "tools": tools})
        if len(call_log) == 1:
            # First turn: model asks for a tool call.
            return None, [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "t.loop_echo", "arguments": json.dumps({"q": "ping"})},
            }], None
        # Second turn: model returns final text after seeing the tool result.
        return "final-answer", None, None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "use the tool"}], ctx,
    )
    assert out == "final-answer"
    # Two dispatches happened.
    assert len(call_log) == 2

    # Second dispatch saw the assistant tool_calls turn + a tool result turn
    # containing the serialized ToolResult.
    convo2 = call_log[1]["convo"]
    roles = [m["role"] for m in convo2]
    assert roles[:3] == ["user", "assistant", "tool"]
    assert convo2[1]["content"] is None
    assert convo2[1]["tool_calls"][0]["function"]["name"] == "t.loop_echo"
    tool_turn = convo2[2]
    assert tool_turn["name"] == "t.loop_echo"
    payload = json.loads(tool_turn["content"])
    assert payload["ok"] is True
    assert payload["data"]["answer"] == "got ping"


@pytest.mark.asyncio
async def test_complete_with_agent_tools_caps_parallel_tool_calls(isolated_registry, monkeypatch):
    """A runaway fan-out of tool_calls is capped at _MAX_TOOL_CALLS_PER_TURN."""
    from core.framework.agent_tools import ai_bridge

    ran: list[str] = []

    @tool("t.mass", "mass", risk=RiskLevel.READ, params=[ParamSpec("i", "int")])
    async def _mass(ctx, args):
        ran.append(f"i={args['i']}")
        return ToolResult.success({"i": args["i"]})

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        if not any(m.get("role") == "tool" for m in convo):
            return None, [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "t.mass", "arguments": json.dumps({"i": i})},
                }
                for i in range(20)
            ], None
        return "done", None, None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)
    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "spam"}], ctx, max_iter=3,
    )
    assert out == "done"
    # Capped at _MAX_TOOL_CALLS_PER_TURN.
    assert len(ran) == ai_bridge._MAX_TOOL_CALLS_PER_TURN


@pytest.mark.asyncio
async def test_complete_with_agent_tools_fallback_text_when_loop_exhausts(isolated_registry, monkeypatch):
    """When the model only ever calls tools, the collapsed-convo fallback complete() produces text."""
    from core.framework.agent_tools import ai_bridge

    @tool("t.spin", "spin", risk=RiskLevel.READ, params=[])
    async def _spin(ctx, args):
        return ToolResult.success({"spun": True})

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        # Always request a tool call -- the loop will exhaust and fall through to complete().
        return None, [{
            "id": "call_x",
            "type": "function",
            "function": {"name": "t.spin", "arguments": "{}"},
        }], None

    async def fake_complete(messages, max_tokens=256, temperature=0.8, model=None, **_kw):
        # Verify the convo was collapsed (no role=tool or assistant tool_calls turns).
        for m in messages:
            assert m.get("role") != "tool", "tool turns should be collapsed"
            assert not (m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content")), \
                "bare tool_calls assistant turns should be collapsed"
        return "fallback-text"

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)
    monkeypatch.setattr(ai_bridge, "complete", fake_complete)
    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "forever"}], ctx, max_iter=2,
    )
    assert out == "fallback-text"


@pytest.mark.asyncio
async def test_complete_with_agent_tools_truncates_huge_tool_result(isolated_registry, monkeypatch):
    """A runaway tool result is capped before being fed back to the model."""
    from core.framework.agent_tools import ai_bridge

    huge_blob = "x" * 10_000

    @tool("t.huge", "huge", risk=RiskLevel.READ, params=[])
    async def _huge(ctx, args):
        return ToolResult.success({"blob": huge_blob})

    seen_tool_turn: dict = {}

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        # Record the tool turn on the second call and return text.
        for m in convo:
            if m.get("role") == "tool":
                seen_tool_turn["content"] = m["content"]
                return "ok", None, None
        return None, [{
            "id": "call_h",
            "type": "function",
            "function": {"name": "t.huge", "arguments": "{}"},
        }], None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)
    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "huge please"}], ctx,
    )
    assert out == "ok"
    assert len(seen_tool_turn["content"]) <= ai_bridge._MAX_TOOL_RESULT_CHARS
    assert "_truncated" in seen_tool_turn["content"]


@pytest.mark.asyncio
async def test_complete_with_agent_tools_falls_back_on_empty_tool_response(isolated_registry, monkeypatch):
    """When the tool backend returns (None, None) -- e.g. Ollama Turbo 400'd on
    an image_url block -- the bridge must NOT drop the user's reply. It should
    fall through to a plain completion so the user still gets an answer.
    """
    from core.framework.agent_tools import ai_bridge

    @tool("t.present", "present", risk=RiskLevel.READ, params=[])
    async def _p(ctx, args):
        return ToolResult.success({"ok": True})

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        # Simulate ollama 400 on image_url: no text, no tool_calls.
        return None, None, None

    called: dict = {}

    async def fake_complete(messages, max_tokens=256, temperature=0.8, model=None, **_kw):
        called["msgs"] = messages
        return "fallback-ok"

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)
    monkeypatch.setattr(ai_bridge, "complete", fake_complete)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "see this screenshot"}], ctx,
    )
    assert out == "fallback-ok"
    assert called["msgs"][-1]["content"] == "see this screenshot"


@pytest.mark.asyncio
async def test_complete_with_agent_tools_falls_back_on_dispatch_exception(isolated_registry, monkeypatch):
    """A raised exception during dispatch must also fall through, not 500 the user."""
    from core.framework.agent_tools import ai_bridge

    @tool("t.present2", "p2", risk=RiskLevel.READ, params=[])
    async def _p(ctx, args):
        return ToolResult.success({"ok": True})

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        raise RuntimeError("network unreachable")

    async def fake_complete(messages, max_tokens=256, temperature=0.8, model=None, **_kw):
        return "safe-reply"

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)
    monkeypatch.setattr(ai_bridge, "complete", fake_complete)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    out = await ai_bridge.complete_with_agent_tools(
        [{"role": "user", "content": "anything"}], ctx,
    )
    assert out == "safe-reply"


def test_strip_image_blocks_replaces_image_url_with_text_marker():
    """Multimodal content blocks are rewritten to ``[ATTACHMENT: <url>]`` markers.

    The chat tool loop runs through a text-only model (either Ollama Turbo,
    which refuses remote image URLs, or the cheap OpenRouter chat model,
    which can't read them either). The strip helper converts multimodal
    user turns into plain text convo with attachment markers so the chat
    model can see that an attachment exists and invoke
    ``vision.describe_image`` to fetch a real description.
    """
    from core.framework.ai.client import _strip_image_blocks

    # 1. Plain text messages round-trip unchanged.
    plain = [{"role": "user", "content": "hello"}]
    assert _strip_image_blocks(plain) == plain

    # 2. Text + image multimodal turn becomes plain text with the marker appended.
    mm = [
        {"role": "system", "content": "you are a bot"},
        {"role": "user", "content": [
            {"type": "text", "text": "what's this"},
            {"type": "image_url", "image_url": {"url": "https://cdn.discordapp.com/x.png"}},
        ]},
    ]
    stripped = _strip_image_blocks(mm)
    assert stripped[0] == {"role": "system", "content": "you are a bot"}
    assert stripped[1]["role"] == "user"
    assert isinstance(stripped[1]["content"], str)
    assert "what's this" in stripped[1]["content"]
    assert "[ATTACHMENT: https://cdn.discordapp.com/x.png]" in stripped[1]["content"]

    # 3. An image-only turn (no text block) still produces a text message
    # carrying just the marker.
    only_img = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://cdn.discordapp.com/y.jpg"}},
    ]}]
    stripped2 = _strip_image_blocks(only_img)
    assert stripped2[0]["content"] == "[ATTACHMENT: https://cdn.discordapp.com/y.jpg]"

    # 4. Multiple images in one turn produce multiple markers.
    multi = [{"role": "user", "content": [
        {"type": "text", "text": "compare"},
        {"type": "image_url", "image_url": {"url": "https://cdn.discordapp.com/a.png"}},
        {"type": "image_url", "image_url": {"url": "https://cdn.discordapp.com/b.png"}},
    ]}]
    stripped3 = _strip_image_blocks(multi)
    assert "[ATTACHMENT: https://cdn.discordapp.com/a.png]" in stripped3[0]["content"]
    assert "[ATTACHMENT: https://cdn.discordapp.com/b.png]" in stripped3[0]["content"]


@pytest.mark.asyncio
async def test_complete_with_agent_tools_strips_image_blocks_before_dispatch(
    isolated_registry, monkeypatch,
):
    """The tool loop feeds stripped, text-only messages to the backend.

    End-to-end check that ``complete_with_agent_tools`` runs the multimodal
    convo through ``_strip_image_blocks`` before it reaches
    ``_dispatch_tool_call``. The chat model must see an attachment marker
    (not a raw ``image_url`` block) so it can invoke ``vision.describe_image``
    instead of crashing the Ollama Turbo backend with an HTTP 400.
    """
    from core.framework.agent_tools import ai_bridge

    # Need at least one registered tool so the bridge runs the tool-call
    # loop instead of short-circuiting to plain complete().
    @tool("t.strip_probe", "strip probe", risk=RiskLevel.READ, params=[])
    async def _probe(ctx, args):
        return ToolResult.success({"ok": True})

    seen_convos: list[list[dict]] = []

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        seen_convos.append(list(convo))
        return "answer", None, None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    mm_msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "https://cdn.discordapp.com/pic.png"}},
        ]},
    ]
    out = await ai_bridge.complete_with_agent_tools(mm_msgs, ctx)
    assert out == "answer"

    # The dispatch function saw a stripped, text-only convo.
    assert seen_convos, "dispatch was never called"
    sent = seen_convos[0]
    for m in sent:
        assert not isinstance(m.get("content"), list), (
            "multimodal content must be stripped before dispatch"
        )
    assert "[ATTACHMENT: https://cdn.discordapp.com/pic.png]" in sent[0]["content"]
    assert "describe this" in sent[0]["content"]


# ── vision.describe_image tool ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vision_describe_image_tool_base64_encodes_and_calls_backend(
    isolated_registry, audit_ctx, monkeypatch,
):
    """``vision.describe_image`` downloads the URL, base64-encodes it, and
    forwards to the Ollama vision backend.

    This is the tool the chat model calls after seeing a
    ``[ATTACHMENT: <url>]`` marker. The test mocks the HTTP download and
    the vision backend, then asserts the tool:
      * validates the host against the allowlist,
      * rejects non-image content types,
      * base64-encodes the bytes into a ``data:image/png;base64,...`` URI
        before calling ``complete_ollama_vision``,
      * returns the description verbatim in the tool result.
    """
    from core.framework.agent_tools.tools import vision as vision_tools

    # The @tool decorator runs at module import time. The first time this
    # module is imported inside an isolated_registry block, it registers
    # vision.describe_image into the currently-empty registry; subsequent
    # imports are a no-op because the module is cached. Either way, the
    # tool is registered and ready to run -- no manual register needed,
    # and doing one would collide with the decorator entry.
    if "vision.describe_image" not in ToolRegistry._tools:
        ToolRegistry.register(ToolSpec(
            name="vision.describe_image",
            summary="describe image",
            risk=RiskLevel.READ,
            params=[
                ParamSpec("url", "str"),
                ParamSpec("prompt", "str", required=False, default="describe"),
            ],
            handler=vision_tools.describe_image,
            category="vision",
            cooldown_s=0,
        ))

    fake_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "image/png"}

        class _Body:
            _data = fake_bytes

            async def read(self, n):
                return type(self)._data

        content = _Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        def get(self, url, headers=None):
            _FakeSession._last_url = url
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(vision_tools.aiohttp, "ClientSession", _FakeSession)

    captured: dict = {}

    async def fake_vision(prompt, image_data_uri, *, model=None, **_):
        captured["prompt"] = prompt
        captured["uri"] = image_data_uri
        return "A small orange cat sitting on a blue rug."

    monkeypatch.setattr(vision_tools, "complete_ollama_vision", fake_vision)

    url = "https://cdn.discordapp.com/attachments/1/2/cat.png"
    res = await run_tool(
        "vision.describe_image", audit_ctx,
        {"url": url, "prompt": "what is this"},
    )
    assert res.ok is True, res.error
    assert res.data["url"] == url
    assert res.data["host"] == "cdn.discordapp.com"
    assert res.data["mime"] == "image/png"
    assert res.data["bytes"] == len(fake_bytes)
    assert res.data["description"] == "A small orange cat sitting on a blue rug."

    # The vision backend received a base64 data URI, not the original URL.
    assert captured["prompt"] == "what is this"
    assert captured["uri"].startswith("data:image/png;base64,")
    import base64 as _b64
    blob = captured["uri"].split(",", 1)[1]
    assert _b64.b64decode(blob) == fake_bytes

    # And the HTTP fetch was against the original Discord CDN URL.
    assert _FakeSession._last_url == url


@pytest.mark.asyncio
async def test_vision_describe_image_rejects_non_allowlisted_host(
    isolated_registry, audit_ctx,
):
    """Only the vetted image hosts may be fetched; an arbitrary URL is refused."""
    from core.framework.agent_tools.tools import vision as vision_tools  # noqa: F401

    # Same decorator-vs-manual-register dance as the base64 test above.
    if "vision.describe_image" not in ToolRegistry._tools:
        ToolRegistry.register(ToolSpec(
            name="vision.describe_image",
            summary="describe image",
            risk=RiskLevel.READ,
            params=[ParamSpec("url", "str")],
            handler=vision_tools.describe_image,
            category="vision",
            cooldown_s=0,
        ))

    res = await run_tool(
        "vision.describe_image", audit_ctx,
        {"url": "https://evil.example.com/payload.png"},
    )
    assert res.ok is False
    assert "host_not_allowed" in (res.error or "")


# ── data.web_search tool ─────────────────────────────────────────────────────

def test_ddg_result_parser_extracts_title_url_snippet():
    """The DDG HTML parser extracts (title, unwrapped url, snippet) tuples."""
    from core.framework.agent_tools.tools.data import _DDGResultParser

    html = (
        '<html><body>'
        '<div class="result">'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>'
        '<a class="result__snippet">Snippet for A</a>'
        '</div>'
        '<div class="result">'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Example B</a>'
        '<a class="result__snippet">Snippet for B</a>'
        '</div>'
        '</body></html>'
    )
    p = _DDGResultParser(max_results=5)
    p.feed(html)
    assert len(p.results) == 2
    assert p.results[0] == {
        "title": "Example A",
        "url": "https://example.com/a",
        "snippet": "Snippet for A",
    }
    assert p.results[1]["url"] == "https://example.com/b"


def test_ddg_result_parser_honours_max_results():
    """The parser stops collecting once max_results is reached."""
    from core.framework.agent_tools.tools.data import _DDGResultParser

    rows = ""
    for i in range(5):
        rows += (
            f'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{i}">T{i}</a>'
            f'<a class="result__snippet">S{i}</a>'
        )
    p = _DDGResultParser(max_results=2)
    p.feed(rows)
    assert len(p.results) == 2
    assert p.results[0]["url"] == "https://e.com/0"
    assert p.results[1]["url"] == "https://e.com/1"


def test_unwrap_ddg_redirect_handles_protocol_relative():
    """`_unwrap_ddg_redirect` returns the real target from ``/l/?uddg=...``."""
    from core.framework.agent_tools.tools.data import _unwrap_ddg_redirect

    assert _unwrap_ddg_redirect(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x"
    ) == "https://example.com/page"
    # Direct URLs pass through unchanged.
    assert _unwrap_ddg_redirect("https://example.com/direct") == "https://example.com/direct"
    # Empty input is handled.
    assert _unwrap_ddg_redirect("") == ""


@pytest.mark.asyncio
async def test_wallet_portfolio_merges_cefi_defi_lp_stakes(isolated_registry, audit_ctx, monkeypatch):
    """wallet.portfolio must aggregate CeFi + DeFi + LP + stakes per symbol.

    Previously the tool only read `crypto_holdings`, so a player with a
    deployed group token (e.g. CAT) in their DeFi wallet and LP would see
    a portfolio claiming they held 0 of it. This test registers a fake
    net_worth service that returns per-source holdings and asserts the
    tool sums them correctly.
    """
    from core.framework.agent_tools.tools import wallet as wallet_tools  # noqa: F401  -- import registers wallet.portfolio
    from core.framework.scale import SCALE

    class _FakeNW:
        # Aggregate USD values don't matter for this test; only the
        # per-source lists are used for the holdings merge.
        wallet = 0.0
        bank = 0.0
        cefi_crypto = 0.0
        defi_wallet = 0.0
        stake_value = 0.0
        pos_stake_value = 0.0
        moon_stake_value = 0.0
        moon_pool_stake_value = 0.0
        lp_value = 0.0
        rig_value = 0.0
        delegation_value = 0.0
        savings_value = 0.0
        items_value = 0.0
        nft_value = 0.0
        loan_liability = 0.0
        total = 0.0

        holdings = [
            {"symbol": "ARC", "amount": 2 * SCALE},  # 2 ARC in CeFi
        ]
        wallet_holdings = [
            # 1000 CAT in DeFi on-chain wallet -- the bug the user hit.
            {"symbol": "CAT", "amount": 1000 * SCALE, "network": "dsc"},
            {"symbol": "ARC", "amount": 1 * SCALE,    "network": "arc"},
        ]
        lp_positions = [
            # CAT/USD LP contributes 500 CAT + 250 USD (already in human
            # units inside compute_net_worth).
            {
                "token_a": "CAT", "token_b": "USD",
                "amount_a": 500.0, "amount_b": 250.0, "usd_value": 500.0,
            },
        ]
        stakes = [
            {"symbol": "CAT", "amount": 200 * SCALE},  # 200 CAT staked
        ]

    async def fake_compute(uid, gid, db):
        return _FakeNW()

    class _FakeDB:
        async def fetch_all(self, query, *args):
            if "crypto_prices" in query:
                return [
                    {"symbol": "CAT", "price": 0.10},
                    {"symbol": "ARC", "price": 3000.0},
                    {"symbol": "USD", "price": 1.0},
                ]
            return []

        async def execute(self, query, *args):
            return "INSERT 0 1"

    # Patch compute_net_worth inside the tool's late-imported module path.
    import services.net_worth as nw_mod
    monkeypatch.setattr(nw_mod, "compute_net_worth", fake_compute)

    # Importing wallet_tools above already ran the @tool decorator for
    # wallet.portfolio inside this isolated registry, so it's registered
    # and ready. No manual ToolRegistry.register needed -- doing so would
    # collide with the decorator-registered entry.

    pf_ctx = ToolContext(
        user_id=42, guild_id=99, db=_FakeDB(),
        actor="user", approved=False,
    )
    res = await run_tool("wallet.portfolio", pf_ctx, {})
    assert res.ok is True

    by_sym = {h["symbol"]: h for h in res.data["holdings"]}

    # CAT: 1000 defi + 500 lp + 200 staked = 1700 total
    assert "CAT" in by_sym, "CAT must appear in merged holdings"
    cat = by_sym["CAT"]
    assert cat["cefi"] == 0.0
    assert cat["defi"] == 1000.0
    assert cat["lp"] == 500.0
    assert cat["staked"] == 200.0
    assert cat["amount"] == 1700.0

    # ARC: 2 cefi + 1 defi = 3 total
    arc = by_sym["ARC"]
    assert arc["cefi"] == 2.0
    assert arc["defi"] == 1.0
    assert arc["amount"] == 3.0

    # USD from the LP pair (amount_b) should also appear.
    assert "USD" in by_sym
    assert by_sym["USD"]["lp"] == 250.0


@pytest.mark.asyncio
async def test_web_search_tool_returns_structured_results(isolated_registry, audit_ctx, monkeypatch):
    """`data.web_search` hits DDG, parses the HTML, returns structured rows."""
    # Register just the web_search tool for this test.
    from core.framework.agent_tools.tools import data as data_tools  # noqa: F401

    fake_html = (
        '<html><body>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com">Alpha</a>'
        '<a class="result__snippet">Alpha snippet</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.com">Beta</a>'
        '<a class="result__snippet">Beta snippet</a>'
        '</body></html>'
    )

    class _FakeResp:
        status = 200

        class _Body:
            async def read(self, n):
                return fake_html.encode("utf-8")

        content = _Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        def get(self, url, headers=None):
            _FakeResp._last_url = url
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(data_tools.aiohttp, "ClientSession", _FakeSession)

    # Register web_search into the isolated registry (the fixture cleared it).
    ToolRegistry.register(ToolSpec(
        name="data.web_search",
        summary="ws",
        risk=RiskLevel.READ,
        params=[
            ParamSpec("query", "str"),
            ParamSpec("max_results", "int", required=False, default=5, min=1, max=10),
        ],
        handler=data_tools.web_search,
        category="data",
        cooldown_s=0,
    ))

    res = await run_tool("data.web_search", audit_ctx, {"query": "discoin", "max_results": 2})
    assert res.ok is True
    assert res.data["query"] == "discoin"
    assert res.data["result_count"] == 2
    assert res.data["results"][0]["title"] == "Alpha"
    assert res.data["results"][0]["url"] == "https://a.com"
    assert res.data["results"][1]["url"] == "https://b.com"
    assert "discoin" in _FakeResp._last_url


# ── ai_bridge: streaming ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fake_stream_text_concatenates_back_to_original():
    """``_fake_stream_text`` must split on word boundaries and round-trip."""
    from core.framework.agent_tools import ai_bridge

    text = (
        "the quick brown fox jumps over the lazy dog and then keeps going "
        "for quite a few more words to exercise the chunking logic"
    )
    chunks = [c async for c in ai_bridge._fake_stream_text(text)]
    assert len(chunks) > 1, "chunker should produce multiple segments"
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_fake_stream_text_empty_yields_nothing():
    from core.framework.agent_tools import ai_bridge

    chunks = [c async for c in ai_bridge._fake_stream_text("")]
    assert chunks == []


@pytest.mark.asyncio
async def test_complete_with_agent_tools_stream_no_tools_calls_complete_then_chunks(
    isolated_registry, monkeypatch,
):
    """No tools registered -> sync complete() then fake-stream the chunks.

    The bridge intentionally does NOT real-stream individual SSE deltas
    here -- they're typically 2-3 chars and Discord's edit throttle would
    paint them out at ~2-3 chars/sec, which feels much slower than the
    polished "spinner-then-chunked-paint-in" UX. This test pins the
    contract: complete() runs once, then _fake_stream_text yields the
    text in chunks, and the final 'done' event carries the full reply
    plus any usage from complete()'s _usage_out.
    """
    from core.framework.agent_tools import ai_bridge

    async def fake_complete(
        messages, *, model=None, max_tokens=300, temperature=0.8, _usage_out=None, **_kw,
    ):
        if _usage_out is not None:
            _usage_out.append({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        return "hello world"

    monkeypatch.setattr(ai_bridge, "complete", fake_complete)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    events: list[dict] = []
    async for ev in ai_bridge.complete_with_agent_tools_stream(
        [{"role": "user", "content": "hi"}], ctx,
    ):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "status"
    assert "delta" in kinds
    assert kinds[-1] == "done"
    # Concatenated deltas reproduce the full text.
    delta_text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert delta_text == "hello world"
    assert events[-1]["text"] == "hello world"
    # Token usage flows through to the done event so the footer can render it.
    assert events[-1]["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_complete_with_agent_tools_stream_runs_tool_then_fake_streams_text(
    isolated_registry, monkeypatch,
):
    """Streaming variant: tool call first turn, text second turn, fake-streamed."""
    from core.framework.agent_tools import ai_bridge

    @tool("t.stream_echo", "echo", risk=RiskLevel.READ,
          params=[ParamSpec("q", "str")])
    async def _echo(ctx, args):
        return ToolResult.success({"answer": f"got {args['q']}"})

    call_log: list[dict] = []

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        call_log.append({"convo": list(convo), "tools": tools})
        if len(call_log) == 1:
            return None, [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "t.stream_echo", "arguments": json.dumps({"q": "ping"})},
            }], None
        return "final streaming answer for the user", None, None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)

    ctx = ToolContext(user_id=1, guild_id=1, db=_AuditDB())
    events: list[dict] = []
    async for ev in ai_bridge.complete_with_agent_tools_stream(
        [{"role": "user", "content": "use the tool"}], ctx,
    ):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "status" in kinds
    # A tool_call event fired for t.stream_echo with ok=True.
    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "t.stream_echo"
    assert tool_events[0]["ok"] is True
    # At least one delta fired and the done event carries the full text.
    deltas = [e for e in events if e["type"] == "delta"]
    assert deltas, "no delta events emitted"
    assert "".join(d["text"] for d in deltas) == "final streaming answer for the user"
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["text"] == "final streaming answer for the user"


@pytest.mark.asyncio
async def test_complete_with_agent_tools_stream_emits_approval_required(
    isolated_registry, monkeypatch,
):
    """A DANGER tool returns approval_required; the stream surfaces it + ID."""
    from core.framework.agent_tools import ai_bridge

    @tool("t.stream_danger", "boom", risk=RiskLevel.DANGER,
          params=[ParamSpec("amount", "int")])
    async def _d(ctx, args):
        return ToolResult.success({"did": True})

    # Register a harmless READ tool so the stream loop runs - danger tools
    # alone are excluded from the schema list and the stream short-circuits
    # to a plain completion if the schema list is empty.
    @tool("t.stream_safe", "safe", risk=RiskLevel.READ, params=[])
    async def _s(ctx, args):
        return ToolResult.success({"ok": True})

    async def fake_dispatch(convo, *, tools, provider, model, max_tokens, temperature, tool_choice="auto", **_kw):
        # Only ask for the tool on the first turn; return text after.
        if not any(m.get("role") == "tool" for m in convo):
            return None, [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "t.stream_danger",
                    "arguments": json.dumps({"amount": 5}),
                },
            }], None
        return "ok, waiting on approval.", None, None

    monkeypatch.setattr(ai_bridge, "_dispatch_tool_call", fake_dispatch)

    persisted: dict = {}

    async def fake_request_approval(db, *, guild_id, user_id, tool, args, reason):
        persisted.update(
            {"guild_id": guild_id, "user_id": user_id, "tool": tool,
             "args": args, "reason": reason},
        )
        return 4242

    monkeypatch.setattr(ai_bridge, "request_approval", fake_request_approval)

    ctx = ToolContext(user_id=7, guild_id=8, db=_AuditDB(), approved=False)
    events: list[dict] = []
    async for ev in ai_bridge.complete_with_agent_tools_stream(
        [{"role": "user", "content": "run the danger"}], ctx,
    ):
        events.append(ev)

    approval_events = [e for e in events if e["type"] == "approval_required"]
    assert len(approval_events) == 1
    ap = approval_events[0]
    assert ap["approval_id"] == 4242
    assert ap["tool"] == "t.stream_danger"
    assert ap["args"] == {"amount": 5}
    # request_approval was invoked with the right envelope.
    assert persisted["guild_id"] == 8
    assert persisted["user_id"] == 7
    assert persisted["tool"] == "t.stream_danger"
    assert persisted["args"] == {"amount": 5}


# ── cogs/approvals.py: _resolve_approval round-trip ─────────────────────────

@pytest.mark.asyncio
async def test_resolve_approval_approve_reruns_tool_with_approved_true(
    isolated_registry,
):
    """Approve path must re-run the tool with ctx.approved=True."""
    from cogs.approvals import _resolve_approval

    ran: list[dict] = []

    @tool("t.approve_probe", "probe", risk=RiskLevel.DANGER,
          params=[ParamSpec("amount", "int")])
    async def _probe(ctx, args):
        ran.append({"approved": ctx.approved, "actor": ctx.actor,
                    "user_id": ctx.user_id, "guild_id": ctx.guild_id,
                    "args": dict(args)})
        return ToolResult.success({"done": True, "amount": args["amount"]})

    class _FakeDB:
        async def fetch_one(self, query, *args):
            # Canonical row: tool/args/reason/user/guild/status/ttl.
            return {
                "tool":     "t.approve_probe",
                "args":     {"amount": 7},
                "reason":   "needs approval",
                "guild_id": 99,
                "user_id":  42,
                "status":   "pending",
                "ttl":      300.0,
            }

        async def execute(self, query, *args):
            # decide_approval expects "UPDATE 1" for success; audit INSERTs
            # during the re-run should return the normal insert status.
            if "UPDATE" in query:
                return "UPDATE 1"
            return "INSERT 0 1"

    class _FakeBot:
        db = _FakeDB()
        bus = None

    outcome = await _resolve_approval(
        _FakeBot(),
        approval_id=1,
        decider_id=42,
        guild_id=99,
        tool_name="t.approve_probe",
        args={"amount": 7},
        approve=True,
    )

    assert outcome["status"] == "approved"
    assert outcome["tool"] == "t.approve_probe"
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["data"] == {"done": True, "amount": 7}
    # The re-run saw ctx.approved=True so the DANGER executor gate passed.
    assert ran == [{
        "approved": True, "actor": "user",
        "user_id": 42, "guild_id": 99, "args": {"amount": 7},
    }]


@pytest.mark.asyncio
async def test_resolve_approval_deny_does_not_rerun_tool(isolated_registry):
    """Deny path must NOT invoke the tool handler."""
    from cogs.approvals import _resolve_approval

    ran: list[int] = []

    @tool("t.deny_probe", "probe", risk=RiskLevel.DANGER,
          params=[ParamSpec("n", "int")])
    async def _probe(ctx, args):
        ran.append(args["n"])
        return ToolResult.success({"n": args["n"]})

    class _FakeDB:
        async def fetch_one(self, query, *args):
            return {
                "tool":     "t.deny_probe",
                "args":     {"n": 1},
                "reason":   "x",
                "guild_id": 1,
                "user_id":  2,
                "status":   "pending",
                "ttl":      60.0,
            }

        async def execute(self, query, *args):
            return "UPDATE 1"

    class _FakeBot:
        db = _FakeDB()
        bus = None

    outcome = await _resolve_approval(
        _FakeBot(),
        approval_id=1,
        decider_id=2,
        guild_id=1,
        tool_name=None, args=None,
        approve=False,
    )

    assert outcome["status"] == "denied"
    assert outcome["tool"] == "t.deny_probe"
    assert ran == [], "deny path must not execute the tool"


@pytest.mark.asyncio
async def test_resolve_approval_expired_row_returns_expired(isolated_registry):
    """An already-decided / expired row should not trigger a re-run."""
    from cogs.approvals import _resolve_approval

    @tool("t.expired_probe", "probe", risk=RiskLevel.DANGER, params=[])
    async def _probe(ctx, args):
        raise AssertionError("must not run")

    class _FakeDB:
        async def fetch_one(self, query, *args):
            return {
                "tool": "t.expired_probe", "args": {}, "reason": "r",
                "guild_id": 1, "user_id": 2, "status": "approved", "ttl": 60.0,
            }

        async def execute(self, query, *args):
            raise AssertionError("execute must not be called")

    class _FakeBot:
        db = _FakeDB()
        bus = None

    outcome = await _resolve_approval(
        _FakeBot(),
        approval_id=1, decider_id=2, guild_id=1,
        tool_name=None, args=None, approve=True,
    )
    assert outcome["status"] == "approved"  # status from row
    assert outcome.get("error") == "approval is no longer pending"


# ── new read-only tools: shop / items / savings / loans / vault / staking / history / leaderboard / groups

def _ensure_registered(name: str, spec_factory) -> None:
    """Register a tool if not already there.

    The @tool-decorated builtins register themselves on first module
    import. isolated_registry clears the dict at setup but cached module
    imports DON'T re-run the decorator, so tests that want to exercise
    one of these builtins need to register a ToolSpec manually if
    missing.
    """
    if name not in ToolRegistry._tools:
        ToolRegistry.register(spec_factory())


@pytest.mark.asyncio
async def test_shop_catalog_lists_items_from_config(
    isolated_registry, audit_ctx, monkeypatch,
):
    from core.framework.agent_tools.tools import shop as shop_tools
    from core.config import Config

    fake_shop = {
        "hashstone": {
            "name": "Hashstone", "emoji": ":rock:", "category": "item",
            "description": "mines", "cost_stable": 100.0,
            "buy_fee_pct": 0.02, "sell_fee_pct": 0.02,
            "leveled": True, "max_level": 10,
            "stackable": False, "max_stack": 0,
            "stats": {"mh": 1.0},
        },
        "validator_guard": {
            "name": "Validator Guard", "emoji": ":shield:",
            "category": "consumable", "description": "blocks slash",
            "cost_stable": 50.0, "buy_fee_pct": 0.0, "sell_fee_pct": 0.0,
            "leveled": False, "max_level": 0,
            "stackable": True, "max_stack": 99,
            "stats": {},
        },
    }
    monkeypatch.setattr(Config, "SHOP_ITEMS", fake_shop)

    _ensure_registered("shop.catalog", lambda: ToolSpec(
        name="shop.catalog", summary="c", risk=RiskLevel.READ,
        params=[ParamSpec("category", "str", required=False, default=None)],
        handler=shop_tools.catalog, category="shop", cooldown_s=0,
    ))

    res = await run_tool("shop.catalog", audit_ctx, {})
    assert res.ok is True
    assert res.data["count"] == 2
    keys = [it["key"] for it in res.data["items"]]
    assert set(keys) == {"hashstone", "validator_guard"}

    # Filter by category.
    res = await run_tool("shop.catalog", audit_ctx, {"category": "consumable"})
    assert res.ok is True
    assert res.data["count"] == 1
    assert res.data["items"][0]["key"] == "validator_guard"


@pytest.mark.asyncio
async def test_shop_item_info_returns_ownership_snapshot(
    isolated_registry, monkeypatch,
):
    from core.framework.agent_tools.tools import shop as shop_tools
    from core.framework.scale import SCALE
    from core.config import Config

    fake_shop = {
        "hashstone": {
            "name": "Hashstone", "emoji": ":r:", "category": "item",
            "cost_stable": 100.0, "leveled": True, "max_level": 10,
            "stats": {"mh": 1.0},
        },
    }
    monkeypatch.setattr(Config, "SHOP_ITEMS", fake_shop)

    class _FakeDB:
        async def get_hashstone(self, uid, gid):
            return PgRow({
                "level": 3, "xp": 42.0,
                "staked_amount": 250 * SCALE, "acquired_at": 123.0,
            })

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("shop.item_info", lambda: ToolSpec(
        name="shop.item_info", summary="i", risk=RiskLevel.READ,
        params=[ParamSpec("item_key", "str")],
        handler=shop_tools.item_info, category="shop", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=2, db=_FakeDB())
    res = await run_tool("shop.item_info", ctx, {"item_key": "hashstone"})
    assert res.ok is True
    assert res.data["key"] == "hashstone"
    owned = res.data["ownership"]
    assert owned["owns"] is True
    assert owned["level"] == 3
    assert owned["staked_amount"] == 250.0


@pytest.mark.asyncio
async def test_shop_item_info_unknown_key_fails(
    isolated_registry, audit_ctx, monkeypatch,
):
    from core.framework.agent_tools.tools import shop as shop_tools
    from core.config import Config

    monkeypatch.setattr(Config, "SHOP_ITEMS", {})

    _ensure_registered("shop.item_info", lambda: ToolSpec(
        name="shop.item_info", summary="i", risk=RiskLevel.READ,
        params=[ParamSpec("item_key", "str")],
        handler=shop_tools.item_info, category="shop", cooldown_s=0,
    ))

    res = await run_tool("shop.item_info", audit_ctx, {"item_key": "nope"})
    assert res.ok is False
    assert "unknown_item" in (res.error or "")


@pytest.mark.asyncio
async def test_items_inventory_aggregates_stones_and_consumables(
    isolated_registry, monkeypatch,
):
    from core.framework.agent_tools.tools import items as items_tools
    from core.framework.scale import SCALE
    from core.config import Config

    fake_shop = {
        "hashstone":       {"name": "Hashstone",       "emoji": ":r:", "max_level": 10, "stats": {"mh": 1.0}},
        "lockstone":       {"name": "Lockstone",       "emoji": ":l:", "max_level": 10, "stats": {}},
        "validator_guard": {"name": "Validator Guard", "emoji": ":s:", "max_stack": 99},
    }
    monkeypatch.setattr(Config, "SHOP_ITEMS", fake_shop)

    class _FakeDB:
        async def get_hashstone(self, uid, gid):
            return PgRow({"level": 2, "xp": 10.0, "staked_amount": 150 * SCALE})

        async def get_lockstone(self, uid, gid):
            return PgRow({"level": 1, "xp": 5.0, "staked_amount": 100 * SCALE})

        async def get_vaultstone(self, uid, gid):
            return None

        async def get_liqstone(self, uid, gid):
            return None

        async def get_validator_guard_count(self, uid, gid):
            return 4

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("items.inventory", lambda: ToolSpec(
        name="items.inventory", summary="i", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=items_tools.inventory, category="items", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=7, guild_id=8, db=_FakeDB())
    res = await run_tool("items.inventory", ctx, {})
    assert res.ok is True
    assert res.data["stone_count"] == 2
    assert res.data["consumable_count"] == 1
    keys = {s["key"] for s in res.data["stones"]}
    assert keys == {"hashstone", "lockstone"}
    hs = next(s for s in res.data["stones"] if s["key"] == "hashstone")
    assert hs["level"] == 2
    assert hs["staked_amount"] == 150.0
    assert res.data["consumables"][0]["count"] == 4


@pytest.mark.asyncio
async def test_savings_summary_computes_apy_floor_and_sums_deposits(
    isolated_registry, monkeypatch,
):
    from core.framework.agent_tools.tools import savings as savings_tools
    from core.framework.scale import SCALE
    from core.config import Config

    # Only DSD is a stablecoin in this fake.
    fake_tokens = {
        "DSD":  {"stablecoin": True},
        "USDC": {"stablecoin": True},
        "MTA":  {"stablecoin": False},
    }
    fake_rate_model = {"base_savings_rate": 0.000165}  # ~6% APY
    monkeypatch.setattr(Config, "TOKENS", fake_tokens)
    monkeypatch.setattr(Config, "SAVINGS_RATE_MODEL", fake_rate_model)

    class _FakeDB:
        async def get_savings_deposit(self, uid, gid, sym):
            if sym == "DSD":
                return PgRow({"amount": 1000 * SCALE, "last_interest": 1234.0})
            if sym == "USDC":
                return None
            return None

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("savings.summary", lambda: ToolSpec(
        name="savings.summary", summary="s", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=savings_tools.summary, category="savings", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("savings.summary", ctx, {})
    assert res.ok is True
    assert res.data["deposit_count"] == 1
    assert res.data["deposits"][0]["symbol"] == "DSD"
    assert res.data["deposits"][0]["amount"] == 1000.0
    assert res.data["total_usd"] == 1000.0
    # 0.000165/day -> ~6% APY; confirm the floor is in a sane range.
    floor = res.data["base_apy_floor"]
    assert 0.04 < floor < 0.10


@pytest.mark.asyncio
async def test_loans_summary_returns_payload_when_loan_exists(
    isolated_registry, monkeypatch,
):
    from core.framework.agent_tools.tools import loans as loans_tools
    from core.framework.scale import SCALE

    class _FakeDB:
        async def get_loan(self, uid, gid):
            return PgRow({
                "principal":    500 * SCALE,
                "outstanding":  550 * SCALE,
                "collateral":   1000 * SCALE,
                "last_interest": 999.0,
            })

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("loans.summary", lambda: ToolSpec(
        name="loans.summary", summary="l", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=loans_tools.summary, category="loans", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("loans.summary", ctx, {})
    assert res.ok is True
    assert res.data["has_loan"] is True
    assert res.data["principal"]   == 500.0
    assert res.data["outstanding"] == 550.0
    assert res.data["collateral"]  == 1000.0


@pytest.mark.asyncio
async def test_loans_summary_returns_has_loan_false_when_missing(
    isolated_registry,
):
    from core.framework.agent_tools.tools import loans as loans_tools

    class _FakeDB:
        async def get_loan(self, uid, gid):
            return None

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("loans.summary", lambda: ToolSpec(
        name="loans.summary", summary="l", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=loans_tools.summary, category="loans", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("loans.summary", ctx, {})
    assert res.ok is True
    assert res.data["has_loan"] is False


@pytest.mark.asyncio
async def test_vault_state_filters_by_network(isolated_registry):
    from core.framework.agent_tools.tools import vault as vault_tools

    class _FakeDB:
        async def get_all_vaults(self, gid):
            return [
                {"network": "sun", "balance": 100.0, "level": 2},
                {"network": "arc", "balance": 50.0,  "level": 1},
                {"network": "dsc", "balance": 10.0,  "level": 0},
            ]

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("vault.state", lambda: ToolSpec(
        name="vault.state", summary="v", risk=RiskLevel.READ,
        params=[ParamSpec("network", "str", required=False, default=None)],
        handler=vault_tools.state, category="vault", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("vault.state", ctx, {})
    assert res.ok is True
    assert res.data["count"] == 3
    assert res.data["total_balance"] == 160.0
    # Sorted by balance desc.
    assert res.data["vaults"][0]["network"] == "sun"

    res = await run_tool("vault.state", ctx, {"network": "arc"})
    assert res.ok is True
    assert res.data["count"] == 1
    assert res.data["vaults"][0]["network"] == "arc"


@pytest.mark.asyncio
async def test_staking_summary_merges_npc_pos_and_delegations(isolated_registry):
    from core.framework.agent_tools.tools import staking as staking_tools

    class _FakeDB:
        async def get_user_stakes(self, uid, gid):
            return [
                {"validator_id": "v1", "name": "Alice", "emoji": ":a:",
                 "network": "arc", "symbol": "ARC", "amount": 1.5,
                 "reward_rate": 0.05, "uptime_rate": 0.99, "slash_rate": 0.0},
                {"validator_id": "v2", "name": "Bob", "amount": 0.0},  # filtered
            ]

        async def get_user_pos_validators(self, uid, gid):
            return [{
                "network": "dsc", "stake_token": "DSC",
                "stake_amount": 1000.0, "commission_rate": 0.1,
                "is_active": True, "slash_count": 0,
                "total_blocks_validated": 42, "total_rewards_earned": 5.5,
            }]

        async def get_user_delegations(self, uid, gid):
            return [{
                "validator_user_id": 999, "network": "dsc",
                "token": "DSC", "amount": 500.0,
                "locked_until": 0, "session_earned": 2.5,
            }]

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("staking.summary", lambda: ToolSpec(
        name="staking.summary", summary="s", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=staking_tools.summary, category="staking", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("staking.summary", ctx, {})
    assert res.ok is True
    assert res.data["npc_stake_count"] == 1           # zero-amount filtered
    assert res.data["npc_stakes"][0]["validator_id"] == "v1"
    assert res.data["pos_validator_count"] == 1
    assert res.data["pos_validators"][0]["network"] == "dsc"
    assert res.data["delegation_count"] == 1
    assert res.data["delegations"][0]["validator_user_id"] == 999


@pytest.mark.asyncio
async def test_history_transactions_handles_raw_scaled_amounts(isolated_registry):
    from core.framework.agent_tools.tools import history as history_tools
    from core.framework.scale import SCALE

    class _FakeDB:
        async def get_user_tx_history(self, uid, gid, limit):
            return [
                {"tx_hash": "0xabc", "type": "trade", "symbol": "ARC",
                 "amount": 2 * SCALE, "network": "arc",
                 "ts": 1000.0, "note": "buy"},
                {"tx_hash": "0xdef", "type": "faucet", "symbol": "DSD",
                 "amount": 10 * SCALE, "network": "dsc",
                 "ts": 1001.0, "note": "daily"},
            ]

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("history.transactions", lambda: ToolSpec(
        name="history.transactions", summary="h", risk=RiskLevel.READ,
        params=[
            ParamSpec("target_id", "uid", required=False, default=None),
            ParamSpec("limit", "int", required=False, default=20, min=1, max=50),
        ],
        handler=history_tools.transactions, category="history", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=1, db=_FakeDB())
    res = await run_tool("history.transactions", ctx, {"limit": 5})
    assert res.ok is True
    assert res.data["count"] == 2
    amounts = [it["amount"] for it in res.data["items"]]
    assert amounts == [2.0, 10.0]
    assert res.data["items"][0]["type"] == "trade"


@pytest.mark.asyncio
async def test_leaderboard_top_and_rank_use_bulk_net_worth(
    isolated_registry, monkeypatch,
):
    from core.framework.agent_tools.tools import leaderboard as lb_tools
    import services.net_worth as nw_mod

    async def fake_bulk(gid, db):
        return {111: 5000.0, 222: 12000.0, 333: 300.0}

    monkeypatch.setattr(nw_mod, "compute_bulk_net_worth", fake_bulk)

    _ensure_registered("leaderboard.top", lambda: ToolSpec(
        name="leaderboard.top", summary="t", risk=RiskLevel.READ,
        params=[ParamSpec("limit", "int", required=False, default=10, min=1, max=25)],
        handler=lb_tools.top, category="leaderboard", cooldown_s=0,
    ))
    _ensure_registered("leaderboard.rank", lambda: ToolSpec(
        name="leaderboard.rank", summary="r", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=lb_tools.rank, category="leaderboard", cooldown_s=0,
    ))

    class _DB:
        async def execute(self, query, *args):
            return "INSERT 0 1"

    ctx = ToolContext(user_id=111, guild_id=99, db=_DB())

    res = await run_tool("leaderboard.top", ctx, {"limit": 2})
    assert res.ok is True
    assert res.data["player_count"] == 3
    top = res.data["top"]
    assert [r["user_id"] for r in top] == [222, 111]
    assert top[0]["rank"] == 1
    assert top[0]["net_worth_usd"] == 12000.0

    res = await run_tool("leaderboard.rank", ctx, {})
    assert res.ok is True
    assert res.data["target_id"] == 111
    assert res.data["rank"] == 2
    assert res.data["net_worth_usd"] == 5000.0


@pytest.mark.asyncio
async def test_groups_summary_returns_false_when_no_group(isolated_registry):
    from core.framework.agent_tools.tools import groups as groups_tools

    class _DB:
        async def get_user_mining_group(self, uid, gid):
            return None

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("groups.summary", lambda: ToolSpec(
        name="groups.summary", summary="g", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=groups_tools.summary, category="groups", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=1, guild_id=2, db=_DB())
    res = await run_tool("groups.summary", ctx, {})
    assert res.ok is True
    assert res.data["has_group"] is False


@pytest.mark.asyncio
async def test_groups_summary_populates_members_and_upgrades(isolated_registry):
    from core.framework.agent_tools.tools import groups as groups_tools
    from core.framework.scale import SCALE

    class _DB:
        async def get_user_mining_group(self, uid, gid):
            return {
                "group_id": "g7", "name": "The Miners",
                "founder_id": 42, "reserve": 250 * SCALE,
                "max_members": 10,
            }

        async def get_group_members(self, gid, group_id):
            assert group_id == "g7"
            return [
                {"user_id": 42, "joined_at": 100.0},
                {"user_id": 43, "joined_at": 200.0},
            ]

        async def get_group_hall_upgrades(self, gid, group_id):
            return [{"upgrade_key": "refinery_lv1"}, {"upgrade_key": "barracks"}]

        async def execute(self, query, *args):
            return "INSERT 0 1"

    _ensure_registered("groups.summary", lambda: ToolSpec(
        name="groups.summary", summary="g", risk=RiskLevel.READ,
        params=[ParamSpec("target_id", "uid", required=False, default=None)],
        handler=groups_tools.summary, category="groups", cooldown_s=0,
    ))

    ctx = ToolContext(user_id=42, guild_id=1, db=_DB())
    res = await run_tool("groups.summary", ctx, {})
    assert res.ok is True
    assert res.data["has_group"] is True
    assert res.data["group_id"] == "g7"
    assert res.data["name"] == "The Miners"
    assert res.data["founder_id"] == 42
    assert res.data["reserve_usd"] == 250.0
    assert res.data["member_count"] == 2
    assert {m["user_id"] for m in res.data["members"]} == {42, 43}
    assert set(res.data["upgrades"]) == {"refinery_lv1", "barracks"}
