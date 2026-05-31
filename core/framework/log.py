from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich import box

# Force UTF-8 output and use ASCII-safe fallback if terminal doesn't support Unicode
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

console = Console(force_terminal=True)

# ── Environment detection ────────────────────────────────────────────────────

RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
SERVICE_NAME = os.getenv("RAILWAY_SERVICE_NAME", os.getenv("SERVICE_NAME", "discoin"))
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if RAILWAY else "rich")

# ── Structured JSON formatter for Railway ────────────────────────────────────

class StructuredJsonFormatter(logging.Formatter):
    """Emit one JSON object per line  -  optimised for Railway's log viewer.

    Railway aggregates stdout lines and renders JSON fields as searchable
    columns in its dashboard, so structured logs are *much* more useful
    than plain text.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach extra structured fields (e.g. user_id, guild_id, latency_ms)
        for key in ("user_id", "guild_id", "command", "latency_ms",
                     "method", "path", "status_code", "duration_ms",
                     "cog", "event", "error_type", "detail"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val

        if record.exc_info and record.exc_info[1]:
            payload["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }
            payload["traceback"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


# ── Logging setup ─────────────────────────────────────────────────────────────

_setup_done = False

def setup_logging() -> None:
    """Configure logging. Call once at startup.

    On Railway (LOG_FORMAT=json) emits structured JSON to stdout.
    Locally (LOG_FORMAT=rich) uses Rich's pretty handler.
    """
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    root = logging.getLogger()
    root.handlers.clear()

    if LOG_FORMAT == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredJsonFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)

        # Suppress noisy third-party loggers
        for name in ("discord", "discord.http", "discord.gateway",
                      "aiohttp", "asyncpg", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)
        # Keep uvicorn error logger at INFO for startup messages
        logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
        )
        # Suppress noisy discord.py internals
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)


# ── Internal structured logger ───────────────────────────────────────────────

_logger = logging.getLogger("discoin")


def _log(level: int, msg: str, **extra: object) -> None:
    """Log with optional structured fields that show up in JSON output."""
    _logger.log(level, msg, extra=extra)


# ── Themed log helpers ────────────────────────────────────────────────────────
# These are the primary API used throughout the codebase.  On Railway they
# delegate to structured JSON; locally they use Rich formatting.

def info(msg: str, *args: object, **extra: object) -> None:
    if args:
        msg = msg % args
    if LOG_FORMAT == "json":
        _log(logging.INFO, msg, **extra)
    else:
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"[dim]{ts}[/dim]  [cyan]INFO [/cyan]  {msg}")

def ok(msg: str, *args: object, **extra: object) -> None:
    if args:
        msg = msg % args
    if LOG_FORMAT == "json":
        _log(logging.INFO, msg, **extra)
    else:
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"[dim]{ts}[/dim]  [bold green]  OK  [/bold green]  {msg}")

def warn(msg: str, *args: object, **extra: object) -> None:
    if args:
        msg = msg % args
    if LOG_FORMAT == "json":
        _log(logging.WARNING, msg, **extra)
    else:
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"[dim]{ts}[/dim]  [bold yellow] WARN [/bold yellow]  {msg}")

def error(msg: str, *args: object, **extra: object) -> None:
    if args:
        msg = msg % args
    if LOG_FORMAT == "json":
        _log(logging.ERROR, msg, **extra)
    else:
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"[dim]{ts}[/dim]  [bold red]ERROR[/bold red]  {msg}")


# ── Startup banner ─────────────────────────────────────────────────────────────

def redact_dsn(value: str) -> str:
    """Redact credentials from a DSN before logging it."""
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value

    hostinfo = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, f"***:***@{hostinfo}", parsed.path, parsed.query, parsed.fragment))

def print_banner(prefix: str, db_path: str, api_port: int | None) -> None:
    if LOG_FORMAT == "json":
        # Structured startup info for Railway
        _logger.info("Discoin starting", extra={
            "event": "startup",
            "prefix": prefix,
            "database": redact_dsn(db_path),
            "api_port": api_port,
            "service": SERVICE_NAME,
            "railway": RAILWAY,
        })
        return

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column()
    grid.add_row("prefix",   f"[bold white]{prefix}[/bold white]")
    grid.add_row("database", f"[dim]{redact_dsn(db_path)}[/dim]")
    grid.add_row("api",      f"[dim]http://0.0.0.0:{api_port}[/dim]" if api_port else "[dim]disabled[/dim]")

    panel = Panel(
        grid,
        title="[bold magenta]Discoin[/bold magenta]",
        subtitle="[dim]economy bot[/dim]",
        border_style="magenta",
        box=box.ASCII2,
        padding=(0, 1),
    )
    console.print(panel)


# ── Ready summary ──────────────────────────────────────────────────────────────

def print_ready(username: str, user_id: int, guild_count: int, cog_count: int) -> None:
    if LOG_FORMAT == "json":
        _logger.info("Bot ready", extra={
            "event": "ready",
            "username": username,
            "user_id": user_id,
            "guild_count": guild_count,
            "cog_count": cog_count,
        })
        return

    t = Table(box=box.ASCII2, show_header=False, padding=(0, 1))
    t.add_column(style="bold cyan", justify="right")
    t.add_column()
    t.add_row("logged in as", f"[bold white]{username}[/bold white] [dim]({user_id})[/dim]")
    t.add_row("guilds",       f"[green]{guild_count}[/green]")
    t.add_row("cogs loaded",  f"[green]{cog_count}[/green]")
    console.print(t)
