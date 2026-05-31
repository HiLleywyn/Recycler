"""
core/framework/agent_tools/lua_plugins.py -- Lua plugin loader for AI agent tools.

Lua plugins live in ``plugins/*.lua`` at the project root. Each file gets its
own isolated, sandboxed LuaRuntime so plugins cannot interfere with each other
or the host process. Files prefixed with ``_`` are skipped (disabled templates).

Files whose stem starts with ``disrepo__<type>__<name>`` are treated as
disrepo-installed bundles: after a successful load the loader calls
``registry_state.mark_installed(...)`` for the bundle, every tool it
registered, and (if it registered any chat hooks) the stem-as-hook, with
``default_enabled=False`` so installed items are off until an operator flips
them on via ``,ai tools|plugins|hooks enable``.

Sandbox removes: io, os, package, require, dofile, loadfile, load, debug,
collectgarbage, rawget, rawset, rawequal, rawlen, setmetatable, getmetatable.

== Agent tools ==

Register a tool the AI can call:

    tool_api.register({
        name    = "my.tool",
        summary = "Short description for the AI.",
        risk    = "read",   -- "read" | "safe" | "mutate"
        params  = {
            { name="who", type="str", required=false, default="world",
              description="Target name." },
        },
        handler = function(args)
            return tool_api.ok({ message = "Hello, " .. (args.who or "world") })
        end,
    })

Return values from handler:
    tool_api.ok({ ... })      -> ToolResult.success
    tool_api.fail("reason")   -> ToolResult.fail

The handler runs in asyncio.to_thread so it never blocks the event loop.

== Real-time chat hooks ==

Hooks intercept the AI chat pipeline. Each hook receives the current string
value and a read-only context table, and returns the (optionally modified)
string. Returning nil or a non-string passes the original through unchanged.

Three hook points in pipeline order:

    -- 1. Fired after the system prompt is assembled, before the AI call.
    tool_api.on_system_prompt(function(prompt, ctx)
        return prompt .. "\\n\\nAlways answer in exactly one sentence."
    end)

    -- 2. Fired after the user message is sanitized, before the AI call.
    tool_api.on_user_message(function(msg, ctx)
        return "[" .. ctx.display_name .. "] " .. msg
    end)

    -- 3. Fired after the AI produces a reply, before it is sent to Discord.
    tool_api.on_ai_reply(function(reply, ctx)
        return reply .. " (powered by Lua)"
    end)

Context table fields (all strings):
    ctx.guild_id, ctx.user_id, ctx.display_name, ctx.channel_id

Multiple plugins can register the same hook type; they run in load order,
each receiving the output of the previous one (pipeline/compose pattern).
Disabled plugin stems are skipped in ``run_hooks`` so operators can mute
a noisy hook without deleting its file.

Reloading:
    lua_plugins.reload()  -- re-scans plugins/ and re-registers everything.
    Chat hooks are fully cleared and rebuilt on reload.
    Agent tools from previous loads stay in the registry (no unregister op).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import registry_state

log = logging.getLogger("discoin.lua_plugins")

_PLUGINS_DIR = Path(__file__).parent.parent.parent / "plugins"

# Map plugin_file_stem -> list[tool_name] so we can report what each file owns.
_loaded: dict[str, list[str]] = {}

# Chat pipeline hooks: list of (plugin_stem, lua_callable) in registration order.
# Cleared and rebuilt on every reload().
_HOOK_TYPES = ("system_prompt", "user_message", "ai_reply")
_hooks: dict[str, list[tuple[str, Any]]] = {k: [] for k in _HOOK_TYPES}

# Dangerous Lua globals stripped from every sandbox.
_STRIP_GLOBALS = (
    "io", "os", "package", "require", "dofile", "loadfile", "load",
    "debug", "collectgarbage", "rawget", "rawset", "rawequal", "rawlen",
    "setmetatable", "getmetatable",
)

# Lua snippet that removes dangerous globals and defines helpers.
_SANDBOX_SETUP = """
do
    local _forbidden = {
        "io","os","package","require","dofile","loadfile","load",
        "debug","collectgarbage","rawget","rawset","rawequal","rawlen",
        "setmetatable","getmetatable",
    }
    for _, k in ipairs(_forbidden) do
        _G[k] = nil
    end
