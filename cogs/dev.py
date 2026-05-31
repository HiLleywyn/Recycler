# cogs/dev.py
"""Developer-only commands  -  restricted to REPORT_TARGET_USER_ID.

.dev status      -  comprehensive DM diagnostic
.dev config      -  view/set dev settings (status DM interval)
.dev heartbeat   -  show all task loop heartbeats
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import platform
import re
import time
from collections import Counter, defaultdict
from typing import Any

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.scale import to_human as _h
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.error_tracker import ErrorSource, Severity
from core.framework.heartbeat import get_all, get_all_intervals, stale_tasks
from constants.ui import C_AMBER, C_ERROR, C_INFO, C_NEUTRAL, C_PURPLE, C_SUCCESS, C_TEAL
from core.framework.ui import CategoryPaginator, fmt_ts
from core.framework.middleware import guild_only
from core.framework.staff_audit import (
    SCOPE_DEV,
    SEVERITY_INFO,
    build_audit_embeds,
    log_staff_action,
    recent_staff_actions,
)
import psutil
from cogs.diagnose import _run_diagnostics

log = logging.getLogger(__name__)

# ── Server-event JSON helpers ─────────────────────────────────────────────────

# Amounts in server_events are human-readable USD floats (stored as NUMERIC(36,0)).
# Legacy rows may contain raw 10^18-scaled integers, which overflow IEEE 754 JSON
# parsers when emitted as bare numbers.  Anything above $10B is treated as scaled.
_AMT_SCALE_THRESHOLD = 10_000_000_000  # $10 billion


def _event_amount_usd(raw: Any) -> float:
    """Convert a server_event amount column value to a human-readable USD float."""
    if raw is None:
        return 0.0
    if isinstance(raw, int) and raw > _AMT_SCALE_THRESHOLD:
        from core.framework.scale import to_human as _to_human
        return round(_to_human(raw), 2)
    return float(raw)


def _serialize_event(r: dict) -> dict:
    """Return a JSON-safe dict for one server_event row."""
    return {
        "user_id": str(r.get("user_id", "")),
        "event_type": r.get("event_type", ""),
        "summary": r.get("summary", ""),
        "amount_usd": _event_amount_usd(r.get("amount")),
        "ts": r.get("ts"),
    }


# ── Dev-only gate ─────────────────────────────────────────────────────────────

def _require_developer():
    """Only the bot owner/developer can use these commands."""
    async def predicate(ctx: DiscoContext) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        await ctx.reply_error("This command is restricted to the bot developer.")
        return False
    return commands.check(predicate)


# ── Auto-DM interval (env-configurable) ──────────────────────────────────────

_DEFAULT_DM_HOURS = 4.0
_dm_interval_hours: float = float(os.getenv("DEV_STATUS_DM_INTERVAL", _DEFAULT_DM_HOURS))


class Dev(commands.Cog):
    """Bot developer tools  -  restricted to REPORT_TARGET_USER_ID."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._dm_interval_hours = _dm_interval_hours
        self.auto_status_dm.start()

    def cog_unload(self) -> None:
        self.auto_status_dm.cancel()

    # ── Auto status DM ────────────────────────────────────────────────────

    @tasks.loop(hours=_dm_interval_hours)
    async def auto_status_dm(self) -> None:
        """Periodically DM the developer a comprehensive status summary.

        The report is built from three independent signals so a failure in
        one section does not blank the others:
          1. Diagnostic blocks  (`_run_diagnostics`)  -- DB / API / cogs / services
          2. Heartbeat registry (`get_all` + `stale_tasks`) -- task loop liveness
          3. Error tracker      (`bot.errors`)        -- recent severe errors

        New since this rework:
          - module-by-module health rollup so silent breakage in modern
            systems (crafting, dungeons, expeditions, fishing, farming,
            quests, achievements, buddies, NFTs, predictions, chat) is
            visible at a glance instead of buried in heartbeat output;
          - prefix-aware footer (uses the live bot prefix instead of `.dev`);
          - economy sanity check that flags suspicious totals (e.g. a
            net-worth aggregate above $1e15) before they leak into a DM.
        """

        uid = Config.REPORT_TARGET_USER_ID
        if not uid:
            return
        try:
            user = await self.bot.fetch_user(uid)
        except Exception:
            return

        guild_id = self.bot.guilds[0].id if self.bot.guilds else 0
        primary_guild = self.bot.guilds[0] if self.bot.guilds else None
        now = time.time()
        prefix = Config.PREFIX

        # Read-only doctor scan -- reused for both the page-1 health bar
        # and the dedicated Doctor Snapshot section below. We do this up
        # front so the score appears in the very first thing the developer
        # sees in the DM rather than hidden three pages in.
        doctor_issues: list = []
        doctor_score = 100
        if primary_guild is not None:
            try:
                from cogs.health import doctor_quick_scan, _health_score
                doctor_issues = await doctor_quick_scan(self.bot, primary_guild)
                doctor_score = _health_score(doctor_issues)
            except Exception as exc:
                log.warning("[dev] auto_status_dm: doctor scan failed: %s", exc)

        try:
            results = await _run_diagnostics(self.bot, guild_id, "all")
        except Exception as exc:
            log.exception("[dev] auto_status_dm: _run_diagnostics raised: %s", exc)
            try:
                err_embed = (
                    card("❌ Auto-Status: Diagnostics Crashed", color=C_ERROR)
                    .description(
                        f"```\n{type(exc).__name__}: {exc}\n```\n"
                        "The status report could not be generated. Check Railway logs."
                    )
                    .build()
                )
                await user.send(embed=err_embed)
            except Exception:
                pass
            return
        total_pass = sum(1 for r in results for i, _, _ in r.checks if i == "✅")
        total_warn = sum(1 for r in results for i, _, _ in r.checks if i == "⚠️")
        total_fail = sum(1 for r in results for i, _, _ in r.checks if i == "❌")

        stale_list = stale_tasks(max_age=300)
        heartbeats = get_all()
        intervals = get_all_intervals()

        overall = "❌" if total_fail else ("⚠️" if total_warn or stale_list else "✅")
        color = C_SUCCESS if overall == "✅" else (C_AMBER if overall == "⚠️" else C_ERROR)

        proc = psutil.Process()
        mem = proc.memory_info()

        uptime_secs = now - getattr(self.bot, "_start_time", now)
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        mins = int((uptime_secs % 3600) // 60)
        uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"

        embeds: list[discord.Embed] = []

        _bar_full = "█" * (doctor_score // 8)
        _bar = _bar_full + "░" * (12 - len(_bar_full))
        _score_emoji = "🟢" if doctor_score >= 90 else ("🟡" if doctor_score >= 60 else "🔴")

        # ── Page 1: overview + heartbeats + diag fails/warns ─────────────
        _b = card(f"{overall} Auto Status Report", color=color)
        _b.description(
            f"**Health:** {_score_emoji} `{_bar}` **{doctor_score}/100** "
            f"({len(doctor_issues)} doctor issue{'s' if len(doctor_issues) != 1 else ''})\n"
            f"**Uptime:** {uptime_str} · **Latency:** {self.bot.latency * 1000:.0f}ms\n"
            f"**Guilds:** {len(self.bot.guilds)} · "
            f"**Members:** {sum(g.member_count or 0 for g in self.bot.guilds):,}\n"
            f"**Memory:** {mem.rss / 1024 / 1024:.0f} MB · **CPU:** {proc.cpu_percent():.1f}%\n"
            f"**Checks:** {total_pass} pass / {total_warn} warn / {total_fail} fail"
        )

        # Task loops -- interval-aware staleness so an hourly loop isn't
        # flagged at 5min and a 15s loop isn't ignored at 4min.
        hb_problems: list[str] = []
        for name in sorted(heartbeats.keys()):
            age = now - heartbeats[name]
            interval = intervals.get(name, 300)
            warn_at = max(interval * 1.5, 300)
            crit_at = max(interval * 3, 600)
            if age >= warn_at:
                icon = "🟡" if age < crit_at else "🔴"
                hb_problems.append(f"{icon} `{name}` {age:.0f}s ago (every ~{interval:.0f}s)")
        for name in stale_list:
            if name not in heartbeats:
                hb_problems.append(f"🔴 `{name}` never pulsed")

        if hb_problems:
            _b.field("Task Loop Issues", "\n".join(hb_problems[:10])[:1024], False)
        else:
            _b.field("🟢 Task Loops", f"All {len(heartbeats)} loops healthy", False)

        # Diagnostic issues only -- the full diag output is sent as a
        # separate embed below, this one is the at-a-glance summary.
        for result in results:
            fails = [f"❌ {l}: {d}" for i, l, d in result.checks if i == "❌"]
            warns = [f"⚠️ {l}: {d}" for i, l, d in result.checks if i == "⚠️"]
            issues = fails + warns
            if issues:
                _b.field(f"{result.worst} {result.name}", "\n".join(issues[:5])[:1024], False)

        # Recent severe errors grouped by command so the same broken
        # surface doesn't fill the embed with 5 copies of itself.
        tracker = self.bot.errors
        recent_severe = (
            tracker.recent(guild_id, severity=Severity.CRITICAL, limit=10)
            + tracker.recent(guild_id, severity=Severity.HIGH, limit=10)
        )
        if recent_severe:
            seen: dict[str, tuple[int, Any]] = {}
            for e in recent_severe:
                key = e.command or e.source.value
                count, last = seen.get(key, (0, e))
                seen[key] = (count + 1, last)
            err_lines = []
            for key, (count, e) in list(seen.items())[:5]:
                tag = f" ×{count}" if count > 1 else ""
                err_lines.append(f"`{key}`{tag} {e.age_str}: {e.short_message}")
            _b.field("Recent Severe Errors", "\n".join(err_lines)[:1024], False)

        # Self-heal scheduler state
        scheduler = getattr(self.bot, "self_heal", None)
        if scheduler is not None:
            heal_lines = [
                f"Uptime: `{int(scheduler.uptime_seconds // 60)}m`",
                f"Notify: `{'on' if scheduler.notify_enabled else 'off'}`",
                f"Redis retries: `{scheduler._redis_retry_attempt}/{5}`",
            ]
            if scheduler._degraded_loops:
                heal_lines.append(
                    f"🔴 Degraded: `{'`, `'.join(sorted(scheduler._degraded_loops))}`"
                )
            if scheduler._loop_fail_counts:
                heal_lines.append(
                    "Fail counts: " + "  ".join(
                        f"`{k}:{v}`" for k, v in sorted(scheduler._loop_fail_counts.items())
                    )
                )
            sh_icon = "🔴" if scheduler._degraded_loops else ("🟡" if scheduler._redis_retry_attempt else "🟢")
            _b.field(f"{sh_icon} Self-Heal", "\n".join(heal_lines), False)

        _b.footer(
            f"Next report in {self._dm_interval_hours}h "
            f"| {prefix}dev config interval <hours>"
        )
        embeds.append(_b.build())

        # ── Page 2: Doctor snapshot (issues actionable via health heal) ──
        # The same read-only doctor scan ,dev status uses, condensed to a
        # single embed. Sends the developer a concrete "here's what to fix"
        # list every interval instead of a wall of green checkmarks.
        try:
            # Rename to dodge the module-level `Severity` from
            # core.framework.error_tracker -- see dev_status for the full
            # explanation of why the bare `from ... import Severity`
            # raises UnboundLocalError here.
            from cogs.health import _SEV_ICON
            from cogs.health import Severity as DoctorSeverity
            _bd = card(f"🩺 Doctor Snapshot  -  {doctor_score}/100", color=color)
            if not doctor_issues:
                _bd.description("**All systems healthy.** No doctor issues to report.")
            else:
                by_cat: dict[str, list] = {}
                for i in doctor_issues:
                    by_cat.setdefault(i.category, []).append(i)
                summary = {
                    "critical": sum(1 for i in doctor_issues if i.severity == DoctorSeverity.CRITICAL),
                    "error":    sum(1 for i in doctor_issues if i.severity == DoctorSeverity.ERROR),
                    "warn":     sum(1 for i in doctor_issues if i.severity == DoctorSeverity.WARN),
                }
                repairable = sum(1 for i in doctor_issues if i.repair_id)
                _bd.description(
                    f"**{len(doctor_issues)}** issue(s)  -  "
                    f"🚨 {summary['critical']} crit · "
                    f"❌ {summary['error']} err · "
                    f"⚠️ {summary['warn']} warn"
                    + (f"\n**{repairable}** auto-fixable via "
                       f"`{prefix}admin health heal`" if repairable else "")
                )
                for cat, group in list(by_cat.items())[:5]:
                    body_lines = []
                    for i in group[:5]:
                        sev_icon = _SEV_ICON.get(i.severity, "•")
                        fix_tag = " 🔧" if i.repair_id else ""
                        body_lines.append(f"{sev_icon} {i.summary}{fix_tag}")
                    if len(group) > 5:
                        body_lines.append(f"…+{len(group) - 5} more")
                    _bd.field(
                        f"{group[0].icon} {cat}",
                        "\n".join(body_lines)[:1024],
                        False,
                    )
            _bd.footer(f"🔧 = auto-fix · {prefix}admin health heal to repair")
            embeds.append(_bd.build())
        except Exception:
            log.exception("[dev] auto_status_dm: doctor snapshot embed failed")

        # ── Page 3: module health + economy sanity ───────────────────────
        # The module-health table was added because the bot has shipped
        # ~50 cogs since this report was last touched. Older versions only
        # surfaced db/api/cogs problems, leaving silent breakage in player
        # systems (crafting, dungeons, fishing, etc.) invisible until a
        # bug report came in.
        try:
            mod_embed = await self._build_module_health_embed(guild_id, color, now)
            if mod_embed is not None:
                embeds.append(mod_embed)
        except Exception:
            log.exception("[dev] auto_status_dm: module health embed failed")

        try:
            for embed in embeds:
                await user.send(embed=embed)
        except discord.Forbidden:
            log.warning("[dev] Cannot DM developer %d  -  DMs closed", uid)
        except Exception as exc:
            log.warning("[dev] Failed to deliver status DM to %d: %s", uid, exc)

    @auto_status_dm.before_loop
    async def before_auto_status(self) -> None:
        await self.bot.wait_until_ready()
        # Wait for all cog tasks to start
        await asyncio.sleep(30)

    # ── Module table catalog ────────────────────────────────────────────────
    # Single source of truth for the per-module health rollup.
    # Each entry is: (display_label, exact_cog_class_name, primary_table_or_None,
    # pulse_key_or_None, module_flag_or_None). Tables are the actual schema
    # names so the count query never hits a missing relation; cog names match
    # ``self.bot.cogs.keys()`` exactly so the lookup can't false-negative on
    # a substring overlap (the previous version mistook "ChatLevelingAdmin"
    # for "ChatLeveling" and similar). ``module_flag`` is the
    # ``guild_settings.module_*`` column to consult so the rollup distinguishes
    # "cog not loaded" from "admin-disabled" -- the latter shouldn't be red.
    _MODULE_CATALOG: list[tuple[str, str, str | None, str | None, str | None]] = [
        ("Crafting",    "Crafting",         "user_crafting",       None,                  "module_crafting"),
        # Dungeon's class is `Dungeon` but it's registered as `Delve` via
        # `class Dungeon(commands.Cog, name="Delve"):`. Use the registered
        # name -- that's what `bot.cogs.keys()` exposes.
        ("Dungeon",     "Delve",            "dungeon_runs",        None,                  None),
        ("Expeditions", "Expeditions",      "buddy_expeditions",   None,                  None),
        ("Farming",     "Farming",          "farming_harvests",    None,                  "module_farming"),
        ("Fishing",     "Fishing",          "fishing_catches",     None,                  "module_fishing"),
        ("Quests",      "Quests",           "user_quests",         None,                  None),
        ("Achievements","Achievements",     "achievement_progress",None,                  None),
        ("Buddies",     "Buddy",            "cc_buddies",          None,                  None),
        ("NFTs",        "NFTs",             "nfts",                None,                  "module_nft"),
        ("Auctions",    "Auction",          "auction_listings",    None,                  None),
        ("Predictions", "Predictions",      "prediction_markets",  None,                  "module_predictions"),
        ("Chat XP",     "ChatLeveling",     "chat_levels",         None,                  None),
        ("Faucet",      "Faucet",           None,                  "faucet",              "module_faucet"),
        ("Moons",       "Moons",            None,                  "lunar_tick",          None),
        ("Seasons",     "Seasons",          None,                  "season_expiry",       None),
        ("Challenges",  "Challenges",       None,                  "challenge_expiry",    None),
        ("Rugpull",     "Rugpull",          None,                  "rugpull_integrity",   "module_rugpull"),
        ("Backup",      "Backup",           None,                  "backup",              None),
    ]

    @staticmethod
    def _module_state(
        cog_class: str,
        flag_col: str | None,
        loaded_cogs: set[str],
        settings: dict,
    ) -> tuple[str, str]:
        """Return (status_icon, status_label) for a module row.

        Decision matrix (matches ,admin <module> truth table):
          - cog absent              -> 🔴 "cog not loaded"
          - flag exists and is False -> ⚪ "admin-disabled"
          - flag NULL or True or absent -> 🟢 "enabled"
        """
        if cog_class not in loaded_cogs:
            return "🔴", "cog not loaded"
        if flag_col is not None:
            val = settings.get(flag_col)
            # NULL = enabled by default for all admin-toggle modules.
            if val is False:
                return "⚪", "admin-disabled"
        return "🟢", "enabled"

    async def _build_module_health_embed(
        self, guild_id: int, color: int, now: float,
    ) -> discord.Embed | None:
        """Per-module health roll-up for the auto-status DM.

        Each row shows admin/cog state + row counts + recent errors + pulse age.
        Admin-disabled rows are shown as ⚪ and do not contribute to red counts;
        previously they showed up as 🔴 "disabled" which was misleading.
        """
        if guild_id == 0:
            return None
        db = self.bot.db
        tracker = self.bot.errors
        prefix = Config.PREFIX

        try:
            settings = await db.get_guild_settings(guild_id) or {}
        except Exception:
            settings = {}
        loaded_cogs = set(self.bot.cogs.keys())
        heartbeats = get_all()
        intervals = get_all_intervals()
        mod_summary = tracker.module_summary(guild_id) or {}

        lines: list[str] = []
        for label, cog_class, table, pulse_key, flag_col in self._MODULE_CATALOG:
            icon, state_label = self._module_state(cog_class, flag_col, loaded_cogs, settings)
            if state_label == "cog not loaded":
                lines.append(f"{icon} **{label}** -- cog not loaded")
                continue
            if state_label == "admin-disabled":
                lines.append(f"{icon} **{label}** -- admin-disabled")
                continue

            mod_errs = mod_summary.get(cog_class.lower(), 0)

            row_count: int | None = None
            if table:
                try:
                    row_count = await db.fetch_val(
                        f"SELECT count(*) FROM {table} WHERE guild_id=$1",  # noqa: S608
                        guild_id,
                    ) or 0
                except Exception:
                    row_count = None  # missing table on this guild's schema

            pulse_str = ""
            pulse_icon: str | None = None
            if pulse_key:
                last = heartbeats.get(pulse_key)
                if last is None:
                    pulse_str, pulse_icon = " · 🔴 no pulse", "🔴"
                else:
                    age = now - last
                    interval = intervals.get(pulse_key, 3600)
                    if age > max(interval * 3, 600):
                        pulse_str, pulse_icon = f" · 🔴 {age:.0f}s old", "🔴"
                    elif age > max(interval * 1.5, 300):
                        pulse_str, pulse_icon = f" · 🟡 {age:.0f}s old", "🟡"
                    else:
                        pulse_str, pulse_icon = f" · 🟢 {age:.0f}s ago", "🟢"

            count_str = f" · `{row_count:,}` rows" if row_count is not None else ""
            err_str = f" · ⚠️ {mod_errs} err" if mod_errs else ""
            row_icon = "🔴" if (mod_errs > 5 or pulse_icon == "🔴") else (
                "🟡" if (mod_errs or pulse_icon == "🟡") else "🟢"
            )
            lines.append(f"{row_icon} **{label}**{count_str}{pulse_str}{err_str}")

        # Economy sanity: catch quintillion-dollar leaks. Wallet/bank are
        # already-descaled in the users table (`_h` returns floats), but
        # raw 10**18 leaks have happened before, so we double-check here.
        sanity_lines: list[str] = []
        try:
            from core.framework.scale import to_human as _to_human
            wallet_total = float(_to_human(await db.fetch_val(
                "SELECT coalesce(sum(wallet), 0) FROM users WHERE guild_id=$1", guild_id,
            ) or 0))
            bank_total = float(_to_human(await db.fetch_val(
                "SELECT coalesce(sum(bank), 0) FROM users WHERE guild_id=$1", guild_id,
            ) or 0))
            # $1 quadrillion is the trip-wire -- anything above is almost
            # certainly a missed descale, not legitimate inflation.
            QUAD = 1e15
            if wallet_total > QUAD or bank_total > QUAD:
                sanity_lines.append(
                    f"🚨 wallet=`${wallet_total:,.0f}` bank=`${bank_total:,.0f}` "
                    "-- looks like a 10**18 leak, audit before users see it"
                )
            else:
                sanity_lines.append(
                    f"🟢 wallet=`${wallet_total:,.0f}` · bank=`${bank_total:,.0f}`"
                )
        except Exception as exc:
            sanity_lines.append(f"⚠️ wallet/bank query failed: {exc!s:.80}")

        # `card(...)` returns a CardBuilder; the caller expects a real
        # `discord.Embed` (declared in the return type and consumed by
        # `embeds.append(...)` followed by `await user.send(embed=...)`).
        # Skipping `.build()` here meant discord.py tried to serialise the
        # builder and crashed with "'CardBuilder' object has no attribute
        # 'to_dict'" on every ,dev status / auto-DM.
        return (
            card("📦 Module Health", color=color)
            .description("\n".join(lines)[:4000])
            .field("Economy Sanity", "\n".join(sanity_lines)[:1024], False)
            .footer(f"Per-module rollup · {prefix}dev check <system> for detail")
            .build()
        )

    # ── .dev group ────────────────────────────────────────────────────────

    @commands.group(name="dev", invoke_without_command=True)
    @_require_developer()
    async def dev(self, ctx: DiscoContext) -> None:
        """Developer tools. Use .dev help for available commands."""
        p = ctx.prefix or Config.PREFIX
        categories = self._build_dev_categories(p)
        await CategoryPaginator.send(ctx, categories)

    def _build_dev_categories(self, p: str) -> dict[str, list[discord.Embed]]:
        """Build the dev help category dict for the dropdown paginator."""
        def _page(title: str, lines: list[str], color=C_PURPLE) -> discord.Embed:
            _b = card(title, color=color)
            for i in range(0, len(lines), 10):
                chunk = lines[i:i + 10]
                _b.field("\u200b", "\n".join(chunk), False)
            _b.footer(f"Use {p}dev <subcommand> to run any command  -  developer only")
            return _b.build()

        categories: dict[str, list[discord.Embed]] = {

            "🔧 Diagnostics": [_page("🔧 Diagnostics", [
                f"`{p}dev status`  -  full system diagnostic sent via DM (6+ pages)",
                f"`{p}dev heartbeat`  -  all task loop heartbeats and stale tasks",
                f"  Alias: `{p}dev hb`",
                f"`{p}dev check <system>`  -  check one system individually",
                f"  Systems: events, mining, staking, validators, prices,",
                f"           savings, lending, security, faucet, chains, pools, errors",
                f"  Example: `{p}dev check prices`",
                f"  Example: `{p}dev check mining`",
            ])],

            "🐛 Errors": [_page("🐛 Error Tracking", [
                f"`{p}dev errors`  -  error tracker overview",
                f"`{p}dev errors summary`  -  counts by source/severity",
                f"`{p}dev errors cmds [keyword]`  -  command errors",
                f"`{p}dev errors bot [keyword]`  -  bot/loop errors",
                f"`{p}dev errors search <keyword>`  -  full-text search",
                f"`{p}dev errors module <name>`  -  errors for a specific cog",
                f"`{p}dev errors export`  -  download raw error log as JSON",
                f"`{p}dev errors clear`  -  wipe error tracker",
            ])],

            "📜 Logs & Activity": [_page("📜 Logs & Activity", [
                f"`{p}dev log`  -  session log + recent activity summary",
                f"`{p}dev reports dm @user|ID`  -  set report DM recipient",
            ])],

            "🤖 AI Inspection": [_page("🤖 AI Context Inspector", [
                f"`{p}dev aictx [@user|user_id]`  -  view AI context for any user",
                f"`{p}dev guildctx`  -  view the guild-level AI context",
                f"`{p}dev channelctx [#channel]`  -  view channel-level AI context",
            ])],

            "⚙ Config": [_page("⚙ Config", [
                f"`{p}dev config`  -  view dev settings",
                f"`{p}dev config interval <hours>`  -  set auto-DM interval",
                f"`{p}dev config dm on|off`  -  toggle auto-DM reports",
                f"`{p}dev config channel on|off`  -  toggle channel error posting",
            ])],

            "📋 Audit": [_page("📋 Developer Audit Feed", [
                f"`{p}dev audit [limit]`  -  recent staff actions on the dev scope",
                f"  Default limit: 50  -  maximum: 250",
                f"  Example: `{p}dev audit 20`",
                f"  Example: `{p}dev audit 100`",
                f"",
                f"Shows every `,dev` action that's been logged to the unified",
                f"staff audit feed. Use `{p}drs audit` and `{p}admin audit`",
                f"for the other staff surfaces.",
            ])],
        }
        return categories

    # ── .dev help ─────────────────────────────────────────────────────────

    @dev.command(name="help")
    @_require_developer()
    async def dev_help(self, ctx: DiscoContext) -> None:
        """Full developer command reference. Usage: ,dev help"""
        p = ctx.prefix or Config.PREFIX
        categories = self._build_dev_categories(p)
        await CategoryPaginator.send(ctx, categories)

    # ── .dev status ───────────────────────────────────────────────────────

    @dev.command(name="status")
    @_require_developer()
    async def dev_status(self, ctx: DiscoContext) -> None:
        """Run comprehensive diagnostics and send detailed results via DM."""

        async with ctx.typing():
            start = time.monotonic()
            results = await _run_diagnostics(self.bot, ctx.guild.id, "all")
            # Run the doctor scan up front so the health score can appear
            # in the page-1 header. The scan is read-only and shares its
            # check sources with the diag pass we just ran, so it adds
            # roughly 100-200ms vs. running it later.
            doctor_issues: list = []
            doctor_score = 100
            try:
                from cogs.health import doctor_quick_scan, _health_score
                doctor_issues = await doctor_quick_scan(self.bot, ctx.guild)
                doctor_score = _health_score(doctor_issues)
            except Exception as exc:
                log.warning("[dev] dev_status: doctor scan failed: %s", exc)
            elapsed = time.monotonic() - start

        now = time.time()
        heartbeats = get_all()
        stale = stale_tasks(max_age=300)

        total_pass = sum(1 for r in results for i, _, _ in r.checks if i == "✅")
        total_warn = sum(1 for r in results for i, _, _ in r.checks if i == "⚠️")
        total_fail = sum(1 for r in results for i, _, _ in r.checks if i == "❌")
        overall = "❌" if total_fail else ("⚠️" if total_warn or stale else "✅")
        color = C_SUCCESS if overall == "✅" else (C_AMBER if overall == "⚠️" else C_ERROR)

        embeds: list[discord.Embed] = []

        # ── Page 1: System + Process ─────────────────────────────────────
        proc = psutil.Process()
        mem = proc.memory_info()
        cpu_pct = proc.cpu_percent(interval=0.1)
        threads = proc.num_threads()
        try:
            fds = proc.num_fds()
        except AttributeError:
            fds = len(proc.open_files())  # Windows fallback
        disk = psutil.disk_usage("/")
        sys_mem = psutil.virtual_memory()

        uptime_secs = now - getattr(self.bot, "_start_time", now)
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        mins = int((uptime_secs % 3600) // 60)
        uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"

        # 12-segment health bar -- mirrors the ,admin health heal output so
        # the dev report and the player-facing repair UI read the same.
        _bar_full = "█" * (doctor_score // 8)
        _bar_empty = "░" * (12 - len(_bar_full))
        _bar = _bar_full + _bar_empty
        _score_emoji = "🟢" if doctor_score >= 90 else ("🟡" if doctor_score >= 60 else "🔴")

        _b = card(f"{overall} Dev Status  -  {ctx.guild.name}", color=color)
        _b.description(
            f"**Health:** {_score_emoji} `{_bar}` **{doctor_score}/100** "
            f"({len(doctor_issues)} issue{'s' if len(doctor_issues) != 1 else ''})\n"
            f"**Diagnostics:** {total_pass} pass / {total_warn} warn / {total_fail} fail ({elapsed:.1f}s)"
        )
        _b.field("Uptime", f"`{uptime_str}`", True)
        _b.field("Latency", f"`{self.bot.latency * 1000:.0f}ms`", True)
        _b.field("Python", f"`{platform.python_version()}`", True)
        _b.field("Process Memory", f"`{mem.rss / 1024 / 1024:.0f} MB` RSS / `{mem.vms / 1024 / 1024:.0f} MB` VMS", True)
        _b.field("System Memory", f"`{sys_mem.used / 1024**3:.1f}` / `{sys_mem.total / 1024**3:.1f} GB` ({sys_mem.percent}%)", True)
        _b.field("CPU", f"`{cpu_pct:.1f}%` proc / `{psutil.cpu_percent():.1f}%` sys ({psutil.cpu_count()} cores)", True)
        _b.field("Disk", f"`{disk.used / 1024**3:.1f}` / `{disk.total / 1024**3:.1f} GB` ({disk.percent}%)", True)
        _b.field("Threads", f"`{threads}`", True)
        _b.field("Open Files", f"`{fds}`", True)
        embeds.append(_b.build())

        # ── Page 2: Discord + DB ─────────────────────────────────────────
        total_members = sum(g.member_count or 0 for g in self.bot.guilds)
        total_channels = sum(len(g.channels) for g in self.bot.guilds)
        cached_msgs = len(self.bot.cached_messages)

        _b2 = card("Discord & Database", color=color)
        _b2.field("Guilds", f"`{len(self.bot.guilds)}`", True)
        _b2.field("Total Members", f"`{total_members:,}`", True)
        _b2.field("Total Channels", f"`{total_channels:,}`", True)
        _b2.field("Cached Messages", f"`{cached_msgs:,}`", True)
        _b2.field("Loaded Cogs", f"`{len(self.bot.cogs)}`", True)
        _b2.field("Commands", f"`{len(list(self.bot.walk_commands()))}`", True)

        # DB pool stats
        pool = getattr(self.bot.db, "_pool", None)
        if pool:
            _b2.field(
                "DB Pool",
                f"Size: `{pool.get_size()}` / max `{pool.get_max_size()}`\n"
                f"Idle: `{pool.get_idle_size()}` / Used: `{pool.get_size() - pool.get_idle_size()}`",
                True,
            )

        # DB row counts for key tables
        try:
            counts_query = """
                SELECT
                    (SELECT count(*) FROM users) AS users,
                    (SELECT count(*) FROM crypto_prices) AS prices,
                    (SELECT count(*) FROM crypto_holdings) AS holdings,
                    (SELECT count(*) FROM transactions) AS txns,
                    (SELECT count(*) FROM pos_validators) AS validators,
                    (SELECT count(*) FROM mining_rigs) AS rigs,
                    (SELECT count(*) FROM pools) AS pools,
                    (SELECT count(*) FROM economy_snapshots) AS snapshots,
                    (SELECT count(*) FROM hashstones) + (SELECT count(*) FROM lockstones) +
                    (SELECT count(*) FROM vaultstones) + (SELECT count(*) FROM liqstones) AS stones
            """
            row = await self.bot.db.fetch_one(counts_query)
            if row:
                _b2.field(
                    "DB Row Counts",
                    f"Users: `{row['users']:,}` · Prices: `{row['prices']:,}`\n"
                    f"Holdings: `{row['holdings']:,}` · Txns: `{row['txns']:,}`\n"
                    f"Validators: `{row['validators']:,}` · Rigs: `{row['rigs']:,}` · Pools: `{row['pools']:,}`\n"
                    f"Snapshots: `{row['snapshots']:,}` · Stones: `{row['stones']:,}`",
                    False,
                )
        except Exception as exc:
            _b2.field("DB Row Counts", f"Query failed: {exc!s:.80}", False)

        embeds.append(_b2.build())

        # ── Page 3: Task Heartbeats ──────────────────────────────────────
        _b3 = card("💓 Task Heartbeats", color=color)
        intervals = get_all_intervals()
        hb_lines = []
        for name in sorted(heartbeats.keys()):
            age = now - heartbeats[name]
            # Use registered interval for thresholds; fall back to 5m/10m
            interval = intervals.get(name, 300)
            warn_at = max(interval * 1.5, 300)
            crit_at = max(interval * 3, 600)
            icon = "🟢" if age < warn_at else ("🟡" if age < crit_at else "🔴")
            hb_lines.append(f"{icon} `{name}`  -  {age:.0f}s ago")
        if stale:
            for name in stale:
                if name not in heartbeats:
                    hb_lines.append(f"🔴 `{name}`  -  never pulsed")

        # Also show all discord.ext.tasks loops
        task_lines = []
        for cog_name, cog in self.bot.cogs.items():
            for attr_name in dir(cog):
                attr = getattr(cog, attr_name, None)
                if isinstance(attr, tasks.Loop):
                    running = attr.is_running()
                    failed = attr.failed() if callable(attr.failed) else attr.failed
                    if failed:
                        task_lines.append(f"🔴 `{cog_name}.{attr_name}` **FAILED**")
                    elif running:
                        task_lines.append(f"🟢 `{cog_name}.{attr_name}` running")
                    else:
                        # One-shot tasks (count=1) are expected to stop after completion
                        is_one_shot = getattr(attr, 'max', None) == 1 or getattr(attr, '_count', None) == 1
                        if is_one_shot:
                            task_lines.append(f"🟢 `{cog_name}.{attr_name}` completed (one-shot)")
                        else:
                            task_lines.append(f"🟡 `{cog_name}.{attr_name}` stopped")

        _b3.description(
            "**Heartbeat Pulses** (🟢 on time / 🟡 delayed / 🔴 stale)\n"
            + ("\n".join(hb_lines) or "No pulses yet")
        )
        if task_lines:
            _b3.field("Task Loops", "\n".join(task_lines[:20]), False)

        # Self-heal scheduler snapshot
        scheduler = getattr(self.bot, "self_heal", None)
        if scheduler is not None:
            sh_lines = [
                f"Scheduler uptime: `{int(scheduler.uptime_seconds // 60)}m`",
                f"Notifications: `{'on' if scheduler.notify_enabled else 'off'}`",
                f"Redis retry: `{scheduler._redis_retry_attempt}/5`",
            ]
            if scheduler._degraded_loops:
                sh_lines.append("🔴 **Degraded loops:**")
                for dl in sorted(scheduler._degraded_loops):
                    sh_lines.append(f"  `{dl}`")
            else:
                sh_lines.append("🟢 No degraded loops")
            if scheduler._loop_fail_counts:
                sh_lines.append("Restart fail counts: " + "  ".join(
                    f"`{k}`×{v}" for k, v in sorted(scheduler._loop_fail_counts.items())
                ))
            sh_icon = "🔴" if scheduler._degraded_loops else ("🟡" if scheduler._redis_retry_attempt else "🟢")
            _b3.field(f"{sh_icon} Self-Heal Scheduler", "\n".join(sh_lines), False)

        embeds.append(_b3.build())

        # ── Page 4: Errors ───────────────────────────────────────────────
        tracker = self.bot.errors
        gid = ctx.guild.id

        _b4 = card("Errors & Incidents", color=color)
        summary = tracker.summary(gid)
        totals = summary.get("_total", {})
        _b4.field(
            "Error Totals (this guild)",
            f"Low: `{totals.get('low', 0)}` · Med: `{totals.get('medium', 0)}` · "
            f"High: `{totals.get('high', 0)}` · Critical: `{totals.get('critical', 0)}`\n"
            f"Total buffered: `{tracker.total_count(gid)}`",
            False,
        )

        # Errors by command
        cmd_errs = tracker.command_summary(gid)
        if cmd_errs:
            top_cmds = list(cmd_errs.items())[:10]
            _b4.field(
                "Errors by Command",
                "\n".join(f"`{cmd}`: {ct}" for cmd, ct in top_cmds),
                True,
            )

        # Recent HIGH/CRITICAL errors
        recent_high = tracker.recent(gid, severity=Severity.HIGH, limit=5)
        recent_crit = tracker.recent(gid, severity=Severity.CRITICAL, limit=5)
        severe = recent_crit + recent_high
        if severe:
            err_lines = []
            for e in severe[:5]:
                err_lines.append(
                    f"**{e.severity.value.upper()}** `{e.command or e.source.value}` ({e.age_str})\n"
                    f"  {e.error_type}: {e.short_message}"
                )
            _b4.field("Recent Severe Errors", "\n".join(err_lines)[:1024], False)
        else:
            _b4.field("Recent Severe Errors", "None  -  clean run", False)

        # Global errors (guild_id=0)
        global_count = tracker.total_count(0)
        if global_count:
            g_summary = tracker.summary(0).get("_total", {})
            _b4.field(
                "Global Errors (non-guild)",
                f"Total: `{global_count}` · High: `{g_summary.get('high', 0)}` · Crit: `{g_summary.get('critical', 0)}`",
                True,
            )
        embeds.append(_b4.build())

        # ── Page 5: Economy snapshot ─────────────────────────────────────
        try:
            _b5 = card("Economy Snapshot", color=color)

            # Token prices
            prices = await self.bot.db.get_all_prices(gid)
            if prices:
                price_lines = []
                for p in prices[:12]:
                    sym = p["symbol"]
                    price = float(p["price"])
                    high = float(p.get("day_high", 0))
                    low = float(p.get("day_low", 0))
                    vol_str = f" (H: {high:.4f} / L: {low:.4f})" if high else ""
                    price_lines.append(f"`{sym}`: ${price:,.4f}{vol_str}")
                _b5.field("Token Prices", "\n".join(price_lines)[:1024], False)

            # Per-guild user stats
            user_count = await self.bot.db.fetch_val(
                "SELECT count(*) FROM users WHERE guild_id=$1", gid
            )
            total_wallet = _h(await self.bot.db.fetch_val(
                "SELECT coalesce(sum(wallet), 0) FROM users WHERE guild_id=$1", gid
            ))
            total_bank = _h(await self.bot.db.fetch_val(
                "SELECT coalesce(sum(bank), 0) FROM users WHERE guild_id=$1", gid
            ))
            _b5.field("Users", f"`{user_count:,}`", True)
            _b5.field("Total Wallet", f"`${float(total_wallet):,.2f}`", True)
            _b5.field("Total Bank", f"`${float(total_bank):,.2f}`", True)

            # Rollback snapshot system
            try:
                snap_count = await self.bot.db.fetch_val(
                    "SELECT count(*) FROM economy_snapshots WHERE guild_id=$1", gid
                )
                latest_snap = await self.bot.db.fetch_one(
                    "SELECT taken_at FROM economy_snapshots WHERE guild_id=$1 ORDER BY taken_at DESC LIMIT 1", gid
                )
                snap_cog = self.bot.get_cog("Snapshots")
                snap_loop_ok = (
                    snap_cog is not None
                    and hasattr(snap_cog, "snapshot_loop")
                    and snap_cog.snapshot_loop.is_running()
                )
                snap_icon = "🟢" if snap_loop_ok else "🔴"
                if latest_snap:
                    _ta = latest_snap["taken_at"]
                    _ta_ts = _ta if isinstance(_ta, (int, float)) else _ta.timestamp()
                    snap_age = now - _ta_ts
                    m, s = divmod(int(snap_age), 60)
                    h, m = divmod(m, 60)
                    age_str = f"{h}h {m}m {s}s ago" if h else f"{m}m {s}s ago"
                    snap_val = (
                        f"{snap_icon} Loop: `{'running' if snap_loop_ok else 'stopped'}`\n"
                        f"Stored: `{snap_count}` · Latest: `{age_str}`\n"
                        f"Rollback: `.admin rollback [minutes]`"
                    )
                else:
                    snap_val = f"{snap_icon} Loop: `{'running' if snap_loop_ok else 'stopped'}` · No snapshots yet"
                _b5.field("Rollback Snapshots", snap_val, False)
            except Exception as snap_exc:
                _b5.field("Rollback Snapshots", f"Query failed: {snap_exc!s:.100}", False)

            # Active validators
            all_validators = await self.bot.db.get_pos_validators(gid)
            active_v = [v for v in all_validators if v["is_active"]]
            _b5.field("Active Validators", f"`{len(active_v)}` / `{len(all_validators)}` total", True)

            # Chain block heights
            from constants.validators import NET_SHORT
            block_lines = []
            for full_name, short in NET_SHORT.items():
                blk = await self.bot.db.get_latest_chain_block(gid, network=short)
                if blk:
                    age = now - (blk["ts"] if isinstance(blk["ts"], (int, float)) else blk["ts"].timestamp())
                    block_lines.append(f"`{short}` #{blk['block_num']} ({age:.0f}s ago)")
            if block_lines:
                _b5.field("Chain Block Heights", "\n".join(block_lines), True)

            # Market events status
            ev_state = await self.bot.db.get_guild_event(gid)
            ev_settings = await self.bot.db.get_guild_settings(gid)
            events_on = ev_settings.get("module_events", True) if ev_settings else True
            disabled_ev = ev_settings.get("disabled_events", "") if ev_settings else ""
            disabled_count = len(list(filter(None, disabled_ev.split(",")))) if disabled_ev else 0
            freq = float(ev_settings.get("event_frequency") or 0.0005) if ev_settings else 0.0005
            ev_line = f"Module: `{'on' if events_on else 'off'}` · Freq: `{freq:.4f}` · Disabled: `{disabled_count}`"
            if ev_state and ev_state.get("current_event"):
                from cogs.events import MARKET_EVENTS
                ek = ev_state["current_event"]
                ev = MARKET_EVENTS.get(ek, {})
                ev_line += f"\nActive: {ev.get('emoji', '')} **{ev.get('title', ek)}**"
            _b5.field("Market Events", ev_line, False)

            embeds.append(_b5.build())
        except Exception as exc:
            _b5_err = card("Economy Snapshot", color=color)
            _b5_err.field("Error", f"Failed to gather economy data: {exc!s:.200}", False)
            embeds.append(_b5_err.build())

        # ── Page 6: Game Systems ─────────────────────────────────────────
        # Per-module health rollup using the catalog the auto-DM uses, so
        # ,dev status surfaces the same crafting/farming/fishing/etc. state
        # the developer would otherwise have to wait 4 hours for.
        try:
            game_embed = await self._build_module_health_embed(
                ctx.guild.id, color, now,
            )
            if game_embed is not None:
                embeds.append(game_embed)
        except Exception as exc:
            _bg_err = card("Game Systems", color=color)
            _bg_err.field("Error", f"Failed to gather: {exc!s:.200}", False)
            embeds.append(_bg_err.build())

        # ── Page 7: Doctor Snapshot ──────────────────────────────────────
        # Reuses the read-only path of the heal/doctor scan so this report
        # surfaces the same actionable issue list ,admin health heal would
        # surface, with severity, category, and a hint about which entries
        # have an auto-fix available. This was previously a separate command
        # nobody ran -- bringing it into ,dev status closes that gap.
        try:
            # Import as `DoctorSeverity` -- there's already a module-level
            # `Severity` from core.framework.error_tracker, and Python's compiler
            # treats any `from X import Severity` inside this function as a
            # local variable, which shadows the module-level one and raises
            # UnboundLocalError on the next reference if anything earlier in
            # the function touched the name. Renaming the import sidesteps
            # the shadowing entirely.
            from cogs.health import _SEV_ICON
            from cogs.health import Severity as DoctorSeverity
            issues = doctor_issues  # pre-computed at the top of dev_status
            score = doctor_score

            _bdoc = card(
                f"🩺 Doctor Snapshot  -  Score {score}/100",
                color=color,
            )
            if not issues:
                _bdoc.description("**All systems healthy.** No issues to report.")
            else:
                # Group by category so the embed reads as a checklist.
                by_cat: dict[str, list] = {}
                for i in issues:
                    by_cat.setdefault(i.category, []).append(i)

                summary_counts = {
                    "critical": sum(1 for i in issues if i.severity == DoctorSeverity.CRITICAL),
                    "error":    sum(1 for i in issues if i.severity == DoctorSeverity.ERROR),
                    "warn":     sum(1 for i in issues if i.severity == DoctorSeverity.WARN),
                }
                repairable = sum(1 for i in issues if i.repair_id)
                _bdoc.description(
                    f"**{len(issues)}** issue(s)  -  "
                    f"🚨 {summary_counts['critical']} crit · "
                    f"❌ {summary_counts['error']} err · "
                    f"⚠️ {summary_counts['warn']} warn"
                    + (f"  -  **{repairable}** auto-fixable via "
                       f"`{Config.PREFIX}admin health heal`" if repairable else "")
                )
                for cat, group in list(by_cat.items())[:6]:
                    body_lines: list[str] = []
                    for i in group[:6]:
                        sev_icon = _SEV_ICON.get(i.severity, "•")
                        fix_tag = " 🔧" if i.repair_id else ""
                        body_lines.append(f"{sev_icon} {i.summary}{fix_tag}")
                    if len(group) > 6:
                        body_lines.append(f"…+{len(group) - 6} more")
                    _bdoc.field(
                        f"{group[0].icon} {cat}",
                        "\n".join(body_lines)[:1024],
                        False,
                    )
            _bdoc.footer(
                f"Doctor scan -- {Config.PREFIX}admin health heal to repair · "
                f"🔧 = auto-fix available"
            )
            embeds.append(_bdoc.build())
        except Exception as exc:
            _bdoc_err = card("🩺 Doctor Snapshot", color=color)
            _bdoc_err.field("Error", f"Doctor scan failed: {exc!s:.200}", False)
            embeds.append(_bdoc_err.build())

        # ── Page 8+: Full diagnostic results ─────────────────────────────
        _bd = card("🔍 Full Diagnostic Results", color=color)
        for result in results:
            rendered = result.render()
            if len(rendered) > 1024:
                rendered = rendered[:1020] + "..."
            if len(_bd._embed.fields) >= 6:
                embeds.append(_bd.build())
                _bd = card(color=color)
            _bd.field(f"{result.worst} {result.name}", rendered, False)
        embeds.append(_bd.build())

        # ── Per-guild breakdown ──────────────────────────────────────────
        if len(self.bot.guilds) > 1:
            _bg = card("Per-Guild Overview", color=color)
            for g in sorted(self.bot.guilds, key=lambda x: x.member_count or 0, reverse=True)[:10]:
                _bg.field(
                    g.name[:50],
                    f"Members: `{g.member_count or 0}` · Channels: `{len(g.channels)}` · Errors: `{tracker.total_count(g.id)}`",
                    False,
                )
            embeds.append(_bg.build())

        # ── Send as DM ───────────────────────────────────────────────────
        try:
            for embed in embeds:
                await ctx.author.send(embed=embed)
            await ctx.reply_success(f"Comprehensive report ({len(embeds)} pages) sent to your DMs.")
        except discord.Forbidden:
            for i, embed in enumerate(embeds):
                if i == 0:
                    await ctx.reply(embed=embed, mention_author=False)
                else:
                    await ctx.send(embed=embed)

        if ctx.guild:
            await log_staff_action(
                ctx.db,
                scope=SCOPE_DEV,
                guild_id=ctx.guild.id,
                actor_id=ctx.author.id,
                action="status",
                severity=SEVERITY_INFO,
                details=f"pass={total_pass} warn={total_warn} fail={total_fail}",
            )

    # ── .dev heartbeat ────────────────────────────────────────────────────

    @dev.command(name="heartbeat", aliases=["hb"])
    @_require_developer()
    async def dev_heartbeat(self, ctx: DiscoContext) -> None:
        """Show all task loop heartbeat timestamps."""
        heartbeats = get_all()
        intervals = get_all_intervals()
        stale = stale_tasks(max_age=300)
        now = time.time()

        if not heartbeats and not stale:
            await ctx.reply_error("No heartbeat data yet. Task loops may not have fired.")
            return

        lines = []
        for name in sorted(set(list(heartbeats.keys()) + stale)):
            last = heartbeats.get(name)
            if last is None:
                lines.append(f"🔴 `{name}`  -  never pulsed")
            else:
                age = now - last
                interval = intervals.get(name, 300)
                warn_at = max(interval * 1.5, 300)
                crit_at = max(interval * 3, 600)
                status = "🟢" if age < warn_at else ("🟡" if age < crit_at else "🔴")
                lines.append(f"{status} `{name}`  -  {age:.0f}s ago")

        _b = card("💓 Task Heartbeats", color=C_PURPLE)
        _b.description("\n".join(lines))
        _b.footer("🟢 on time | 🟡 delayed | 🔴 stale or never")
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── .dev audit ────────────────────────────────────────────────────────

    @dev.command(name="audit")
    @_require_developer()
    async def dev_audit(self, ctx: DiscoContext, limit: int = 50) -> None:
        """Show recent staff actions logged to the developer scope."""
        if limit < 1 or limit > 250:
            await ctx.reply_error("limit must be between 1 and 250")
            return
        entries = await recent_staff_actions(
            ctx.db,
            guild_id=ctx.guild.id if ctx.guild else None,
            scope=SCOPE_DEV,
            limit=limit,
        )
        pages = build_audit_embeds(entries, scope=SCOPE_DEV, guild=ctx.guild)
        if not pages:
            _b = card("\U0001F4CB Developer Audit", color=C_PURPLE)
            _b.description("No audit entries found for the dev scope.")
            await ctx.reply(embed=_b.build(), mention_author=False)
            return
        if len(pages) > 1:
            await CategoryPaginator.send(ctx, {"\U0001F4CB Developer Audit": pages})
        else:
            await ctx.reply(embed=pages[0], mention_author=False)

    # ── .dev config ───────────────────────────────────────────────────────

    @dev.command(name="config")
    @_require_developer()
    async def dev_config(self, ctx: DiscoContext, key: str = "", value: str = "") -> None:
        """View or set dev configuration.

        .dev config                       -  view all settings
        .dev config interval <hours>      -  set auto-DM interval
        .dev config dm on|off             -  toggle auto-DM reports
        .dev config channel on|off        -  toggle channel error posting
        """
        if not key:
            _b = card("⚙️ Dev Config", color=C_PURPLE)
            _b.field("Auto-DM Interval", f"`{self._dm_interval_hours}` hours", True)
            _dev_id = Config.REPORT_TARGET_USER_ID
            _b.field("Developer ID", f"`{_dev_id}`" if _dev_id else "❌ Not set (`REPORT_TARGET_USER_ID`)", True)
            _b.field("Auto-DM Active", "✅ Running" if self.auto_status_dm.is_running() else "❌ Stopped", True)
            _b.field("Channel Errors", "✅ On" if getattr(self, "_channel_errors", True) else "❌ Off", True)
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        k = key.lower()

        if k == "interval":
            if not value:
                await ctx.reply_error("Usage: `.dev config interval <hours>` (e.g. `4`, `0.5`)")
                return
            try:
                hrs = float(value)
                if hrs < 0.25:
                    await ctx.reply_error("Minimum interval is 0.25 hours (15 minutes).")
                    return
            except ValueError:
                await ctx.reply_error("Interval must be a number.")
                return
            self._dm_interval_hours = hrs
            # Safely restart the task loop
            if self.auto_status_dm.is_running():
                self.auto_status_dm.cancel()
                await asyncio.sleep(0.5)
            self.auto_status_dm.change_interval(hours=hrs)
            if not self.auto_status_dm.is_running():
                self.auto_status_dm.start()
            await ctx.reply_success(f"Auto-DM interval set to **{hrs}** hours.")

        elif k == "dm":
            on = value.lower() in ("on", "true", "1", "yes", "enable")
            off = value.lower() in ("off", "false", "0", "no", "disable")
            if not on and not off:
                await ctx.reply_error("Usage: `.dev config dm on|off`")
                return
            if on:
                if not self.auto_status_dm.is_running():
                    self.auto_status_dm.start()
                await ctx.reply_success("Auto-DM reports **enabled**.")
            else:
                if self.auto_status_dm.is_running():
                    self.auto_status_dm.cancel()
                await ctx.reply_success("Auto-DM reports **disabled**.")

        elif k == "channel":
            on = value.lower() in ("on", "true", "1", "yes", "enable")
            off = value.lower() in ("off", "false", "0", "no", "disable")
            if not on and not off:
                await ctx.reply_error("Usage: `.dev config channel on|off`")
                return
            self._channel_errors = on
            await ctx.reply_success(f"Channel error posting **{'enabled' if on else 'disabled'}**.")

        else:
            _CONFIG_KEYS = ("interval", "dm", "channel")
            available = ", ".join(f"`{k}`" for k in _CONFIG_KEYS)
            await ctx.reply_error(f"Unknown config key `{key}`. Available: {available}")

    # ── .dev reports dm ───────────────────────────────────────────────────

    @dev.command(name="reports")
    @_require_developer()
    async def dev_reports(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Manage the report DM notification recipient.

        .dev reports dm @user|ID  - set who gets new-report DMs
        .dev reports dm reset     - reset to REPORT_TARGET_USER_ID default
        .dev reports dm           - show current recipient
        """
        p = ctx.prefix or Config.PREFIX
        parts = args.strip().split() if args.strip() else []

        if not parts or parts[0].lower() != "dm":
            await ctx.reply_error(
                f"Usage: `{p}dev reports dm @user|<id>` or `{p}dev reports dm reset`"
            )
            return

        if len(parts) < 2:
            val = await ctx.db.get_bot_config("report_dm_recipient_id")
            current_id = int(val) if val else 0
            if current_id:
                desc = f"<@{current_id}> (`{current_id}`)"
            else:
                default_id = Config.REPORT_TARGET_USER_ID
                desc = f"Default - <@{default_id}> (`{default_id}`)" if default_id else "Not configured"
            embed = card("Report DM Recipient", description=desc, color=C_INFO).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        query = parts[1]
        if query.lower() in ("reset", "off", "clear"):
            await ctx.db.set_bot_config("report_dm_recipient_id", "0")
            default_id = Config.REPORT_TARGET_USER_ID
            await ctx.reply_success(
                f"Report DM recipient reset to default (`{default_id}`)."
            )
            return

        mention_match = re.match(r"<@!?(\d+)>", query)
        if mention_match:
            user_id = int(mention_match.group(1))
        elif query.isdigit():
            user_id = int(query)
        else:
            await ctx.reply_error(
                f"Usage: `{p}dev reports dm @user` or `{p}dev reports dm <user_id>`"
            )
            return

        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.NotFound:
            await ctx.reply_error(f"No user found with ID `{user_id}`.")
            return
        except Exception:
            await ctx.reply_error(f"Could not resolve user `{user_id}`.")
            return

        await ctx.db.set_bot_config("report_dm_recipient_id", str(user_id))
        await ctx.reply_success(
            f"Report DM notifications will now be sent to **{user}** (`{user_id}`)."
        )

    # ── .dev check <system> ──────────────────────────────────────────────

    @dev.group(name="check", invoke_without_command=True)
    @_require_developer()
    async def dev_check(self, ctx: DiscoContext) -> None:
        """Check an individual system. Usage: .dev check <system>"""
        p = ctx.prefix or Config.PREFIX
        systems = (
            "events, mining, staking, validators, prices, savings, lending, "
            "security, faucet, chains, pools, errors"
        )
        await ctx.reply_error(
            f"Usage: `{p}dev check <system>`\n"
            f"Systems: {systems}"
        )

    @dev_check.command(name="events")
    @_require_developer()
    async def check_events(self, ctx: DiscoContext) -> None:
        """Check the market events system."""
        from cogs.events import MARKET_EVENTS
        gid = ctx.guild.id
        db = self.bot.db
        now = time.time()

        settings = await db.get_guild_settings(gid)
        events_on = settings.get("module_events", True) if settings else True
        disabled_raw = settings.get("disabled_events", "") if settings else ""
        disabled = set(filter(None, disabled_raw.split(","))) if disabled_raw else set()
        freq = float(settings.get("event_frequency") or 0.0005) if settings else 0.0005
        ev_state = await db.get_guild_event(gid)

        _b = card("📡 Events System Check", color=C_PURPLE)

        # Module status
        _b.field("Module", "🟢 Enabled" if events_on else "🔴 Disabled", True)

        # Frequency
        if freq == 0:
            freq_str = "🔴 Off (0)"
        else:
            approx_h = (1.0 / freq / 3600 * Config.PRICE_TICK_SECONDS)
            freq_str = f"🟢 {freq:.6f}/tick (~{approx_h:.1f}h)"
        _b.field("Frequency", freq_str, True)

        # Event types
        _b.field("Event Types", f"{len(MARKET_EVENTS)} total, {len(disabled)} disabled", True)

        # Active event
        if ev_state and ev_state.get("current_event"):
            ek = ev_state["current_event"]
            ev = MARKET_EVENTS.get(ek, {})
            expires = ev_state.get("event_expires_at")
            remaining = 0
            if expires:
                exp_ts = expires.timestamp() if hasattr(expires, "timestamp") else float(expires)
                remaining = max(0, int(exp_ts - now))
            m, s = divmod(remaining, 60)
            _b.field("Active Event",
                     f"{ev.get('emoji', '')} **{ev.get('title', ek)}**\n"
                     f"Vol: {ev.get('vol_mult', '?')}x · Bias: {float(ev.get('bias', 0))*100:+.1f}%/day · {m}m {s}s left",
                     False)
        else:
            _b.field("Active Event", "None  -  markets calm", False)

        # Disabled list
        if disabled:
            d_lines = []
            for k in sorted(disabled):
                ev = MARKET_EVENTS.get(k)
                d_lines.append(f"🚫 {ev['emoji'] if ev else '?'} `{k}`")
            _b.field("Disabled Events", "\n".join(d_lines)[:1024], False)

        # Events channel
        ch_id = settings.get("events_channel") if settings else None
        if ch_id:
            ch = ctx.guild.get_channel(int(ch_id))
            _b.field("Events Channel", ch.mention if ch else f"⚠️ {ch_id} (not found)", True)
        else:
            _b.field("Events Channel", "Not set (uses crypto channel fallback)", True)

        # AI events
        ai_events = settings.get("ai_events_enabled", True) if settings else True
        _b.field("AI Event Narration", "🟢 On" if ai_events else "🔴 Off", True)

        await ctx.reply(embed=_b.build(), mention_author=False)
        await log_staff_action(
            ctx.db, scope=SCOPE_DEV, guild_id=gid, actor_id=ctx.author.id,
            action="check", severity=SEVERITY_INFO, details="system=events",
        )

    @dev_check.command(name="mining")
    @_require_developer()
    async def check_mining(self, ctx: DiscoContext) -> None:
        """Check the mining system."""
        now = time.time()
        heartbeats = get_all()
        gid = ctx.guild.id

        _b = card("⛏ Mining System Check", color=C_PURPLE)

        # Heartbeat
        for key, label in [("mining_tick", "Mining Tick"), ("chain_tick", "Chain Tick"), ("keeper_loop", "Keeper Loop")]:
            age = (now - heartbeats[key]) if key in heartbeats else None
            icon = "🟢" if age and age < 600 else ("🟡" if age and age < 1200 else "🔴")
            _b.field(label, f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        # PoW chains
        chains = await self.bot.db.get_all_guild_pow_networks(gid)
        if chains:
            for ch in chains:
                sym = ch.get("chain_symbol", "?")
                height = ch.get("block_height", 0)
                diff = float(ch.get("difficulty", 0))
                _b.field(f"Chain: {sym}", f"Block #{height:,} · Diff {diff:,.0f}", True)

        # Active miners
        try:
            rig_count = await self.bot.db.fetch_val(
                "SELECT count(*) FROM mining_rigs WHERE guild_id=$1", gid
            )
            _b.field("Active Rigs", f"`{rig_count or 0}`", True)
        except Exception:
            pass

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="staking")
    @_require_developer()
    async def check_staking(self, ctx: DiscoContext) -> None:
        """Check the staking system."""
        now = time.time()
        heartbeats = get_all()
        gid = ctx.guild.id

        _b = card("🌐 Staking System Check", color=C_PURPLE)

        for key, label in [("staking_tick", "Staking Tick"), ("validator_tick", "Validator Tick"), ("pos_validator_tick", "PoS Validator Tick")]:
            age = (now - heartbeats[key]) if key in heartbeats else None
            icon = "🟢" if age and age < 7200 else ("🟡" if age and age < 14400 else "🔴")
            _b.field(label, f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        validators = await self.bot.db.get_pos_validators(gid)
        active = [v for v in (validators or []) if v.get("is_active")]
        total_staked = sum(_h(v.get("stake_amount", 0)) for v in (validators or []))
        _b.field("Validators", f"Active: `{len(active)}` / `{len(validators or [])}` total", True)
        _b.field("Total Staked", f"`${total_staked:,.2f}`", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="prices")
    @_require_developer()
    async def check_prices(self, ctx: DiscoContext) -> None:
        """Check the price engine."""
        now = time.time()
        heartbeats = get_all()
        gid = ctx.guild.id

        _b = card("📈 Price Engine Check", color=C_PURPLE)

        # `price_drift_trade` is the live key used by trade.py. The legacy
        # `price_drift` key is gone -- listing it would always show 🔴.
        for key, label in [("price_drift_trade", "Price Drift"), ("lp_yield", "LP Yield")]:
            age = (now - heartbeats[key]) if key in heartbeats else None
            warn_at = max(Config.PRICE_TICK_SECONDS * 4, 60) if key == "price_drift_trade" else 7200
            crit_at = warn_at * 2
            icon = "🟢" if age and age < warn_at else ("🟡" if age and age < crit_at else "🔴")
            _b.field(label, f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        prices = await self.bot.db.get_all_prices(gid)
        if prices:
            for p in prices[:12]:
                sym = p["symbol"]
                price = float(p["price"])
                _b.field(sym, f"${price:,.4f}", True)
        _b.field("Total Tokens", f"`{len(prices or [])}`", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="validators")
    @_require_developer()
    async def check_validators(self, ctx: DiscoContext) -> None:
        """Check validators. Alias for dev check staking."""
        await self.check_staking(ctx)

    @dev_check.command(name="savings")
    @_require_developer()
    async def check_savings(self, ctx: DiscoContext) -> None:
        """Check savings and lending systems."""
        now = time.time()
        heartbeats = get_all()

        _b = card("🏦 Savings & Lending Check", color=C_PURPLE)
        for key, label in [("savings_interest", "Savings Interest"), ("loan_interest", "Loan Interest")]:
            age = (now - heartbeats[key]) if key in heartbeats else None
            icon = "🟢" if age and age < 3600 else ("🟡" if age and age < 7200 else "🔴")
            _b.field(label, f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="lending")
    @_require_developer()
    async def check_lending(self, ctx: DiscoContext) -> None:
        """Alias for dev check savings."""
        await self.check_savings(ctx)

    @dev_check.command(name="security")
    @_require_developer()
    async def check_security(self, ctx: DiscoContext) -> None:
        """Check the security monitor."""
        now = time.time()
        heartbeats = get_all()

        _b = card("🛡 Security Check", color=C_PURPLE)
        age = (now - heartbeats["security_scan"]) if "security_scan" in heartbeats else None
        icon = "🟢" if age and age < 300 else ("🟡" if age and age < 600 else "🔴")
        _b.field("Security Scan", f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="faucet")
    @_require_developer()
    async def check_faucet(self, ctx: DiscoContext) -> None:
        """Check the faucet system."""
        now = time.time()
        heartbeats = get_all()

        _b = card("🚰 Faucet Check", color=C_PURPLE)
        age = (now - heartbeats["faucet"]) if "faucet" in heartbeats else None
        icon = "🟢" if age and age < 7200 else ("🟡" if age and age < 14400 else "🔴")
        _b.field("Faucet Loop", f"{icon} {age:.0f}s ago" if age else "🔴 never pulsed", True)

        settings = await self.bot.db.get_guild_settings(ctx.guild.id)
        faucet_on = settings.get("module_faucet", True) if settings else True
        _b.field("Module", "🟢 Enabled" if faucet_on else "🔴 Disabled", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="chains")
    @_require_developer()
    async def check_chains(self, ctx: DiscoContext) -> None:
        """Check chain blocks. Alias for dev check mining."""
        await self.check_mining(ctx)

    @dev_check.command(name="pools")
    @_require_developer()
    async def check_pools(self, ctx: DiscoContext) -> None:
        """Check liquidity pools."""
        gid = ctx.guild.id
        pools = await self.bot.db.get_all_pools(gid)
        prices = await self.bot.db.get_all_prices(gid)
        price_map = {r["symbol"]: float(r["price"]) for r in (prices or [])}

        _b = card("🌊 Pools Check", color=C_PURPLE)
        _b.field("Pool Count", f"`{len(pools or [])}`", True)

        if pools:
            total_tvl = 0.0
            for pool in pools:
                # reserve_a / reserve_b are stored as NUMERIC(36,0) scaled
                # by 10**18; without descaling the TVL prints quintillions.
                ra = _h(pool.get("reserve_a", 0))
                rb = _h(pool.get("reserve_b", 0))
                pa = price_map.get(pool.get("token_a", ""), 0)
                pb = price_map.get(pool.get("token_b", ""), 0)
                total_tvl += ra * pa + rb * pb
            _b.field("Total TVL", f"`${total_tvl:,.2f}`", True)

        await ctx.reply(embed=_b.build(), mention_author=False)

    @dev_check.command(name="errors")
    @_require_developer()
    async def check_errors(self, ctx: DiscoContext) -> None:
        """Quick error summary."""
        tracker = self.bot.errors
        gid = ctx.guild.id
        summary = tracker.summary(gid)
        totals = summary.get("_total", {})

        _b = card("Errors Summary", color=C_PURPLE)
        _b.field("This Guild",
                 f"Low: `{totals.get('low', 0)}` · Med: `{totals.get('medium', 0)}` · "
                 f"High: `{totals.get('high', 0)}` · Crit: `{totals.get('critical', 0)}`\n"
                 f"Total: `{tracker.total_count(gid)}`",
                 False)

        # Global
        g_total = tracker.total_count(0)
        if g_total:
            g_summary = tracker.summary(0).get("_total", {})
            _b.field("Global",
                     f"Total: `{g_total}` · High: `{g_summary.get('high', 0)}` · Crit: `{g_summary.get('critical', 0)}`",
                     False)

        await ctx.reply(embed=_b.build(), mention_author=False)

    # ═════════════════════════════════════════════════════════════════════════
    #  Development-only implementations (formerly admin commands)
    # ══════════════════════════════════════════════════════════════════════════

    # ── .dev log ────────────────────────────────────────────────────────────

    @dev.command(name="log", invoke_without_command=False)
    @_require_developer()
    async def dev_log_impl(self, ctx: DiscoContext) -> None:
        """Upload the session debug log with a parsed summary. Usage: .dev log"""
        from core.framework.session_log import LOG_PATH

        if not LOG_PATH.exists():
            await ctx.reply_error("No session log found. The bot may have just started.")
            return

        loop = asyncio.get_running_loop()
        def _read_log_lines():
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                return f.readlines()
        lines = await loop.run_in_executor(None, _read_log_lines)

        # ── Parse ─────────────────────────────────────────────────────────
        session_start = None
        errors: list[dict] = []          # {ts, cmd, input, etype}
        discord_warns: list[str] = []    # raw warn lines
        cmd_counts: Counter = Counter()
        user_cmd_counts: Counter = Counter()
        valblock: dict = defaultdict(lambda: {"blocks": 0, "confirmed": 0, "rejected": 0, "gas": 0.0})
        chain_blocks: dict = defaultdict(lambda: {"blocks": 0, "txns": 0})
        mempool_counts: Counter = Counter()
        mining_blocks = 0
        mining_reward = 0.0
        event_counts: Counter = Counter()

        i = 0
        _line_re = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[(\w+\s*)\] (.*)$")
        _session_re = re.compile(r"SESSION STARTED\s+(\S+ \S+ UTC)")

        while i < len(lines):
            line = lines[i].rstrip()

            # Session start time
            m = _session_re.search(line)
            if m:
                try:
                    import datetime as _dt
                    session_start = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    pass
                i += 1
                continue

            m = _line_re.match(line)
            if not m:
                i += 1
                continue

            ts, cat, msg = m.group(1), m.group(2).strip(), m.group(3)

            if cat == "CMD":
                arrow = msg.find("→")
                content = msg[arrow + 1:].strip() if arrow != -1 else msg
                cmd_name = content.split()[0].lstrip(".") if content else "?"
                cmd_counts[cmd_name] += 1
                user_part = msg.split("(")[0].strip() if "(" in msg else "?"
                user_cmd_counts[user_part] += 1

            elif cat == "ERR":
                err_entry = {"ts": ts, "cmd": "?", "input": "?", "etype": "?", "tb_lines": []}
                if msg.startswith("cmd="):
                    parts = dict(p.split("=", 1) for p in msg.split("  ") if "=" in p)
                    err_entry["cmd"] = parts.get("cmd", "?")
                elif msg.startswith("input:"):
                    err_entry["input"] = msg[6:].strip()
                elif msg.startswith("type:"):
                    err_entry["etype"] = msg[5:].strip()
                i += 1
                while i < len(lines):
                    next_line = lines[i].rstrip()
                    if next_line.startswith("           "):
                        err_entry["tb_lines"].append(next_line.strip())
                        i += 1
                    elif _line_re.match(next_line):
                        nm = _line_re.match(next_line)
                        if nm and nm.group(2).strip() == "ERR":
                            sub_msg = nm.group(3)
                            if sub_msg.startswith("input:"):
                                err_entry["input"] = sub_msg[6:].strip()
                            elif sub_msg.startswith("type:"):
                                err_entry["etype"] = sub_msg[5:].strip()
                            i += 1
                        else:
                            break
                    else:
                        break
                errors.append(err_entry)
                continue

            elif cat == "DISCORD":
                if "429" in msg or "rate limit" in msg.lower():
                    discord_warns.append(f"`{ts}` {msg[:120]}")

            elif cat == "VALBLOCK":
                net_m = re.search(r"net=([^\s]+(?:\s+[^\s]+)*?)  ", msg)
                ok_m  = re.search(r"✅=(\d+)", msg)
                bad_m = re.search(r"❌=(\d+)", msg)
                gas_m = re.search(r"gas=([\d.e+-]+)", msg)
                if net_m:
                    net = net_m.group(1).strip()
                    valblock[net]["blocks"] += 1
                    valblock[net]["confirmed"] += int(ok_m.group(1)) if ok_m else 0
                    valblock[net]["rejected"]  += int(bad_m.group(1)) if bad_m else 0
                    valblock[net]["gas"]       += float(gas_m.group(1)) if gas_m else 0.0

            elif cat == "CHAIN":
                net_m = re.search(r"net=(\S+)", msg)
                txn_m = re.search(r"txns=(\d+)", msg)
                if net_m:
                    net = net_m.group(1)
                    chain_blocks[net]["blocks"] += 1
                    chain_blocks[net]["txns"] += int(txn_m.group(1)) if txn_m else 0

            elif cat == "MEMPOOL":
                net_m = re.search(r"net=([^\s]+(?:\s+[^\s]+)*?)  ", msg)
                if net_m:
                    mempool_counts[net_m.group(1).strip()] += 1
                else:
                    mempool_counts["unknown"] += 1

            elif cat == "MINING":
                mining_blocks += 1
                rew_m = re.search(r"reward=([\d.]+)", msg)
                if rew_m:
                    mining_reward += float(rew_m.group(1))

            elif cat == "EVENT":
                evt_name = msg.split("  ")[0].strip() if "  " in msg else msg.strip()
                event_counts[evt_name] += 1

            i += 1

        # ── Build embeds ───────────────────────────────────────────────────
        import datetime as _dt
        now_utc = _dt.datetime.utcnow()
        uptime_str = "unknown"
        if session_start:
            delta = now_utc - session_start
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m2, s = divmod(rem, 60)
            uptime_str = f"{h}h {m2}m {s}s" if h else f"{m2}m {s}s"

        from constants.ui import C_WARNING as C_WARN, C_SUCCESS as C_OK, C_ERROR as C_CRIT
        has_issues = bool(errors or discord_warns)

        # ── Embed 1: Overview ─────────────────────────────────────────────
        _e1 = (
            card(
                "📋 Session Log  -  Summary",
                color=C_CRIT if errors else (C_WARN if discord_warns else C_OK),
            )
            .field("Started",  fmt_ts(session_start, "%Y-%m-%d %H:%M UTC") if session_start else "?", True)
            .field("Uptime",   uptime_str,                                                               True)
            .field("Log Size", f"{LOG_PATH.stat().st_size / 1024:.1f} KB",                              True)
        )

        # Commands
        total_cmds = sum(cmd_counts.values())
        if cmd_counts:
            top_cmds = cmd_counts.most_common(8)
            cmd_lines = [f"`{cmd}` ×{n}" for cmd, n in top_cmds]
            if len(cmd_counts) > 8:
                cmd_lines.append(f"…+{len(cmd_counts) - 8} more")
            top_users = user_cmd_counts.most_common(5)
            user_lines = [f"**{u}** ×{n}" for u, n in top_users]
            _e1.field(f"Commands ({total_cmds} total)", "\n".join(cmd_lines), True)
            _e1.field("Top Users", "\n".join(user_lines), True)
        else:
            _e1.field("Commands", "None logged", True)

        # Errors
        if errors:
            err_lines = []
            for e in errors[-6:]:  # last 6 errors
                err_lines.append(f"`{e['ts']}` **{e['cmd']}**  -  {e['etype'][:80]}")
                if e['input'] and e['input'] != '?':
                    err_lines.append(f"-# input: `{e['input'][:60]}`")
            _e1.field(f"⚠️ Errors ({len(errors)})", "\n".join(err_lines)[:1020] or "none", False)

        # Discord warnings
        if discord_warns:
            _e1.field(
                f"🚦 Discord Warnings ({len(discord_warns)})",
                "\n".join(discord_warns[-6:])[:1020],
                False,
            )

        await ctx.reply(embed=_e1.build(), mention_author=False)

        # ── Embed 2: All Activity ────────────────────────────────────────
        _e2 = card("📊 Session Activity", color=C_INFO)

        _ECON_KEYS    = {"daily_claimed", "work_completed", "gamble_result", "deposited",
                         "withdrew", "balance_updated", "balance_transferred", "gift_sent",
                         "drop_claimed", "loan_taken", "loan_repaid", "savings_deposited",
                         "savings_withdrew", "savings_interest"}
        _TRADE_KEYS   = {"buy_executed", "sell_executed", "trade_executed", "swap_executed",
                         "arb_trade", "oracle_rebalance", "token_sent", "transfer"}
        _STAKE_KEYS   = {"staked", "unstaked", "validator_registered", "validator_slashed",
                         "reward_paid", "validator_action"}
        _POOL_KEYS    = {"lp_added", "lp_removed", "pool_created", "pool_seeded"}
        _CONTRACT_KEYS= {"contract_deployed", "contract_called", "contract_event"}
        _CHAIN_KEYS   = {"block_bundled", "validator_block", "mining_block", "block_found",
                         "mempool_submitted"}
        _SKIP_KEYS    = {"prices_updated"}

        econ_events: Counter    = Counter()
        trade_events: Counter   = Counter()
        stake_events: Counter   = Counter()
        pool_events: Counter    = Counter()
        contract_events: Counter= Counter()
        chain_events: Counter   = Counter()
        other_events: Counter   = Counter()

        for evt_name, count in event_counts.items():
            if evt_name in _SKIP_KEYS:
                continue
            elif evt_name in _ECON_KEYS:
                econ_events[evt_name] += count
            elif evt_name in _TRADE_KEYS:
                trade_events[evt_name] += count
            elif evt_name in _STAKE_KEYS:
                stake_events[evt_name] += count
            elif evt_name in _POOL_KEYS:
                pool_events[evt_name] += count
            elif evt_name in _CONTRACT_KEYS:
                contract_events[evt_name] += count
            elif evt_name in _CHAIN_KEYS:
                chain_events[evt_name] += count
            else:
                other_events[evt_name] += count

        def _fmt_event_group(counter: Counter, limit: int = 8) -> str:
            lines = [f"`{e}` ×{n}" for e, n in counter.most_common(limit)]
            if len(counter) > limit:
                lines.append(f"…+{len(counter) - limit} more")
            return "\n".join(lines)

        if econ_events:
            _e2.field(f"💰 Economy ({sum(econ_events.values())} events)", _fmt_event_group(econ_events), True)
        if trade_events:
            _e2.field(f"📈 Trading ({sum(trade_events.values())} events)", _fmt_event_group(trade_events), True)
        if stake_events:
            _e2.field(f"🔒 Staking ({sum(stake_events.values())} events)", _fmt_event_group(stake_events), True)
        if pool_events:
            _e2.field(f"🌊 Pools ({sum(pool_events.values())} events)", _fmt_event_group(pool_events), True)
        if contract_events:
            _e2.field(f"📜 Contracts ({sum(contract_events.values())} events)", _fmt_event_group(contract_events), True)

        if valblock:
            vb_lines = []
            for net, d in sorted(valblock.items()):
                vb_lines.append(
                    f"**{net}**  -  {d['blocks']} block{'s' if d['blocks'] != 1 else ''} · "
                    f"✅ {d['confirmed']} / ❌ {d['rejected']}"
                )
            _e2.field("⛓ Validator Blocks", "\n".join(vb_lines), False)

        if chain_blocks:
            cb_lines = []
            for net, d in sorted(chain_blocks.items()):
                cb_lines.append(f"**{net}**  -  {d['blocks']} block{'s' if d['blocks'] != 1 else ''} · {d['txns']} txns")
            _e2.field("📦 Chain Bundles", "\n".join(cb_lines), True)

        if mempool_counts:
            mp_lines = [f"**{net}** ×{n}" for net, n in sorted(mempool_counts.items())]
            _e2.field(f"🕐 Mempool ({sum(mempool_counts.values())} total)", "\n".join(mp_lines), True)

        if mining_blocks:
            _e2.field(
                "⛏ SUN Mining",
                f"{mining_blocks} block{'s' if mining_blocks != 1 else ''} · {mining_reward:.2f} SUN total",
                True,
            )

        if other_events:
            _e2.field(f"🔹 Other Events ({sum(other_events.values())})", _fmt_event_group(other_events, limit=10), False)

        await ctx.send(embed=_e2.build())

        # ── Attach raw file ───────────────────────────────────────────────
        MAX_BYTES = 24 * 1024 * 1024
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, LOG_PATH.read_bytes)
        if len(raw) > MAX_BYTES:
            raw = raw[-MAX_BYTES:]
            fname = "bot_run_tail.log"
        else:
            fname = "bot_run.log"
        await ctx.send(file=discord.File(io.BytesIO(raw), filename=fname))

    # ── .dev errors ──────────────────────────────────────────────────────────

    @dev.group(name="errors", aliases=["err"], invoke_without_command=True)
    @_require_developer()
    async def dev_errors_main(self, ctx: DiscoContext) -> None:
        """Error tracker  -  developer-only diagnostic tool.

        Usage:
          .dev errors summary           -  overview of all error sources
          .dev errors cmds [keyword]    -  recent command errors
          .dev errors cmdchains         -  recent command chain errors
          .dev errors bot               -  recent bot/event errors
          .dev errors module [name]     -  recent module/cog errors
          .dev errors search <keyword>  -  search all errors
          .dev errors export            -  export all errors as CSV
          .dev errors clear             -  clear all tracked errors
        """
        if ctx.invoked_subcommand is not None:
            return
        p = ctx.prefix or Config.PREFIX
        embed = card("🔍 Error Tracker (Dev)", color=C_INFO)
        embed.description(
            "Developer-only error tracking across all bot subsystems.\n\n"
            f"`{p}dev errors summary`  -  error overview by source & severity\n"
            f"`{p}dev errors cmds [keyword]`  -  recent command errors\n"
            f"`{p}dev errors cmdchains [keyword]`  -  command chain errors\n"
            f"`{p}dev errors bot`  -  internal bot/event errors\n"
            f"`{p}dev errors module [name]`  -  module/cog errors\n"
            f"`{p}dev errors search <keyword>`  -  search all errors\n"
            f"`{p}dev errors export`  -  export all errors as CSV\n"
            f"`{p}dev errors clear`  -  clear all tracked errors"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @dev_errors_main.command(name="summary")
    @_require_developer()
    async def dev_errors_summary_impl(self, ctx: DiscoContext) -> None:
        """Show error summary grouped by source and severity."""
        tracker = self.bot.errors
        stats = tracker.summary(ctx.guild.id)
        total_count = tracker.total_count(ctx.guild.id)

        if not stats or total_count == 0:
            await ctx.reply_error("No errors tracked for this server.")
            return

        _SEV_ICONS = {"info": "🔵", "warning": "🟠", "low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}
        _SRC_ICONS = {
            "cmd": "⌨️", "cmdchain": "⛓️", "bot": "🤖",
            "module": "📦", "service": "⚙️", "task": "🔄",
        }

        lines: list[str] = []
        for src in ErrorSource:
            if src.value not in stats:
                continue
            counts = stats[src.value]
            icon = _SRC_ICONS.get(src.value, "❓")
            total = sum(counts.values())
            sev_parts = []
            for sev in Severity:
                c = counts.get(sev.value, 0)
                if c > 0:
                    sev_parts.append(f"{_SEV_ICONS[sev.value]} {c}")
            line = f"{icon} **{src.value}**  -  {total} error{'s' if total != 1 else ''}"
            if sev_parts:
                line += f"\n-# {' '.join(sev_parts)}"
            lines.append(line)

        mod_stats = tracker.module_summary(ctx.guild.id)
        if mod_stats:
            top_modules = list(mod_stats.items())[:5]
            mod_line = " · ".join(f"`{m}` ({c})" for m, c in top_modules)
            lines.append(f"\n📦 **Top modules:** {mod_line}")

        cmd_stats = tracker.command_summary(ctx.guild.id)
        if cmd_stats:
            top_cmds = list(cmd_stats.items())[:5]
            cmd_line = " · ".join(f"`{c}` ({n})" for c, n in top_cmds)
            lines.append(f"⌨️ **Top commands:** {cmd_line}")

        embed = card("📊 Error Summary", description="\n".join(lines), color=C_INFO).footer(f"{total_count} total errors tracked this session").build()
        await ctx.reply(embed=embed, mention_author=False)

    @dev_errors_main.command(name="cmds")
    @_require_developer()
    async def dev_errors_cmds_impl(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent command execution errors."""
        await self._show_errors_dev(ctx, ErrorSource.CMD, keyword, title="⌨️ Command Errors")

    @dev_errors_main.command(name="cmdchains")
    @_require_developer()
    async def dev_errors_cmdchains_impl(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent command chain errors."""
        await self._show_errors_dev(ctx, ErrorSource.CMDCHAIN, keyword, title="⛓️ Chain Errors")

    @dev_errors_main.command(name="bot")
    @_require_developer()
    async def dev_errors_bot_impl(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent internal bot/event errors."""
        await self._show_errors_dev(ctx, ErrorSource.BOT, keyword, title="🤖 Bot Errors")

    @dev_errors_main.command(name="module")
    @_require_developer()
    async def dev_errors_module_impl(self, ctx: DiscoContext, *, name: str = "") -> None:
        """Show recent module/cog errors, optionally filtered by module name."""
        tracker = self.bot.errors
        results = tracker.recent(
            ctx.guild.id, source=ErrorSource.MODULE,
            module=name, limit=10,
        )
        if not results:
            msg = f"No module errors" + (f" for `{name}`" if name else "") + "."
            await ctx.reply_error(msg)
            return

        lines = self._format_error_list_dev(results, show_module=True)
        embed = card(f"📦 Module Errors" + (f"  -  {name}" if name else ""), description="\n\n".join(lines), color=C_ERROR).footer(f"{len(results)} error(s)").build()
        await ctx.reply(embed=embed, mention_author=False)

    @dev_errors_main.command(name="search")
    @_require_developer()
    async def dev_errors_search_impl(self, ctx: DiscoContext, *, keyword: str) -> None:
        """Search all errors by keyword."""
        tracker = self.bot.errors
        results = tracker.recent(ctx.guild.id, keyword=keyword, limit=10)
        if not results:
            await ctx.reply_error(f"No errors matching `{keyword}`.")
            return

        lines = self._format_error_list_dev(results, show_source=True)
        embed = card(f"🔍 Errors matching \"{keyword}\"", description="\n\n".join(lines), color=C_INFO).footer(f"{len(results)} result(s)").build()
        await ctx.reply(embed=embed, mention_author=False)

    @dev_errors_main.command(name="export")
    @_require_developer()
    async def dev_errors_export_impl(self, ctx: DiscoContext) -> None:
        """Export all tracked errors for this server as a CSV file."""
        import csv as _csv, datetime as _dt

        tracker = self.bot.errors
        results = tracker.recent(ctx.guild.id, limit=500)
        if not results:
            await ctx.reply_error("No errors to export.")
            return
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["Timestamp", "Source", "Severity", "Command", "Module", "Error Type", "Message", "User ID"])
        for e in results:
            ts = _dt.datetime.fromtimestamp(e.timestamp, tz=_dt.timezone.utc).isoformat()
            writer.writerow([
                ts, e.source.value, e.severity.value, e.command,
                e.module, e.error_type, e.message[:500], e.user_id or "",
            ])
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="errors_export.csv")
        await ctx.reply(f"Exported **{len(results)}** errors.", file=file, mention_author=False)

    @dev_errors_main.command(name="clear")
    @_require_developer()
    async def dev_errors_clear_impl(self, ctx: DiscoContext) -> None:
        """Clear all tracked errors for this server."""
        tracker = self.bot.errors
        count = tracker.clear(ctx.guild.id)
        if count == 0:
            await ctx.reply_error("No errors to clear.")
            return
        embed = card("", description=f"🗑️ Cleared **{count}** tracked error{'s' if count != 1 else ''}.", color=C_INFO).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Error display helpers ──────────────────────────────────────────────

    async def _show_errors_dev(
        self,
        ctx: DiscoContext,
        source,
        keyword: str,
        title: str,
    ) -> None:
        """Shared helper to display recent errors for a specific source."""
        tracker = self.bot.errors
        results = tracker.recent(
            ctx.guild.id, source=source, keyword=keyword or "", limit=10,
        )
        if not results:
            msg = "No errors" + (f" matching `{keyword}`" if keyword else "") + "."
            await ctx.reply_error(msg)
            return

        lines = self._format_error_list_dev(results)
        embed = card(title, description="\n\n".join(lines), color=C_ERROR).footer(f"{len(results)} error(s)").build()
        await ctx.reply(embed=embed, mention_author=False)

    @staticmethod
    def _format_error_list_dev(
        results,
        *,
        show_source: bool = False,
        show_module: bool = False,
    ) -> list[str]:
        """Format a list of ErrorRecord objects into display lines."""
        _SEV_ICONS = {"info": "🔵", "warning": "🟠", "low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}
        lines: list[str] = []
        for entry in results:
            sev_icon = _SEV_ICONS.get(entry.severity.value, "❓")
            parts = [f"{sev_icon}"]

            if show_source:
                parts.append(f"**[{entry.source.value}]**")
            if show_module and entry.module:
                parts.append(f"**{entry.module}**")
            if entry.command:
                parts.append(f"`{entry.command}`")

            parts.append(f" -  {entry.age_str}")

            line = " ".join(parts)
            line += f"\n-# `{entry.short_message}`"

            if entry.error_type:
                line += f"\n-# Type: `{entry.error_type}`"
            if entry.user_id:
                line += f" · User: <@{entry.user_id}>"

            lines.append(line)
        return lines

    # ── AI context inspector ──────────────────────────────────────────────────

    @dev.command(name="aictx")
    @_require_developer()
    async def dev_aictx(self, ctx: DiscoContext, user: str = "") -> None:
        """.dev aictx [user|userid]  -  inspect AI memory tables and context for a user.

        Accepts a @mention, username, or raw user ID.

        Shows 4 pages:
          1. Context Preview  -  exact string build_user_context() would inject
          2. Traits           -  ai_user_traits rows by layer with confidence/weight
          3. Memory           -  raw text memory + tool counts + reaction ratios
          4. Events           -  last 10 ai_user_events (raw signal log)

        This command only reads. Nothing shown here feeds any AI call.
        """
        uid: int
        name: str
        if not user:
            uid = ctx.author.id
            name = ctx.author.display_name
        else:
            # Strip mention formatting if present
            raw = user.strip("<@!>")
            if raw.isdigit():
                uid = int(raw)
                member = ctx.guild.get_member(uid) if ctx.guild else None
                name = member.display_name if member else f"User {uid}"
            else:
                # Try to find by name in guild
                if ctx.guild:
                    found = discord.utils.find(
                        lambda m: m.display_name.lower() == user.lower() or m.name.lower() == user.lower(),
                        ctx.guild.members,
                    )
                    if found:
                        uid = found.id
                        name = found.display_name
                    else:
                        await ctx.reply_error(f"Member `{user}` not found. Pass a @mention or user ID.")
                        return
                else:
                    await ctx.reply_error("Pass a user ID in DMs.")
                    return
        gid = ctx.guild.id if ctx.guild else 0

        db = self.bot.db

        # Fetch everything in parallel
        try:
            (
                memory,
                all_traits,
                reaction_rows,
                tool_rows,
                events,
            ) = await asyncio.gather(
                db.get_ai_user_memory(uid, gid),
                db.get_ai_traits(uid, gid, min_confidence=0.0, limit=50),
                db.get_ai_reaction_memory(uid, gid, limit=10),
                db.get_ai_tool_memory(uid, gid, limit=10),
                db.fetch_all(
                    "SELECT event_type, event_subtype, created_at "
                    "FROM ai_user_events WHERE user_id=$1 AND guild_id=$2 "
                    "ORDER BY created_at DESC LIMIT 10",
                    uid, gid,
                ),
            )
        except Exception as exc:
            await ctx.reply_error(f"DB fetch failed: {exc}")
            return

        # ── Page 1: Context Preview ───────────────────────────────────────────
        # Build the exact context string without touching any AI call
        from services.ai_memory import build_user_context
        try:
            ctx_str = await build_user_context(db, uid, gid, name)
        except Exception as exc:
            ctx_str = f"(build_user_context failed: {exc})"

        # Pages are built by the shared inspector renderer so ,disco ctx
        # and ,dev aictx never drift apart.
        from services.ai_context_render import build_aictx_pages

        pages = await build_aictx_pages(db, uid, gid, name)
        await ctx.paginate(pages)

        # DM full raw ctx as a JSON file
        try:
            all_events = await db.fetch_all(
                "SELECT event_type, event_subtype, created_at "
                "FROM ai_user_events WHERE user_id=$1 AND guild_id=$2 "
                "ORDER BY created_at DESC",
                uid, gid,
            )
            raw_payload = {
                "user_id": uid,
                "guild_id": gid,
                "name": name,
                "context_string": ctx_str,
                "memory": memory,
                "traits": [dict(t) for t in all_traits],
                "reaction_rows": [dict(r) for r in reaction_rows],
                "tool_rows": [dict(r) for r in tool_rows],
                "events": [dict(e) for e in all_events],
            }
            raw_bytes = json.dumps(raw_payload, default=str, indent=2).encode()
            fname = f"aictx_{uid}_{gid}.json"
            await ctx.author.send(file=discord.File(io.BytesIO(raw_bytes), filename=fname))
        except Exception:
            pass

    # ── Server events inspector ───────────────────────────────────────────────

    @dev.command(name="guildctx")
    @_require_developer()
    @guild_only
    async def dev_guildctx(self, ctx: DiscoContext) -> None:
        """.dev guildctx  -  show recent server_events for this guild."""
        gid = ctx.guild.id
        rows = await self.bot.db.get_recent_server_events(gid, limit=40)

        if not rows:
            await ctx.reply_error("No server events recorded for this guild yet.")
            return

        pages: list = []
        chunk_size = 5
        chunks = [rows[i : i + chunk_size] for i in range(0, len(rows), chunk_size)]
        total_pages = len(chunks)

        for page_idx, chunk in enumerate(chunks, 1):
            b = card(f"Guild Events ({ctx.guild.name})", color=C_INFO)
            for r in chunk:
                uid_str = f"<@{r['user_id']}>"
                ts_str = fmt_ts(r["ts"])
                amt_usd = _event_amount_usd(r.get("amount"))
                amt_str = f"  ${amt_usd:,.2f}" if amt_usd else ""
                summary = (r.get("summary") or "").strip()
                b.field(
                    f"`{r['event_type']}`  {ts_str}",
                    f"{uid_str}{amt_str}\n{summary[:200] or '(no summary)'}",
                    False,
                )
            b.footer(f"Page {page_idx}/{total_pages}  -  {len(rows)} events total")
            pages.append(b.build())

        await ctx.paginate(pages)

        # DM full raw guild events as a JSON file
        try:
            all_rows = await self.bot.db.get_recent_server_events(gid, limit=1000)
            raw_bytes = json.dumps(
                {
                    "guild_id": str(gid),
                    "guild_name": ctx.guild.name,
                    "events": [_serialize_event(r) for r in all_rows],
                },
                default=str, indent=2,
            ).encode()
            fname = f"guildctx_{gid}.json"
            await ctx.author.send(file=discord.File(io.BytesIO(raw_bytes), filename=fname))
        except Exception:
            pass

    # ── Channel context inspector ─────────────────────────────────────────────

    @dev.command(name="channelctx")
    @_require_developer()
    @guild_only
    async def dev_channelctx(self, ctx: DiscoContext, channel: discord.TextChannel | None = None) -> None:
        """.dev channelctx [#channel]  -  show recent channel_context for a channel.

        Defaults to the current channel if none is specified.
        """
        target = channel or ctx.channel
        gid = ctx.guild.id
        rows = await self.bot.db.get_recent_channel_context(gid, target.id, limit=20)

        if not rows:
            await ctx.reply_error(f"No channel context recorded for {target.mention} yet.")
            return

        pages: list = []
        chunk_size = 5
        chunks = [rows[i : i + chunk_size] for i in range(0, len(rows), chunk_size)]
        total_pages = len(chunks)

        for page_idx, chunk in enumerate(chunks, 1):
            b = card(f"Channel Context: #{target.name}", color=C_TEAL)
            for r in chunk:
                uid_str = f"<@{r['user_id']}>"
                ts_str = fmt_ts(r["ts"])
                target_str = f"  -> <@{r['target_user_id']}>" if r.get("target_user_id") else ""
                content = (r.get("content") or "").strip()
                b.field(
                    f"`{r['event_type']}`  {ts_str}",
                    f"{uid_str}{target_str}\n{content[:200] or '(no content)'}",
                    False,
                )
            b.footer(f"Page {page_idx}/{total_pages}  -  {len(rows)} entries total")
            pages.append(b.build())

        await ctx.paginate(pages)

        # DM full raw channel context as a JSON file
        try:
            all_rows = await self.bot.db.get_recent_channel_context(gid, target.id, limit=500)
            raw_bytes = json.dumps(
                {
                    "guild_id": gid,
                    "channel_id": target.id,
                    "channel_name": target.name,
                    "entries": [dict(r) for r in all_rows],
                },
                default=str, indent=2,
            ).encode()
            fname = f"channelctx_{gid}_{target.id}.json"
            await ctx.author.send(file=discord.File(io.BytesIO(raw_bytes), filename=fname))
        except Exception:
            pass


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Dev(bot))
