"""
core/framework/agent_tools/core.py -- shared types for the agent tools framework.

Design goals:
  - Fewer, more powerful tools with strict schemas (no one-per-tiny-action).
  - Every tool returns a ToolResult envelope, not a raw text blob.
  - Input validation before the handler ever runs.
  - Risk level drives an explicit approval step for dangerous actions.
  - Every invocation is auditable via the agent_tool_audit table.
  - The AI function-calling feed (``openai_tool_schemas``) and registry
    lookups are gated by ``registry_state`` so disabled tools become
    invisible without touching the handler code.
"""
from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("discoin.agent_tools")


# ── Risk classification ─────────────────────────────────────────────────────────────

class RiskLevel(str, enum.Enum):
    """How dangerous a tool is. Drives the approval policy.

    READ    -- read-only, no side effects. Safe for autonomous agents.
    SAFE    -- idempotent local writes (alerts, triggers). Safe for users.
    MUTATE  -- changes economy state for the caller. Requires authenticated caller.
    DANGER  -- irreversible or high-value. Always needs explicit approval.
    """

    READ = "read"
    SAFE = "safe"
    MUTATE = "mutate"
    DANGER = "danger"


# ── Result envelope ───────────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    """Structured tool result. Tools MUST return this, never a raw blob."""

    ok: bool
    data: dict | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "data": self.data, "error": self.error, "meta": self.meta},
            default=str,
        )

    @classmethod
    def success(cls, data: dict, **meta: Any) -> "ToolResult":
        return cls(ok=True, data=data, meta=dict(meta))

    @classmethod
    def fail(cls, error: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=error, meta=dict(meta))

    @classmethod
    def needs_approval(cls, reason: str, preview: dict) -> "ToolResult":
        return cls(
            ok=False,
            error="approval_required",
            data=preview,
            meta={"reason": reason},
        )


# ── Invocation context ─────────────────────────────────────────────────────────────────

@dataclass
class ToolContext:
    """What a tool sees: caller, guild, db, bus, actor. No raw discord objects."""

    user_id: int
    guild_id: int
    db: Any
    bus: Any = None
    # Discord channel/thread id the invocation originated in, when known.
    # Set by the chat loop so DAG tools can resolve "the current thread"
    # without the AI ever passing -- or seeing -- a Discord handle. It is a
    # plain lookup key, never an execution handle; None outside a channel.
    channel_id: int | None = None
    actor: str = "user"
        # "user" | "agent" | "chain" | "queue" | "trigger"
    approved: bool = False
    dry_run: bool = False
    # Populated by the ai_bridge when image.generate succeeds so callers
    # can send the image URL to Discord without parsing AI text output.
    generated_images: list[str] = field(default_factory=list)
    # Populated by the ai_bridge when data.web_search succeeds so the Discord
    # layer can attach a "Sources" button to the reply.
    search_sources: list[dict] = field(default_factory=list)

    def audit_tag(self) -> str:
        return f"{self.actor}:{self.user_id}:{self.guild_id}"


# ── Parameter + tool specs ────────────────────────────────────────────────────────────

@dataclass
class ParamSpec:
    """A single parameter. Strict types, no hallucinated shapes."""

    name: str
    type: str
        # "str" | "int" | "float" | "bool" | "symbol" | "network" | "uid" | "json"
    required: bool = True
    default: Any = None
    description: str = ""
    choices: list[Any] | None = None
    min: float | None = None
    max: float | None = None


@dataclass
class ToolSpec:
    name: str
    summary: str
    risk: RiskLevel
    params: list[ParamSpec]
    handler: Callable[["ToolContext", dict], Awaitable[ToolResult]]
    category: str = "general"
    idempotent: bool = False
    cooldown_s: int = 0

    def to_openai_schema(self) -> dict:
        """Export as an OpenAI / Anthropic function-call tool definition."""
        type_map = {
            "str": "string", "symbol": "string", "network": "string", "uid": "string",
            "int": "integer", "float": "number", "bool": "boolean",
        }
        props: dict[str, dict] = {}
        required: list[str] = []
        for p in self.params:
            entry: dict = {"description": p.description or ""}
            if p.type == "json":
                # validation._coerce accepts both objects and arrays for json
                # params, so the schema must allow both shapes -- otherwise the
                # AI sees an "object" hint and the validator silently rejects
                # legitimate array payloads (or vice versa).
                entry["anyOf"] = [{"type": "object"}, {"type": "array"}]
            else:
                entry["type"] = type_map.get(p.type, "string")
            if p.choices:
                entry["enum"] = list(p.choices)
            if p.min is not None:
                entry["minimum"] = p.min
            if p.max is not None:
                entry["maximum"] = p.max
            props[p.name] = entry
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.risk.value}] {self.summary}",
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }


# ── Registry ──────────────────────────────────────────────────────────────────────────

class ToolRegistry:
    """Global registry. Tools register via the @tool(...) decorator on import."""

    _tools: dict[str, ToolSpec] = {}

    @classmethod
    def register(cls, spec: ToolSpec, *, replace: bool = False) -> None:
        if spec.name in cls._tools:
            if replace:
                cls._tools[spec.name] = spec
                log.info("[agent_tools] replaced tool %s (%s)", spec.name, spec.risk.value)
                return
            raise ValueError(f"agent tool {spec.name!r} already registered")
        cls._tools[spec.name] = spec
        log.info("[agent_tools] registered %s (%s)", spec.name, spec.risk.value)

    @classmethod
    def get(cls, name: str) -> ToolSpec | None:
        return cls._tools.get(name)

    @classmethod
    def all(cls) -> list[ToolSpec]:
        return list(cls._tools.values())

    @classmethod
    def by_category(cls, category: str) -> list[ToolSpec]:
        return [t for t in cls._tools.values() if t.category == category]

    @classmethod
    def is_enabled(cls, name: str) -> bool:
        """True if ``name`` is currently enabled per registry_state.

        Built-in tools default to enabled -- the gate only says "no" if an
        operator explicitly disabled the tool or if it was installed from
        disrepo and hasn't been flipped on yet.
        """
        # Lazy import to avoid a circular import at module load.
        from . import registry_state
        return registry_state.is_enabled("tool", name, default=True)

    @classmethod
    def enabled_all(cls) -> list[ToolSpec]:
        """Every registered tool that is currently enabled."""
        from . import registry_state
        return [
            t for t in cls._tools.values()
            if registry_state.is_enabled("tool", t.name, default=True)
        ]

    @classmethod
    def openai_tool_schemas(cls, exclude_danger: bool = True) -> list[dict]:
        from . import registry_state
        tools = cls.all()
        if exclude_danger:
            tools = [t for t in tools if t.risk != RiskLevel.DANGER]
        tools = [
            t for t in tools
            if registry_state.is_enabled("tool", t.name, default=True)
        ]
        return [t.to_openai_schema() for t in tools]


def tool(
    name: str,
    summary: str,
    risk: RiskLevel = RiskLevel.READ,
    params: list[ParamSpec] | None = None,
    category: str = "general",
    idempotent: bool = False,
    cooldown_s: int = 0,
) -> Callable:
    """Decorator: register an async function as an agent tool."""
    def decorator(fn: Callable[[ToolContext, dict], Awaitable[ToolResult]]):
        ToolRegistry.register(
            ToolSpec(
                name=name,
                summary=summary,
                risk=risk,
                params=list(params or []),
                handler=fn,
                category=category,
                idempotent=idempotent,
                cooldown_s=cooldown_s,
            )
        )
        return fn
    return decorator
