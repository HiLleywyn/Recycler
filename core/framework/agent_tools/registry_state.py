"""
core/framework/agent_tools/registry_state.py -- enable/disable state for tools,
plugins, hooks and disrepo-installed agent personas.

Design goals
  - Every callable thing in the framework (built-in tools, Lua plugins, chat
    hooks, disrepo-installed agents) has one source of truth for whether it
    should fire right now.
  - State is persisted to data/agent_registry_state.json so it survives
    restarts and does not depend on the database being up.
  - Built-in tools default to ENABLED (pre-existing behaviour is unchanged
    for operators who never touch the new surface).
  - Anything installed from disrepo defaults to DISABLED per the explicit
    spec: "By default, they're disabled. You would enable it with
    ,ai tools enable x".

Key schema
  _state = {
      "tool":   { <tool_name>:    {enabled, installed, meta} },
      "plugin": { <plugin_stem>:  {enabled, installed, meta} },
      "hook":   { <plugin_stem>:  {enabled, installed, meta} },
      "agent":  { <agent_name>:   {enabled, installed, meta} },
  }

The three gating choke points that read this file:
  1. core.ToolRegistry.openai_tool_schemas() -- AI function-calling feed
  2. executor.run_tool()                     -- direct/chain/queue/trigger
  3. lua_plugins.run_hooks()                 -- chat pipeline hooks
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("discoin.agent_tools.registry_state")

_STATE_PATH = Path(__file__).parent.parent.parent / "data" / "agent_registry_state.json"
_VALID_TYPES = ("tool", "plugin", "hook", "agent")

_lock = threading.RLock()
_state: dict[str, dict[str, dict[str, Any]]] | None = None


def _empty_state() -> dict[str, dict[str, dict[str, Any]]]:
    return {t: {} for t in _VALID_TYPES}


def _load() -> dict[str, dict[str, dict[str, Any]]]:
    global _state
    with _lock:
        if _state is not None:
            return _state
        if not _STATE_PATH.exists():
            _state = _empty_state()
            return _state
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("[registry_state] could not parse %s: %s", _STATE_PATH, exc)
            _state = _empty_state()
            return _state
        if not isinstance(raw, dict):
            _state = _empty_state()
            return _state
        # Normalise: ensure every type bucket exists.
        for t in _VALID_TYPES:
            bucket = raw.get(t)
            if not isinstance(bucket, dict):
                raw[t] = {}
        _state = raw  # type: ignore[assignment]
        return _state


def _save() -> None:
    with _lock:
        state = _load()
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(
                json.dumps(state, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("[registry_state] failed to persist %s: %s", _STATE_PATH, exc)


def _validate_type(item_type: str) -> str:
    t = (item_type or "").strip().lower()
    if t not in _VALID_TYPES:
        raise ValueError(f"invalid item_type {item_type!r}; valid: {_VALID_TYPES}")
    return t


def is_enabled(item_type: str, name: str, *, default: bool = True) -> bool:
    """Return True if the given (type, name) is currently enabled.

    ``default`` controls the fallback when the item has no explicit entry:
    built-in tools / hooks pass ``default=True`` so pre-existing behaviour
    is preserved, while disrepo-installed items are tracked with an explicit
    ``enabled=False`` row the moment they are installed.
    """
    t = _validate_type(item_type)
    name = (name or "").strip()
    if not name:
        return default
    with _lock:
        bucket = _load().get(t, {})
        entry = bucket.get(name)
        if entry is None or not isinstance(entry, dict):
            return default
        return bool(entry.get("enabled", default))


def set_enabled(item_type: str, name: str, enabled: bool) -> None:
    """Toggle a single (type, name) entry. Creates the row if it does not exist."""
    t = _validate_type(item_type)
    name = (name or "").strip()
    if not name:
        raise ValueError("name cannot be empty")
    with _lock:
        bucket = _load().setdefault(t, {})
        entry = bucket.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["enabled"] = bool(enabled)
        entry.setdefault("installed", False)
        bucket[name] = entry
        _save()
    log.info("[registry_state] %s/%s -> enabled=%s", t, name, bool(enabled))


def mark_installed(
    item_type: str,
    name: str,
    *,
    meta: dict | None = None,
    default_enabled: bool = False,
) -> None:
    """Record that an item was installed from disrepo.

    Installed items default to DISABLED per the user-facing contract. Call
    ``set_enabled`` to flip them on later. Re-installing an item preserves
    the operator's previous enable choice.
    """
    t = _validate_type(item_type)
    name = (name or "").strip()
    if not name:
        raise ValueError("name cannot be empty")
    with _lock:
        bucket = _load().setdefault(t, {})
        entry = bucket.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["installed"] = True
        entry.setdefault("enabled", bool(default_enabled))
        if meta:
            entry_meta = entry.get("meta") or {}
            if not isinstance(entry_meta, dict):
                entry_meta = {}
            entry_meta.update({str(k): v for k, v in meta.items()})
            entry["meta"] = entry_meta
        bucket[name] = entry
        _save()
    log.info(
        "[registry_state] installed %s/%s (default_enabled=%s)",
        t, name, default_enabled,
    )


def mark_uninstalled(item_type: str, name: str) -> None:
    """Record that an item's underlying files have been removed."""
    t = _validate_type(item_type)
    name = (name or "").strip()
    if not name:
        return
    with _lock:
        bucket = _load().setdefault(t, {})
        entry = bucket.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["installed"] = False
        entry["enabled"] = False
        bucket[name] = entry
        _save()
    log.info("[registry_state] uninstalled %s/%s", t, name)


def forget(item_type: str, name: str) -> None:
    """Drop an entry entirely (used when uninstalling and cleaning up)."""
    t = _validate_type(item_type)
    name = (name or "").strip()
    if not name:
        return
    with _lock:
        bucket = _load().setdefault(t, {})
        bucket.pop(name, None)
        _save()


def installed_items(item_type: str | None = None) -> list[dict[str, Any]]:
    """List every item currently marked as installed from disrepo.

    Each row is ``{"type", "name", "enabled", "meta"}``.
    """
    rows: list[dict[str, Any]] = []
    with _lock:
        state = _load()
        types: Iterable[str] = (_validate_type(item_type),) if item_type else _VALID_TYPES
        for t in types:
            bucket = state.get(t, {})
            for name, entry in bucket.items():
                if not isinstance(entry, dict):
                    continue
                if not entry.get("installed"):
                    continue
                rows.append({
                    "type":    t,
                    "name":    name,
                    "enabled": bool(entry.get("enabled", False)),
                    "meta":    dict(entry.get("meta") or {}),
                })
    rows.sort(key=lambda r: (r["type"], r["name"]))
    return rows


def all_entries(item_type: str | None = None) -> list[dict[str, Any]]:
    """List every tracked entry (installed or not) for introspection."""
    rows: list[dict[str, Any]] = []
    with _lock:
        state = _load()
        types: Iterable[str] = (_validate_type(item_type),) if item_type else _VALID_TYPES
        for t in types:
            bucket = state.get(t, {})
            for name, entry in bucket.items():
                if not isinstance(entry, dict):
                    continue
                rows.append({
                    "type":      t,
                    "name":      name,
                    "enabled":   bool(entry.get("enabled", False)),
                    "installed": bool(entry.get("installed", False)),
                    "meta":      dict(entry.get("meta") or {}),
                })
    rows.sort(key=lambda r: (r["type"], r["name"]))
    return rows


def reload() -> None:
    """Force the in-memory cache to re-read from disk on next access."""
    global _state
    with _lock:
        _state = None
