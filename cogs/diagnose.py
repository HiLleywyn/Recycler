"""cogs/diagnose.py  -  Comprehensive system diagnostic for Discoin.

Provides `.admin diagnose [target]` to verify all subsystems are operational,
and runs a startup self-test on bot ready.

Targets:
  all        -  run every check (default)
  db         -  PostgreSQL connectivity, pool, schema, repos
  cogs       -  all cogs loaded, background tasks running
  api        -  FastAPI server, health endpoint, routes
  modules    -  guild module toggles vs. required data
  services   -  Redis, OpenRouter, external dependencies
  commands   -  verify all registered commands have valid checks
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable
import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin, COGS
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import C_AMBER, C_ERROR, C_INFO, C_NEUTRAL, C_SUCCESS, C_WARNING
from core.framework.ai.diagnose_ai import (
    classify_query, enforce_limit, ask_investigator, get_diagnose_ai_config,
)

log = logging.getLogger(__name__)

# ── Result helpers ──────────────────────────────────────────────────────────

_OK = "✅"
_WARN = "⚠️"
_FAIL = "❌"


class DiagResult:
    """Collects pass/warn/fail results for a diagnostic section."""

    __slots__ = ("name", "checks")

    def __init__(self, name: str) -> None:
        self.name = name
        self.checks: list[tuple[str, str, str]] = []  # (icon, label, detail)

    def ok(self, label: str, detail: str = "") -> None:
        self.checks.append((_OK, label, detail))

    def warn(self, label: str, detail: str = "") -> None:
        self.checks.append((_WARN, label, detail))

    def fail(self, label: str, detail: str = "") -> None:
        self.checks.append((_FAIL, label, detail))

    @property
    def worst(self) -> str:
        icons = {c[0] for c in self.checks}
        if _FAIL in icons:
            return _FAIL
        if _WARN in icons:
            return _WARN
        return _OK

    def render(self) -> str:
        lines = []
        for icon, label, detail in self.checks:
            line = f"{icon} **{label}**"
            if detail:
                line += f"  -  {detail}"
            lines.append(line)
        return "\n".join(lines) or "No checks ran."


# ── Diagnostic checks ──────────────────────────────────────────────────────

async def _check_db(bot: Discoin) -> DiagResult:
    """Check PostgreSQL connectivity, pool health, schema, and repos."""
    r = DiagResult("Database")

    # Pool exists
    pool = getattr(bot.db, "_pool", None)
    if pool is None:
        r.fail("Connection pool", "not initialized")
        return r

    # Connectivity
    try:
        val = await bot.db.fetch_val("SELECT 1")
        if val == 1:
            r.ok("PostgreSQL connectivity")
        else:
            r.fail("PostgreSQL connectivity", f"unexpected result: {val}")
    except Exception as exc:
        r.fail("PostgreSQL connectivity", str(exc)[:120])
        return r

    # Pool stats
    r.ok("Pool size", f"min={pool.get_min_size()}, max={pool.get_max_size()}, "
         f"free={pool.get_idle_size()}, used={pool.get_size() - pool.get_idle_size()}")

    # Schema  -  check critical tables exist
    critical_tables = [
        "users", "guild_settings", "crypto_prices", "crypto_holdings",
        "transactions", "pools", "validators", "mining_rigs",
        "nft_collections", "nfts", "nft_collection_images",
        "governance_proposals", "governance_votes",
        "economy_snapshots",
        "guild_tokens", "mining_groups",
    ]
    try:
        rows = await bot.db.fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        existing = {row["table_name"] for row in rows}
        missing = [t for t in critical_tables if t not in existing]
        if missing:
            r.fail("Schema tables", f"missing: {', '.join(missing)}")
        else:
            r.ok("Schema tables", f"{len(existing)} tables present")
    except Exception as exc:
        r.fail("Schema check", str(exc)[:120])

    # Repos initialized
    repo_names = [
        "users", "transactions", "markets", "pools",
        "validators", "mining", "contracts", "guilds", "reports",
    ]
    missing_repos = [n for n in repo_names if getattr(bot.db, n, None) is None]
    if missing_repos:
        r.fail("Repositories", f"not initialized: {', '.join(missing_repos)}")
    else:
        r.ok("Repositories", f"all {len(repo_names)} repos initialized")

    return r


async def _check_cogs(bot: Discoin) -> DiagResult:
    """Check all cogs are loaded and background tasks are running."""
    r = DiagResult("Cogs & Tasks")

    # Loaded cogs
    loaded = set(bot.cogs.keys())
    expected_cog_modules = COGS
    missing_modules = []
    for mod_path in expected_cog_modules:
        # Extract cog name from module path (e.g. "cogs.bank" -> check if loaded)
        if mod_path not in bot.extensions:
            missing_modules.append(mod_path)

    if missing_modules:
        r.fail("Cog loading", f"failed to load: {', '.join(missing_modules)}")
    else:
        r.ok("Cog loading", f"all {len(expected_cog_modules)} cogs loaded")

    # Background tasks
    now = discord.utils.utcnow()
    tasks_status = []
    for cog_name, cog in bot.cogs.items():
        for attr_name in dir(cog):
            attr = getattr(cog, attr_name, None)
            if isinstance(attr, tasks.Loop):
                running = attr.is_running() if callable(attr.is_running) else attr.is_running
                failed = attr.failed() if callable(attr.failed) else attr.failed
                label = f"{cog_name}.{attr_name}"

                if failed:
                    tasks_status.append((_FAIL, label, "FAILED"))
                elif not running:
                    # One-shot tasks (count=1) are expected to stop after completion.
                    is_one_shot = getattr(attr, 'count', None) == 1
                    # Loops with _heal_skip are intentionally excluded from auto-restart
                    # (e.g. _test_heal_loop) - not running is normal, not a failure.
                    heal_skip = getattr(attr, '_heal_skip', False)
                    if is_one_shot:
                        tasks_status.append((_OK, label, "completed (one-shot)"))
                    elif heal_skip:
                        tasks_status.append((_OK, label, "idle (heal-skipped)"))
                    else:
                        tasks_status.append((_FAIL, label, "not running"))
                else:
                    # Check if the task is overdue based on its interval
                    next_iter = attr.next_iteration
                    # Compute expected interval from the loop's time/seconds/minutes/hours
                    interval_secs = 0.0
                    if hasattr(attr, '_seconds'):
                        interval_secs = attr._seconds
                    elif hasattr(attr, 'seconds'):
                        interval_secs = attr.seconds
                    if hasattr(attr, '_minutes'):
                        interval_secs += attr._minutes * 60
                    elif hasattr(attr, 'minutes'):
                        interval_secs += attr.minutes * 60
                    if hasattr(attr, '_hours'):
                        interval_secs += attr._hours * 3600
                    elif hasattr(attr, 'hours'):
                        interval_secs += attr.hours * 3600

                    if next_iter is not None and interval_secs > 0:
                        # Make next_iter timezone-aware if it isn't
                        if next_iter.tzinfo is None:
                            next_iter = next_iter.replace(tzinfo=datetime.timezone.utc)
                        overdue = (now - next_iter).total_seconds()
                        if overdue > interval_secs * 2:
                            # More than 2x the interval overdue → red
                            tasks_status.append((_FAIL, label, f"overdue by {int(overdue)}s"))
                        elif overdue > interval_secs * 0.5:
                            # More than half the interval overdue → yellow
                            tasks_status.append((_WARN, label, f"delayed ({int(overdue)}s overdue)"))
                        else:
                            tasks_status.append((_OK, label, "running"))
                    else:
                        tasks_status.append((_OK, label, "running"))

    if tasks_status:
        for icon, label, detail in tasks_status:
            if icon == _OK:
                r.ok(label, detail)
            elif icon == _WARN:
                r.warn(label, detail)
            else:
                r.fail(label, detail)
    else:
        r.ok("Background tasks", "none detected")

    return r


async def _check_api(bot: Discoin) -> DiagResult:
    """Check the FastAPI server is running and responding."""
    r = DiagResult("API Server")

    api_port = Config.API_PORT
    if not api_port:
        r.warn("API server", "API_PORT not configured  -  API disabled")
        return r

    server = getattr(bot, "_api_server", None)
    if server is None:
        r.fail("API server", "uvicorn server not started")
        return r

    if server.started:
        r.ok("API server", f"listening on port {api_port}")
    else:
        r.warn("API server", "server object exists but not yet started")

    # Try hitting health endpoint
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{api_port}/health", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                r.ok("Health endpoint", f"status={data.get('status')}, version={data.get('version')}")
            else:
                r.fail("Health endpoint", f"HTTP {resp.status_code}")
    except ImportError:
        # httpx not available, try with aiohttp or urllib
        try:
            import urllib.request
            resp = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{api_port}/health",
                timeout=5,
            )
            if resp.status == 200:
                r.ok("Health endpoint", "responding (200)")
            else:
                r.warn("Health endpoint", f"HTTP {resp.status}")
        except Exception as exc:
            r.warn("Health endpoint", f"could not verify: {exc!s:.80}")
    except Exception as exc:
        r.warn("Health endpoint", f"request failed: {exc!s:.80}")

    return r


async def _check_modules(bot: Discoin, guild_id: int) -> DiagResult:
    """Check guild module configuration and required data."""
    r = DiagResult("Modules")

    settings = await bot.db.get_guild_settings(guild_id)

    # Kept aligned with the live `guild_settings.module_*` columns and any
    # migrations that have introduced new flags. Older versions only knew
    # about the v1.7 modules; the additions below close the gap with the
    # crafting / farming / fishing / etc. systems that have shipped since.
    module_checks = [
        ("module_gambling",     "Gambling"),
        ("module_lending",      "Lending"),
        ("module_staking",      "Staking"),
        ("module_mining",       "Mining"),
        ("module_faucet",       "Faucet"),
        ("module_savings",      "Savings"),
        ("module_validators",   "Validators"),
        ("module_pools",        "Pools"),
        ("module_contracts",    "Contracts"),
        ("module_security",     "Security"),
        ("module_groups",       "Groups"),
        ("module_chart",        "Charts"),
        ("module_crypto",       "Crypto"),
        ("module_daily",        "Daily"),
        ("module_work",         "Work"),
        ("module_economy",      "Economy"),
        ("module_chain",        "Chain"),
        ("module_shop",         "Shop"),
        ("module_games",        "Games"),
        ("module_ape",          "Ape"),
        ("module_nft",          "NFT"),
        ("module_governance",   "Governance"),
        ("module_predictions",  "Predictions"),
        ("module_events",       "Events"),
        ("module_rugpull",      "Rugpull"),
        ("module_crafting",     "Crafting"),
        ("module_farming",      "Farming"),
        ("module_fishing",      "Fishing"),
    ]

    enabled = []
    disabled = []
    for col, name in module_checks:
        # NULL means "enabled by default" for module flags that were added
        # without DEFAULT TRUE (module_crafting/farming/fishing/rugpull and
        # several admin-toggle migrations). `dict.get(col, True)` returns
        # None when the column exists with a NULL value, which is falsy --
        # using `is not False` keeps NULL on the enabled side, matching the
        # canonical `,admin <module>` display logic in cogs/admin.py.
        if settings.get(col) is not False:
            enabled.append(name)
        else:
            disabled.append(name)

    r.ok("Enabled modules", ", ".join(enabled) if enabled else "none")
    if disabled:
        r.warn("Disabled modules", ", ".join(disabled))

    # Data checks for enabled modules. Same `is not False` pattern so a
    # NULL flag is treated as enabled-by-default, matching the canonical
    # admin-toggle display logic.
    def _on(col: str) -> bool:
        return settings.get(col) is not False

    if _on("module_staking") or _on("module_validators"):
        validators = await bot.db.get_validators(guild_id)
        if not validators:
            r.warn("Staking data", "no validators seeded  -  staking won't work")
        else:
            r.ok("Staking data", f"{len(validators)} validator(s)")

    if _on("module_mining"):
        network = await bot.db.get_network(guild_id)
        if not network:
            r.warn("Mining data", "network not initialized (initializes on first mine)")
        else:
            r.ok("Mining data", f"block #{network.get('block_height', 0)}")

    if _on("module_crypto"):
        prices = await bot.db.get_all_prices(guild_id)
        if not prices:
            r.warn("Price data", "no token prices  -  trade or wait for drift tick")
        else:
            r.ok("Price data", f"{len(prices)} token(s)")

    return r


async def _check_services(bot: Discoin) -> DiagResult:
    """Check external service connectivity."""
    r = DiagResult("Services")

    # Discord
    if bot.is_ready():
        r.ok("Discord", f"logged in as {bot.user} in {len(bot.guilds)} guild(s)")
    else:
        r.warn("Discord", "bot not ready yet")

    # Redis
    api_server = getattr(bot, "_api_server", None)
    if api_server and hasattr(api_server, "config") and hasattr(api_server.config, "app"):
        app = api_server.config.app
        redis = getattr(getattr(app, "state", None), "redis", None)
        if redis:
            try:
                pong = await redis.ping()
                r.ok("Redis", "connected" if pong else "ping failed")
            except Exception as exc:
                r.fail("Redis", str(exc)[:80])
        else:
            r.warn("Redis", "not configured (cache/pubsub disabled)")
    else:
        redis_url = getattr(Config, "REDIS_URL", None)
        if redis_url:
            r.warn("Redis", "configured but cannot verify (API not attached)")
        else:
            r.warn("Redis", "not configured")

    # OpenRouter / AI
    key_set = bool(getattr(Config, "OPENROUTER_API_KEY", ""))
    if key_set:
        r.ok("OpenRouter AI", f"key configured, default model={Config.OPENROUTER_MODEL}")
    else:
        r.warn("OpenRouter AI", "API key not set  -  AI features disabled")

    # Uptime
    start = getattr(bot, "_start_time", None)
    if start:
        uptime = time.time() - start
        hours = int(uptime // 3600)
        mins = int((uptime % 3600) // 60)
        r.ok("Uptime", f"{hours}h {mins}m")

    return r


async def _check_commands(bot: Discoin) -> DiagResult:
    """Verify all registered commands have valid structure."""
    r = DiagResult("Commands")

    all_cmds = list(bot.walk_commands())
    prefix_cmds = [c for c in all_cmds if not isinstance(c, commands.Group)]
    groups = [c for c in all_cmds if isinstance(c, commands.Group)]

    r.ok("Prefix commands", f"{len(prefix_cmds)} commands, {len(groups)} groups")

    # Slash commands
    slash_cmds = bot.tree.get_commands()
    r.ok("Slash commands", f"{len(slash_cmds)} registered")

    # Check for top-level commands without a cog (orphaned)
    # Subcommands inherit cog from their parent group; only flag root-level commands.
    orphaned = [c.qualified_name for c in all_cmds if c.cog is None and c.parent is None]
    if orphaned:
        r.warn("Orphaned commands", ", ".join(orphaned[:10]))
    else:
        r.ok("Command ownership", "all commands belong to a cog")

    return r


# ── Integrity checks ────────────────────────────────────────────────────────

async def _check_integrity(bot: Discoin) -> DiagResult:
    """Validate 1.7/1.8 structural consistency: columns, migrations, framework modules, config."""
    r = DiagResult("Integrity (1.7/1.8)")
    import os
    import ast

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ── Schema file presence ────────────────────────────────────────────────
    schema_path = os.path.join(base, "database", "schema.sql")
    if os.path.isfile(schema_path):
        schema_text = open(schema_path).read()
        # Tables expected in schema.sql
        _schema_tables = ("rugpull_king", "rugpull_stats", "nft_collections",
                          "nfts", "nft_collection_images",
                          "governance_proposals", "governance_votes",
                          "economy_snapshots")
        for table in _schema_tables:
            if table in schema_text:
                r.ok(f"schema: {table}", "defined")
            else:
                r.warn(f"schema: {table}", "missing from schema.sql (migration-only)")
    else:
        r.warn("Schema file", "database/schema.sql not found")

    # ── Migration coverage ──────────────────────────────────────────────────
    migrations_dir = os.path.join(base, "database", "migrations")
    if os.path.isdir(migrations_dir):
        migration_files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
        r.ok("Migration files", f"{len(migration_files)} total (latest: {migration_files[-1] if migration_files else 'none'})")
        expected_migrations = {
            "group_token_contracts": "0061",
            "nft_collection_images": "0063",
        }
        for keyword, expected_prefix in expected_migrations.items():
            match = [f for f in migration_files if keyword in f]
            if match:
                r.ok(f"migration: {keyword}", match[0])
            else:
                r.fail(f"migration: {keyword}", f"expected ~{expected_prefix}_*.sql")

    # ── DB columns: guild_tokens 1.8 additions ──────────────────────────────
    try:
        cols = await bot.db.fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='guild_tokens' AND table_schema='public'"
        )
        col_names = {c["column_name"] for c in cols}
        for col in ("contract_address", "token_hash", "trading_enabled", "token_type"):
            if col in col_names:
                r.ok(f"guild_tokens.{col}", "present")
            else:
                r.fail(f"guild_tokens.{col}", "column missing - run migration 0061")
    except Exception as exc:
        r.warn("guild_tokens columns", str(exc)[:80])

    # ── core/framework/contracts.py ───────────────────────────────────────────────
    contracts_path = os.path.join(base, "core", "framework", "contracts.py")
    if os.path.isfile(contracts_path):
        try:
            ast.parse(open(contracts_path).read())
            r.ok("core/framework/contracts.py", "present and valid")
        except SyntaxError as exc:
            r.fail("core/framework/contracts.py", f"syntax error line {exc.lineno}")
    else:
        r.fail("core/framework/contracts.py", "missing - group token identity broken")

    # ── Cog syntax check (key 1.8 files) ────────────────────────────────────
    for cog_file in ("rugpull.py", "groups.py", "chain_group.py"):
        cog_path = os.path.join(base, "cogs", cog_file)
        if os.path.isfile(cog_path):
            try:
                ast.parse(open(cog_path).read())
                r.ok(f"syntax: cogs/{cog_file}", "valid")
            except SyntaxError as exc:
                r.fail(f"syntax: cogs/{cog_file}", f"line {exc.lineno}: {exc.msg}")
        else:
            r.warn(f"cogs/{cog_file}", "not found")

    # ── Config: rugpull settings ─────────────────────────────────────────────
    for attr in ("RUGPULL_ROLE_ID", "RUGPULL_QUEEN_ROLE_ID", "RUGPULL_WORK_BONUS", "RUGPULL_APE_BONUS", "RUGPULL_TIERS", "RUGPULL_CROWN_DISCOUNT", "RUGPULL_DEFEND_PCT_PER_USD"):
        if hasattr(Config, attr):
            r.ok(f"Config.{attr}", "defined")
        else:
            r.fail(f"Config.{attr}", "missing from core/config.py")

    return r


async def _check_governance(bot: Discoin, guild_id: int) -> DiagResult:
    """Check governance system health."""
    r = DiagResult("Governance")

    # Table presence
    try:
        rows = await bot.db.fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN "
            "('governance_proposals','governance_votes')"
        )
        present = {row["table_name"] for row in rows}
        for t in ("governance_proposals", "governance_votes"):
            if t in present:
                r.ok(f"Table: {t}", "exists")
            else:
                r.fail(f"Table: {t}", "missing - run migration 0059")
    except Exception as exc:
        r.fail("Governance tables", str(exc)[:100])
        return r

    # Guild stats
    try:
        active = await bot.db.fetch_val(
            "SELECT COUNT(*) FROM governance_proposals WHERE guild_id=$1 AND status='active'",
            guild_id,
        ) or 0
        total = await bot.db.fetch_val(
            "SELECT COUNT(*) FROM governance_proposals WHERE guild_id=$1",
            guild_id,
        ) or 0
        r.ok("Active proposals", f"{active} active / {total} total")

        zero_supply = await bot.db.fetch_val(
            "SELECT COUNT(*) FROM governance_proposals "
            "WHERE guild_id=$1 AND (supply_snapshot IS NULL OR supply_snapshot = 0)",
            guild_id,
        ) or 0
        if zero_supply:
            r.warn("Supply snapshots", f"{zero_supply} proposals with zero/null supply (quorum unreachable)")
        else:
            r.ok("Supply snapshots", "all valid")
    except Exception as exc:
        r.warn("Governance stats", str(exc)[:100])

    return r


async def _check_drs(bot: Discoin, guild_id: int) -> DiagResult:
    """Check DRS Terminal system health."""
    r = DiagResult("DRS Terminal")

    # game_helpers table
    try:
        exists = await bot.db.fetch_val(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='game_helpers'"
        )
        if exists:
            r.ok("Table: game_helpers", "exists")
        else:
            r.fail("Table: game_helpers", "missing")
    except Exception as exc:
        r.fail("DRS table", str(exc)[:100])
        return r

    # DRS operator count - informational only, operators are not required for DRS to function
    helper_count = 0
    try:
        helper_count = await bot.db.fetch_val(
            "SELECT COUNT(*) FROM game_helpers WHERE guild_id=$1", guild_id,
        ) or 0
        r.ok("DRS operators", f"{helper_count} registered" if helper_count else "none")
    except Exception as exc:
        r.warn("DRS operators", str(exc)[:100])

    # drs_commands beta feature is OPT-IN per guild. The diag block is
    # informational only; it should never warn for guilds that simply
    # haven't enabled the feature. Players reported this firing
    # repeatedly ("⚠️ drs_commands feature: not enabled") even after
    # granting access; whether the grant landed or not, an opt-in
    # feature shouldn't pollute the diagnostic surface with a warning.
    # ``r.ok`` keeps the line but downgrades the icon, so admins can
    # still see the feature's grant status at a glance.
    try:
        enabled = await bot.db.fetch_val(
            "SELECT 1 FROM beta_features WHERE guild_id=$1 AND feature_name='drs_commands'",
            guild_id,
        )
        if enabled or helper_count:
            r.ok("drs_commands feature", "enabled")
        else:
            r.ok(
                "drs_commands feature",
                "disabled (opt-in -- `.admin beta grant drs_commands @user/@role` to enable)",
            )
    except Exception as exc:
        r.warn("drs_commands feature", str(exc)[:100])

    return r


# ── DiagBlock registry ──────────────────────────────────────────────────────

@dataclass
class DiagBlock:
    """A self-contained diagnostic check.

    ``fn`` signature is either ``async (bot) -> DiagResult``
    or ``async (bot, guild_id) -> DiagResult`` when ``needs_guild=True``.

    ``startup`` - included in the startup self-test (guild-specific blocks are excluded
    from startup automatically regardless of this flag).
    ``health``  - included when health_heal runs its DB/service scan pass.
    """
    key: str
    fn: Callable
    needs_guild: bool = False
    startup: bool = True
    health: bool = True


# Ordered list of all diagnostic blocks.
# Add new blocks here; dispatchers pick them up automatically.
DIAG_BLOCKS: list[DiagBlock] = [
    DiagBlock("db",         _check_db,         needs_guild=False, startup=True,  health=True),
    DiagBlock("cogs",       _check_cogs,        needs_guild=False, startup=True,  health=False),
    DiagBlock("api",        _check_api,         needs_guild=False, startup=True,  health=True),
    DiagBlock("services",   _check_services,    needs_guild=False, startup=True,  health=True),
    DiagBlock("modules",    _check_modules,     needs_guild=True,  startup=False, health=False),
    DiagBlock("commands",   _check_commands,    needs_guild=False, startup=True,  health=False),
    DiagBlock("integrity",  _check_integrity,   needs_guild=False, startup=True,  health=False),
    DiagBlock("governance", _check_governance,  needs_guild=True,  startup=False, health=False),
    DiagBlock("drs",        _check_drs,         needs_guild=True,  startup=False, health=False),
]

_BLOCKS_BY_KEY: dict[str, DiagBlock] = {b.key: b for b in DIAG_BLOCKS}

# Aliases for user-facing target names
_TARGET_ALIASES: dict[str, str] = {
    "database":  "db",
    "gov":       "governance",
}

_VALID_TARGETS = sorted(
    {b.key for b in DIAG_BLOCKS} | set(_TARGET_ALIASES.keys())
)

# Backward-compat: these were previously used by health.py imports
_GUILD_CHECKS: frozenset = frozenset(b.fn for b in DIAG_BLOCKS if b.needs_guild)

_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _color_for(results: list[DiagResult]) -> int:
    has_fail = any(i == _FAIL for r in results for i, _, _ in r.checks)
    has_warn = any(i == _WARN for r in results for i, _, _ in r.checks)
    return C_ERROR if has_fail else (C_AMBER if has_warn else C_SUCCESS)


def _resolve_blocks(target: str) -> list[DiagBlock]:
    """Return the list of DiagBlocks for a given target string."""
    if target == "all":
        return list(DIAG_BLOCKS)
    key = _TARGET_ALIASES.get(target, target)
    block = _BLOCKS_BY_KEY.get(key)
    return [block] if block else []


async def _run_diagnostics(
    bot: Discoin, guild_id: int, target: str = "all",
) -> list[DiagResult]:
    """Non-interactive run (used by dev.py and startup). No live editing."""
    blocks = _resolve_blocks(target)
    results: list[DiagResult] = []
    for block in blocks:
        try:
            if block.needs_guild:
                result = await block.fn(bot, guild_id)
            else:
                result = await block.fn(bot)
        except Exception as exc:
            result = DiagResult(block.key.title())
            result.fail("Check error", str(exc)[:200])
        results.append(result)
    return results


async def _run_diagnostics_live(
    bot: Discoin,
    guild_id: int,
    target: str,
    msg: discord.Message,
) -> list[DiagResult]:
    """Run checks one by one, editing msg after each to show live progress."""
    blocks = _resolve_blocks(target)
    results: list[DiagResult] = []
    spin_i = 0

    for block in blocks:
        section_name = block.key.replace("_", " ").title()
        spin_i = (spin_i + 1) % len(_SPIN)

        b = card(f"{_SPIN[spin_i]} Diagnosing: {section_name}...", color=C_INFO)
        for done in results:
            rendered = done.render()
            if len(rendered) > 900:
                rendered = rendered[:896] + "..."
            b.field(f"{done.worst} {done.name}", rendered, False)
        try:
            await msg.edit(embed=b.build())
        except Exception:
            pass

        try:
            if block.needs_guild:
                result = await block.fn(bot, guild_id)
            else:
                result = await block.fn(bot)
        except Exception as exc:
            result = DiagResult(section_name)
            result.fail("Check error", str(exc)[:200])
        results.append(result)

    return results


# ── Startup self-test ───────────────────────────────────────────────────────

async def _startup_selftest(bot: Discoin) -> None:
    """Run core diagnostics on startup and log results.

    Logs every diag block (db / cogs / api / services / commands / integrity),
    plus three lightweight checks that have no DiagBlock counterpart but
    are cheap and high-signal at boot:

      - heartbeat coverage: any task that registered an interval but has
        not yet pulsed gets an ⚠️ line so a silently-dead loop is obvious;
      - module-flag coverage: warns if `guild_settings.module_*` columns
        in the DB don't match the list this cog knows about, since drift
        between schema additions and this file has been a recurring bug;
      - self-heal scheduler state (kept from the previous version).
    """
    from core.framework import log
    from core.framework.heartbeat import get_all as _hb_get_all, get_all_intervals as _hb_intervals

    log.info("[bold]Running startup self-test...[/bold]")

    all_ok = True

    for block in DIAG_BLOCKS:
        if block.needs_guild or not block.startup:
            continue
        try:
            result = await block.fn(bot)
        except Exception as exc:
            log.error("Startup self-test %s failed: %s", block.key, exc)
            all_ok = False
            continue

        for icon, label, detail in result.checks:
            detail_str = f"  -  {detail}" if detail else ""
            if icon == _FAIL:
                log.error(f"  {icon} {result.name} > {label}{detail_str}")
                all_ok = False
            elif icon == _WARN:
                log.warn(f"  {icon} {result.name} > {label}{detail_str}")
            else:
                log.info("  %s %s > %s%s", icon, result.name, label, detail_str)

    # Heartbeat coverage. We can't expect a "fresh" pulse 3s after on_ready
    # for a task that runs every 30 minutes, so this only flags the case
    # where a registered task has *no* pulse at all -- catches a loop that
    # crashed during its first iteration before the heal scheduler picks it up.
    hb = _hb_get_all()
    intervals = _hb_intervals()
    never_pulsed = [name for name in intervals if name not in hb]
    if never_pulsed:
        log.warn(
            "  ⚠️ Heartbeats > %d loop(s) never pulsed yet: %s",
            len(never_pulsed),
            ", ".join(sorted(never_pulsed)),
        )
    else:
        log.info(
            "  ✅ Heartbeats > %d task loop(s) registered, %d already pulsed",
            len(intervals),
            len(hb),
        )

    # Module-flag coverage. `_check_modules` walks a hardcoded list; this
    # catches the case where a migration adds a `module_*` column the cog
    # doesn't yet know about, so the new module silently never appears in
    # the modules diag block.
    #
    # Legacy + sub-module flags that the schema still carries but
    # ``_check_modules`` deliberately doesn't enumerate. These columns
    # exist for back-compat (the cog they gated has been removed or
    # absorbed) or because the parent umbrella flag is what's checked
    # now (``module_gambling`` covers all the per-game variants). They
    # default TRUE and nothing reads them at runtime, so flagging them
    # in the diag warning is noise. A future cleanup migration can
    # drop the columns; until then the diag stays quiet.
    _SILENT_MODULE_FLAGS = {
        # Cog removed in the dead-cog cleanup pass; faucet absorbed the
        # auto-drop surface.
        "module_drops",
        # Per-game gambling flags from before gambling was consolidated
        # under one ``module_gambling`` umbrella.
        "module_gambling_blackjack",
        "module_gambling_coinflip",
        "module_gambling_dice",
        "module_gambling_roulette",
        "module_gambling_slots",
    }

    try:
        col_rows = await bot.db.fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='guild_settings' AND column_name LIKE 'module\\_%' ESCAPE '\\'"
        )
        db_modules = {r["column_name"] for r in (col_rows or [])}
        # Re-derive what `_check_modules` knows about from its source list
        # so this doesn't go stale when that list changes.
        import inspect
        src = inspect.getsource(_check_modules)
        known = set(re.findall(r'"(module_[a-z_]+)"', src))
        unknown_to_diag = sorted((db_modules - known) - _SILENT_MODULE_FLAGS)
        if unknown_to_diag:
            log.warn(
                "  ⚠️ Modules > %d DB module flag(s) not in diag list: %s",
                len(unknown_to_diag),
                ", ".join(unknown_to_diag),
            )
        else:
            log.info(
                "  ✅ Modules > diag list covers all %d DB flags (%d legacy silenced)",
                len(db_modules), len(db_modules & _SILENT_MODULE_FLAGS),
            )
    except Exception as exc:
        log.warn("  ⚠️ Modules > coverage check failed: %s", exc)

    # Self-heal scheduler check (runs after on_ready, so scheduler should be up by now)
    scheduler = getattr(bot, "self_heal", None)
    if scheduler is None or (scheduler._task and scheduler._task.done()):
        log.warn("  ⚠️ Self-Heal > Scheduler not running  -  may not have started yet")
    else:
        degraded = sorted(scheduler._degraded_loops)
        if degraded:
            log.error("  ❌ Self-Heal > %d degraded loop(s): %s", len(degraded), ", ".join(degraded))
            all_ok = False
        else:
            notify_state = "on" if scheduler.notify_enabled else "off"
            log.info("  ✅ Self-Heal > Scheduler running (notify=%s, degraded=0)", notify_state)

    if all_ok:
        log.ok("[bold]Startup self-test passed[/bold]")
    else:
        log.warn("[bold]Startup self-test completed with warnings/failures[/bold]")


# ── AI Investigation Views ───────────────────────────────────────────────────

class _InvestigateView(discord.ui.View):
    """Offers an 'Investigate with AI' button after a failed/warned diagnose."""

    def __init__(self, bot: Discoin, guild_id: int, diag_summary: str) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.diag_summary = diag_summary
        self.used = False

    @discord.ui.button(label="Investigate with AI", emoji="\U0001f50d", style=discord.ButtonStyle.blurple)
    async def investigate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.used:
            await interaction.response.defer()
            return
        self.used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        config = await get_diagnose_ai_config(self.bot.db, self.guild_id)
        await _run_ai_investigation(interaction, self.bot, self.guild_id, self.diag_summary, config)


async def _execute_context_tool(
    tool_name: str,
    args: dict,
    bot: Discoin,
    guild_id: int,
) -> str:
    """Auto-execute a safe context tool and return a text result for the AI."""
    if tool_name == "get_guild_settings":
        try:
            settings = await bot.db.get_guild_settings(guild_id)
            # Return a safe subset (no secrets)
            _SAFE_KEYS = {
                "prefix", "currency_name", "embed_color", "server_name",
                "module_gambling", "module_lending", "module_staking", "module_mining",
                "module_drops", "module_savings", "module_validators", "module_pools",
                "module_contracts", "module_groups", "module_crypto", "module_daily",
                "module_work", "module_economy", "module_chain", "module_shop",
                "module_games", "module_nft", "module_governance",
                "module_predictions",
            }
            safe = {k: v for k, v in settings.items() if k in _SAFE_KEYS}
            return json.dumps(safe, default=str)
        except Exception as exc:
            return f"Error: {exc!s:.120}"

    if tool_name == "list_loaded_cogs":
        cog_names = list(bot.cogs.keys())
        ext_names = list(bot.extensions.keys())
        return f"Loaded cogs: {', '.join(cog_names)}\nLoaded extensions: {', '.join(ext_names)}"

    if tool_name == "get_task_loops":
        lines = []
        for cog_name, cog in bot.cogs.items():
            for attr_name in dir(cog):
                attr = getattr(cog, attr_name, None)
                if not isinstance(attr, tasks.Loop):
                    continue
                running = attr.is_running() if callable(attr.is_running) else attr.is_running
                failed = attr.failed() if callable(attr.failed) else attr.failed
                heal_skip = getattr(attr, "_heal_skip", False)
                status = "FAILED" if failed else ("running" if running else ("skip" if heal_skip else "stopped"))
                lines.append(f"{cog_name}.{attr_name}: {status}")
        return "\n".join(lines) or "No task loops found"

    if tool_name == "get_beta_features":
        try:
            rows = await bot.db.fetch_all(
                "SELECT feature_name, enabled FROM beta_features WHERE guild_id=$1", guild_id
            )
            if not rows:
                return "No beta features configured for this guild"
            return "\n".join(f"{r['feature_name']}: {'enabled' if r['enabled'] else 'disabled'}" for r in rows)
        except Exception as exc:
            return f"Error: {exc!s:.120}"

    return f"Unknown tool: {tool_name}"


async def _run_ai_investigation(
    interaction: discord.Interaction,
    bot: Discoin,
    guild_id: int,
    diag_summary: str,
    config: dict,
    history: list[dict] | None = None,
    round_num: int = 1,
    _max_rounds: int = 5,
) -> None:
    """Run one AI investigation round and handle the result interactively.

    Supports two modes:
    - Tool-call mode (OpenRouter): AI can call context tools (auto-exec) or run_sql_query (confirm).
    - JSON fallback (Ollama): AI proposes a single SQL query per round.
    """
    if history is None:
        history = []

    thinking_embed = (
        card("AI Investigation", color=C_INFO)
        .description(f"Round {round_num}/{_max_rounds} - analyzing...")
        .build()
    )
    await interaction.followup.send(embed=thinking_embed)

    result = await ask_investigator(diag_summary, history, config)

    if result is None:
        await interaction.followup.send(
            embed=card("AI Investigation Failed", color=C_ERROR).description(
                "The AI did not respond. Check OPENROUTER_API_KEY or AI settings."
            ).build()
        )
        return

    if result.get("done"):
        await interaction.followup.send(
            embed=card("Investigation Complete", color=C_SUCCESS)
            .description(result.get("reasoning") or "No further queries needed.")
            .build()
        )
        return

    # ── Tool-call mode ───────────────────────────────────────────────────────
    tool_calls: list[dict] | None = result.get("tool_calls")
    if tool_calls:
        new_history = list(history)
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            if tool_name == "conclude":
                await interaction.followup.send(
                    embed=card("Investigation Complete", color=C_SUCCESS)
                    .description(args.get("summary", "Investigation concluded."))
                    .build()
                )
                return

            if tool_name == "run_sql_query":
                raw_query = args.get("sql", "")[:1000]
                risk = classify_query(raw_query)
                query = enforce_limit(raw_query) if risk != "blocked" else raw_query

                if risk == "blocked":
                    await interaction.followup.send(
                        embed=card("Blocked Query", color=C_ERROR)
                        .description(
                            f"The AI requested a query that is not allowed:\n```sql\n{raw_query}\n```\n"
                            "Only SELECT queries are permitted."
                        )
                        .build()
                    )
                    return

                risk_icon = "\u2705" if risk == "safe" else "\u26a0\ufe0f"
                risk_label = "Safe" if risk == "safe" else "Sensitive (user data)"
                color = C_INFO if risk == "safe" else C_WARNING
                proposal_embed = (
                    card(f"AI SQL Query - Round {round_num}", color=color)
                    .field("Proposed Query", f"```sql\n{query}\n```", False)
                    .field("Risk Level", f"{risk_icon} {risk_label}", True)
                    .build()
                )
                view = _QueryConfirmView(
                    bot=bot, guild_id=guild_id, query=query, risk=risk,
                    diag_summary=diag_summary, history=new_history,
                    round_num=round_num, max_rounds=_max_rounds, config=config,
                )
                await interaction.followup.send(embed=proposal_embed, view=view)
                return  # user confirms SQL in the view; further rounds chain from there

            # Auto-execute context tool
            tool_result = await _execute_context_tool(tool_name, args, bot, guild_id)
            new_history.append({"tool": tool_name, "args": args, "result": tool_result[:800]})

            ctx_embed = (
                card(f"Tool: {tool_name}", color=C_NEUTRAL)
                .description(f"```\n{tool_result[:900]}\n```")
                .build()
            )
            await interaction.followup.send(embed=ctx_embed)

        # After all auto-executed tools, continue to next round automatically
        if round_num < _max_rounds:
            await _run_ai_investigation(
                interaction=interaction, bot=bot, guild_id=guild_id,
                diag_summary=diag_summary, config=config,
                history=new_history, round_num=round_num + 1, _max_rounds=_max_rounds,
            )
        else:
            await interaction.followup.send(
                embed=card("Investigation Limit Reached", color=C_AMBER)
                .description(f"Maximum {_max_rounds} rounds reached.")
                .build()
            )
        return

    # ── JSON-fallback mode (SQL only) ────────────────────────────────────────
    raw_query = (result.get("query") or "")[:1000]
    if not raw_query:
        await interaction.followup.send(
            embed=card("Investigation Complete", color=C_SUCCESS)
            .description(result.get("reasoning") or "No further queries needed.")
            .build()
        )
        return

    risk = classify_query(raw_query)
    query = enforce_limit(raw_query) if risk != "blocked" else raw_query

    if risk == "blocked":
        await interaction.followup.send(
            embed=card("Blocked Query", color=C_ERROR)
            .description(
                f"**Reasoning:** {result.get('reasoning', '')}\n\n"
                f"The AI proposed a query that is not allowed:\n```sql\n{raw_query}\n```\n"
                "Only SELECT queries are permitted."
            )
            .build()
        )
        return

    risk_icon = "\u2705" if risk == "safe" else "\u26a0\ufe0f"
    risk_label = "Safe" if risk == "safe" else "Sensitive (user data)"
    color = C_INFO if risk == "safe" else C_WARNING

    proposal_embed = (
        card(f"AI Query Proposal - Round {round_num}", color=color)
        .field("Reasoning", result.get("reasoning", "")[:512], False)
        .field("Proposed Query", f"```sql\n{query}\n```", False)
        .field("Risk Level", f"{risk_icon} {risk_label}", True)
        .build()
    )
    view = _QueryConfirmView(
        bot=bot, guild_id=guild_id, query=query, risk=risk,
        diag_summary=diag_summary, history=history,
        round_num=round_num, max_rounds=_max_rounds, config=config,
    )
    await interaction.followup.send(embed=proposal_embed, view=view)


class _QueryConfirmView(discord.ui.View):
    """Handles confirmation of a proposed query (once for safe, twice for sensitive)."""

    def __init__(
        self,
        bot: Discoin,
        guild_id: int,
        query: str,
        risk: str,
        diag_summary: str,
        history: list[dict],
        round_num: int,
        max_rounds: int,
        config: dict,
    ) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.query = query
        self.risk = risk
        self.diag_summary = diag_summary
        self.history = history
        self.round_num = round_num
        self.max_rounds = max_rounds
        self.config = config
        self.used = False

        label = "Run Query" if risk == "safe" else "Confirm (1/2)"
        style = discord.ButtonStyle.green if risk == "safe" else discord.ButtonStyle.danger
        self.confirm_btn = discord.ui.Button(label=label, style=style, emoji="\u25b6")
        self.confirm_btn.callback = self._on_confirm
        self.add_item(self.confirm_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="\u2716")
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=card("Cancelled", color=C_NEUTRAL).build())
        self.stop()

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if self.used:
            await interaction.response.defer()
            return
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        if self.risk == "sensitive":
            # Show second confirmation
            warn_embed = (
                card("Final Confirmation Required", color=C_ERROR)
                .description(
                    "This query accesses **sensitive user data**.\n\n"
                    f"```sql\n{self.query}\n```\n\n"
                    "Are you absolutely sure you want to run this?"
                )
                .build()
            )
            view2 = _SensitiveConfirm2View(
                bot=self.bot,
                guild_id=self.guild_id,
                query=self.query,
                diag_summary=self.diag_summary,
                history=self.history,
                round_num=self.round_num,
                max_rounds=self.max_rounds,
                config=self.config,
            )
            await interaction.followup.send(embed=warn_embed, view=view2)
        else:
            await _execute_query_round(
                interaction=interaction,
                bot=self.bot,
                guild_id=self.guild_id,
                query=self.query,
                diag_summary=self.diag_summary,
                history=self.history,
                round_num=self.round_num,
                max_rounds=self.max_rounds,
                config=self.config,
            )
        self.stop()


class _SensitiveConfirm2View(discord.ui.View):
    """Second (final) confirmation for sensitive queries."""

    def __init__(self, bot, guild_id, query, diag_summary, history, round_num, max_rounds, config):
        super().__init__(timeout=30)
        self.bot = bot
        self.guild_id = guild_id
        self.query = query
        self.diag_summary = diag_summary
        self.history = history
        self.round_num = round_num
        self.max_rounds = max_rounds
        self.config = config
        self.used = False

    @discord.ui.button(label="Yes, run it (2/2)", style=discord.ButtonStyle.danger, emoji="\u26a0")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.used:
            await interaction.response.defer()
            return
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await _execute_query_round(
            interaction=interaction,
            bot=self.bot,
            guild_id=self.guild_id,
            query=self.query,
            diag_summary=self.diag_summary,
            history=self.history,
            round_num=self.round_num,
            max_rounds=self.max_rounds,
            config=self.config,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="\u2716")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=card("Cancelled", color=C_NEUTRAL).build())
        self.stop()


async def _execute_query_round(
    interaction: discord.Interaction,
    bot: Discoin,
    guild_id: int,
    query: str,
    diag_summary: str,
    history: list[dict],
    round_num: int,
    max_rounds: int,
    config: dict,
) -> None:
    """Run the query, show results, and offer another round if under max."""
    try:
        rows = await bot.db.fetch_all(query)
    except Exception as exc:
        err_embed = (
            card("Query Error", color=C_ERROR)
            .description(f"```\n{str(exc)[:800]}\n```")
            .build()
        )
        await interaction.followup.send(embed=err_embed)
        return

    if not rows:
        result_text = "(no rows returned)"
    else:
        cols = list(rows[0].keys())
        lines = [" | ".join(str(r.get(c, "")) for c in cols) for r in rows[:20]]
        header = " | ".join(cols)
        result_text = header + "\n" + "-" * min(len(header), 60) + "\n" + "\n".join(lines)
        if len(rows) > 20:
            result_text += f"\n... ({len(rows)} rows total, showing 20)"

    # Truncate for embed
    if len(result_text) > 900:
        result_text = result_text[:896] + "..."

    result_embed = (
        card(f"Query Results - Round {round_num}", color=C_SUCCESS)
        .field("Query", f"```sql\n{query[:400]}\n```", False)
        .field("Results", f"```\n{result_text}\n```", False)
        .build()
    )
    await interaction.followup.send(embed=result_embed)

    # Update history
    new_history = history + [{"query": query, "result": result_text}]

    if round_num >= max_rounds:
        await interaction.followup.send(
            embed=card("Investigation Limit Reached", color=C_AMBER)
            .description(f"Maximum {max_rounds} rounds reached.")
            .build()
        )
        return

    # Offer another round
    cont_view = _ContinueView(
        bot=bot,
        guild_id=guild_id,
        diag_summary=diag_summary,
        history=new_history,
        round_num=round_num + 1,
        max_rounds=max_rounds,
        config=config,
    )
    await interaction.followup.send(
        embed=card("Continue?", color=C_NEUTRAL)
        .description(
            f"Round {round_num} complete. Continue AI investigation "
            f"({round_num}/{max_rounds} rounds used)?"
        )
        .build(),
        view=cont_view,
    )


class _ContinueView(discord.ui.View):
    """Offers continue / done after a successful query round."""

    def __init__(self, bot, guild_id, diag_summary, history, round_num, max_rounds, config):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.diag_summary = diag_summary
        self.history = history
        self.round_num = round_num
        self.max_rounds = max_rounds
        self.config = config
        self.used = False

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.blurple, emoji="\U0001f504")
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.used:
            await interaction.response.defer()
            return
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await _run_ai_investigation(
            interaction=interaction,
            bot=self.bot,
            guild_id=self.guild_id,
            diag_summary=self.diag_summary,
            config=self.config,
            history=self.history,
            round_num=self.round_num,
            _max_rounds=self.max_rounds,
        )
        self.stop()

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, emoji="\u2705")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=card("Investigation Closed", color=C_NEUTRAL).build()
        )
        self.stop()


# ── Cog ─────────────────────────────────────────────────────────────────────

def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.reply_error("You need **Manage Guild** to use this command.")
            return False
        return True
    return commands.check(predicate)


class Diagnose(commands.Cog):
    """System diagnostic tools for server admins."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._selftest_done = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Run startup self-test once on first ready."""
        if self._selftest_done:
            return
        self._selftest_done = True
        # Small delay to let API server start
        await asyncio.sleep(3)
        try:
            await _startup_selftest(self.bot)
        except Exception as exc:
            log.error("Startup self-test error: %s", exc)

    @commands.command(name="diagnose", aliases=["diag", "doctor"], hidden=True)
    @guild_only
    @_require_manage_guild()
    async def diagnose_cmd(self, ctx: DiscoContext, target: str = "all") -> None:
        """Run live system diagnostics with animated progress.

        Usage: ,diagnose [target]
        Targets: all, db, cogs, api, modules, services, commands, integrity, homes
        """
        target = target.lower().strip()

        if target != "all" and target not in _BLOCKS_BY_KEY and target not in _TARGET_ALIASES:
            targets_str = ", ".join(f"`{t}`" for t in _VALID_TARGETS)
            await ctx.reply_error(
                f"Unknown target `{target}`. Valid: {targets_str}"
            )
            return

        # Post initial spinner embed - will be live-edited as checks run
        init_embed = (
            card(f"{_SPIN[0]} Running diagnostics...", color=C_INFO)
            .description(f"Target: `{target}`  -  stand by...")
            .build()
        )
        msg = await ctx.reply(embed=init_embed, mention_author=False)

        start = time.monotonic()
        results = await _run_diagnostics_live(self.bot, ctx.guild.id, target, msg)
        elapsed = time.monotonic() - start

        if not results:
            await msg.edit(embed=card("No diagnostic results.", color=C_AMBER).build())
            return

        total_pass = sum(1 for r in results for i, _, _ in r.checks if i == _OK)
        total_warn = sum(1 for r in results for i, _, _ in r.checks if i == _WARN)
        total_fail = sum(1 for r in results for i, _, _ in r.checks if i == _FAIL)
        overall = _FAIL if total_fail else (_WARN if total_warn else _OK)
        color = _color_for(results)

        status_label = "PASS" if overall == _OK else ("WARN" if overall == _WARN else "FAIL")
        header = f"{overall} Diagnostic Complete - `{target}` [{status_label}]"
        summary = (
            f"**{total_pass}** passed  **{total_warn}** warnings  **{total_fail}** failures"
            f"  -  {elapsed:.1f}s"
        )

        # Build paginated embeds (max 5 fields each to avoid 6000-char embed limit)
        embeds: list[discord.Embed] = []
        _b = card(header, description=summary, color=color)
        for result in results:
            rendered = result.render()
            if len(rendered) > 900:
                rendered = rendered[:896] + "..."
            section_title = f"{result.worst} {result.name}"
            if len(_b._embed.fields) >= 5:
                embeds.append(_b.build())
                _b = card(color=color)
            _b.field(section_title, rendered, False)
        embeds.append(_b.build())

        # Edit first embed in-place (replaces spinner), send overflow pages
        await msg.edit(embed=embeds[0])
        for e in embeds[1:]:
            await ctx.send(embed=e)

        # Offer AI investigation if there are failures or warnings and AI is configured
        if (total_fail or total_warn) and Config.OPENROUTER_API_KEY:
            diag_summary = "\n\n".join(
                f"[{r.name}]\n" + "\n".join(
                    f"  {icon} {label}: {detail}" for icon, label, detail in r.checks
                )
                for r in results
            )
            inv_view = _InvestigateView(self.bot, ctx.guild.id, diag_summary)
            await ctx.send(
                embed=card("AI Investigation Available", color=C_INFO)
                .description(
                    f"Found **{total_fail}** failures and **{total_warn}** warnings.\n"
                    "Click to let the AI propose SQL queries to investigate the root cause."
                )
                .build(),
                view=inv_view,
            )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Diagnose(bot))