end

-- Safe string representation for Lua values (used in error messages).
local function _repr(v)
    local t = type(v)
    if t == "string" then return v end
    if t == "number" or t == "boolean" then return tostring(v) end
    return "[" .. t .. "]"
end
"""


def _to_python(val: Any) -> Any:
    """Recursively convert a lupa Lua table proxy to a plain Python dict/list."""
    try:
        from lupa import LuaRuntime  # noqa: F401 - just ensure lupa is importable
        # Check if this is a Lua table using lupa's type checking
        if hasattr(val, "items") and callable(val.items):
            # Could be a table-as-dict or table-as-list; inspect keys
            try:
                pairs = list(val.items())
            except Exception:
                return str(val)
            # If all keys are consecutive ints starting at 1, treat as list.
            keys = [k for k, _ in pairs]
            if keys and all(isinstance(k, int) for k in keys):
                keys_sorted = sorted(keys)
                if keys_sorted == list(range(1, len(keys_sorted) + 1)):
                    return [_to_python(v) for _, v in sorted(pairs)]
            return {str(k): _to_python(v) for k, v in pairs}
        if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
            # Generic iterable (shouldn't hit for normal Lua tables, but safe)
            return [_to_python(item) for item in val]
    except Exception:
        pass
    if isinstance(val, (int, float, bool, str, type(None))):
        return val
    return str(val)


def _parse_disrepo_stem(stem: str) -> tuple[str, str] | None:
    """Return (type, name) if ``stem`` follows the disrepo naming convention.

    Files installed by disrepo.install_item land at
    ``plugins/disrepo__<type>__<name>.lua`` so the stem alone tells us what
    registry_state buckets to update.
    """
    if not stem.startswith("disrepo__"):
        return None
    parts = stem.split("__", 2)
    if len(parts) != 3:
        return None
    d_type, d_name = parts[1], parts[2]
    if d_type not in ("tool", "plugin", "hook") or not d_name:
        return None
    return d_type, d_name


def _make_tool_api(lua_rt, registered_names: list[str], plugin_stem: str = "") -> Any:
    """Build the ``tool_api`` table injected into a plugin's Lua sandbox."""
    from .core import ParamSpec, RiskLevel, ToolContext, ToolResult, ToolRegistry, ToolSpec

    _RISK_MAP = {
        "read":   RiskLevel.READ,
        "safe":   RiskLevel.SAFE,
        "mutate": RiskLevel.MUTATE,
    }

    def _lua_register(cfg_tbl) -> None:
        """Called from Lua: tool_api.register({ name=..., ... })"""
        if not hasattr(cfg_tbl, "items") or not callable(cfg_tbl.items):
            raise TypeError("tool_api.register expects a table")

        # Extract the handler function from the raw Lua table BEFORE converting
        # the rest to Python, because _to_python() turns Lua functions into
        # strings (they have no .items() method).
        lua_handler = None
        try:
            lua_handler = cfg_tbl["handler"]
        except Exception:
            pass
        if lua_handler is None or not callable(lua_handler):
            # Also try attribute access
            try:
                lua_handler = getattr(cfg_tbl, "handler", None)
            except Exception:
                pass
        if lua_handler is None or not callable(lua_handler):
            name_raw = "unknown"
            try:
                name_raw = str(cfg_tbl["name"])
            except Exception:
                pass
            raise ValueError(f"tool_api.register: {name_raw!r} has no callable handler")

        # Now convert the rest of the table (handler key becomes a string, but
        # we already have the real callable above, so that is fine).
        cfg = _to_python(cfg_tbl)
        if not isinstance(cfg, dict):
            raise TypeError("tool_api.register: config must be a table")

        name = str(cfg.get("name") or "").strip()
        if not name:
            raise ValueError("tool_api.register: name is required")
        summary = str(cfg.get("summary") or name)
        risk_key = str(cfg.get("risk") or "read").lower()
        risk = _RISK_MAP.get(risk_key, RiskLevel.READ)
        category = str(cfg.get("category") or "lua_plugin")
        cooldown_s = int(cfg.get("cooldown_s") or 0)

        raw_params = cfg.get("params") or []
        params: list[ParamSpec] = []
        for p in (raw_params if isinstance(raw_params, list) else []):
            if not isinstance(p, dict):
                continue
            params.append(ParamSpec(
                name=str(p.get("name") or "arg"),
                type=str(p.get("type") or "str"),
                required=bool(p.get("required", True)),
                default=p.get("default"),
                description=str(p.get("description") or ""),
                choices=list(p["choices"]) if isinstance(p.get("choices"), list) else None,
                min=float(p["min"]) if p.get("min") is not None else None,
                max=float(p["max"]) if p.get("max") is not None else None,
            ))

        # Snapshot the handler so the closure stays valid after the Lua state
        # continues executing.  lupa keeps the function object alive as long
        # as Python holds a reference to it.
        _lua_fn = lua_handler

        def _make_async_handler(fn, tool_name: str):
            async def _handler(ctx: ToolContext, args: dict) -> ToolResult:
                # Build a Lua-compatible args table.
                try:
                    lua_args = lua_rt.table_from(
                        {k: v for k, v in args.items() if not k.startswith("_")}
                    )
                except Exception as exc:
                    return ToolResult.fail(f"lua_args_build_failed: {exc}")

                # Run synchronous Lua handler in a thread so the event loop
                # is not blocked, even for slow scripts.
                def _run():
                    try:
                        return fn(lua_args)
                    except Exception as exc:
                        return {"_lua_error": str(exc)}

                try:
                    raw = await asyncio.to_thread(_run)
                except Exception as exc:
                    return ToolResult.fail(f"lua_thread_error: {exc}")

                # The handler must return a Lua table produced by tool_api.ok/fail.
                result_dict = _to_python(raw) if raw is not None else {}
                if not isinstance(result_dict, dict):
                    return ToolResult.fail(
                        f"lua handler for {tool_name!r} must return tool_api.ok() or tool_api.fail()"
                    )
                if "_lua_error" in result_dict:
                    return ToolResult.fail(f"lua_runtime_error: {result_dict['_lua_error']}")

                ok = result_dict.get("_ok")
                if ok is True:
                    data = result_dict.get("_data") or {}
                    return ToolResult.success(data if isinstance(data, dict) else {"value": data})
                if ok is False:
                    return ToolResult.fail(str(result_dict.get("_error") or "unknown lua error"))
                # Bare table returned without tool_api.ok/fail - treat as success data
                return ToolResult.success({k: v for k, v in result_dict.items() if not k.startswith("_")})

            return _handler

        spec = ToolSpec(
            name=name,
            summary=summary,
            risk=risk,
            params=params,
            handler=_make_async_handler(_lua_fn, name),
            category=category,
            idempotent=bool(cfg.get("idempotent", False)),
            cooldown_s=cooldown_s,
        )
        ToolRegistry.register(spec, replace=True)
        registered_names.append(name)
        log.info("[lua_plugins] registered tool %r (risk=%s)", name, risk.value)

    def _lua_ok(data_tbl=None) -> Any:
        """Called from Lua: return tool_api.ok({ key = value })"""
        data = _to_python(data_tbl) if data_tbl is not None else {}
        return lua_rt.table_from({"_ok": True, "_data": data if isinstance(data, dict) else {"value": data}})

    def _lua_fail(error: str = "error") -> Any:
        """Called from Lua: return tool_api.fail("reason")"""
        return lua_rt.table_from({"_ok": False, "_error": str(error)})

    def _lua_log(msg: str) -> None:
        """Called from Lua: tool_api.log("message")"""
        log.info("[lua_plugin] %s", str(msg)[:500])

    # ── Chat pipeline hook registration ──────────────────────────────────────────────────

    def _make_hook_registrar(hook_type: str):
        def _register_hook(fn) -> None:
            if fn is None or not callable(fn):
                raise ValueError(f"tool_api.on_{hook_type}: expected a function")
            _hooks[hook_type].append((plugin_stem, fn))
            log.info("[lua_plugins] %s registered on_%s hook", plugin_stem, hook_type)
        return _register_hook

    tbl = lua_rt.table_from({
        "register":         _lua_register,
        "ok":               _lua_ok,
        "fail":             _lua_fail,
        "log":              _lua_log,
        "on_system_prompt": _make_hook_registrar("system_prompt"),
        "on_user_message":  _make_hook_registrar("user_message"),
        "on_ai_reply":      _make_hook_registrar("ai_reply"),
    })
    return tbl


