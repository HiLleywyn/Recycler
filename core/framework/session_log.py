"""Per-run session log  -  overwrites every restart.

Captures: commands, errors, event bus events, validator blocks,
chain blocks, mining, mempool submissions, startup/shutdown.

Usage:
    from core.framework.session_log import slog
    slog.cmd(ctx)
    slog.error(ctx, error)
    slog.event("validator_block", network="Arcadia Network", ...)
    slog.info("Pool seeding complete")
"""
from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path

LOG_PATH = Path("logs/bot_run.log")

_CATS = {
    "STARTUP":  "STARTUP  ",
    "CMD":      "CMD      ",
    "ERR":      "ERR      ",
    "EVENT":    "EVENT    ",
    "VALBLOCK": "VALBLOCK ",
    "CHAIN":    "CHAIN    ",
    "MINING":   "MINING   ",
    "MEMPOOL":  "MEMPOOL  ",
    "DISCORD":  "DISCORD  ",
    "INFO":     "INFO     ",
    "WARN":     "WARN     ",
}


class SessionLog:
    """Append-only structured log for one bot run. Opened fresh (overwrite) on each start."""

    def __init__(self) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(LOG_PATH, "w", encoding="utf-8", buffering=1)  # line-buffered
        self._start_ts = time.time()
        self._write_separator()
        self._raw(f"SESSION STARTED  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        self._write_separator()
        self._f.write("\n")
        self._install_logging_bridge()

    # ── Internal ────────────────────────────────────────────────────────────

    def _ts(self) -> str:
        return time.strftime("%H:%M:%S", time.gmtime())

    def _raw(self, line: str) -> None:
        self._f.write(line + "\n")

    def _write_separator(self) -> None:
        self._f.write("=" * 80 + "\n")

    def _log(self, cat: str, msg: str) -> None:
        label = _CATS.get(cat, cat.upper().ljust(9))
        self._f.write(f"[{self._ts()}] [{label}] {msg}\n")

    def _install_logging_bridge(self) -> None:
        """Forward Python logging (discord.py, aiohttp, etc.) to the session file."""
        handler = _FileLogHandler(self._f)
        handler.setFormatter(logging.Formatter("%(name)s  -  %(message)s"))
        handler.setLevel(logging.WARNING)
        root = logging.getLogger()
        root.addHandler(handler)

    # ── Public API ──────────────────────────────────────────────────────────

    def startup(self, msg: str) -> None:
        self._log("STARTUP", msg)

    def info(self, msg: str) -> None:
        self._log("INFO", msg)

    def warn(self, msg: str) -> None:
        self._log("WARN", msg)

    def cmd(self, ctx) -> None:
        """Log a command invocation."""
        user = getattr(ctx, "author", None)
        guild = getattr(ctx, "guild", None)
        msg = getattr(ctx, "message", None)
        content = getattr(msg, "content", "") or ""
        # Truncate very long messages (e.g. large args)
        if len(content) > 300:
            content = content[:297] + "..."
        user_str = f"{user} ({user.id})" if user else "unknown"
        guild_str = f"{guild.name} ({guild.id})" if guild else "DM"
        self._log("CMD", f"{user_str}  in  {guild_str}  →  {content}")

    def error(self, ctx, error: Exception) -> None:
        """Log a command error with full traceback."""
        user = getattr(ctx, "author", None)
        guild = getattr(ctx, "guild", None)
        invoked = getattr(ctx, "invoked_with", None) or "?"
        msg = getattr(ctx, "message", None)
        content = getattr(msg, "content", "") or ""
        if len(content) > 200:
            content = content[:197] + "..."

        user_str = f"{user} ({user.id})" if user else "unknown"
        guild_str = f"{guild.name} ({guild.id})" if guild else "DM"

        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self._log("ERR", f"cmd={invoked}  user={user_str}  guild={guild_str}")
        self._log("ERR", f"input: {content}")
        self._log("ERR", f"type:  {type(error).__name__}: {str(error)[:400]}")
        if tb.strip():
            # Write traceback indented under the error entry
            for line in tb.splitlines():
                self._f.write(f"           {line}\n")
        self._f.write("\n")

    def event(self, event_name: str, **kwargs) -> None:
        """Log an EventBus publish (brief summary)."""
        # Build a concise one-liner from kwargs
        parts = []
        for k, v in kwargs.items():
            if k in ("guild",):
                parts.append(f"guild={getattr(v, 'name', str(v))}")
            elif k in ("results",):
                confirmed = sum(1 for r in v if r.get("success"))
                rejected = len(v) - confirmed
                parts.append(f"confirmed={confirmed} rejected={rejected}")
            elif hasattr(v, "__len__") and not isinstance(v, str):
                parts.append(f"{k}=[{len(v)}]")
            else:
                sv = str(v)
                if len(sv) > 60:
                    sv = sv[:57] + "..."
                parts.append(f"{k}={sv}")
        self._log("EVENT", f"{event_name}   -   {' | '.join(parts)}")

    def validator_block(
        self,
        guild_name: str,
        network: str,
        validator_id: int,
        total_actions: int,
        confirmed: int,
        rejected: int,
        total_gas: float,
        gas_coin: str,
        results: list[dict] | None = None,
    ) -> None:
        """Log a processed validator block with per-action summary."""
        self._log(
            "VALBLOCK",
            f"guild={guild_name}  net={network}  validator={validator_id}  "
            f"actions={total_actions}  ✅={confirmed}  ❌={rejected}  "
            f"gas={total_gas:.8f} {gas_coin}"
        )
        if results:
            for r in results:
                a = r.get("action", {})
                status = "✅" if r.get("success") else "❌"
                atype = a.get("action_type", "?")
                uid = a.get("user_id", "?")
                reason = r.get("reason", "")
                gas = r.get("gas", 0.0)
                self._f.write(
                    f"           {status} [{atype}] user={uid}  gas={gas:.8f}"
                    + (f"  reason={reason}" if reason else "") + "\n"
                )

    def chain_block(
        self,
        guild_name: str,
        network: str,
        block_num: int,
        tx_count: int,
        oracle_count: int = 0,
        user_count: int = 0,
    ) -> None:
        """Log a bundled chain block."""
        self._log(
            "CHAIN",
            f"guild={guild_name}  net={network}  block=#{block_num}  "
            f"txns={tx_count}  (oracle={oracle_count}  user={user_count})"
        )

    def mining_block(
        self,
        guild_name: str,
        block_height: int,
        miner_id: int | None,
        reward: float,
        total_hashrate: float,
    ) -> None:
        """Log a SUN mining block."""
        self._log(
            "MINING",
            f"guild={guild_name}  height=#{block_height}  "
            f"miner={miner_id or 'pool'}  reward={reward:.4f} SUN  "
            f"hashrate={total_hashrate:,.0f} MH/s"
        )

    def mempool_submit(
        self,
        guild_name: str,
        user_id: int,
        network: str,
        action_type: str,
        mempool_id: int,
        gas_fee: float,
        gas_coin: str,
        extra: str = "",
    ) -> None:
        """Log a mempool submission."""
        self._log(
            "MEMPOOL",
            f"guild={guild_name}  #{mempool_id}  [{action_type}]  "
            f"user={user_id}  net={network}  gas={gas_fee:.8f} {gas_coin}"
            + (f"  {extra}" if extra else "")
        )

    def close(self) -> None:
        elapsed = time.time() - self._start_ts
        self._f.write("\n")
        self._write_separator()
        self._raw(
            f"SESSION ENDED  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
            f"  (uptime {elapsed:.0f}s)"
        )
        self._write_separator()
        self._f.close()


class _FileLogHandler(logging.Handler):
    """Forwards Python logging records to the session log file."""

    def __init__(self, f) -> None:
        super().__init__()
        self._f = f

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = time.strftime("%H:%M:%S", time.gmtime())
            lvl = record.levelname[:4].upper()
            msg = self.format(record)
            self._f.write(f"[{ts}] [DISCORD  ] [{lvl}] {msg}\n")
        except Exception:
            pass


# ── Module-level singleton ───────────────────────────────────────────────────

slog: SessionLog | None = None


def init() -> SessionLog:
    """Initialise the session logger. Call once at bot startup."""
    global slog
    slog = SessionLog()
    return slog


def get() -> SessionLog | None:
    """Return the active session log, or None if not yet initialised."""
    return slog
