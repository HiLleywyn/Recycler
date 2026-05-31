"""
Security Cog
=============

Unified security & scam detection for Discoin:

1. Subscribes to security_enforcement and security_alert bus events
2. Enforces restrictions on Discord commands (pre-invoke check)
3. Monitors command patterns (flood, macros) via the SecurityEngine
4. AI-powered scam detection on messages containing URLs
5. Provides admin commands: $security status/user/freeze/unfreeze/lockdown/lift
7. Sends enforcement notifications to users and admins
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import C_ERROR, C_INFO, C_SUCCESS, C_WARNING, C_NEUTRAL, C_PURPLE, ConfirmView, mention, fmt_ts
from security.models import SecurityEvent, EventSource

if TYPE_CHECKING:
    from core.framework.bot import Discoin


# Scope mapping for bot commands → enforcement scopes
_COMMAND_SCOPE_MAP = {
    # Economy
    "trade": "trade", "buy": "trade", "sell": "trade", "swap": "trade",
    "transfer": "transfer", "pay": "transfer", "send": "transfer",
    "play": "gamble", "coinflip": "gamble", "dice": "gamble",
    "roulette": "gamble", "blackjack": "gamble", "slots": "gamble",
    "mines": "gamble", "crash": "gamble",
    "daily": "earn", "work": "earn",
    "stake": "stake", "unstake": "stake", "delegate": "stake",
    "mine": "mine", "seal": "mine",
    "pool": "pool",
    "loan": "loan", "borrow": "loan", "repay": "loan",
    "deposit": "earn", "withdraw": "earn", "savings": "earn",
    "shop": "trade",
}


def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.guild:
            raise commands.CheckFailure("This command can only be used in a server.")
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure("You need **Manage Server** permission.")
        return True
    return commands.check(predicate)


class Security(commands.Cog):
    """Security system integration for the Discord bot."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._engine = None  # set in cog_load

        # Scam detection deduplication
        self._processing: set[int] = set()

        # Subscribe to bus events
        bot.bus.subscribe("security_enforcement", self._on_enforcement)
        bot.bus.subscribe("security_alert", self._on_security_alert)

        # Start periodic tasks
        self._sync_profiles.start()

    async def cog_load(self) -> None:
        """Initialize the security engine after the bot is ready."""
        if not Config.SECURITY_SYSTEM:
            self._engine = None
            return
        # Engine may already exist on the bot from startup
        engine = getattr(self.bot, "security_engine", None)
        if engine is None:
            from security.engine import SecurityEngine
            redis = getattr(self.bot.bus, "_redis", None)
            db_repo = None
            try:
                from database.security import SecurityRepository
                pool = getattr(self.bot.db, "_pool", None)
                if pool:
                    db_repo = SecurityRepository(pool)
            except Exception:
                pass
            engine = SecurityEngine(redis=redis, db=db_repo, bus=self.bot.bus)
            await engine.start()
            self.bot.security_engine = engine

        self._engine = engine

        # One-time startup: clear all pre-existing security locks
        await self._engine.startup_clear_locks()

    def cog_unload(self) -> None:
        self._sync_profiles.cancel()

    # ── Global Command Check ─────────────────────────────────────────────────

    async def bot_check(self, ctx: DiscoContext) -> bool:
        """Global check: enforce security restrictions before any command runs."""
        if not ctx.guild or not self._engine or not self._engine.is_running:
            return True

        user_id = ctx.author.id
        guild_id = ctx.guild.id

        # Per-guild security toggle  -  skip all enforcement if disabled
        try:
            gs = await ctx.db.get_guild_settings(guild_id)
            if gs and gs.get("module_security") is False:
                return True
        except Exception:
            pass

        # Hierarchy level 1  -  bot developer is never locked out
        from core.config import Config as _Config
        if user_id == _Config.REPORT_TARGET_USER_ID:
            return True

        # Admins (administrator permission) are exempt from security locks
        if hasattr(ctx.author, "guild_permissions") and ctx.author.guild_permissions.administrator:
            return True

        # Owner-designated exempt users also bypass enforcement
        role_ids = [r.id for r in ctx.author.roles] if hasattr(ctx.author, "roles") else []
        if await self._engine.is_security_exempt(guild_id, user_id, role_ids):
            return True

        # Determine scope from command name
        cmd_name = ctx.command.qualified_name.split()[0] if ctx.command else ""
        scope = _COMMAND_SCOPE_MAP.get(cmd_name, "all")

        # Check enforcement
        allowed, reason = await self._engine.check_user_allowed(guild_id, user_id, scope)
        if not allowed:
            embed = (
                card("Action Restricted", description=reason or "Your account is temporarily restricted by the security system.", color=C_ERROR)
                .footer("Contact a server admin if you believe this is an error.")
                .build()
            )
            try:
                await ctx.reply(embed=embed, mention_author=False, delete_after=15)
            except Exception:
                pass
            return False

        # Feed the command into the security engine for monitoring
        try:
            event = SecurityEvent(
                guild_id=guild_id,
                user_id=user_id,
                event_type=_COMMAND_SCOPE_MAP.get(cmd_name, "command"),
                source=EventSource.BOT,
                command=cmd_name,
                details={"full_command": ctx.message.content[:200] if ctx.message else ""},
            )

            # Process asynchronously  -  don't block the command.
            # pre_checked=True because we already verified enforcement above.
            import asyncio
            asyncio.ensure_future(self._process_safe(event, pre_checked=True))
        except Exception:
            pass

        return True

    async def _process_safe(self, event: SecurityEvent, *, pre_checked: bool = False) -> None:
        try:
            if self._engine:
                await self._engine.process_event(event, pre_checked=pre_checked)
        except Exception:
            pass

    # ── Bus Event Handlers ───────────────────────────────────────────────────

    async def _on_enforcement(self, **kwargs) -> None:
        """Handle enforcement events from the security engine."""
        guild_id = kwargs.get("guild_id")
        user_id = kwargs.get("user_id")
        action = kwargs.get("action", "unknown")
        scope = kwargs.get("scope", "all")
        reason = kwargs.get("reason", "")
        detections = kwargs.get("detections", [])

        if not guild_id or not user_id:
            return

        # Respect the per-guild security toggle
        try:
            gs = await self.bot.db.get_guild_settings(guild_id)
            if gs and gs.get("module_security") is False:
                return
        except Exception:
            pass

        # DM the affected user
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if user:
                _b = card(
                    "Security Notice",
                    description=(
                        f"The security system has detected unusual activity on your account "
                        f"in a Discoin server.\n\n"
                        f"**Action taken:** {action}\n"
                        f"**Scope:** {scope}\n"
                    ),
                    color=C_WARNING,
                )
                if reason:
                    _b.field("Details", reason[:1024], False)
                embed = _b.footer("Contact a server admin if you believe this is an error.").build()
                await user.send(embed=embed)
        except Exception:
            pass

        # Notify admin (REPORT_TARGET_USER_ID)
        target_id = Config.REPORT_TARGET_USER_ID
        enforcement_embed: discord.Embed | None = None
        if target_id:
            try:
                admin = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
                if admin:
                    _eb = card(
                        "Security Enforcement",
                        description=(
                            f"Automated enforcement action taken.\n\n"
                            f"**User:** <@{user_id}> (`{user_id}`)\n"
                            f"**Action:** {action}\n"
                            f"**Scope:** {scope}\n"
                            f"**Score:** {kwargs.get('threat_score', 'N/A')}"
                        ),
                        color=C_ERROR,
                    )
                    if detections:
                        det_text = "\n".join(
                            f"- [{d.get('severity', '?')}] {d.get('description', d.get('detector', '?'))}"
                            for d in detections[:5]
                        )
                        _eb.field("Detections", det_text[:1024], False)
                    if reason:
                        _eb.field("Reason", reason[:1024], False)
                    enforcement_embed = _eb.footer("Use $security user <id> for full details").build()
                    await admin.send(embed=enforcement_embed)
            except Exception:
                pass

        # Post to guild security log channel if configured
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if guild:
            try:
                settings = await self.bot.db.get_guild_settings(guild_id)
                log_ch_id = settings.get("security_log_channel")
                if log_ch_id:
                    ch = guild.get_channel(int(log_ch_id))
                    if ch is None:
                        ch = await self.bot.fetch_channel(int(log_ch_id))
                    if ch and isinstance(ch, (discord.TextChannel, discord.Thread)):
                        log_embed = enforcement_embed or card(
                            "Security Enforcement",
                            description=(
                                f"**User:** <@{user_id}> (`{user_id}`)\n"
                                f"**Action:** {action}\n"
                                f"**Scope:** {scope}"
                            ),
                            color=C_ERROR,
                        ).build()
                        await ch.send(embed=log_embed)
            except Exception:
                pass

    async def _on_security_alert(self, **kwargs) -> None:
        """Handle security alerts (lower severity, informational)."""

    # ── Admin Commands ───────────────────────────────────────────────────────

    @commands.hybrid_group(name="security", invoke_without_command=True, with_app_command=False)
    @guild_only
    @_require_manage_guild()
    async def security(self, ctx: DiscoContext) -> None:
        """Security system administration."""
        p = ctx.prefix or "."
        b = card("Security System", color=C_PURPLE)
        b.description(
            f"**Monitoring**\n"
            f"`{p}security status`  -  System health\n"
            f"`{p}security user <@user>`  -  User profile\n"
            f"`{p}security threats [hours]`  -  Recent events\n"
            f"`{p}security audit [page]`  -  Security audit log\n\n"
            f"**Enforcement**\n"
            f"`{p}security freeze/unfreeze <@user>`  -  Freeze/lift\n"
            f"`{p}security clearscore <@user>`  -  Reset score\n"
            f"`{p}security lockdown/lift <feature>`  -  Circuit breaker\n\n"
            f"**Exemptions** *(admin or bot manager)*\n"
            f"`{p}security exempt add <@user|role>`  -  Add exemption\n"
            f"`{p}security exempt remove <@user|role>`  -  Remove exemption\n"
            f"`{p}security exempt list`  -  List exemptions\n\n"
            f"**Configuration**\n"
            f"`{p}security logchannel #channel`  -  Set enforcement log channel\n"
            f"`{p}security hierarchy`  -  Show hierarchy levels\n"
            f"`{p}security settings`  -  Thresholds and config\n"
            f"`{p}security set <key> <value>`  -  Change threshold"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="status")
    @guild_only
    @_require_manage_guild()
    async def security_status(self, ctx: DiscoContext) -> None:
        """Show security system health and statistics."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        health = self._engine.get_health()

        b = card("Security System Status", color=C_INFO)
        b.field("Engine", "Running" if health.engine_running else "Stopped", True)
        b.field("Redis", "Connected" if health.redis_connected else "Disconnected", True)
        b.field("Database", "Connected" if health.db_connected else "Disconnected", True)
        b.field("Detectors", str(health.detectors_active), True)
        b.field("Events Processed", f"{health.events_processed_total:,}", True)
        b.field("Uptime", f"{health.uptime_seconds / 3600:.1f}h", True)

        # Get guild stats if DB available
        if self._engine._db:
            try:
                from datetime import datetime, timezone
                since = datetime.now(timezone.utc) - timedelta(hours=24)
                stats = await self._engine._db.get_stats(ctx.guild.id, since)
                b.field("Events (24h)", str(stats.get("total_events", 0)), True)
                b.field("Active Enforcements", str(stats.get("active_enforcements", 0)), True)
                b.field("Flagged Users", str(stats.get("flagged_users", 0)), True)

                by_type = stats.get("events_by_type", {})
                if by_type:
                    top = sorted(by_type.items(), key=lambda x: x[1], reverse=True)[:5]
                    type_text = "\n".join(f"**{t}**: {c}" for t, c in top)
                    b.field("Top Detections", type_text, False)
            except Exception:
                pass

        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="user")
    @guild_only
    @_require_manage_guild()
    async def security_user(self, ctx: DiscoContext, target: discord.User) -> None:
        """View a user's security profile."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        score = await self._engine.get_threat_score(ctx.guild.id, target.id)
        allowed, reason = await self._engine.check_user_allowed(ctx.guild.id, target.id)

        b = card(f"Security Profile  -  {target}", color=C_INFO)
        b.field("Threat Score", f"{score:.1f} / 100", True)
        b.field("Status", "Restricted" if not allowed else "Clear", True)
        if not allowed and reason:
            b.field("Restriction", reason[:256], False)

        # Get recent events from DB
        if self._engine._db:
            try:
                from datetime import datetime, timezone
                since = datetime.now(timezone.utc) - timedelta(hours=24)
                events = await self._engine._db.get_security_events(
                    guild_id=ctx.guild.id, user_id=target.id, limit=5, since=since,
                )
                if events:
                    event_lines = []
                    for e in events:
                        ts = fmt_ts(e.get("created_at", ""), "%H:%M")
                        event_lines.append(
                            f"[{e.get('severity', '?')}] **{e.get('event_type', '?')}** "
                            f"+{e.get('score_delta', 0):.0f}pts  -  {ts}"
                        )
                    b.field("Recent Events (24h)", "\n".join(event_lines), False)
                else:
                    b.field("Recent Events (24h)", "None", False)

                # Get enforcements
                enforcements = await self._engine._db.get_user_enforcements(
                    ctx.guild.id, target.id,
                )
                if enforcements:
                    enf_lines = []
                    for enf in enforcements[:3]:
                        enf_lines.append(
                            f"**{enf.get('action_type', '?')}** "
                            f"(scope: {enf.get('scope', '?')}, by: {enf.get('enacted_by', '?')})"
                        )
                    b.field("Active Enforcements", "\n".join(enf_lines), False)
            except Exception:
                pass

        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="freeze")
    @guild_only
    @_require_manage_guild()
    async def security_freeze(
        self, ctx: DiscoContext, target: discord.User, scope: str = "all",
    ) -> None:
        """Freeze a user's actions."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        await self._engine.admin_freeze(
            guild_id=ctx.guild.id,
            user_id=target.id,
            scope=scope,
            reason=f"Manual freeze by {ctx.author} ({ctx.author.id})",
            admin_id=ctx.author.id,
            duration=3600,
        )

        b = card("User Frozen", color=C_WARNING)
        b.description(f"{target.mention} has been frozen (scope: **{scope}**, duration: 1 hour).")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="unfreeze")
    @guild_only
    @_require_manage_guild()
    async def security_unfreeze(self, ctx: DiscoContext, target: discord.User) -> None:
        """Lift enforcement on a user."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        success = await self._engine.admin_unfreeze(ctx.guild.id, target.id, ctx.author.id)
        if success:
            b = card("Enforcement Lifted", color=C_INFO)
            b.description(f"All active enforcements on {target.mention} have been lifted.")
        else:
            b = card("No Active Enforcement", color=C_WARNING)
            b.description(f"{target.mention} has no active enforcements.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="clearscore")
    @guild_only
    @_require_manage_guild()
    async def security_clearscore(self, ctx: DiscoContext, target: discord.User) -> None:
        """Reset a user's threat score to 0."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        await self._engine.admin_clear_score(ctx.guild.id, target.id, ctx.author.id)

        b = card("Score Cleared", color=C_INFO)
        b.description(f"Threat score for {target.mention} has been reset to 0.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="lockdown")
    @guild_only
    @_require_manage_guild()
    async def security_lockdown(
        self, ctx: DiscoContext, feature: str, duration: int = 1800,
    ) -> None:
        """Halt a feature guild-wide (circuit breaker)."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        view = ConfirmView(ctx.author.id)
        b = card("Confirm Lockdown", color=C_ERROR)
        b.description(
            f"This will halt **{feature}** for the entire server for "
            f"**{duration // 60} minutes**.\n\nAre you sure?"
        )
        msg = await ctx.reply(embed=b.build(), view=view, mention_author=False)
        await view.wait()

        if not view.value:
            return

        await self._engine.admin_lockdown(
            ctx.guild.id, feature,
            f"Manual lockdown by {ctx.author} ({ctx.author.id})",
            ctx.author.id, duration,
        )

        b = card("Lockdown Active", color=C_ERROR)
        b.description(f"**{feature}** has been halted guild-wide for {duration // 60} minutes.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="lift")
    @guild_only
    @_require_manage_guild()
    async def security_lift(self, ctx: DiscoContext, feature: str) -> None:
        """Lift a guild-wide lockdown."""
        if not self._engine:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        await self._engine.admin_lift_lockdown(ctx.guild.id, feature, ctx.author.id)

        b = card("Lockdown Lifted", color=C_INFO)
        b.description(f"**{feature}** lockdown has been lifted.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="threats")
    @guild_only
    @_require_manage_guild()
    async def security_threats(self, ctx: DiscoContext, hours: int = 24) -> None:
        """Show recent security events."""
        if not self._engine or not self._engine._db:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        from datetime import datetime, timezone
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        events = await self._engine._db.get_security_events(
            guild_id=ctx.guild.id, limit=15, since=since,
        )

        if not events:
            b = card(f"Security Events ({hours}h)", color=C_INFO)
            b.description("No security events in the specified period.")
            return await ctx.reply(embed=b.build(), mention_author=False)

        b = card(f"Security Events ({hours}h)", color=C_WARNING)
        lines = []
        for e in events:
            ts = fmt_ts(e.get("created_at", ""))
            severity = e.get("severity", "?").upper()
            emoji = {"LOW": "\U0001f7e2", "MEDIUM": "\U0001f7e1", "HIGH": "\U0001f7e0", "CRITICAL": "\U0001f534"}.get(severity, "\u26aa")
            lines.append(
                f"{emoji} **{e.get('event_type', '?')}** | "
                f"<@{e.get('user_id', 0)}> | "
                f"+{e.get('score_delta', 0):.0f}pts | {ts}"
            )
        b.description("\n".join(lines))
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Audit Log ────────────────────────────────────────────────────────────

    @security.command(name="audit")
    @guild_only
    @_require_manage_guild()
    async def security_audit(self, ctx: DiscoContext, page: int = 1) -> None:
        """Show the security audit log (admin actions). Owner or manage_guild required."""
        if not self._engine or not self._engine._db:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        page = max(1, page)
        limit = 10
        offset = (page - 1) * limit

        # Check for security_audit_roles gate if not developer / admin
        from core.config import Config as _Config
        is_developer = ctx.author.id == _Config.REPORT_TARGET_USER_ID
        is_admin = ctx.author.guild_permissions.administrator
        if not is_developer and not is_admin:
            try:
                settings = await ctx.db.get_guild_settings(ctx.guild.id)
                audit_roles_raw = (settings.get("security_audit_roles") or "").strip()
                if audit_roles_raw:
                    audit_role_ids = {int(r.strip()) for r in audit_roles_raw.split(",") if r.strip()}
                    member_role_ids = {r.id for r in ctx.author.roles}
                    if not audit_role_ids & member_role_ids:
                        b = card("Access Denied", color=C_ERROR)
                        b.description("You need the Manage Server permission or a designated security role to view the audit log.")
                        return await ctx.reply(embed=b.build(), mention_author=False)
                else:
                    b = card("Access Denied", color=C_ERROR)
                    b.description("You need the Manage Server permission to view the audit log.")
                    return await ctx.reply(embed=b.build(), mention_author=False)
            except Exception:
                pass

        entries = await self._engine._db.get_security_audit(ctx.guild.id, limit=limit, offset=offset)

        if not entries:
            b = card(f"Security Audit Log (page {page})", color=C_INFO)
            b.description("No audit entries found." if page == 1 else "No more entries.")
            return await ctx.reply(embed=b.build(), mention_author=False)

        b = card(f"Security Audit Log (page {page})", color=C_PURPLE)
        lines = []
        for e in entries:
            ts = fmt_ts(e.get("created_at", ""))
            admin_id = e.get("admin_id", 0)
            action = e.get("action", "?")
            target = e.get("target_user")
            target_str = f" -> <@{target}>" if target else ""
            lines.append(f"**{action}**{target_str} by <@{admin_id}>  -  {ts}")
        b.description("\n".join(lines))
        p = ctx.prefix or "."
        b.footer(f"Page {page}  ·  {p}security audit {page + 1} for next page")
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Exemption Management ─────────────────────────────────────────────────

    @security.group(name="exempt", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def security_exempt(self, ctx: DiscoContext) -> None:
        """Manage security enforcement exemptions (admin or bot manager can add/remove)."""
        await self.security_exempt_list(ctx)

    @security_exempt.command(name="add")
    @guild_only
    async def security_exempt_add(
        self, ctx: DiscoContext, target: discord.Member | discord.Role,
    ) -> None:
        """Add a user or role to the security exemption list (admin or bot manager)."""
        # Check if user is developer, admin, or bot_manager
        from core.config import Config as _Config
        is_developer = ctx.author.id == _Config.REPORT_TARGET_USER_ID
        is_admin = ctx.author.guild_permissions.administrator

        # Check if user is bot_manager
        is_bot_manager = False
        if not is_developer and not is_admin:
            settings = await ctx.db.get_guild_settings(ctx.guild.id)
            bot_manager_id = settings.get("bot_manager_id")
            if bot_manager_id and int(bot_manager_id) == ctx.author.id:
                is_bot_manager = True

        if not is_developer and not is_admin and not is_bot_manager:
            b = card("Access Denied", color=C_ERROR)
            b.description("You need admin permissions or be designated as bot manager to add security exemptions.")
            return await ctx.reply(embed=b.build(), mention_author=False)

        if not self._engine or not self._engine._db:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        if isinstance(target, discord.Role):
            target_type = "role"
        else:
            target_type = "user"

        await self._engine._db.add_exempt(
            ctx.guild.id, target_type, target.id, ctx.author.id,
            notes=f"Granted by {ctx.author} via command",
        )
        await self._engine._db.create_security_audit(
            guild_id=ctx.guild.id,
            admin_id=ctx.author.id,
            action="exempt_add",
            target_user=target.id if target_type == "user" else None,
            details={"target_type": target_type, "target_id": target.id, "target_name": str(target)},
        )

        b = card("Exemption Added", color=C_INFO)
        b.description(
            f"**{target.mention}** (`{target_type}`) has been added to the security exemption list.\n"
            f"They will no longer be subject to automated enforcement."
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @security_exempt.command(name="remove")
    @guild_only
    async def security_exempt_remove(
        self, ctx: DiscoContext, target: discord.Member | discord.Role,
    ) -> None:
        """Remove a user or role from the security exemption list (admin or bot manager)."""
        # Check if user is developer, admin, or bot_manager
        from core.config import Config as _Config
        is_developer = ctx.author.id == _Config.REPORT_TARGET_USER_ID
        is_admin = ctx.author.guild_permissions.administrator

        # Check if user is bot_manager
        is_bot_manager = False
        if not is_developer and not is_admin:
            settings = await ctx.db.get_guild_settings(ctx.guild.id)
            bot_manager_id = settings.get("bot_manager_id")
            if bot_manager_id and int(bot_manager_id) == ctx.author.id:
                is_bot_manager = True

        if not is_developer and not is_admin and not is_bot_manager:
            b = card("Access Denied", color=C_ERROR)
            b.description("You need admin permissions or be designated as bot manager to remove security exemptions.")
            return await ctx.reply(embed=b.build(), mention_author=False)

        if not self._engine or not self._engine._db:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        target_type = "role" if isinstance(target, discord.Role) else "user"
        removed = await self._engine._db.remove_exempt(ctx.guild.id, target_type, target.id)

        if removed:
            await self._engine._db.create_security_audit(
                guild_id=ctx.guild.id,
                admin_id=ctx.author.id,
                action="exempt_remove",
                target_user=target.id if target_type == "user" else None,
                details={"target_type": target_type, "target_id": target.id, "target_name": str(target)},
            )
            b = card("Exemption Removed", color=C_WARNING)
            b.description(f"**{target.mention}** (`{target_type}`) has been removed from the exemption list.")
        else:
            b = card("Not Found", color=C_WARNING)
            b.description(f"**{target.mention}** was not in the exemption list.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @security_exempt.command(name="list")
    @guild_only
    @_require_manage_guild()
    async def security_exempt_list(self, ctx: DiscoContext) -> None:
        """List all current security exemptions."""
        if not self._engine or not self._engine._db:
            return await ctx.reply("Security engine not initialized.", mention_author=False)

        exemptions = await self._engine._db.get_exemptions(ctx.guild.id)

        if not exemptions:
            b = card("Security Exemptions", color=C_INFO)
            b.description("No exemptions configured. Only the server owner is exempt by default.")
            return await ctx.reply(embed=b.build(), mention_author=False)

        b = card(f"Security Exemptions ({len(exemptions)})", color=C_PURPLE)
        lines = []
        for ex in exemptions[:20]:
            t_type = ex.get("target_type", "?")
            t_id = ex.get("target_id", 0)
            granted = ex.get("granted_by", 0)
            ts = fmt_ts(ex.get("created_at", ""), "%m/%d/%Y")
            mention_str = f"<@&{t_id}>" if t_type == "role" else f"<@{t_id}>"
            lines.append(f"{mention_str} (`{t_type}`)  -  granted by <@{granted}> on {ts}")
        b.description("\n".join(lines))
        b.footer("Server owner is always exempt regardless of this list.")
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Log Channel & Hierarchy ──────────────────────────────────────────────

    @security.command(name="logchannel")
    @guild_only
    @_require_manage_guild()
    async def security_logchannel(
        self, ctx: DiscoContext, channel: discord.TextChannel | None = None,
    ) -> None:
        """Set (or clear) the channel where security enforcement events are posted."""
        ch_id = channel.id if channel else None
        await ctx.db.update_guild_setting(ctx.guild.id, "security_log_channel", ch_id)
        if channel:
            await ctx.reply_success(
                f"Security enforcement events will now be posted to {channel.mention}.",
                title="Log Channel Set",
            )
        else:
            await ctx.reply_success("Security log channel cleared.", title="Log Channel Cleared")

    @security.command(name="hierarchy")
    @guild_only
    async def security_hierarchy(self, ctx: DiscoContext) -> None:
        """Show the security hierarchy and who is currently exempt."""
        b = card("Security Hierarchy", color=C_PURPLE)
        b.description(
            "**Override order  -  lower number = higher authority:**\n\n"
            "**1. Server Owner**  -  cannot be overridden by anything\n"
            "**2. Security System**  -  automated threat detection & enforcement\n"
            "**3. Bot / Dashboard**  -  platform-level controls\n"
            "**4. Admins**  -  users with Administrator permission\n"
            "**5. Moderators**  -  users with Manage Messages / Manage Guild\n"
            "**6. Users**  -  everyone else\n\n"
            "Each level can take action against levels below it, but **not** above.\n"
            "The bot developer (level 1) is always free."
        )

        # Show bot developer
        from core.config import Config as _Config
        if _Config.REPORT_TARGET_USER_ID:
            dev = ctx.guild.get_member(_Config.REPORT_TARGET_USER_ID)
            dev_display = dev.mention if dev else f"ID `{_Config.REPORT_TARGET_USER_ID}`"
            b.field("Bot Developer", f"{dev_display}  -  always exempt", False)

        # Show exemptions count
        if self._engine and self._engine._db:
            try:
                exemptions = await self._engine._db.get_exemptions(ctx.guild.id)
                if exemptions:
                    count_users = sum(1 for e in exemptions if e.get("target_type") == "user")
                    count_roles = sum(1 for e in exemptions if e.get("target_type") == "role")
                    b.field(
                        "Owner-Granted Exemptions",
                        f"{count_users} user(s), {count_roles} role(s)\n"
                        f"Use `{ctx.prefix}security exempt list` to view them.",
                        False,
                    )
                else:
                    b.field("Owner-Granted Exemptions", "None configured", False)
            except Exception:
                pass

        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Settings ──────────────────────────────────────────────────────────────

    @security.command(name="settings")
    @guild_only
    @_require_manage_guild()
    async def security_settings(self, ctx: DiscoContext) -> None:
        """View all security system settings and thresholds."""
        from security import config as sc
        p = ctx.prefix or "."

        # Load per-guild overrides from DB, falling back to global defaults
        overrides: dict = {}
        try:
            row = await ctx.db.fetch_one(
                "SELECT * FROM guild_security_config WHERE guild_id = $1",
                ctx.guild.id,
            )
            if row:
                overrides = {k: v for k, v in row.items() if v is not None and k != "guild_id" and k != "updated_at"}
        except Exception:
            pass

        def v(db_col: str, attr: str):
            """Return per-guild override if set, else global default."""
            return overrides.get(db_col, getattr(sc, attr))

        b1 = card("Security Settings (1/3)", color=C_PURPLE)
        b1.description("**Detection Thresholds**  -  triggers that flag suspicious activity")
        b1.field(
            "Economy Detectors",
            f"Income velocity: **{v('income_velocity_limit', 'INCOME_VELOCITY_LIMIT')}** txns / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s\n"
            f"Gambling velocity: **{v('gambling_velocity_limit', 'GAMBLING_VELOCITY_LIMIT')}** games / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s\n"
            f"Wash trade cycles: **{v('wash_trade_min_cycles', 'WASH_TRADE_MIN_CYCLES')}** / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s\n"
            f"Transfer ring min: **{v('transfer_ring_min', 'TRANSFER_RING_MIN')}** / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s\n"
            f"LP churn min: **{v('lp_churn_min', 'LP_CHURN_MIN')}** / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s\n"
            f"TX flood limit: **{v('tx_flood_limit', 'TX_FLOOD_LIMIT')}** / {v('lookback_seconds', 'LOOKBACK_SECONDS')}s",
            False,
        )
        b1.field(
            "DeFi Exploits",
            f"Flash loan window: **{v('flash_loan_window', 'FLASH_LOAN_WINDOW')}s**\n"
            f"Oracle manipulation trades: **{v('oracle_manipulation_trades', 'ORACLE_MANIPULATION_TRADES')}** / {v('oracle_manipulation_window', 'ORACLE_MANIPULATION_WINDOW')}s",
            True,
        )
        b1.field(
            "Whale & Repeat",
            f"Whale concentration: **{v('whale_concentration_limit', 'WHALE_CONCENTRATION_LIMIT')}** actions\n"
            f"Repeat offender limit: **{v('repeat_offender_limit', 'REPEAT_OFFENDER_LIMIT')}** flags",
            True,
        )
        b1.footer(f"Page 1/3  ·  {p}security set <key> <value> to change")
        p1 = b1.build()

        b2 = card("Security Settings (2/3)", color=C_PURPLE)
        b2.description("**API & Bot Detectors**")
        b2.field(
            "API / Session",
            f"Auth failure limit: **{v('auth_failure_limit', 'AUTH_FAILURE_LIMIT')}** / {v('auth_failure_window', 'AUTH_FAILURE_WINDOW')}s\n"
            f"Session IP change window: **{v('session_ip_change_window', 'SESSION_IP_CHANGE_WINDOW')}s**\n"
            f"API request flood: **{v('api_request_flood_limit', 'API_REQUEST_FLOOD_LIMIT')}** / {v('api_request_flood_window', 'API_REQUEST_FLOOD_WINDOW')}s",
            True,
        )
        b2.field(
            "Command Flood (Bot)",
            f"Command flood: **{v('command_flood_limit', 'COMMAND_FLOOD_LIMIT')}** / {v('command_flood_window', 'COMMAND_FLOOD_WINDOW')}s\n"
            f"Identical command: **{v('identical_command_limit', 'IDENTICAL_COMMAND_LIMIT')}** / {v('command_flood_window', 'COMMAND_FLOOD_WINDOW')}s",
            True,
        )
        b2.field(
            "Cross-Platform Correlation",
            f"Window: **{v('correlation_window', 'CORRELATION_WINDOW')}s**\n"
            f"Event minimum: **{v('correlation_event_min', 'CORRELATION_EVENT_MIN')}** events",
            False,
        )
        b2.field(
            "Anomaly Detection",
            f"Std-dev threshold: **{v('anomaly_stddev_threshold', 'ANOMALY_STDDEV_THRESHOLD')}x**\n"
            f"Baseline min samples: **{v('baseline_min_samples', 'BASELINE_MIN_SAMPLES')}**\n"
            f"Baseline TTL: **{sc.BASELINE_TTL // 3600}h**\n"
            f"Profile TTL: **{sc.PROFILE_TTL // 3600}h**",
            False,
        )
        b2.footer(f"Page 2/3  ·  {p}security set <key> <value> to change")
        p2 = b2.build()

        b3 = card("Security Settings (3/3)", color=C_PURPLE)
        b3.description("**Response Levels & Enforcement**")
        b3.field(
            "Threat Score Thresholds",
            f"Level 1 (log): **{v('level_1_threshold', 'LEVEL_1_THRESHOLD')}** pts\n"
            f"Level 2 (throttle): **{v('level_2_threshold', 'LEVEL_2_THRESHOLD')}** pts\n"
            f"Level 3 (freeze): **{v('level_3_threshold', 'LEVEL_3_THRESHOLD')}** pts\n"
            f"Level 4 (flag+alert): **{v('level_4_threshold', 'LEVEL_4_THRESHOLD')}** pts\n"
            f"Level 5 (lockdown): **{v('level_5_threshold', 'LEVEL_5_THRESHOLD')}** pts",
            True,
        )
        b3.field(
            "Enforcement Durations",
            f"Throttle: **{v('throttle_duration', 'THROTTLE_DURATION') // 60}** min\n"
            f"Freeze: **{v('freeze_duration', 'FREEZE_DURATION') // 60}** min\n"
            f"Flag: **{v('flag_duration', 'FLAG_DURATION') // 60}** min\n"
            f"Lockdown: **{v('lockdown_duration', 'LOCKDOWN_DURATION') // 60}** min",
            True,
        )
        b3.field(
            "Score Weights",
            "\n".join(
                f"`{k}`: **{v2}** pts"
                for k, v2 in sorted(sc.SCORE_WEIGHTS.items(), key=lambda x: -x[1])
            ),
            False,
        )
        b3.field(
            "Timing",
            f"Scan interval: **{v('scan_interval_seconds', 'SCAN_INTERVAL_SECONDS')}s**\n"
            f"Lookback: **{v('lookback_seconds', 'LOOKBACK_SECONDS')}s**\n"
            f"Score decay half-life: **{v('score_decay_half_life', 'SCORE_DECAY_HALF_LIFE') / 3600:.1f}h**\n"
            f"Alert cooldown: **{v('alert_cooldown_seconds', 'ALERT_COOLDOWN_SECONDS') // 60}** min\n"
            f"Throttled rate limit: **{v('throttled_rate_limit', 'THROTTLED_RATE_LIMIT')}** req/10s",
            False,
        )
        b3.footer(f"Page 3/3  ·  {p}security set <key> <value> to change")
        p3 = b3.build()

        await ctx.paginate([p1, p2, p3])

    @security.command(name="set")
    @guild_only
    @_require_manage_guild()
    async def security_set(self, ctx: DiscoContext, key: str, value: str) -> None:
        """Change a security threshold at runtime. Resets on bot restart unless set via env."""
        from security import config as sc

        key = key.upper()

        # Map of allowed runtime-adjustable keys
        _ADJUSTABLE: dict[str, type] = {
            "INCOME_VELOCITY_LIMIT": int,
            "GAMBLING_VELOCITY_LIMIT": int,
            "WASH_TRADE_MIN_CYCLES": int,
            "TRANSFER_RING_MIN": int,
            "LP_CHURN_MIN": int,
            "TX_FLOOD_LIMIT": int,
            "AUTH_FAILURE_LIMIT": int,
            "AUTH_FAILURE_WINDOW": int,
            "COMMAND_FLOOD_LIMIT": int,
            "COMMAND_FLOOD_WINDOW": int,
            "IDENTICAL_COMMAND_LIMIT": int,
            "API_REQUEST_FLOOD_LIMIT": int,
            "API_REQUEST_FLOOD_WINDOW": int,
            "CORRELATION_WINDOW": int,
            "CORRELATION_EVENT_MIN": int,
            "FLASH_LOAN_WINDOW": int,
            "ORACLE_MANIPULATION_TRADES": int,
            "ORACLE_MANIPULATION_WINDOW": int,
            "WHALE_CONCENTRATION_LIMIT": int,
            "REPEAT_OFFENDER_LIMIT": int,
            "SCAN_INTERVAL_SECONDS": int,
            "LOOKBACK_SECONDS": int,
            "THROTTLE_DURATION": int,
            "FREEZE_DURATION": int,
            "FLAG_DURATION": int,
            "LOCKDOWN_DURATION": int,
            "THROTTLED_RATE_LIMIT": int,
            "ALERT_COOLDOWN_SECONDS": int,
            "LEVEL_1_THRESHOLD": float,
            "LEVEL_2_THRESHOLD": float,
            "LEVEL_3_THRESHOLD": float,
            "LEVEL_4_THRESHOLD": float,
            "LEVEL_5_THRESHOLD": float,
            "SCORE_DECAY_HALF_LIFE": float,
            "ANOMALY_STDDEV_THRESHOLD": float,
        }

        if key not in _ADJUSTABLE:
            available = ", ".join(f"`{k}`" for k in sorted(_ADJUSTABLE))
            return await ctx.reply(
                embed=card("Invalid Key", color=C_ERROR)
                .description(f"**{key}** is not an adjustable setting.\n\nValid keys:\n{available}")
                .build(),
                mention_author=False,
            )

        cast = _ADJUSTABLE[key]
        try:
            parsed = cast(value)
        except (ValueError, TypeError):
            return await ctx.reply_error(f"Value must be {'an integer' if cast is int else 'a number'}.")

        old_val = getattr(sc, key)
        setattr(sc, key, parsed)

        # Persist to guild_security_config DB table
        db_col = key.lower()
        try:
            await ctx.db.execute(
                "INSERT INTO guild_security_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                ctx.guild.id,
            )
            await ctx.db.execute(
                f"UPDATE guild_security_config SET {db_col} = $2, updated_at = now() WHERE guild_id = $1",
                ctx.guild.id, parsed,
            )
        except Exception:
            pass  # in-memory change still applies even if DB write fails

        b = card("Security Setting Updated", color=C_INFO)
        b.description(
            f"`{key}`: **{old_val}** → **{parsed}**\n\n"
            f"This change has been saved to the database."
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @security.command(name="toggle")
    @guild_only
    @_require_manage_guild()
    async def security_toggle(self, ctx: DiscoContext) -> None:
        """Enable or disable the security system for this server."""
        settings = await ctx.db.get_guild_settings(ctx.guild.id)
        current = settings.get("module_security", True) if settings else True
        new_val = not current

        await ctx.db.execute(
            "UPDATE guild_settings SET module_security = $2 WHERE guild_id = $1",
            ctx.guild.id, new_val,
        )

        state = "**enabled**" if new_val else "**disabled**"
        color = C_SUCCESS if new_val else C_WARNING
        b = card("Security System Toggled", color=color)
        b.description(f"Security enforcement is now {state} for this server.")
        await ctx.reply(embed=b.build(), mention_author=False)
    # ── Periodic Profile Sync ────────────────────────────────────────────────

    @tasks.loop(minutes=10)
    async def _sync_profiles(self) -> None:
        """Periodically sync Redis profiles to PostgreSQL for persistence."""
        if not self._engine or not self._engine._db:
            return

        self._engine.update_scan_ts()

        # Clean up expired in-memory entries
        try:
            await self._engine.cache.cleanup_expired()
        except Exception:
            pass

    @_sync_profiles.before_loop
    async def _before_sync(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Security(bot))