def _load_plugin_file(path: Path) -> list[str]:
    """Load a single .lua plugin file. Returns list of registered tool names."""
    try:
        from lupa import LuaRuntime, LuaSyntaxError, LuaError
    except ImportError:
        log.error("[lua_plugins] lupa is not installed; Lua plugins disabled")
        return []

    stem = path.stem
    registered: list[str] = []

    try:
        lua_rt = LuaRuntime(unpack_returned_tuples=True, attribute_handlers=None)
    except Exception as exc:
        log.error("[lua_plugins] Failed to create LuaRuntime for %s: %s", path.name, exc)
        return []

    # Run the sandbox setup (strip dangerous globals).
    try:
        lua_rt.execute(_SANDBOX_SETUP)
    except Exception as exc:
        log.error("[lua_plugins] Sandbox setup failed for %s: %s", path.name, exc)
        return []

    # Inject tool_api.
    try:
        lua_rt.globals().tool_api = _make_tool_api(lua_rt, registered, plugin_stem=stem)
    except Exception as exc:
        log.error("[lua_plugins] Failed to inject tool_api for %s: %s", path.name, exc)
        return []

    # Snapshot hook counts so we can tell whether the plugin registered any
    # chat-pipeline hooks -- needed for the disrepo mark_installed step below.
    hook_counts_before = {k: len(_hooks[k]) for k in _HOOK_TYPES}

    # Execute the plugin source.
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("[lua_plugins] Cannot read %s: %s", path, exc)
        return []

    try:
        lua_rt.execute(source)
    except LuaSyntaxError as exc:
        log.error("[lua_plugins] Syntax error in %s: %s", path.name, exc)
        return []
    except LuaError as exc:
        log.error("[lua_plugins] Runtime error in %s: %s", path.name, exc)
        return []
    except Exception as exc:
        log.error("[lua_plugins] Unexpected error loading %s: %s", path.name, exc)
        return []

    new_hook_count = sum(
        len(_hooks[k]) - hook_counts_before[k] for k in _HOOK_TYPES
    )

    if registered:
        _loaded[stem] = registered
        log.info(
            "[lua_plugins] %s loaded %d tool(s): %s",
            path.name, len(registered), registered,
        )
    else:
        log.info(
            "[lua_plugins] %s loaded (no tools, %d hook(s))",
            path.name, new_hook_count,
        )
        # Still record an empty entry so hook_summary can list the file.
        _loaded.setdefault(stem, [])

    # Disrepo bookkeeping: make sure every artefact the file produced is
    # tracked in registry_state with default_enabled=False. Re-installs keep
    # the operator's previous enable choice thanks to setdefault semantics
    # inside registry_state.mark_installed.
    parsed = _parse_disrepo_stem(stem)
    if parsed is not None:
        d_type, d_name = parsed
        try:
            registry_state.mark_installed(
                d_type, d_name,
                meta={"stem": stem, "file": path.name},
                default_enabled=False,
            )
            for tname in registered:
                registry_state.mark_installed(
                    "tool", tname,
                    meta={"owner": f"{d_type}/{d_name}", "stem": stem},
                    default_enabled=False,
                )
            if new_hook_count > 0:
                registry_state.mark_installed(
                    "hook", stem,
                    meta={"owner": f"{d_type}/{d_name}"},
                    default_enabled=False,
                )
        except Exception as exc:
            log.warning(
                "[lua_plugins] mark_installed failed for %s: %s", stem, exc,
            )

    return registered


