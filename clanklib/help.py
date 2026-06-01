"""clanklib/help.py -- the dynamic help model behind the modern .help hub.

Help is presented as ONE bot. Commands are grouped into sections; the help UI
offers a Components V2 multi-select so a user can pick one or more sections and
their commands combine into a single seamless list. The command lines are
generated from the bot's *live* command tree (not hand-maintained text), so the
help can never drift from what the bot actually exposes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Section:
    key: str            # stable id used by the select
    label: str          # shown in the select + as the group heading
    emoji: str          # select option emoji
    blurb: str          # one-line description
    commands: tuple[str, ...]  # top-level command names that belong here


# The feature map. ``commands`` are top-level command/group names; subcommands
# are discovered live from each group. Keep keys stable (used as select values).
SECTIONS: tuple[Section, ...] = (
    Section("backups", "Backups", "\U0001F4E6",
            "Full server snapshots you can restore or schedule.",
            ("backup",)),
    Section("templates", "Templates", "\U0001F4D0",
            "Shareable, structure-only server blueprints.",
            ("template",)),
    Section("chatlog", "Chatlog", "\U0001F4DC",
            "Archive a channel's messages and replay them.",
            ("chatlog",)),
    Section("sync", "Sync", "\U0001F501",
            "Mirror messages and propagate bans between servers.",
            ("sync",)),
    Section("importexport", "Import / Export", "\U0001F4BE",
            "Move backups in and out as portable files.",
            ("export", "import")),
    Section("settings", "Settings", "\U00002699️",
            "Per-server configuration.",
            ("settings", "set")),
)

SECTIONS_BY_KEY: dict[str, Section] = {s.key: s for s in SECTIONS}


def _walk_subcommands(cmd: Any) -> list[Any]:
    """Return a command's direct subcommands (for a group), else []."""
    subs = getattr(cmd, "commands", None)
    if not subs:
        return []
    # de-dupe (aliases share the object) and sort by name
    seen: dict[str, Any] = {}
    for c in subs:
        seen[c.name] = c
    return [seen[n] for n in sorted(seen)]


def command_lines(bot: Any, section: Section, prefix: str) -> list[str]:
    """Render the command lines for one section from the live command tree.

    A group lists its subcommands (``backup create``, ``backup load`` ...); a
    plain command lists itself. Hidden commands are skipped. Short help text is
    appended when present."""
    lines: list[str] = []
    for name in section.commands:
        cmd = bot.get_command(name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        subs = _walk_subcommands(cmd)
        if subs:
            for sub in subs:
                if getattr(sub, "hidden", False):
                    continue
                short = (sub.short_doc or "").strip()
                line = f"`{prefix}{cmd.name} {sub.name}`"
                lines.append(line + (f" -- {short}" if short else ""))
        else:
            short = (cmd.short_doc or "").strip()
            line = f"`{prefix}{cmd.name}`"
            lines.append(line + (f" -- {short}" if short else ""))
    return lines


def selected_sections(values: Iterable[str]) -> list[Section]:
    """Resolve select values to Sections, preserving SECTIONS order."""
    chosen = set(values)
    return [s for s in SECTIONS if s.key in chosen]
