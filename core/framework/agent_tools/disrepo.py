"""
core/framework/agent_tools/disrepo.py -- download plugins/agents/tools/hooks from
the disrepo GitHub repository and install them into the running bot.

Repository layout (see hilleywyn/disrepo README):

    registry.json
    tools/<name>/manifest.json
    tools/<name>/<entry>.lua
    agents/<name>/manifest.json
    agents/<name>/agent.json
    plugins/<name>/manifest.json
    plugins/<name>/<entry>.lua
    hooks/<name>/manifest.json
    hooks/<name>/<entry>.lua

Manifest schema (JSON):

    {
        "name":        "sample_price_check",
        "type":        "tool" | "agent" | "plugin" | "hook",
        "version":     "1.0.0",
        "summary":     "Short one-line description",
        "author":      "handle",
        "risk":        "read" | "safe" | "mutate",   (tool/plugin/hook only)
        "entry":       "tool.lua",                    (relative filename)
        "tool_names":  ["sample.price_check"],        (optional, informational)
        "tags":        ["demo", "price"]              (optional)
    }

Installed files land at:

    plugins/disrepo__tool__<name>.lua
    plugins/disrepo__plugin__<name>.lua
    plugins/disrepo__hook__<name>.lua
    data/disrepo_agents/<name>.json

This file-name convention is *load-bearing*: the existing lua_plugins loader
picks up any *.lua in plugins/, and the ``disrepo__<type>__<name>`` prefix
lets us tie a stem back to its registry_state row for enable/disable gating.

Every install records a row via registry_state.mark_installed(...) with
``default_enabled=False`` so installed items are off until an operator runs
``,ai tools enable <name>`` (or the equivalent plugin/hook enable command).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry_state

log = logging.getLogger("discoin.agent_tools.disrepo")

DISREPO_OWNER = "hilleywyn"
DISREPO_REPO = "disrepo"
DISREPO_BRANCH = "main"
DISREPO_RAW_BASE = (
    f"https://raw.githubusercontent.com/{DISREPO_OWNER}/{DISREPO_REPO}/{DISREPO_BRANCH}"
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_PLUGINS_DIR = _REPO_ROOT / "plugins"
_AGENTS_DIR = _REPO_ROOT / "data" / "disrepo_agents"

_VALID_TYPES = ("tool", "agent", "plugin", "hook")

# Maps item type to the top-level folder in the disrepo repository.
_TYPE_DIR = {
    "tool":   "tools",
    "agent":  "agents",
    "plugin": "plugins",
    "hook":   "hooks",
}

# File system extension for each non-agent type. Agents are JSON bundles.
_LUA_TYPES = ("tool", "plugin", "hook")

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,63}$")

# Defence-in-depth: reject Lua payloads that reference symbols a plugin
# should never need. The sandbox in lua_plugins.py already strips these at
# runtime, but matching them at install time gives us an audit trail and a
# clearer error message for operators.
_FORBIDDEN_LUA_SUBSTRINGS = (
    "io.popen",
    "io.open",
    "os.execute",
    "os.remove",
    "os.rename",
    "os.exit",
    "package.loadlib",
    "require(",
    "dofile(",
    "loadfile(",
    "debug.debug",
    "debug.getregistry",
)

# Hard size ceilings so a malicious or misconfigured manifest cannot blow up
# local storage or the HTTP client.
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ENTRY_BYTES = 512 * 1024
_HTTP_TIMEOUT_S = 15


def _get_github_token() -> str:
    """Return the configured GITHUB_TOKEN, or empty string if not set.

    Lazy import avoids circular-import issues at module load time and lets
    disrepo work even if core/config.py hasn't been fully initialised yet.
    """
    try:
        from core.config import Config
        return getattr(Config, "GITHUB_TOKEN", "") or ""
    except Exception:  # noqa: BLE001
        return ""


class DisrepoError(Exception):
    """Raised when a disrepo fetch, validation, or install fails."""


@dataclass
class DisrepoRef:
    """Parsed ``<type>/<name>`` identifier from the user command."""

    type: str
    name: str

    def slug(self) -> str:
        return f"{self.type}/{self.name}"


@dataclass
class InstalledItem:
    """Return value from install_item -- describes what landed on disk."""

    type: str
    name: str
    manifest: dict
    installed_path: Path
    tool_names: list[str] = field(default_factory=list)

    def stem(self) -> str:
        if self.type in _LUA_TYPES:
            return f"disrepo__{self.type}__{self.name}"
        return self.name


# ── ref parsing ──────────────────────────────────────────────────────────────

def parse_ref(ref: str) -> DisrepoRef:
    """Parse ``tools/toolname`` or ``tool/toolname`` into a DisrepoRef.

    Accepts both the plural folder name (``tools``) and the singular type key
    (``tool``) so ``,ai install tools/sample_price_check`` and
    ``,ai install tool/sample_price_check`` both work.
    """
    if not ref or "/" not in ref:
        raise DisrepoError(
            f"invalid ref {ref!r}: expected '<type>/<name>' "
            f"e.g. 'tools/sample_price_check'"
        )
    head, _, tail = ref.partition("/")
    head = head.strip().lower()
    tail = tail.strip()
    # Allow either folder form ("tools") or singular type ("tool").
    if head.endswith("s") and head[:-1] in _VALID_TYPES:
        type_key = head[:-1]
    elif head in _VALID_TYPES:
        type_key = head
    else:
        raise DisrepoError(
            f"invalid ref type {head!r}; valid: {', '.join(_VALID_TYPES)} "
            f"(or their plural forms)"
        )
    if not _NAME_RE.match(tail):
        raise DisrepoError(
            f"invalid ref name {tail!r}: letters/digits/_-. only, max 64 chars"
        )
    return DisrepoRef(type=type_key, name=tail)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _raw_url(*parts: str) -> str:
    clean = [p.strip("/") for p in parts if p]
    return "/".join([DISREPO_RAW_BASE, *clean])


def _fetch_bytes(url: str, *, max_bytes: int) -> bytes:
    log.info("[disrepo] GET %s", url)
    headers = {"User-Agent": "discoin-disrepo/1.0"}
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and not token:
            raise DisrepoError(
                f"http 404 for {url} -- if disrepo is private, "
                f"set GITHUB_TOKEN in your .env"
            ) from exc
        raise DisrepoError(f"http {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise DisrepoError(f"network error for {url}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DisrepoError(f"fetch failed for {url}: {exc}") from exc
    if len(data) > max_bytes:
        raise DisrepoError(f"response exceeds {max_bytes} bytes: {url}")
    return data


def _fetch_text(url: str, *, max_bytes: int) -> str:
    return _fetch_bytes(url, max_bytes=max_bytes).decode("utf-8", errors="replace")


def _fetch_json(url: str, *, max_bytes: int) -> Any:
    text = _fetch_text(url, max_bytes=max_bytes)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DisrepoError(f"invalid JSON from {url}: {exc}") from exc


# ── registry & search ───────────────────────────────────────────────────────

def fetch_registry() -> dict:
    """Download the top-level registry.json from disrepo."""
    data = _fetch_json(_raw_url("registry.json"), max_bytes=_MAX_MANIFEST_BYTES * 4)
    if not isinstance(data, dict):
        raise DisrepoError("registry.json root must be an object")
    return data


def search_disrepo(query: str = "") -> list[dict]:
    """Return all registry entries whose name/summary/tags match ``query``.

    An empty query returns every entry. Each result row has:
        {type, name, summary, version, author, tags, installed, enabled}
    The installed/enabled fields are resolved locally via registry_state so
    the UI can render the right icon without a second round-trip.
    """
    reg = fetch_registry()
    q = (query or "").strip().lower()
    out: list[dict] = []
    installed_rows = {
        (row["type"], row["name"]): row for row in registry_state.installed_items()
    }
    for type_key in _VALID_TYPES:
        plural = _TYPE_DIR[type_key]
        for entry in reg.get(plural, []) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            summary = str(entry.get("summary") or "")
            tags = [str(t) for t in (entry.get("tags") or []) if t]
            haystack = " ".join([name, summary, " ".join(tags)]).lower()
            if q and q not in haystack:
                continue
            row = installed_rows.get((type_key, name))
            out.append({
                "type":      type_key,
                "name":      name,
                "summary":   summary,
                "version":   str(entry.get("version") or ""),
                "author":    str(entry.get("author") or ""),
                "tags":      tags,
                "installed": bool(row is not None),
                "enabled":   bool(row["enabled"]) if row else False,
            })
    out.sort(key=lambda r: (r["type"], r["name"]))
    return out


# ── validation ───────────────────────────────────────────────────────────────

def _validate_manifest(ref: DisrepoRef, manifest: Any) -> dict:
    if not isinstance(manifest, dict):
        raise DisrepoError(f"{ref.slug()}: manifest.json must be an object")
    m_type = str(manifest.get("type") or "").strip().lower()
    if m_type != ref.type:
        raise DisrepoError(
            f"{ref.slug()}: manifest type {m_type!r} does not match ref type {ref.type!r}"
        )
    m_name = str(manifest.get("name") or "").strip()
    if m_name != ref.name:
        raise DisrepoError(
            f"{ref.slug()}: manifest name {m_name!r} does not match ref name {ref.name!r}"
        )
    entry = str(manifest.get("entry") or "").strip()
    if not entry or "/" in entry or ".." in entry or entry.startswith("."):
        raise DisrepoError(f"{ref.slug()}: invalid manifest.entry {entry!r}")
    if ref.type in _LUA_TYPES and not entry.endswith(".lua"):
        raise DisrepoError(
            f"{ref.slug()}: {ref.type} entries must be .lua files, got {entry!r}"
        )
    if ref.type == "agent" and not entry.endswith(".json"):
        raise DisrepoError(
            f"{ref.slug()}: agent entries must be .json files, got {entry!r}"
        )
    return manifest


def _scan_lua_payload(ref: DisrepoRef, source: str) -> None:
    lower = source.lower()
    hits = [tok for tok in _FORBIDDEN_LUA_SUBSTRINGS if tok in lower]
    if hits:
        raise DisrepoError(
            f"{ref.slug()}: entry rejected -- contains forbidden symbols: "
            f"{', '.join(hits)}"
        )


# ── install / uninstall ──────────────────────────────────────────────────────

def _installed_path(ref: DisrepoRef) -> Path:
    if ref.type in _LUA_TYPES:
        return _PLUGINS_DIR / f"disrepo__{ref.type}__{ref.name}.lua"
    return _AGENTS_DIR / f"{ref.name}.json"


def install_item(ref_str: str) -> InstalledItem:
    """Fetch + validate + write a disrepo item onto the local filesystem.

    The file is placed where the existing loaders can find it:
      - tool/plugin/hook -> plugins/disrepo__<type>__<name>.lua
      - agent            -> data/disrepo_agents/<name>.json
    A registry_state row is written with ``default_enabled=False`` so every
    install starts disabled per the user-facing contract.
    """
    ref = parse_ref(ref_str)
    folder = _TYPE_DIR[ref.type]

    manifest_raw = _fetch_json(
        _raw_url(folder, ref.name, "manifest.json"),
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest = _validate_manifest(ref, manifest_raw)
    entry_rel = str(manifest["entry"])

    target = _installed_path(ref)
    target.parent.mkdir(parents=True, exist_ok=True)

    if ref.type in _LUA_TYPES:
        source = _fetch_text(
            _raw_url(folder, ref.name, entry_rel),
            max_bytes=_MAX_ENTRY_BYTES,
        )
        _scan_lua_payload(ref, source)
        header = (
            f"-- disrepo: {ref.type}/{ref.name} v{manifest.get('version', '?')}\n"
            f"-- source: {DISREPO_OWNER}/{DISREPO_REPO}@{DISREPO_BRANCH}/"
            f"{folder}/{ref.name}/{entry_rel}\n"
            f"-- installed-disabled by default; enable with "
            f",ai {folder} enable {ref.name}\n\n"
        )
        target.write_text(header + source, encoding="utf-8")
    else:
        # agent: fetch the persona bundle and store it alongside the manifest.
        bundle = _fetch_json(
            _raw_url(folder, ref.name, entry_rel),
            max_bytes=_MAX_ENTRY_BYTES,
        )
        if not isinstance(bundle, dict):
            raise DisrepoError(f"{ref.slug()}: agent entry must be a JSON object")
        envelope = {
            "manifest":  manifest,
            "bundle":    bundle,
            "source":    f"{DISREPO_OWNER}/{DISREPO_REPO}@{DISREPO_BRANCH}",
        }
        target.write_text(
            json.dumps(envelope, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

    meta = {
        "version": str(manifest.get("version") or ""),
        "summary": str(manifest.get("summary") or ""),
        "author":  str(manifest.get("author") or ""),
        "entry":   entry_rel,
        "path":    str(target),
    }
    tool_names_raw = manifest.get("tool_names") or []
    tool_names = [str(t) for t in tool_names_raw if isinstance(t, str)]
    if tool_names:
        meta["tool_names"] = tool_names

    if tool_names and ref.type == "tool":
        # For tools with explicit tool_names, register ONLY the dot-notation
        # names as canonical IDs. Skipping the bundle-level underscore row
        # prevents the duplicate split where operators see both
        # ``tool/sample_price_check`` and ``tool/sample.price_check``.
        for tname in tool_names:
            registry_state.mark_installed(
                "tool", tname,
                meta={**meta, "disrepo_bundle": ref.name},
                default_enabled=False,
            )
    else:
        # Agents, plugins, hooks: register under directory name as before.
        registry_state.mark_installed(
            ref.type, ref.name, meta=meta, default_enabled=False,
        )
        # Also seed per-tool-name rows so operators can toggle individual tools.
        for tname in tool_names:
            registry_state.mark_installed(
                "tool", tname,
                meta={"owner": ref.slug(), "source_path": str(target)},
                default_enabled=False,
            )

    return InstalledItem(
        type=ref.type,
        name=ref.name,
        manifest=manifest,
        installed_path=target,
        tool_names=tool_names,
    )


def uninstall_item(ref_str: str) -> DisrepoRef:
    """Remove an installed item from disk and mark it uninstalled.

    The underlying tool stays in ToolRegistry until the bot restarts, but the
    ``is_enabled`` gate means it will not fire -- and a subsequent
    ``,ai tools enable`` would silently succeed on a removed tool, so we
    purge the registry_state entry completely via ``forget``.
    """
    ref = parse_ref(ref_str)
    target = _installed_path(ref)

    # Pull the recorded tool_names before we drop the state entry so we can
    # also forget the per-tool rows.
    tool_names: list[str] = []
    for row in registry_state.all_entries(ref.type):
        if row["name"] == ref.name:
            meta = row.get("meta") or {}
            raw = meta.get("tool_names") or []
            if isinstance(raw, list):
                tool_names = [str(x) for x in raw if isinstance(x, str)]
            break
    if not tool_names:
        # New-style installs: tool_name rows carry disrepo_bundle in meta rather
        # than a parent bundle row. Scan all tool rows to find the children.
        for row in registry_state.all_entries("tool"):
            if (row.get("meta") or {}).get("disrepo_bundle") == ref.name:
                tool_names.append(row["name"])

    if target.exists():
        try:
            target.unlink()
            log.info("[disrepo] removed %s", target)
        except OSError as exc:
            raise DisrepoError(f"could not delete {target}: {exc}") from exc

    registry_state.mark_uninstalled(ref.type, ref.name)
    registry_state.forget(ref.type, ref.name)
    for tname in tool_names:
        registry_state.mark_uninstalled("tool", tname)
        registry_state.forget("tool", tname)

    return ref