def load_lua_plugins() -> int:
    """Scan plugins/ and load every .lua file. Returns total tools registered."""
    try:
        from lupa import LuaRuntime  # noqa: F401
    except ImportError:
        log.warning("[lua_plugins] lupa not installed; skipping Lua plugin load")
        return 0

    if not _PLUGINS_DIR.exists():
        _PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        log.info("[lua_plugins] Created plugins/ directory at %s", _PLUGINS_DIR)

    # Files prefixed with "_" are treated as disabled templates and skipped.
    lua_files = sorted(p for p in _PLUGINS_DIR.glob("*.lua") if not p.name.startswith("_"))
    if not lua_files:
        log.debug(
            "[lua_plugins] No active .lua files found in %s "
            "(underscore-prefixed files are skipped)",
            _PLUGINS_DIR,
        )
        return 0

    total = 0
    for path in lua_files:
        names = _load_plugin_file(path)
        total += len(names)

    log.info(
        "[lua_plugins] Loaded %d Lua plugin tool(s) from %d file(s)",
        total, len(lua_files),
    )
    return total


def reload() -> int:
    """Re-scan plugins/ and load any new or changed files.

    Chat hooks are fully cleared and rebuilt from the new plugin set.
    Already-registered agent tools stay in the registry (no unregister op);
    duplicate tool names in updated files are skipped with a warning.
    """
    _loaded.clear()
    for k in _HOOK_TYPES:
        _hooks[k].clear()
    return load_lua_plugins()


