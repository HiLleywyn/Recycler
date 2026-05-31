"""
services/ai_agents.py  -  Disco's tool context system.

Tools are defined in tools.json at the project root. Each tool is a named
expertise module with keyword triggers and a prompt fragment. When a player
asks something relevant, the matching tool's context is injected into Disco's
system prompt so she has deeper knowledge on that topic.

Adding a new tool:
  1. Add an entry to tools.json (key, name, triggers, context, emoji, button_label, button_command).
  2. Done  -  detection and injection happen automatically.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import NamedTuple

import discord

_TOOLS_FILE = Path(__file__).parent.parent / "tools.json"


class ToolDef(NamedTuple):
    key: str               # internal key
    name: str              # display name
    triggers: list[str]    # lowercase substrings that activate this tool
    context: str           # prompt fragment injected when tool is triggered
    emoji: str             # button emoji (e.g. "⛏️")
    button_label: str      # short label shown on the Discord button
    button_command: str    # command hinted when the button is clicked (e.g. "mine status")


# Pre-compiled patterns per tool key  -  rebuilt on reload.
_TOOL_PATTERNS: dict[str, list[re.Pattern]] = {}


def _compile_patterns(tools: list[ToolDef]) -> dict[str, list[re.Pattern]]:
    out: dict[str, list[re.Pattern]] = {}
    for tool in tools:
        out[tool.key] = [
            re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])")
            for t in tool.triggers
        ]
    return out


def _load_tools() -> list[ToolDef]:
    """Load tool definitions from tools.json."""
    try:
        raw = json.loads(_TOOLS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logging.warning("[ai_tools] tools.json not found at %s", _TOOLS_FILE)
        return []
    except Exception:
        logging.exception("[ai_tools] Failed to parse tools.json")
        return []

    tools = []
    for entry in raw:
        key = str(entry.get("key", "")).strip().lower()
        if not key:
            continue
        tools.append(ToolDef(
            key=key,
            name=str(entry.get("name", key.title())),
            triggers=[str(t).lower() for t in entry.get("triggers", [])],
            context=str(entry.get("context", "")),
            emoji=str(entry.get("emoji", "")),
            button_label=str(entry.get("button_label", "")),
            button_command=str(entry.get("button_command", "")),
        ))
    return tools


TOOLS: list[ToolDef] = _load_tools()
_TOOL_PATTERNS = _compile_patterns(TOOLS)


def reload_tools() -> None:
    """Hot-reload tools.json without restarting."""
    global TOOLS, _TOOL_PATTERNS
    TOOLS = _load_tools()
    _TOOL_PATTERNS = _compile_patterns(TOOLS)
    logging.info("[ai_tools] Reloaded %d tools: %s", len(TOOLS), [t.key for t in TOOLS])


def detect_tools(content: str) -> list[ToolDef]:
    """
    Return tools whose triggers match the message content.
    Uses pre-compiled patterns  -  word-boundary aware so 'stake' won't match 'mistake'.
    """
    lower = content.lower()
    matched = []
    for tool in TOOLS:
        for pat in _TOOL_PATTERNS.get(tool.key, []):
            if pat.search(lower):
                matched.append(tool)
                break
    return matched


def build_tool_context(tools: list[ToolDef]) -> str:
    """Format matched tool contexts for injection into the system prompt."""
    if not tools:
        return ""
    sections = [tool.context for tool in tools if tool.context]
    return "\n\n".join(sections) if sections else ""


def get_tool_keys() -> list[str]:
    """Return all currently loaded tool keys."""
    return [t.key for t in TOOLS]


def get_tool_summary() -> str:
    """Return a compact summary of all tools for self-reflection prompts."""
    if not TOOLS:
        return "(no tools loaded)"
    lines = []
    for t in TOOLS:
        triggers_preview = ", ".join(t.triggers[:5])
        if len(t.triggers) > 5:
            triggers_preview += f", +{len(t.triggers) - 5} more"
        lines.append(f"- {t.key} ({t.name}): triggers on [{triggers_preview}]")
    return "\n".join(lines)


async def generate_tool_suggestions(user_msg: str, ai_reply: str, ai_complete_fn) -> str | None:
    """Ask the AI to suggest new tool triggers or new tools based on a conversation.

    Returns a plain-text suggestion string, or None if there's nothing worth suggesting.
    This is low-priority and should only be called occasionally (e.g. 1 in 20 messages).
    """
    tool_summary = get_tool_summary()
    prompt = (
        f"Existing tool modules (key: triggers):\n{tool_summary}\n\n"
        f"Recent conversation:\nUser: {user_msg[:300]}\nBot: {ai_reply[:300]}\n\n"
        "Based on this conversation, suggest ONE of the following if applicable:\n"
        "1. A new trigger word to add to an existing tool (format: ADD_TRIGGER <key> <trigger>)\n"
        "2. A completely new tool module (format: NEW_TOOL <key> | <name> | <trigger1,trigger2> | <one-line context>)\n"
        "If nothing is missing, reply with: NONE\n"
        "Reply with ONLY the formatted suggestion or NONE. No explanation."
    )
    try:
        result = await ai_complete_fn([{"role": "user", "content": prompt}], max_tokens=80)
        if result:
            result = result.strip()
            if result and result.upper() != "NONE":
                return result
    except Exception:
        logging.warning("[ai_tools] Tool suggestion generation failed")
    return None


def apply_tool_suggestion(suggestion: str, log_fn=None) -> bool:
    """Parse and apply a tool suggestion to tools.json.

    Supports two formats from generate_tool_suggestions():
      ADD_TRIGGER <key> <trigger>
      NEW_TOOL <key> | <name> | <trigger1,trigger2,...> | <context>

    Returns True if tools.json was modified and reloaded.
    """
    suggestion = suggestion.strip()
    try:
        raw = json.loads(_TOOLS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("[ai_tools] Failed to read tools.json for suggestion apply")
        return False

    modified = False

    if suggestion.upper().startswith("ADD_TRIGGER"):
        parts = suggestion.split(None, 2)
        if len(parts) == 3:
            _, key, trigger = parts
            key = key.lower().strip()
            trigger = trigger.lower().strip()
            for entry in raw:
                if entry.get("key", "").lower() == key:
                    if trigger not in entry.get("triggers", []):
                        entry.setdefault("triggers", []).append(trigger)
                        modified = True
                        logging.info("[ai_tools] Added trigger %r to tool %r", trigger, key)
                        if log_fn:
                            log_fn("ADD_TRIGGER", key, trigger)
                    break

    elif suggestion.upper().startswith("NEW_TOOL"):
        rest = suggestion[len("NEW_TOOL"):].strip()
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) >= 4:
            key, name, triggers_raw, context = parts[0], parts[1], parts[2], parts[3]
            key = re.sub(r"[^a-z0-9_]", "_", key.lower().strip())
            triggers = [t.strip().lower() for t in triggers_raw.split(",") if t.strip()]
            existing_keys = {e.get("key", "").lower() for e in raw}
            if key and key not in existing_keys and triggers:
                raw.append({
                    "key": key,
                    "name": name,
                    "emoji": "",
                    "button_label": "",
                    "button_command": "",
                    "triggers": triggers,
                    "context": context,
                })
                modified = True
                logging.info("[ai_tools] Added new tool %r with triggers %s", key, triggers)
                if log_fn:
                    log_fn("NEW_TOOL", key, triggers)

    if modified:
        try:
            _TOOLS_FILE.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            reload_tools()
            return True
        except Exception:
            logging.exception("[ai_tools] Failed to write tools.json after suggestion")
    return False


def build_tools_view(tools: list[ToolDef], prefix: str) -> "discord.ui.View | None":
    """Build a Discord UI View with one button per matched tool (max 3).

    Each button sends an ephemeral message hinting the relevant command.
    Returns None if no tools have a button_command set.
    """
    buttons = [t for t in tools if t.button_command][:3]
    if not buttons:
        return None

    view = discord.ui.View(timeout=120)
    for tool in buttons:
        label = f"{tool.emoji} {tool.button_label}".strip() if tool.emoji else tool.button_label

        def _make_cb(t=tool, p=prefix):
            async def callback(interaction: discord.Interaction) -> None:
                if t.key == "heal":
                    member = interaction.user
                    has_perm = (
                        isinstance(member, discord.Member)
                        and member.guild_permissions.manage_guild
                    )
                    if not has_perm:
                        await interaction.response.send_message(
                            "You need **Manage Server** permission to run heal commands.",
                            ephemeral=True,
                        )
                        return
                await interaction.response.send_message(
                    f"-> `{p}{t.button_command}`",
                    ephemeral=True,
                )
            return callback

        btn = discord.ui.Button(label=label or tool.name, style=discord.ButtonStyle.secondary)
        btn.callback = _make_cb()
        view.add_item(btn)
    return view


def build_tools_footer(tools: list[ToolDef]) -> str:
    """Return the subtext footer string for matched tools, or empty string."""
    if not tools:
        return ""
    return "\n-# " + " · ".join(t.name for t in tools)
