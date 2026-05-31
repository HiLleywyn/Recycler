"""Health check cog for Discoin.

Commands live under .admin health (check, heal, test, notify, analyze).
This cog provides the implementations as plain async methods plus the
_test_heal_loop task used by .admin health test.

Checks: feed channels, bot permissions, MM webhook, personas, AI settings, prices,
validators, mining network, and guild settings.

The ``health_heal`` method is the consolidated "Doctor" command: it runs a
full diagnostic scan (channels, webhook, Redis, tasks, DB, services, AI,
integrity), triages issues by severity, asks AI for a repair plan, then
executes fixes step-by-step with a live-updating embed that shows progress.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_AMBER, C_ERROR, C_SUCCESS, C_TEAL
from core.framework.ai.heal_ai import complete_heal, build_health_report
from core.framework.ai import resolve_model as _resolve_model

log = logging.getLogger("discoin.health")


# ══════════════════════════════════════════════════════════════════════════════
# Doctor system  -  consolidated diagnostic + repair engine
# ══════════════════════════════════════════════════════════════════════════════

class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    CRITICAL = 4


_SEV_ICON = {
    Severity.OK: "✅",
    Severity.INFO: "ℹ️",
    Severity.WARN: "⚠️",
    Severity.ERROR: "❌",
    Severity.CRITICAL: "🚨",
}

_SEV_LABEL = {
    Severity.OK: "Healthy",
    Severity.INFO: "Info",
    Severity.WARN: "Warning",
    Severity.ERROR: "Error",
    Severity.CRITICAL: "Critical",
}

# Progress animation frames
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SCAN_BAR_LEN = 12


@dataclass
class DoctorIssue:
    """A single issue found during the diagnostic scan."""
    category: str          # e.g. "Feed Channels", "Redis", "Task Loops"
    icon: str              # emoji icon for the category
    severity: Severity
    summary: str           # one-line description
    detail: str = ""       # optional longer explanation
    repair_id: str = ""    # key linking to a repair function (empty = no auto-fix)
    repair_ctx: dict = field(default_factory=dict)  # extra data for the repair


@dataclass
class RepairResult:
    """Outcome of a single repair action."""
    success: bool
    message: str


def _health_score(issues: list[DoctorIssue]) -> int:
    """Compute a 0-100 health score from the issue list."""
    if not issues:
        return 100
    deductions = {Severity.INFO: 1, Severity.WARN: 5, Severity.ERROR: 15, Severity.CRITICAL: 30}
    total = sum(deductions.get(i.severity, 0) for i in issues if i.severity > Severity.OK)
    return max(0, 100 - total)


async def doctor_quick_scan(
    bot: Discoin,
    guild: discord.Guild,
    settings: dict | None = None,
) -> list[DoctorIssue]:
    """Read-only Doctor scan -- the same checks ``,admin health heal`` runs,
    minus the live embed and the repair phase.

    Designed for the auto-status DM and ``.dev status`` so the developer's
    overview surfaces the same actionable issue list players would trigger
    a heal for, without having to run the full repair flow. Returns a list
    of ``DoctorIssue`` ordered by severity (highest first).

    The scan covers: feed channels, MM webhook, Redis bus, task loops,
    self-heal scheduler, DiagBlock results, DB row bloat, and bot perms.
    All checks are best-effort -- a broken sub-check appends a single
    issue rather than aborting the whole scan.
    """
    issues: list[DoctorIssue] = []
    db = bot.db
    me = guild.me
    gid = guild.id

    if settings is None:
        try:
            settings = await db.get_guild_settings(gid) or {}
        except Exception:
            settings = {}

    # 1. Feed channels  -  deleted/forum/permission issues
    try:
        for col, label, icon in _CHANNEL_CHECKS:
            ch_id = settings.get(col)
            if not ch_id:
                continue
            ch = guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(ch_id)
                except Exception:
                    ch = None
            if ch is None:
                issues.append(DoctorIssue(
                    category="Feed Channels", icon=icon, severity=Severity.ERROR,
                    summary=f"{label}: channel deleted (ID `{ch_id}`)",
                    repair_id="clear_channel",
                ))
                continue
            if isinstance(ch, discord.ForumChannel):
                issues.append(DoctorIssue(
                    category="Feed Channels", icon=icon, severity=Severity.WARN,
                    summary=f"{label}: forum container, expected a thread",
                ))
                continue
            perm_target = ch.parent if isinstance(ch, discord.Thread) and ch.parent else ch
            try:
                perms = perm_target.permissions_for(me)
                missing = [
                    name for name, ok in (
                        ("Send Messages", perms.send_messages),
                        ("Embed Links", perms.embed_links),
                    ) if not ok
                ]
                if missing:
                    issues.append(DoctorIssue(
                        category="Feed Channels", icon=icon, severity=Severity.WARN,
                        summary=f"{label}: missing {', '.join(missing)}",
                    ))
            except Exception:
                pass
    except Exception:
        pass

    # 2. MM webhook  -  DB row vs. live webhook
    try:
        wh_row = await db.get_mm_webhook(gid)
        if wh_row:
            try:
                await guild.fetch_webhook(int(wh_row["webhook_id"]))
            except discord.NotFound:
                issues.append(DoctorIssue(
                    category="MM Webhook", icon="🎭", severity=Severity.ERROR,
                    summary="Webhook deleted from Discord",
                    repair_id="clear_webhook",
                ))
            except Exception:
                pass  # transient network errors not worth flagging
    except Exception:
        pass

    # 3. Redis bus
    bus = getattr(bot, "bus", None)
    if bus is not None:
        if not bus.is_connected:
            issues.append(DoctorIssue(
                category="Redis", icon="🔌", severity=Severity.CRITICAL,
                summary="Redis bus offline",
                repair_id="reconnect_redis",
            ))
        else:
            try:
                if not await asyncio.wait_for(bus.ping(), timeout=3.0):
                    issues.append(DoctorIssue(
                        category="Redis", icon="🔌", severity=Severity.CRITICAL,
                        summary="Redis PING failed (ghost connection)",
                        repair_id="reconnect_redis",
                    ))
            except Exception:
                issues.append(DoctorIssue(
                    category="Redis", icon="🔌", severity=Severity.CRITICAL,
                    summary="Redis PING timed out",
                    repair_id="reconnect_redis",
                ))

    # 4. Task loops  -  failed or stopped (skip one-shots and heal-skipped)
    healer = getattr(bot, "self_heal", None)
    degraded = healer._degraded_loops if healer is not None else set()
    for cog_name, cog in list(bot.cogs.items()):
        for attr_name in dir(cog):
            try:
                attr = getattr(cog, attr_name, None)
                if not isinstance(attr, tasks.Loop):
                    continue
                if getattr(attr, "count", None) == 1:
                    continue
                if getattr(attr, "_heal_skip", False):
                    continue
                failed = attr.failed() if callable(attr.failed) else attr.failed
                running = attr.is_running() if callable(attr.is_running) else attr.is_running
                if not (failed or not running):
                    continue
                label = f"{cog_name}.{attr_name}"
                if label in degraded:
                    issues.append(DoctorIssue(
                        category="Task Loops", icon="🔄", severity=Severity.CRITICAL,
                        summary=f"`{label}`: circuit-breaker tripped",
                    ))
                else:
                    issues.append(DoctorIssue(
                        category="Task Loops", icon="🔄", severity=Severity.ERROR,
                        summary=f"`{label}`: {'FAILED' if failed else 'stopped'}",
                        repair_id="restart_loop",
                    ))
            except Exception:
                pass

    # 5. Self-heal scheduler
    if healer is not None:
        try:
            snap = healer.status()
            if not snap.get("scheduler_running", True):
                issues.append(DoctorIssue(
                    category="Self-Heal", icon="🛡️", severity=Severity.ERROR,
                    summary="Self-heal scheduler not running",
                    repair_id="restart_scheduler",
                ))
            stale_hb = snap.get("stale_heartbeats", []) or []
            if stale_hb:
                issues.append(DoctorIssue(
                    category="Self-Heal", icon="💓", severity=Severity.WARN,
                    summary=f"{len(stale_hb)} stale heartbeat(s): "
                            f"{', '.join(stale_hb[:5])}",
                ))
        except Exception:
            pass

    # 6. DB / services diagnostics from the shared DiagBlock registry
    try:
        from cogs.diagnose import DIAG_BLOCKS
        for block in DIAG_BLOCKS:
            if not block.health or block.needs_guild:
                continue
            try:
                result = await block.fn(bot)
                for icon_str, lab, det in result.checks:
                    if icon_str == "❌":
                        issues.append(DoctorIssue(
                            category=result.name, icon="🔍",
                            severity=Severity.ERROR,
                            summary=f"{lab}: {det}" if det else lab,
                        ))
                    elif icon_str == "⚠️":
                        issues.append(DoctorIssue(
                            category=result.name, icon="🔍",
                            severity=Severity.WARN,
                            summary=f"{lab}: {det}" if det else lab,
                        ))
            except Exception:
                pass
    except ImportError:
        pass

    # 7. Bot permissions
    try:
        gp = me.guild_permissions
        missing = [
            name for name, ok in (
                ("Send Messages", gp.send_messages),
                ("Embed Links", gp.embed_links),
                ("Read Message History", gp.read_message_history),
                ("Manage Webhooks", gp.manage_webhooks),
                ("Add Reactions", gp.add_reactions),
            ) if not ok
        ]
        if missing:
            issues.append(DoctorIssue(
                category="Bot Permissions", icon="🔐", severity=Severity.WARN,
                summary=f"Missing: {', '.join(missing)}",
            ))
    except Exception:
        pass

    issues.sort(key=lambda i: i.severity, reverse=True)
    return issues


def _score_bar(score: int) -> str:
    """Render a visual health bar: [########--] 82%"""
    filled = round(score / 10)
    empty = 10 - filled
    if score >= 80:
        segment = "🟩"
    elif score >= 50:
        segment = "🟨"
    else:
        segment = "🟥"
    return segment * filled + "⬛" * empty + f"  **{score}%**"


def _progress_bar(current: int, total: int, width: int = _SCAN_BAR_LEN) -> str:
    """Render a progress bar: [======>   ] 6/12"""
    if total == 0:
        return "`[" + "=" * width + "]` done"
    filled = round(current / total * width)
    arrow = ">" if filled < width else ""
    bar = "=" * max(0, filled - 1) + arrow + " " * (width - filled)
    return f"`[{bar}]` {current}/{total}"


def _phase_header(phase: str, icon: str, status: str = "in progress") -> str:
    """Build a phase header line for the live embed."""
    return f"### {icon}  {phase}  *({status})*"


_CHANNEL_CHECKS = [
    ("trade_channel",      "Trade feed",           "🔄"),
    ("mine_channel",       "Mining feed",          "⛏️"),
    ("staking_channel",    "Staking feed",         "💎"),
    ("validators_channel", "Validator block feed", "🔐"),
    ("gambling_channel",   "Gambling feed",        "🎲"),
    ("pools_channel",      "Pools feed",           "🌊"),
    ("crypto_channel",     "Crypto feed",          "📈"),
    ("drops_channel",      "Drops feed (claims)",  "💰"),
    ("drops_spawn_channel","Drops spawn",          "🎁"),
    ("job_channel",        "Job feed",             "💼"),
    ("wallet_channel",     "Wallet feed",          "👛"),
    ("error_channel",      "Error feed",           "🚨"),
    ("contracts_channel",  "Contracts feed",       "📜"),
    ("nft_channel",          "NFT feed",           "🎨"),
    ("predictions_channel",  "Predictions feed",   "🔮"),
    ("events_channel",       "Events feed",        "🌐"),
    ("ape_channel",          "Ape/degen feed",     "🦍"),
]


class Health(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._test_heal_trigger: bool = False

    async def cog_load(self) -> None:
        # Loop.__get__ returns a fresh bound copy that drops class-level custom attrs.
        # Stamp _heal_skip on the bound instance so the diagnose task-check sees it.
        self._test_heal_loop._heal_skip = True

    # ── Test loop  -  only purpose is to be breakable on demand ─────────────────
    # reconnect=False keeps it in failed() state after raising so heal can find it.
    # Faults are drawn from a realistic pool so the output never looks canned.

    _FAULT_POOL: list[tuple[str, BaseException]] = [
        (
            "asyncio.TimeoutError",
            asyncio.TimeoutError("acquire connection timed out after 30.0s"),
        ),
        (
            "ConnectionResetError",
            ConnectionResetError(104, "Connection reset by peer"),
        ),
        (
            "OSError",
            OSError(32, "Broken pipe"),
        ),
        (
            "asyncio.TimeoutError",
            asyncio.TimeoutError("Redis PING did not respond within 2.0s"),
        ),
        (
            "RuntimeError",
            RuntimeError(
                "background worker lost its DB pool reference "
                "(pool closed during hot-reload?)"
            ),
        ),
    ]

    @tasks.loop(seconds=60, reconnect=False)
    async def _test_heal_loop(self) -> None:
        """Dummy loop used exclusively by ,health test. Raises when triggered."""
        if self._test_heal_trigger:
            self._test_heal_trigger = False
            # Pick a random realistic root cause, then wrap it in a higher-level
            # RuntimeError the same way a real background task would propagate it.
            _label, _cause = random.choice(self._FAULT_POOL)
            raise RuntimeError(
                f"background task exited unexpectedly: {_label}"
            ) from _cause

    _test_heal_loop._heal_skip = True  # excluded from SelfHealScheduler auto-restart

    async def health_check(self, ctx: DiscoContext) -> None:
        """Run a full health diagnostic for this server's bot configuration."""
        guild = ctx.guild
        db = ctx.db
        settings = await db.get_guild_settings(guild.id)
        me = guild.me

        lines: list[tuple[str, str]] = []  # (section_name, content)

        # ── 1. Feed Channels ─────────────────────────────────────────────────
        ch_lines = []
        for col, label, icon in _CHANNEL_CHECKS:
            ch_id = settings.get(col)
            if not ch_id:
                ch_lines.append(f"❌ {icon} {label}: **not set**")
                continue

            # Resolve channel  -  support text channels, threads, and forum post threads
            ch = guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(ch_id)
                except Exception:
                    ch = None

            if ch is None:
                ch_lines.append(f"⚠️ {icon} {label}: set but **channel not found** (deleted?)")
                continue

            # Reject ForumChannel (parent container  -  cannot be posted to directly)
            if isinstance(ch, discord.ForumChannel):
                ch_lines.append(
                    f"⚠️ {icon} {label}: {ch.mention}  -  ⚠️ **forum channel** (set to a thread inside it, not the forum itself)"
                )
                continue

            # Thread-specific checks
            thread_note = ""
            if isinstance(ch, discord.Thread):
                thread_type = "forum post" if isinstance(ch.parent, discord.ForumChannel) else "thread"
                archived_note = " **[ARCHIVED  -  will be auto-unarchived on post]**" if ch.archived else ""
                thread_note = f" ({thread_type}{archived_note})"

            # Permission check  -  threads inherit from parent
            perm_target = ch.parent if isinstance(ch, discord.Thread) and ch.parent else ch
            try:
                perms = perm_target.permissions_for(me)
            except Exception:
                perms = None

            issues = []
            if perms is not None:
                if not perms.send_messages:
                    issues.append("no Send Messages")
                if not perms.embed_links:
                    issues.append("no Embed Links")

            mention = f"<#{ch_id}>"
            if issues:
                ch_lines.append(f"⚠️ {icon} {label}: {mention}{thread_note}  -  ⚠️ {', '.join(issues)}")
            else:
                ch_lines.append(f"✅ {icon} {label}: {mention}{thread_note}")
        lines.append(("📡 Feed Channels", "\n".join(ch_lines)))

        # ── 2. MM Webhook ────────────────────────────────────────────────────
        wh_row = await db.get_mm_webhook(guild.id)
        if not wh_row:
            wh_lines = ["❌ Not configured  -  use `.admin mmwebhook create`"]
        else:
            ch = guild.get_channel(wh_row["channel_id"])
            ch_str = ch.mention if ch else f"<#{wh_row['channel_id']}> (not found)"
            # Verify webhook still exists on Discord
            wh_valid = False
            try:
                await guild.fetch_webhook(int(wh_row["webhook_id"]))
                wh_valid = True
            except Exception:
                pass
            wh_status = "✅ Valid" if wh_valid else "❌ Invalid/deleted  -  run `.admin mmwebhook delete` then `create`"
            wh_lines = [f"{wh_status}", f"Channel: {ch_str}"]
            # Check Manage Webhooks perm in that channel
            if ch:
                if not ch.permissions_for(me).manage_webhooks:
                    wh_lines.append("⚠️ Bot is missing **Manage Webhooks** in that channel")

        # Personas
        personas = await db.get_mm_personas(guild.id)
        active_p = [p for p in personas if p["active"]]
        if not personas:
            wh_lines.append("❌ No personas  -  use `.admin persona list` or seed defaults")
        else:
            wh_lines.append(f"✅ {len(active_p)}/{len(personas)} personas active")
        lines.append(("🎭 MM Webhook & Personas", "\n".join(wh_lines)))

        # ── 3. AI / OpenRouter ───────────────────────────────────────────────
        key_set = bool(Config.OPENROUTER_API_KEY)
        ai_flags, resolved_chat = await asyncio.gather(
            db.get_ai_flags(guild.id),
            _resolve_model(db, guild.id, "chat"),
        )
        ai_lines = [
            f"{'✅' if key_set else '❌'} API Key: {'configured' if key_set else 'not set in .env'}",
            f"Chat model: `{resolved_chat}`",
        ]
        flag_parts = []
        for feat, enabled in ai_flags.items():
            flag_parts.append(f"{'✅' if enabled else '❌'} `{feat}`")
        ai_lines.append("Features: " + "  ".join(flag_parts))
        if not key_set:
            ai_lines.append("⚠️ All AI features disabled without a key")
        lines.append(("🤖 AI / OpenRouter", "\n".join(ai_lines)))

        # ── 4. Validators ────────────────────────────────────────────────────
        validators = await db.get_validators(guild.id)
        if not validators:
            v_str = "❌ No validators seeded  -  staking will not work"
        else:
            names = ", ".join(v["validator_id"] for v in validators[:5])
            v_str = f"✅ {len(validators)} validator(s): {names}"
        lines.append(("⚡ Validators", v_str))

        # ── 5. Prices / Tokens ───────────────────────────────────────────────
        prices = await db.get_all_prices(guild.id)
        if not prices:
            p_str = "❌ No prices seeded  -  run a trade or wait for drift tick"
        else:
            p_str = f"✅ {len(prices)} token(s) with price data"
        lines.append(("💹 Token Prices", p_str))

        # ── 6. Mining Network ────────────────────────────────────────────────
        network = await db.get_network(guild.id)
        if not network:
            m_str = "❌ Mining network not initialized  -  happens on first mine command"
        else:
            height = network.get("block_height", 0)
            diff = network.get("difficulty", Config.POW_NETWORKS["SUN"]["initial_difficulty"])
            m_str = f"✅ Height #{height:,}  |  Difficulty {diff:,.0f}"
        lines.append(("⛏️ Mining Network", m_str))

        # ── 7. Guild Settings ────────────────────────────────────────────────
        prefix = settings.get("prefix") or Config.PREFIX
        currency = settings.get("currency_name") or "USD"
        color = settings.get("embed_color")
        color_str = f"#{color:06x}" if color else "default"
        server_name = settings.get("server_name") or guild.name
        s_str = (
            f"Prefix: `{prefix}`\n"
            f"Currency: **{currency}**\n"
            f"Server name: {server_name}\n"
            f"Embed color: `{color_str}`"
        )
        lines.append(("⚙️ Settings", s_str))

        # ── 8. Bot Permissions (global) ──────────────────────────────────────
        perm_issues = []
        gp = guild.me.guild_permissions
        if not gp.send_messages:
            perm_issues.append("Send Messages")
        if not gp.embed_links:
            perm_issues.append("Embed Links")
        if not gp.read_message_history:
            perm_issues.append("Read Message History")
        if wh_row and not gp.manage_webhooks:
            perm_issues.append("Manage Webhooks (needed for MM webhook)")
        if not gp.add_reactions:
            perm_issues.append("Add Reactions")
        if perm_issues:
            perm_str = "⚠️ Missing: " + ", ".join(perm_issues)
        else:
            perm_str = "✅ All required permissions present"
        lines.append(("🔐 Bot Permissions", perm_str))

        # ── Build embeds (paginate if > 6 fields) ────────────────────────────
        color = settings.get("embed_color") or C_SUCCESS
        embeds = []
        _b = card(f"🏥 Health Check  -  {guild.name}", description="Diagnostic report for all bot systems.").color(color)
        for name, value in lines:
            if len(_b._embed.fields) >= 6:
                embeds.append(_b.build())
                _b = card(color=color)
            _b.field(name, value[:1024], False)
        embeds.append(_b.build())

        for i, e in enumerate(embeds):
            if i == 0:
                await ctx.reply(embed=e, mention_author=False)
            else:
                await ctx.send(embed=e)

    # ── .health heal  -  consolidated Doctor command ────────────────────────

    async def health_heal(self, ctx: DiscoContext) -> None:
        """Run a full diagnostic scan across all systems, triage with AI,
        and execute repairs step-by-step with live-updating embeds."""
        guild = ctx.guild
        db = ctx.db
        me = guild.me
        gid = guild.id
        t0 = time.monotonic()

        try:
            settings = await db.get_guild_settings(gid)
        except Exception:
            settings = {}

        embed_color = settings.get("embed_color") or C_TEAL

        # ── Phase 1: Initial embed  -  "Doctor is in" ────────────────────────

        scan_embed = self._doctor_embed(
            embed_color,
            phase="Scanning",
            phase_icon="🔬",
            description=(
                "Running comprehensive diagnostic across all subsystems...\n\n"
                f"{_progress_bar(0, 8)}\n"
                "```\n"
                "  Initializing scan...\n"
                "```"
            ),
            score=None,
        )
        msg = await ctx.reply(embed=scan_embed, mention_author=False)

        # ── Phase 2: Scan  -  collect all issues ─────────────────────────────

        issues: list[DoctorIssue] = []
        scan_log: list[str] = []  # running log lines for the embed
        scan_step = 0
        total_steps = 8

        async def _tick(label: str) -> None:
            nonlocal scan_step
            scan_step += 1
            scan_log.append(f"  {_SPIN[scan_step % len(_SPIN)]}  {label}")
            # Keep only last 6 lines to avoid embed overflow
            visible = scan_log[-6:]
            try:
                await msg.edit(embed=self._doctor_embed(
                    embed_color,
                    phase="Scanning",
                    phase_icon="🔬",
                    description=(
                        "Running comprehensive diagnostic across all subsystems...\n\n"
                        f"{_progress_bar(scan_step, total_steps)}\n"
                        "```\n"
                        + "\n".join(visible) + "\n"
                        "```"
                    ),
                    score=None,
                ))
            except Exception:
                pass

        # 1. Feed Channels
        await _tick("Checking feed channels...")
        for col, label, icon in _CHANNEL_CHECKS:
            ch_id = settings.get(col)
            if not ch_id:
                continue
            ch = guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(ch_id)
                except Exception:
                    ch = None
            if ch is None:
                issues.append(DoctorIssue(
                    category="Feed Channels", icon=icon, severity=Severity.ERROR,
                    summary=f"{label}: channel deleted (ID `{ch_id}`)",
                    detail=f"Stale channel reference in `{col}`",
                    repair_id="clear_channel",
                    repair_ctx={"col": col, "ch_id": ch_id, "label": label, "icon": icon},
                ))
                continue
            if isinstance(ch, discord.ForumChannel):
                issues.append(DoctorIssue(
                    category="Feed Channels", icon=icon, severity=Severity.WARN,
                    summary=f"{label}: set to forum container, should be a thread",
                ))
                continue
            perm_target = ch.parent if isinstance(ch, discord.Thread) and ch.parent else ch
            try:
                perms = perm_target.permissions_for(me)
                perm_issues = []
                if not perms.send_messages:
                    perm_issues.append("Send Messages")
                if not perms.embed_links:
                    perm_issues.append("Embed Links")
                if perm_issues:
                    issues.append(DoctorIssue(
                        category="Feed Channels", icon=icon, severity=Severity.WARN,
                        summary=f"{label}: missing {', '.join(perm_issues)}",
                    ))
            except Exception:
                pass

        # 2. MM Webhook
        await _tick("Verifying MM webhook...")
        try:
            wh_row = await db.get_mm_webhook(gid)
            if wh_row:
                try:
                    await guild.fetch_webhook(int(wh_row["webhook_id"]))
                except discord.NotFound:
                    issues.append(DoctorIssue(
                        category="MM Webhook", icon="🎭", severity=Severity.ERROR,
                        summary="Webhook deleted from Discord",
                        detail="DB still has a record but the webhook no longer exists",
                        repair_id="clear_webhook",
                    ))
                except Exception:
                    issues.append(DoctorIssue(
                        category="MM Webhook", icon="🎭", severity=Severity.WARN,
                        summary="Could not verify webhook (network error)",
                    ))
        except Exception as exc:
            issues.append(DoctorIssue(
                category="MM Webhook", icon="🎭", severity=Severity.ERROR,
                summary=f"Webhook check failed: {exc!s:.60}",
            ))

        # 3. Redis bus
        await _tick("Pinging Redis bus...")
        bus = getattr(self.bot, "bus", None)
        if bus is not None:
            if not bus.is_connected:
                issues.append(DoctorIssue(
                    category="Redis", icon="🔌", severity=Severity.CRITICAL,
                    summary="Redis bus is offline",
                    detail="Pub/sub events are down; running in-memory fallback mode",
                    repair_id="reconnect_redis",
                ))
            else:
                try:
                    alive = await asyncio.wait_for(bus.ping(), timeout=3.0)
                    if not alive:
                        issues.append(DoctorIssue(
                            category="Redis", icon="🔌", severity=Severity.CRITICAL,
                            summary="Redis PING failed (ghost connection)",
                            detail="is_connected=True but PING timed out",
                            repair_id="reconnect_redis",
                        ))
                except Exception:
                    issues.append(DoctorIssue(
                        category="Redis", icon="🔌", severity=Severity.CRITICAL,
                        summary="Redis PING timed out",
                        repair_id="reconnect_redis",
                    ))

        # 4. Task loops
        await _tick("Inspecting task loops...")
        healer = getattr(self.bot, "self_heal", None)
        _degraded = healer._degraded_loops if healer is not None else set()
        for cog_name, cog in list(self.bot.cogs.items()):
            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name, None)
                    if not isinstance(attr, tasks.Loop):
                        continue
                    if getattr(attr, "count", None) == 1:
                        continue
                    if getattr(attr, "_heal_skip", False):
                        continue
                    failed = attr.failed() if callable(attr.failed) else attr.failed
                    running = attr.is_running() if callable(attr.is_running) else attr.is_running
                    if not (failed or not running):
                        continue
                    loop_label = f"{cog_name}.{attr_name}"
                    exc_note = ""
                    try:
                        inner = getattr(attr, "_task", None)
                        if inner is not None and inner.done():
                            exc = inner.exception()
                            if exc is not None:
                                exc_note = f"{type(exc).__name__}: {exc!s:.60}"
                    except Exception:
                        pass
                    if loop_label in _degraded:
                        issues.append(DoctorIssue(
                            category="Task Loops", icon="🔄", severity=Severity.CRITICAL,
                            summary=f"`{loop_label}`: circuit-breaker tripped",
                            detail=">=5 consecutive restart failures. Check logs for root cause.",
                        ))
                    else:
                        issues.append(DoctorIssue(
                            category="Task Loops", icon="🔄", severity=Severity.ERROR,
                            summary=f"`{loop_label}`: {'FAILED' if failed else 'stopped'}",
                            detail=exc_note or "No exception captured",
                            repair_id="restart_loop",
                            repair_ctx={"cog_name": cog_name, "attr_name": attr_name, "label": loop_label},
                        ))
                except Exception:
                    pass

        # 5. Self-heal scheduler
        await _tick("Checking self-heal scheduler...")
        if healer is not None:
            snap = healer.status()
            if not snap["scheduler_running"]:
                issues.append(DoctorIssue(
                    category="Self-Heal", icon="🛡️", severity=Severity.ERROR,
                    summary="Self-heal scheduler is not running",
                    repair_id="restart_scheduler",
                ))
            stale_hb = snap.get("stale_heartbeats", [])
            if stale_hb:
                issues.append(DoctorIssue(
                    category="Self-Heal", icon="💓", severity=Severity.WARN,
                    summary=f"{len(stale_hb)} stale heartbeat(s): {', '.join(stale_hb[:5])}",
                ))

        # 6. DB + services diagnostics (from shared DiagBlock registry)
        await _tick("Running DB and service diagnostics...")
        try:
            from cogs.diagnose import DIAG_BLOCKS
            for block in DIAG_BLOCKS:
                if not block.health or block.needs_guild:
                    continue
                try:
                    result = await block.fn(self.bot)
                    for icon_str, label, detail in result.checks:
                        if icon_str == "❌":
                            issues.append(DoctorIssue(
                                category=result.name, icon="🔍",
                                severity=Severity.ERROR,
                                summary=f"{label}: {detail}" if detail else label,
                            ))
                        elif icon_str == "⚠️":
                            issues.append(DoctorIssue(
                                category=result.name, icon="🔍",
                                severity=Severity.WARN,
                                summary=f"{label}: {detail}" if detail else label,
                            ))
                except Exception:
                    pass
        except ImportError:
            pass

        # 7. DB row bloat check
        await _tick("Checking for database bloat...")
        _CLEANUP_TABLES = [
            ("transactions", "ts"),
            ("game_results", "played_at"),
            ("price_candles", "ts"),
        ]
        _CLEANUP_DAYS = 90
        old_row_counts: dict[str, int] = {}
        try:
            for table, col in _CLEANUP_TABLES:
                count = await db.fetch_val(
                    f"SELECT count(*) FROM {table} WHERE guild_id=$1 AND {col} < now() - interval '{_CLEANUP_DAYS} days'",  # noqa: S608
                    gid,
                )
                if count and int(count) > 0:
                    old_row_counts[table] = int(count)
            if old_row_counts:
                total_old = sum(old_row_counts.values())
                summary_parts = ", ".join(f"{t}: {c:,}" for t, c in old_row_counts.items())
                issues.append(DoctorIssue(
                    category="Database", icon="🗑️", severity=Severity.WARN,
                    summary=f"{total_old:,} rows older than {_CLEANUP_DAYS}d ({summary_parts})",
                    repair_id="db_cleanup",
                    repair_ctx={"tables": _CLEANUP_TABLES, "days": _CLEANUP_DAYS, "counts": old_row_counts},
                ))
        except Exception:
            pass

        # 8. Bot permissions (global)
        await _tick("Verifying bot permissions...")
        gp = me.guild_permissions
        perm_missing = []
        if not gp.send_messages:
            perm_missing.append("Send Messages")
        if not gp.embed_links:
            perm_missing.append("Embed Links")
        if not gp.read_message_history:
            perm_missing.append("Read Message History")
        if not gp.manage_webhooks:
            perm_missing.append("Manage Webhooks")
        if not gp.add_reactions:
            perm_missing.append("Add Reactions")
        if perm_missing:
            issues.append(DoctorIssue(
                category="Bot Permissions", icon="🔐", severity=Severity.WARN,
                summary=f"Missing: {', '.join(perm_missing)}",
            ))

        # ── Phase 3: Triage  -  sort and score ───────────────────────────────

        issues.sort(key=lambda i: i.severity, reverse=True)
        score = _health_score(issues)
        repairable = [i for i in issues if i.repair_id]
        non_repairable = [i for i in issues if not i.repair_id and i.severity > Severity.OK]

        elapsed_scan = time.monotonic() - t0

        # If everything is healthy, show clean bill of health
        if not issues:
            clean_embed = self._doctor_embed(
                embed_color,
                phase="Complete",
                phase_icon="✅",
                description=(
                    f"**All systems operational.** No issues detected.\n\n"
                    f"Health Score: {_score_bar(100)}\n\n"
                    f"*Scan completed in {elapsed_scan:.1f}s across 8 subsystems.*"
                ),
                score=100,
            )
            try:
                await msg.edit(embed=clean_embed)
            except Exception:
                await ctx.send(embed=clean_embed)
            return

        # ── Phase 4: Show scan results + ask AI for triage ───────────────────

        # Build issue summary for the embed
        issue_lines = []
        for i in issues:
            sev_icon = _SEV_ICON[i.severity]
            issue_lines.append(f"{sev_icon} {i.icon} **{i.category}** - {i.summary}")

        issue_text = "\n".join(issue_lines[:15])
        if len(issues) > 15:
            issue_text += f"\n*...and {len(issues) - 15} more*"

        triage_desc = (
            f"Found **{len(issues)}** issue(s)  -  "
            f"**{len(repairable)}** auto-fixable, "
            f"**{len(non_repairable)}** manual.\n\n"
            f"Health Score: {_score_bar(score)}\n"
        )

        triage_embed = self._doctor_embed(
            embed_color,
            phase="Triage",
            phase_icon="📋",
            description=triage_desc,
            score=score,
        )
        triage_embed.add_field(
            name="Issues Found",
            value=issue_text[:1024],
            inline=False,
        )

        if repairable:
            triage_embed.add_field(
                name=f"🔧 Repairs Queued ({len(repairable)})",
                value="\n".join(
                    f"  `{r+1}.` {_SEV_ICON[i.severity]} {i.summary}"
                    for r, i in enumerate(repairable[:10])
                )[:1024],
                inline=False,
            )

        # AI triage (non-blocking, best-effort)
        ai_analysis = None
        try:
            heal_cfg = await db.get_heal_ai_config(gid)
            report_sections = [(i.category, f"{_SEV_LABEL[i.severity]}: {i.summary}\n{i.detail}") for i in issues]
            report_text = build_health_report(report_sections)
            ai_analysis = await asyncio.wait_for(
                complete_heal(report_text, heal_cfg),
                timeout=15.0,
            )
        except Exception:
            pass

        if ai_analysis:
            triage_embed.add_field(
                name="🤖 AI Triage",
                value=ai_analysis[:1024],
                inline=False,
            )

        try:
            await msg.edit(embed=triage_embed)
        except Exception:
            msg = await ctx.send(embed=triage_embed)

        # If nothing is repairable, stop here
        if not repairable:
            return

        await asyncio.sleep(1.5)  # brief pause before repairs begin

        # ── Phase 5: Execute repairs  -  live-updating ───────────────────────

        fixed: list[str] = []
        still_broken: list[str] = []
        repair_total = len(repairable)

        for idx, issue in enumerate(repairable, 1):
            # Update embed to show current repair
            repair_lines = []
            for fi in fixed:
                repair_lines.append(f"  ✅  {fi}")
            for bi in still_broken:
                repair_lines.append(f"  ❌  {bi}")
            repair_lines.append(f"  {_SPIN[idx % len(_SPIN)]}  **Repairing:** {issue.summary}")

            repair_embed = self._doctor_embed(
                embed_color,
                phase="Repairing",
                phase_icon="🔧",
                description=(
                    f"Executing repair {idx}/{repair_total}...\n\n"
                    f"{_progress_bar(idx - 1, repair_total)}\n"
                    "```\n"
                    + "\n".join(repair_lines[-8:]) + "\n"
                    "```"
                ),
                score=score,
            )
            try:
                await msg.edit(embed=repair_embed)
            except Exception:
                pass

            # Execute the repair
            result = await self._execute_repair(ctx, issue)

            if result.success:
                fixed.append(f"{issue.icon} {result.message}")
            else:
                still_broken.append(f"{issue.icon} {result.message}")

            await asyncio.sleep(0.3)  # small delay for visual effect

        # ── Phase 6: Final report ────────────────────────────────────────────

        # Recalculate score with fixes applied
        # Simple approximation: bump score based on fixes
        fix_bonus = len(fixed) * 8
        final_score = min(100, score + fix_bonus)

        elapsed_total = time.monotonic() - t0

        # Build final embed
        if not still_broken and not non_repairable:
            status_line = "All issues resolved!"
            final_icon = "✅"
        elif still_broken:
            status_line = f"{len(fixed)} fixed, {len(still_broken)} still broken"
            final_icon = "⚠️"
        else:
            status_line = f"{len(fixed)} fixed, {len(non_repairable)} need manual attention"
            final_icon = "🔧"

        final_embed = self._doctor_embed(
            embed_color,
            phase="Complete",
            phase_icon=final_icon,
            description=(
                f"**{status_line}**\n\n"
                f"Health Score: {_score_bar(final_score)}\n\n"
                f"*Completed in {elapsed_total:.1f}s*"
            ),
            score=final_score,
        )

        if fixed:
            final_embed.add_field(
                name=f"✅ Fixed ({len(fixed)})",
                value="\n".join(fixed)[:1024],
                inline=False,
            )
        if still_broken:
            final_embed.add_field(
                name=f"❌ Still Broken ({len(still_broken)})",
                value="\n".join(still_broken)[:1024],
                inline=False,
            )
        if non_repairable:
            manual_lines = []
            for i in non_repairable[:10]:
                manual_lines.append(f"{_SEV_ICON[i.severity]} {i.icon} {i.summary}")
            if len(non_repairable) > 10:
                manual_lines.append(f"*...and {len(non_repairable) - 10} more*")
            final_embed.add_field(
                name=f"⚠️ Manual Action Required ({len(non_repairable)})",
                value="\n".join(manual_lines)[:1024],
                inline=False,
            )

        if ai_analysis:
            final_embed.add_field(
                name="🤖 AI Summary",
                value=ai_analysis[:800],
                inline=False,
            )

        try:
            await msg.edit(embed=final_embed)
        except Exception:
            await ctx.send(embed=final_embed)

    # ── Doctor embed builder ─────────────────────────────────────────────────

    @staticmethod
    def _doctor_embed(
        color: int,
        *,
        phase: str,
        phase_icon: str,
        description: str,
        score: int | None = None,
    ) -> discord.Embed:
        """Build a doctor-themed embed with consistent styling."""
        embed = (
            card(f"{phase_icon}  Doctor  -  {phase}", description=description, color=color)
            .timestamp(discord.utils.utcnow())
            .footer(
                (
                    f"Health: {score}%" if score is not None
                    else "Discoin Health System"
                )
                + "  |  admin health heal",
            )
            .build()
        )
        return embed

    # ── Repair dispatcher ────────────────────────────────────────────────────

    async def _execute_repair(self, ctx: DiscoContext, issue: DoctorIssue) -> RepairResult:
        """Dispatch and execute a single repair action."""
        guild = ctx.guild
        db = ctx.db
        gid = guild.id

        try:
            rid = issue.repair_id
            rc = issue.repair_ctx

            if rid == "clear_channel":
                col = rc["col"]
                allowed = {c for c, _, _ in _CHANNEL_CHECKS}
                if col not in allowed:
                    return RepairResult(False, f"Unknown channel column: {col}")
                await db.execute(
                    f"UPDATE guild_settings SET {col} = NULL WHERE guild_id = $1",  # noqa: S608
                    gid,
                )
                return RepairResult(True, f"{rc['label']}: cleared stale channel ID")

            elif rid == "clear_webhook":
                await db.execute("DELETE FROM mm_webhooks WHERE guild_id = $1", gid)
                return RepairResult(True, "Removed stale webhook record (run `.admin mmwebhook create`)")

            elif rid == "reconnect_redis":
                bus = getattr(self.bot, "bus", None)
                if bus is None:
                    return RepairResult(False, "Redis bus not configured")
                try:
                    await asyncio.wait_for(bus.close(), timeout=3.0)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(bus.connect(), timeout=5.0)
                    if bus.is_connected:
                        healer = getattr(self.bot, "self_heal", None)
                        if healer:
                            healer._redis_retry_attempt = 0
                        return RepairResult(True, "Redis bus reconnected")
                    return RepairResult(False, "Reconnect did not establish connection (in-memory mode)")
                except asyncio.TimeoutError:
                    return RepairResult(False, "Redis reconnect timed out")

            elif rid == "restart_loop":
                cog_name = rc["cog_name"]
                attr_name = rc["attr_name"]
                loop_label = rc["label"]
                cog = self.bot.get_cog(cog_name)
                if cog is None:
                    return RepairResult(False, f"`{loop_label}`: cog not found")
                attr = getattr(cog, attr_name, None)
                if attr is None or not isinstance(attr, tasks.Loop):
                    return RepairResult(False, f"`{loop_label}`: loop not found")
                if attr.is_running():
                    attr.cancel()
                    await asyncio.sleep(1.0)
                attr.start()
                healer = getattr(self.bot, "self_heal", None)
                if healer:
                    healer._loop_fail_counts.pop(loop_label, None)
                return RepairResult(True, f"`{loop_label}`: restarted")

            elif rid == "restart_scheduler":
                healer = getattr(self.bot, "self_heal", None)
                if healer is None:
                    return RepairResult(False, "Self-heal scheduler not configured")
                healer.start()
                return RepairResult(True, "Self-heal scheduler restarted")

            elif rid == "db_cleanup":
                tables = rc["tables"]
                days = rc["days"]
                counts = rc["counts"]
                deleted_total = 0
                for table, col in tables:
                    if table not in counts:
                        continue
                    result = await db.execute(
                        f"DELETE FROM {table} WHERE guild_id=$1 AND {col} < now() - interval '{days} days'",  # noqa: S608
                        gid,
                    )
                    deleted_total += int(result.split()[-1])
                return RepairResult(True, f"Cleaned {deleted_total:,} old rows (>{days}d)")

            else:
                return RepairResult(False, f"Unknown repair: {rid}")

        except Exception as exc:
            return RepairResult(False, f"{issue.summary}: {exc!s:.60}")

    # ── .health test ─────────────────────────────────────────────────────────

    async def health_test(self, ctx: DiscoContext, *, mode: str = "") -> None:
        """Inject a simulated task-loop failure then verify ,health heal fixes it.

        Usage:
          ,health test           - inject fault, show broken state
          ,health test autofix   - inject fault then immediately run heal
        """

        autofix = "autofix" in mode.lower()

        # ── Step 1: stop any leftover test loop from a previous run ──────────
        if self._test_heal_loop.is_running():
            self._test_heal_loop.cancel()
            await asyncio.sleep(0.1)

        # ── Step 2: arm the trigger and start  -  first tick fires immediately,
        #    raises RuntimeError, loop enters failed() with reconnect=False ────
        self._test_heal_trigger = True
        self._test_heal_loop.start()
        await asyncio.sleep(0.2)  # yield so the event loop processes the exception

        failed  = self._test_heal_loop.failed()
        running = self._test_heal_loop.is_running()

        label = "Health._test_heal_loop"

        if not failed and running:
            # Shouldn't happen, but handle gracefully
            await ctx.reply(
                embed=card(
                    "⚠️ Test Inconclusive",
                    description="The test loop started but did not enter a failed state.\nTry again.",
                    color=C_AMBER,
                ).build(),
                mention_author=False,
            )
            return

        # ── Step 3: surface the exception from the dead task ─────────────────
        inner_task = self._test_heal_loop.get_task()
        exc = inner_task.exception() if (inner_task and not inner_task.cancelled()) else None
        cause = exc.__cause__ if exc else None
        exc_type  = type(exc).__name__  if exc   else "unknown"
        exc_msg   = str(exc)            if exc   else "-"
        cause_str = f"{type(cause).__name__}: {cause}" if cause else "-"

        # ── Step 4: forward to error channel (same path as a real crash) ──────
        if exc is not None:
            await self.bot._post_error(ctx.guild, exc)

        fault_embed = (
            card("🧪 Fault Injected", color=C_ERROR)
            .field("Loop", f"`{label}`", True)
            .field("failed()", f"`{failed}`", True)
            .field("is_running()", f"`{running}`", True)
            .field("Exception", f"`{exc_type}: {exc_msg}`", False)
            .field("Caused by", f"`{cause_str}`", False)
            .description(
                "The loop crashed with a realistic chained exception and was "
                "forwarded to the **error channel** exactly as a real crash would be.\n"
                "The loop is now in **failed** state - exactly what the self-healer "
                "looks for.\n\n"
                + (
                    "Running `health heal` now..."
                    if autofix else
                    f"Run `{ctx.prefix}health heal` to fix it, or use "
                    f"`{ctx.prefix}health test autofix` to do it in one step."
                )
            )
            .build()
        )
        await ctx.reply(embed=fault_embed, mention_author=False)

        if not autofix:
            return

        # ── Step 5 (autofix): run heal and show its result ────────────────────
        await asyncio.sleep(0.5)
        await self.health_heal(ctx)

    # ── .health notify ───────────────────────────────────────────────────────

    async def health_notify(self, ctx: DiscoContext, state: str = "") -> None:
        """Toggle or show self-heal error-channel and DM notifications.

        Usage:
          ,health notify        - show current state
          ,health notify on     - enable notifications
          ,health notify off    - silence notifications
        """

        scheduler = getattr(self.bot, "self_heal", None)
        if scheduler is None:
            await ctx.reply_error("Self-heal scheduler is not running.")
            return

        if state.lower() in ("on", "enable", "1", "true", "yes"):
            scheduler.notify_enabled = True
        elif state.lower() in ("off", "disable", "0", "false", "no"):
            scheduler.notify_enabled = False

        status = "🟢 **on**" if scheduler.notify_enabled else "🔴 **off**"
        embed = (
            card("🔔 Self-Heal Notifications", color=C_SUCCESS if scheduler.notify_enabled else C_AMBER)
            .description(
                f"Notifications are currently {status}.\n\n"
                f"**On:** loop restarts post to the error channel; circuit-breaker trips "
                f"and Redis failures also DM `REPORT_TARGET_USER_ID`.\n"
                f"**Off:** all self-heal alerts are suppressed (Railway logs only).\n\n"
                f"Toggle with `{ctx.prefix}health notify on` / `{ctx.prefix}health notify off`"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── .health analyze ───────────────────────────────────────────────────────

    async def health_analyze(self, ctx: DiscoContext) -> None:
        """Run a health diagnostic and have AI explain every issue with fix steps."""

        guild    = ctx.guild
        db       = ctx.db
        settings = await db.get_guild_settings(guild.id)
        me       = guild.me

        # ── Run the same checks as .health check ──────────────────────────────
        sections: list[tuple[str, str]] = []

        # Feed channels
        ch_lines = []
        for col, label, icon in _CHANNEL_CHECKS:
            ch_id = settings.get(col)
            if not ch_id:
                ch_lines.append(f"{icon} {label}: not set")
                continue
            ch = guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(ch_id)
                except Exception:
                    ch = None
            if ch is None:
                ch_lines.append(f"{icon} {label}: set but channel not found (deleted?)")
                continue
            if isinstance(ch, discord.ForumChannel):
                ch_lines.append(f"{icon} {label}: forum container (should be a thread)")
                continue
            perm_target = ch.parent if isinstance(ch, discord.Thread) and ch.parent else ch
            try:
                perms = perm_target.permissions_for(me)
                issues = []
                if not perms.send_messages:
                    issues.append("no Send Messages")
                if not perms.embed_links:
                    issues.append("no Embed Links")
                if issues:
                    ch_lines.append(f"{icon} {label}: permission issues  -  {', '.join(issues)}")
                else:
                    ch_lines.append(f"{icon} {label}: OK")
            except Exception:
                ch_lines.append(f"{icon} {label}: OK")
        sections.append(("Feed Channels", "\n".join(ch_lines)))

        # Webhook
        wh_row = await db.get_mm_webhook(guild.id)
        if not wh_row:
            sections.append(("MM Webhook", "Not configured"))
        else:
            try:
                await guild.fetch_webhook(int(wh_row["webhook_id"]))
                sections.append(("MM Webhook", "Valid"))
            except discord.NotFound:
                sections.append(("MM Webhook", "Invalid  -  webhook was deleted from Discord"))
            except Exception:
                sections.append(("MM Webhook", "Could not verify"))

        # Redis
        bus = getattr(self.bot, "bus", None)
        redis_status = "connected" if (bus and bus.is_connected) else "OFFLINE"
        sections.append(("Redis Bus", redis_status))

        # Background tasks
        task_lines = []
        for cog_name, cog in list(self.bot.cogs.items()):
            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name, None)
                    if not isinstance(attr, tasks.Loop):
                        continue
                    if getattr(attr, "count", None) == 1:
                        continue
                    if getattr(attr, "_heal_skip", False):
                        continue
                    failed  = attr.failed()  if callable(attr.failed)     else attr.failed
                    running = attr.is_running() if callable(attr.is_running) else attr.is_running
                    label   = f"{cog_name}.{attr_name}"
                    if failed:
                        task_lines.append(f"{label}: FAILED")
                    elif not running:
                        task_lines.append(f"{label}: not running")
                    else:
                        task_lines.append(f"{label}: running")
                except Exception:
                    pass
        sections.append(("Background Tasks", "\n".join(task_lines) if task_lines else "none detected"))

        # AI / self-heal
        key_set = bool(Config.OPENROUTER_API_KEY)
        chat_model = await _resolve_model(db, guild.id, "chat")
        sections.append(("AI Config", f"API key: {'set' if key_set else 'MISSING'}  Chat model: {chat_model}"))
        healer = getattr(self.bot, "self_heal", None)
        sections.append(("Self-Heal Scheduler", "running" if (healer and healer.status()["scheduler_running"]) else "STOPPED"))

        # ── Send to AI ────────────────────────────────────────────────────────
        await ctx.reply("⏳ Analyzing…", mention_author=False)

        heal_cfg = await db.get_heal_ai_config(guild.id)
        report   = build_health_report(sections)

        try:
            analysis = await asyncio.wait_for(
                complete_heal(report, heal_cfg),
                timeout=40.0,
            )
        except asyncio.TimeoutError:
            analysis = None

        color = settings.get("embed_color") or C_SUCCESS

        if not analysis:
            await ctx.reply_error(
                "AI analysis timed out or is unavailable.\n"
                f"Backend: `{heal_cfg['backend']}`  Model: `{heal_cfg['model'] or 'default'}`\n"
                "Configure with `.admin heal backend/model` or check your API key."
            )
            return

        # Build embed  -  split at 1024 chars if needed
        b = card("🤖 Heal Analysis", description=analysis[:2048]).color(color)
        b.field(
            "Provider",
            f"`{heal_cfg['backend']}`  ·  `{heal_cfg['model'] or 'default'}`",
            inline=True,
        )
        b.field(
            "Run Fixes",
            f"`{ctx.prefix or '.'}health heal`",
            inline=True,
        )
        await ctx.reply(embed=b.build(), mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Health(bot))
