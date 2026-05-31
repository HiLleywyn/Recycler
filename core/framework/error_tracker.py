"""
core/framework/error_tracker.py  -  Unified error tracker for all bot subsystems.

Captures, categorises, and stores errors from:
  - Command execution (cmds)
  - Command chains (cmdchains)
  - Bot events / internal errors (bot)
  - Cog / module load/runtime errors (module)

Each error is stored as an ErrorRecord with metadata (timestamp, source,
user, guild, traceback).  The tracker maintains a rolling in-memory buffer
per guild with search, filtering, and summary capabilities.

Usage:
    from core.framework.error_tracker import ErrorTracker, ErrorSource

    tracker = ErrorTracker()
    tracker.record(ErrorSource.CMD, "Division by zero", guild_id=123,
                   user_id=456, command="trade buy", traceback_str="...")
    recent = tracker.recent(guild_id=123, source=ErrorSource.CMD, limit=5)
    summary = tracker.summary(guild_id=123)
"""
from __future__ import annotations

import enum
import time
from collections import defaultdict
from dataclasses import dataclass, field


class ErrorSource(str, enum.Enum):
    """Where the error originated."""

    CMD       = "cmd"        # prefix/slash command execution
    CMDCHAIN  = "cmdchain"   # command chain step failure
    BOT       = "bot"        # internal bot event / listener error
    MODULE    = "module"     # cog load / unload / runtime error
    SERVICE   = "service"    # service layer (trade, swap, stake, etc.)
    TASK      = "task"       # background task / loop error


# ── Severity levels ──────────────────────────────────────────────────────

class Severity(str, enum.Enum):
    INFO     = "info"      # informational: missing args, bad syntax, tips
    WARNING  = "warning"   # non-errors: cooldowns, check failures, soft limits
    LOW      = "low"       # minor errors: user input edge cases
    MEDIUM   = "medium"    # command invoke errors, service failures
    HIGH     = "high"      # unhandled exceptions, module crashes
    CRITICAL = "critical"  # bot-level failures, DB errors


@dataclass
class ErrorRecord:
    """A single recorded error event."""

    source: ErrorSource
    message: str
    severity: Severity = Severity.MEDIUM
    guild_id: int = 0
    user_id: int = 0
    command: str = ""
    module: str = ""          # cog/module name
    error_type: str = ""      # exception class name
    traceback_str: str = ""   # formatted traceback (truncated)
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    context: dict = field(default_factory=dict)  # arbitrary extra data

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def age_str(self) -> str:
        age = self.age_seconds
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age / 60)}m ago"
        if age < 86400:
            return f"{int(age / 3600)}h ago"
        return f"{int(age / 86400)}d ago"

    @property
    def short_message(self) -> str:
        return self.message[:120] if len(self.message) > 120 else self.message


# ── Max buffer size per guild ────────────────────────────────────────────

MAX_PER_GUILD = 500
MAX_GLOBAL = 200  # for guild_id=0 (DM / bot-level errors)


class ErrorTracker:
    """In-memory rolling error buffer with search and summary."""

    def __init__(self) -> None:
        self._errors: dict[int, list[ErrorRecord]] = defaultdict(list)

    # ── Recording ────────────────────────────────────────────────────────

    def record(
        self,
        source: ErrorSource,
        message: str,
        *,
        severity: Severity = Severity.MEDIUM,
        guild_id: int = 0,
        user_id: int = 0,
        command: str = "",
        module: str = "",
        error_type: str = "",
        traceback_str: str = "",
        context: dict | None = None,
    ) -> ErrorRecord:
        """Record an error and return the created record."""
        entry = ErrorRecord(
            source=source,
            message=message,
            severity=severity,
            guild_id=guild_id,
            user_id=user_id,
            command=command,
            module=module,
            error_type=error_type,
            traceback_str=traceback_str[:2000] if traceback_str else "",
            context=context or {},
        )

        buf = self._errors[guild_id]
        buf.append(entry)

        # Trim to max size
        cap = MAX_GLOBAL if guild_id == 0 else MAX_PER_GUILD
        if len(buf) > cap:
            self._errors[guild_id] = buf[-cap:]

        return entry

    # ── Querying ─────────────────────────────────────────────────────────

    def recent(
        self,
        guild_id: int,
        *,
        source: ErrorSource | None = None,
        module: str = "",
        command: str = "",
        user_id: int = 0,
        keyword: str = "",
        severity: Severity | None = None,
        limit: int = 10,
    ) -> list[ErrorRecord]:
        """Return the most recent errors matching the filters."""
        buf = self._errors.get(guild_id, [])
        results: list[ErrorRecord] = []

        for entry in reversed(buf):
            if source and entry.source != source:
                continue
            if module and entry.module.lower() != module.lower():
                continue
            if command and entry.command.lower() != command.lower():
                continue
            if user_id and entry.user_id != user_id:
                continue
            if severity and entry.severity != severity:
                continue
            if keyword:
                kw = keyword.lower()
                if (kw not in entry.message.lower()
                        and kw not in entry.error_type.lower()
                        and kw not in entry.command.lower()
                        and kw not in entry.module.lower()):
                    continue
            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def summary(self, guild_id: int) -> dict[str, dict[str, int]]:
        """Return error counts grouped by source and severity.

        Returns::

            {
                "cmd":      {"low": 3, "medium": 5, "high": 1},
                "cmdchain": {"medium": 2},
                "bot":      {"high": 1},
                ...
                "_total":   {"low": 3, "medium": 7, "high": 2, "critical": 0},
            }
        """
        buf = self._errors.get(guild_id, [])
        out: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {s.value: 0 for s in Severity}

        for entry in buf:
            src = entry.source.value
            if src not in out:
                out[src] = {}
            sev = entry.severity.value
            out[src][sev] = out[src].get(sev, 0) + 1
            totals[sev] = totals.get(sev, 0) + 1

        out["_total"] = totals
        return out

    def module_summary(self, guild_id: int) -> dict[str, int]:
        """Return error counts grouped by module/cog name."""
        buf = self._errors.get(guild_id, [])
        counts: dict[str, int] = {}
        for entry in buf:
            name = entry.module or entry.source.value
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))

    def command_summary(self, guild_id: int) -> dict[str, int]:
        """Return error counts grouped by command name."""
        buf = self._errors.get(guild_id, [])
        counts: dict[str, int] = {}
        for entry in buf:
            if entry.command:
                counts[entry.command] = counts.get(entry.command, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))

    def total_count(self, guild_id: int) -> int:
        return len(self._errors.get(guild_id, []))

    def clear(self, guild_id: int) -> int:
        """Clear all errors for a guild. Returns count cleared."""
        count = len(self._errors.get(guild_id, []))
        self._errors.pop(guild_id, None)
        return count