async def run_hooks(hook_type: str, value: str, ctx: dict) -> str:
    """Run all registered hooks of ``hook_type`` in pipeline order.

    Each hook receives the current string value and a read-only context dict.
    If a hook returns a non-empty string it replaces the value for the next hook.
    If it returns nil, empty, or a non-string, the value passes through unchanged.
    Errors in individual hooks are logged and skipped -- they never crash the caller.

    Hooks whose owning plugin stem is disabled in ``registry_state`` are
    skipped entirely so operators can mute a noisy hook without deleting the
    file. Local (non-disrepo) plugin files default ENABLED so existing
    behaviour is preserved until an operator explicitly toggles them off.

    Args:
        hook_type: "system_prompt" | "user_message" | "ai_reply"
        value:     The string to thread through the hook pipeline.
        ctx:       Dict with guild_id, user_id, display_name, channel_id (all str).

    Returns:
        The final string after all hooks have had a chance to modify it.
    """
    hooks = _hooks.get(hook_type, [])
    if not hooks:
        return value

    current = value
    for stem, fn in hooks:
        if not registry_state.is_enabled("hook", stem, default=True):
            continue
        try:
            def _run(f=fn, v=current, c=dict(ctx)):
                try:
                    # Build a plain Lua-compatible table from the Python dict.
                    # We pass ctx fields as individual args rather than a table
                    # because we don't have the originating LuaRuntime here.
                    # Plugins must accept ctx as a table; lupa passes Python
                    # dicts as mapping proxies automatically.
                    result = f(v, c)
                    return result
                except Exception as exc:
                    return ("_hook_error", str(exc))

            raw = await asyncio.to_thread(_run)

            if isinstance(raw, tuple) and raw[0] == "_hook_error":
                log.warning("[lua_plugins] %s on_%s hook error: %s", stem, hook_type, raw[1])
                continue

            # Accept string returns; coerce from lupa proxies if needed.
            if raw is None:
                continue
            coerced = raw if isinstance(raw, str) else (str(raw) if not hasattr(raw, "items") else None)
            if coerced:
                current = coerced

        except Exception as exc:
            log.warning("[lua_plugins] %s on_%s hook crashed: %s", stem, hook_type, exc)

    return current


def hook_summary() -> str:
    """Return a short summary of all registered chat hooks."""
    lines = []
    for htype in _HOOK_TYPES:
        entries = _hooks[htype]
        if entries:
            stems = ", ".join(s for s, _ in entries)
            lines.append(f"  on_{htype}: [{stems}]")
    return "\n".join(lines) if lines else "  (no chat hooks registered)"


def plugin_summary() -> str:
    """Return a short human-readable summary of loaded Lua plugins."""
    if not _loaded:
        return "(no Lua plugins loaded)"
    lines = [f"- {stem}: {', '.join(names) if names else '(hooks only)'}" for stem, names in sorted(_loaded.items())]
    return "\n".join(lines)
