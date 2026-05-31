from __future__ import annotations

import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

# ── Global: case-insensitive command groups ──────────────────────────────────
# discord.py's ``case_insensitive=True`` on the Bot only flips the TOP-LEVEL
# command lookup. Every ``commands.group(...)`` / ``commands.hybrid_group(...)``
# decorator instantiates its own ``all_commands`` dict with ``case_insensitive``
# defaulting to False, so ``,fish inv`` works but ``,fish Inv`` would 404 with
# 'command not found'. We monkey-patch ``GroupMixin.__init__`` once at import
# time so every Group / sub-Group / Bot instance defaults to case-insensitive
# matching without any decorator opt-in or per-cog edits.
#
# This must run BEFORE any cog imports its ``@commands.group(...)`` decorators
# (the decorator instantiates the Group at class-definition / import time).
# main.py imports ``core.framework.bot`` before loading cogs, so this patch fires
# first and every cog's groups inherit the new default.
def _enable_case_insensitive_groups() -> None:
    from discord.ext.commands.core import GroupMixin
    _orig_init = GroupMixin.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("case_insensitive", True)
        _orig_init(self, *args, **kwargs)

    GroupMixin.__init__ = _patched_init  # type: ignore[assignment]


_enable_case_insensitive_groups()


from core.config import Config
from constants.ui import C_AMBER, C_CRIMSON, C_ERROR, C_INFO, C_WARNING
from database import Database
from core.framework.context import DiscoContext
from core.framework import log
from core.framework import session_log as _sl
from core.framework.embed import card
from core.framework.error_tracker import ErrorTracker, ErrorSource, Severity
from core.framework.internal_commands import InternalCommandModule, setup_internal_commands_cog
from core.framework.discord_images import has_image as _has_image
from core.framework.live import live as _live_engine
from core.framework.redis_bus import RedisBus

# ── Cog registry ──────────────────────────────────────────────────────────────

COGS = [
    "cogs.clanktank",   # Clanktank: scammer/bot containment -- the only clanker command surface
]

# Commands that can run in ANY channel even when bot_channels is set.
# Everything NOT in this set is treated as a game command and is restricted
# to bot_channels (and their threads) when the list is non-empty.
_ALLOW_ANYWHERE: frozenset[str] = frozenset({
    "help", "ask", "admin", "gm", "security", "clanker", "ai", "dev",
    "migrate", "backup", "diagnose", "status", "approve", "deny",
    "approvals", "report", "bounty", "reports-bounties", "bugbounty",
    "ping", "about", "invite", "uptime", "health", "botinfo", "version",
    "changelog", "changes", "whatsnew",
    "premium", "subscribe", "subscription",
    "calendar", "agenda", "schedule",
    # Saved-chat-thread management -- AI threads spawn off any channel, so
    # their save/recall commands must work outside bot_channels too.
    "thread", "threads",
    # ,disco command group -- AI controls + context opt-in/out, usable from
    # any channel (the bare help page must always be reachable).
    "disco",
})

# ── Dynamic prefix ────────────────────────────────────────────────────────────

async def _get_prefix(bot: "Discoin", message: discord.Message) -> str | list[str]:
    # The "$" prefix is reserved for the real-crypto cog (cogs/realmarket.py)
    # and handled by its own on_message listener, NOT discord.py's command
    # dispatch. Leaving "$" out of this prefix list is what stops "$chart"
    # from being routed to the game-chart compat shim.
    if message.guild and getattr(bot, "db", None):
        try:
            settings = await bot.db.get_guild_settings(message.guild.id)
            # Bot channels: accept the guild prefix, comma, and bare commands.
            # The guild prefix comes first so "$work" works normally, then ","
            # so ",work" works, then "" (empty) so bare "work" works too.
            # "" must be last  -  it always matches.
            bot_chs = settings.get("bot_channels", "")
            if bot_chs and str(message.channel.id) in set(filter(None, bot_chs.split(","))):
                guild_prefix = settings.get("prefix") or Config.PREFIX
                # Include the guild prefix so prefixed commands still work
                # in bot channels.  Order matters: longest/most-specific
                # prefixes first, empty string last (it always matches).
                prefixes = [guild_prefix]
                if "," not in prefixes:
                    prefixes.append(",")
                prefixes.append("")
                return prefixes
            # Group hall thread with prefixless toggle: same multi-prefix
            # treatment as admin-set bot channels, but scoped to the
            # group's Hall thread and gated on the founder's
            # ``,group hall prefixless on`` opt-in. The hall thread id
            # lives on mining_groups.hall_thread_id and the toggle is
            # mining_groups.hall_prefixless (migration 0206).
            try:
                ch = message.channel
                ch_id = int(getattr(ch, "id", 0) or 0)
                if ch_id:
                    grp = await bot.db.fetch_one(
                        "SELECT hall_prefixless FROM mining_groups "
                        "WHERE guild_id = $1 AND hall_thread_id = $2",
                        int(message.guild.id), ch_id,
                    )
                    if grp and bool(grp.get("hall_prefixless")):
                        guild_prefix = settings.get("prefix") or Config.PREFIX
                        prefixes = [guild_prefix]
                        if "," not in prefixes:
                            prefixes.append(",")
                        prefixes.append("")
                        return prefixes
            except Exception:
                pass
            if settings.get("prefix"):
                return settings["prefix"]
        except Exception:
            pass
    return Config.PREFIX


def _is_reply_to_bot(message: discord.Message, bot: "Discoin") -> bool:
    """Return True if ``message`` is a reply to a message authored by the bot.

    Checks the resolved reference first (fast path, no HTTP). If the
    reference isn't yet resolved, falls back to the Help cog's
    recent-AI-message deque so a reply to an AI placeholder still routes
    correctly even when Discord hasn't embedded the reference payload.
    """
    bot_user = bot.user
    if not bot_user:
        return False
    ref = message.reference
    if not ref or not ref.message_id:
        return False
    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot_user.id
    # Reference not resolved -- fall back to the Help cog's tracked AI
    # reply deque. Any id in that deque is definitely one of our
    # messages, so a positive match is safe to forward.
    help_cog = bot.get_cog("Help")
    if help_cog is not None:
        tracked = getattr(help_cog, "_ai_message_ids", None)
        if tracked and ref.message_id in tracked:
            return True
    return False


def _bot_is_mentioned(bot: "Discoin", message: discord.Message) -> bool:
    """True when the bot is @mentioned directly or via its managed role.

    Discord puts only direct user mentions in ``message.mentions``; a ping
    of the bot's integration-managed role lands in ``role_mentions``
    instead. Treating that role mention as a bot mention means
    "@DiscoRole find thread <code>" routes through the AI pipeline just
    like a direct @mention, so it resolves to ,thread find instead of
    being ignored.
    """
    if bot.user in message.mentions:
        return True
    guild = message.guild
    if guild is None:
        return False
    role = getattr(guild, "self_role", None)
    return role is not None and role in message.role_mentions

# ── Discoin ───────────────────────────────────────────────────────────────────

async def _send_fuzzy_suggestion(ctx, invoked: str, suggestion: str, args_str: str = "") -> None:
    # If the user typed "help" as the only arg (e.g. ",mining help"), they want help
    # for the command  -  swap to "{prefix}help {suggestion}" which is more useful.
    if args_str.strip().lower() == "help":
        suggestion = f"help {suggestion}"
        args_str = ""

    full_suggestion = f"{suggestion} {args_str}".strip() if args_str else suggestion

    class FuzzyView(discord.ui.View):
        def __init__(self, original_ctx, suggestion: str, args_str: str) -> None:
            super().__init__(timeout=30.0)
            self.original_ctx = original_ctx
            self.suggestion = suggestion
            self.args_str = args_str

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.original_ctx.author.id:
                await interaction.response.send_message("Not your prompt.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
        async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(view=self)
            prefix = self.original_ctx.prefix or "."
            import copy
            new_msg = copy.copy(self.original_ctx.message)
            full = f"{self.suggestion} {self.args_str}".strip() if self.args_str else self.suggestion
            new_msg.content = f"{prefix}{full}"  # type: ignore[attr-defined]
            await self.original_ctx.bot.process_commands(new_msg)
            self.stop()

        @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
        async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(view=self)
            self.stop()

        async def on_timeout(self) -> None:
            self.stop()

    pfx = ctx.prefix or "."
    desc = f"\U0001F914  Command `{pfx}{invoked}` not found. Did you mean **`{pfx}{full_suggestion}`**?"
    embed = card(description=desc, color=C_AMBER).build()
    view = FuzzyView(ctx, suggestion, args_str)
    try:
        await ctx.reply(embed=embed, view=view, mention_author=False)
    except Exception:
        pass

class Discoin(commands.Bot):
    db: Database
    bus: RedisBus

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.auto_moderation_execution = True
        super().__init__(
            command_prefix=_get_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions.none(),
            owner_id=Config.REPORT_TARGET_USER_ID or None,
        )
        self.bus = RedisBus(redis_url=Config.REDIS_URL)
        self.errors = ErrorTracker()
        self.startup_phase = "init"
        self._error_freq: dict[str, list[float]] = {}  # error_type -> recent timestamps
        self.internal_commands = InternalCommandModule(self)
        self._start_time: float = __import__('time').time()
        self._autodelete_tasks: set[asyncio.Task] = set()
        # message_id → pending delete task (so on_command_error can cancel false positives)
        self._autodelete_by_msg: dict[int, asyncio.Task] = {}
        # message IDs that were auto-deleted; kept briefly so on_message_delete can filter them
        self._autodelete_done: set[int] = set()
        self._api_server = None
        self._api_server_task = None
        # Message IDs where scam deletion succeeded  -  AI handlers check this to avoid
        # responding to a message that was (or is being) removed by the moderation cog.
        self._scam_deleted_ids: set[int] = set()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _check_command_roles(self, guild: discord.Guild | None, member: discord.Member | None, cmd_name: str) -> bool:
        """Return True if member may use cmd_name in guild (or if no restriction is set).
        Raises commands.CheckFailure if the member lacks the required role."""
        if not guild or not member:
            return True
        try:
            allowed_roles = await self.db.guilds.get_command_allowed_roles(guild.id, cmd_name)
        except Exception:
            return True  # DB error  -  fail open
        if not allowed_roles:
            return True  # no restriction
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids.intersection(allowed_roles):
            return True
        raise commands.CheckFailure(f"You don't have the required role to use `{cmd_name}`.")

    async def _ensure_api_server_started(self) -> None:
        """Start the embedded FastAPI server as early as possible during boot.

        Railway only sees the service as healthy once something is listening on the
        assigned port. Starting the HTTP server before heavy Discord startup work
        avoids long windows of connection-refused / 502 responses during deploys.
        """
        if not Config.API_PORT or self._api_server_task is not None:
            return

        import uvicorn
        try:
            from api.v2.main import create_app as create_v2_app
        except ModuleNotFoundError:
            # Recycler is the clanker-only bot and ships no economy REST API.
            # The economy dashboard/API lives in Discoin; skip cleanly rather
            # than crash when PORT is injected (e.g. on Railway).
            log.info("No economy API package present; skipping embedded FastAPI server")
            return

        v2_app = create_v2_app()
        v2_app.state.bot = self

        config = uvicorn.Config(
            v2_app,
            host="0.0.0.0",
            port=Config.API_PORT,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._api_server = server
        self._api_server_task = asyncio.create_task(server.serve())
        self.startup_phase = "api_listening"
        log.ok(f"FastAPI listening on [link]http://0.0.0.0:{Config.API_PORT}[/link]")

    async def setup_hook(self) -> None:
        self.startup_phase = "booting"
        # Initialise session log first so everything below gets captured
        sl = _sl.init()
        sl.startup(f"Bot process starting  -  PID {__import__('os').getpid()}")
        dsn = Config.DATABASE_URL
        safe_dsn = log.redact_dsn(dsn)
        sl.startup(f"Config prefix={Config.PREFIX}  DB={safe_dsn}")

        # Start the HTTP server before the rest of boot so Railway health checks
        # have a live listener even while Discord login / slash sync are ongoing.
        await self._ensure_api_server_started()

        self.db = Database(dsn)
        await self.db.connect()
        self.startup_phase = "db_connected"
        log.info(f"Database connected  -  [dim]{safe_dsn}[/dim]")
        sl.startup(f"Database connected  -  {safe_dsn}")

        # Recover any game_sessions left 'active' from a prior process that
        # was killed before graceful shutdown could resolve them. Refunds the
        # bet and marks the session 'cancelled' so this is a one-shot sweep.
        try:
            from core.framework.shutdown import recover_orphaned_sessions
            recovered = await recover_orphaned_sessions(self.db)
            if recovered:
                log.ok(f"Recovered {recovered} orphaned game session(s) from previous run")
                sl.startup(f"Recovered {recovered} orphaned game session(s)")
        except Exception as exc:
            log.warn(f"Orphaned session recovery raised: {exc}")
            sl.warn(f"Orphaned session recovery raised: {exc}")

        # Connect Redis-backed event bus (graceful  -  falls back to in-memory)
        try:
            await self.bus.connect()
            if self.bus.is_connected:
                log.ok("RedisBus connected")
                sl.startup(f"RedisBus connected  -  {Config.REDIS_URL}")
            else:
                log.warn("RedisBus running in-memory only (Redis unavailable)")
        except Exception as exc:
            log.warn(f"RedisBus failed to connect ({exc})  -  falling back to in-memory")
            sl.warn(f"RedisBus connect failed: {exc}")

        # Global prefix-command role check  -  integrates with discord.py's check system
        @self.check
        async def _global_role_check(ctx: DiscoContext) -> bool:
            cmd_name = getattr(ctx.command, "name", None) or ""
            if cmd_name and ctx.guild:
                await self._check_command_roles(ctx.guild, ctx.author, cmd_name)
            return True

        # Global prefix-command bot-channel gate. When a guild has configured
        # bot_channels, GAME commands may only run inside those channels and
        # their threads. Meta/chat/admin surfaces stay available everywhere.
        @self.check
        async def _global_botchannel_gate(ctx: DiscoContext) -> bool:
            if not ctx.guild or not ctx.command:
                return True
            try:
                bot_chs = await self.db.guilds.get_bot_channels(ctx.guild.id)
            except Exception:
                return True  # fail open on DB error
            if not bot_chs:
                return True  # no restriction configured
            root = ctx.command.root_parent or ctx.command
            name = getattr(root, "name", "") or ""
            if name in _ALLOW_ANYWHERE:
                return True
            channel = ctx.channel
            ch_id = getattr(channel, "id", 0)
            if ch_id in bot_chs:
                return True
            if isinstance(channel, discord.Thread) and getattr(channel, "parent_id", 0) in bot_chs:
                return True
            # Group hall threads bypass the gate too: a paid-feature
            # private thread owned by a mining group (mining_groups.
            # hall_thread_id) acts as that group's dedicated bot
            # channel, so ,chess move / ,checkers move / ,fish cast /
            # any game command works inside without the operator
            # having to wedge the thread into bot_channels.
            if isinstance(channel, discord.Thread) and ch_id:
                try:
                    grp = await self.db.fetch_one(
                        "SELECT 1 FROM mining_groups "
                        "WHERE guild_id = $1 AND hall_thread_id = $2",
                        int(ctx.guild.id), int(ch_id),
                    )
                    if grp:
                        return True
                except Exception:
                    pass
            mention_list = ", ".join(f"<#{cid}>" for cid in bot_chs[:5])
            raise commands.CheckFailure(
                f"Game commands can only run in the bot channel(s): {mention_list}."
            )

        # Global slash-command role + bot-channel check  -  runs before every app command
        _bot_ref = self
        async def _slash_interaction_check(interaction: discord.Interaction) -> bool:
            cmd = interaction.command
            if cmd is None:
                return True
            guild = interaction.guild
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if guild and not member:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    pass
            try:
                if not await _bot_ref._check_command_roles(guild, member, cmd.name):
                    return False
            except commands.CheckFailure as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return False
            # Bot-channel gate (same logic as the prefix check above)
            if guild is not None:
                try:
                    bot_chs = await _bot_ref.db.guilds.get_bot_channels(guild.id)
                except Exception:
                    bot_chs = []
                if bot_chs:
                    # Resolve root slash-command name (slash groups store parent on `.parent`)
                    root_cmd = cmd
                    while getattr(root_cmd, "parent", None) is not None:
                        root_cmd = root_cmd.parent
                    root_name = getattr(root_cmd, "name", "") or ""
                    channel = interaction.channel
                    ch_id = getattr(channel, "id", 0)
                    in_allowed = (
                        root_name in _ALLOW_ANYWHERE
                        or ch_id in bot_chs
                        or (isinstance(channel, discord.Thread)
                            and getattr(channel, "parent_id", 0) in bot_chs)
                    )
                    # Group hall threads also bypass the gate (mirrors
                    # the prefix-command gate): paid-feature group
                    # halls act as the group's dedicated bot channel.
                    if not in_allowed and isinstance(channel, discord.Thread) and ch_id:
                        try:
                            grp = await _bot_ref.db.fetch_one(
                                "SELECT 1 FROM mining_groups "
                                "WHERE guild_id = $1 AND hall_thread_id = $2",
                                int(guild.id), int(ch_id),
                            )
                            if grp:
                                in_allowed = True
                        except Exception:
                            pass
                    if not in_allowed:
                        mention_list = ", ".join(f"<#{cid}>" for cid in bot_chs[:5])
                        msg = f"Game commands can only run in the bot channel(s): {mention_list}."
                        try:
                            await interaction.response.send_message(msg, ephemeral=True)
                        except Exception:
                            pass
                        return False
            return True
        self.tree.interaction_check = _slash_interaction_check

        # Start live dashboard engine
        _live_engine.init(self)
        self.live = _live_engine
        log.ok("Live dashboard engine started")

        # Start agent tools framework (task queue, trigger engine, tool registry)
        try:
            from core.framework.agent_tools import AgentTools
            self.agent_tools = AgentTools(self)
            self.agent_tools.start()
            log.ok("Agent tools framework started")
        except Exception as exc:
            log.warn(f"Agent tools framework failed to start: {exc}")
            sl.warn(f"Agent tools framework start failed: {exc}")

        for cog in COGS:
            try:
                await self.load_extension(cog)
            except Exception as exc:
                sl.warn(f"Failed to load cog {cog}: {exc}")
                import traceback as _tb
                self.errors.record(
                    ErrorSource.MODULE, str(exc), severity=Severity.CRITICAL,
                    module=cog, error_type=type(exc).__name__,
                    traceback_str="".join(_tb.format_exception(type(exc), exc, exc.__traceback__)),
                )
                raise
        await setup_internal_commands_cog(self)
        self.startup_phase = "cogs_loaded"
        log.ok(f"Loaded [bold]{len(COGS)}[/bold] cogs")
        sl.startup(f"Loaded {len(COGS)} cogs: {', '.join(COGS)}")

        # Sync slash commands to Discord
        slash_guild_id = int(os.getenv("SLASH_GUILD_ID") or "0")
        if slash_guild_id:
            guild_obj = discord.Object(id=slash_guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.ok(f"Slash commands synced to dev guild [bold]{slash_guild_id}[/bold]")
            # Purge stale global commands in the background (non-blocking)
            async def _clear_global():
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                    log.ok("Stale global slash commands cleared")
                except Exception as e:
                    log.warn(f"Could not clear global commands: {e} [dim](expire in ~1hr)[/dim]")
            asyncio.create_task(_clear_global())
        else:
            await self.tree.sync()
            log.ok("Slash commands synced globally")
        self.startup_phase = "commands_synced"

        # Auto-seed TOKEN/stablecoin pools on startup if enabled
        if Config.AUTO_SEED_POOLS:
            log.info("AUTO_SEED_POOLS=true  -  seeding pools for all guilds after ready")
            # Schedule post-ready seeding (guilds aren't available until on_ready)
            async def _seed_pools_on_ready():
                await self.wait_until_ready()
                for guild in self.guilds:
                    # Ensure the guild row exists before seeding pools (FK constraint)
                    await self.db.execute(
                        "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                        guild.id,
                    )
                    await self.db.seed_pools(guild.id)
                log.ok("Pool auto-seed complete")
            asyncio.create_task(_seed_pools_on_ready())

        # Companion to the 0271 migration: built-in tokens read max_supply
        # from Config.TOKENS rather than a DB column, so SQL alone cannot
        # clamp their circulating_supply.  Walk every (guild, built-in
        # token) pair once at boot and clamp crypto_prices to the configured
        # cap.  Idempotent + best-effort; a single bad row should not block
        # startup.
        async def _clamp_builtin_supply_caps():
            await self.wait_until_ready()
            from core.framework.scale import to_raw as _tr
            clamped = 0
            for sym, cfg in Config.TOKENS.items():
                max_h = cfg.get("max_supply") or 0
                if max_h <= 0:
                    continue
                max_raw = _tr(max_h)
                try:
                    res = await self.db.execute(
                        "UPDATE crypto_prices "
                        "SET circulating_supply = LEAST(circulating_supply, $1) "
                        "WHERE symbol = $2 AND circulating_supply > $1",
                        max_raw, sym,
                    )
                except Exception:
                    log.exception("supply clamp failed for %s", sym)
                    continue
                # asyncpg returns "UPDATE n"; count when present.
                try:
                    if isinstance(res, str) and res.startswith("UPDATE "):
                        clamped += int(res.split(" ", 1)[1])
                except Exception:
                    pass
            if clamped:
                log.ok("Built-in supply clamp: %d crypto_prices rows reduced to cap", clamped)
            else:
                log.info("Built-in supply clamp: nothing over cap")
        asyncio.create_task(_clamp_builtin_supply_caps())

    async def close(self) -> None:
        self.startup_phase = "closing"
        sl = _sl.get()
        if sl:
            sl.startup("Bot shutting down  -  closing database and session log")
        # Drain bet-backed game views before teardown so players get their
        # funds back (or cashed out at current state) instead of having bets
        # deducted with no resolution when Railway redeploys.
        try:
            from core.framework.shutdown import active_view_count, drain_active_views
            if active_view_count() > 0:
                log.info(f"Graceful shutdown: draining {active_view_count()} active game view(s)")
                await drain_active_views(timeout=20.0)
        except Exception as exc:
            log.warn(f"Graceful drain raised: {exc}")
        if sl:
            sl.close()
        if hasattr(self, "self_heal"):
            self.self_heal.stop()
        try:
            from core.framework.ai import close_client as _close_ai_client

            await _close_ai_client()
        except Exception as exc:
            log.warn(f"AI client shutdown raised: {exc}")
        api_server = getattr(self, "_api_server", None)
        api_task = getattr(self, "_api_server_task", None)
        if api_server is not None:
            api_server.should_exit = True
        if api_task is not None:
            try:
                await api_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warn(f"API server shutdown raised: {exc}")
        _live_engine.stop()
        if hasattr(self, "agent_tools"):
            try:
                self.agent_tools.stop()
            except Exception as exc:
                log.warn(f"Agent tools stop raised: {exc}")
        if hasattr(self, "bus"):
            await self.bus.close()
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()

    # ── Context injection ──────────────────────────────────────────────────

    async def get_context(self, message, *, cls=DiscoContext):
        ctx = await super().get_context(message, cls=cls)
        if ctx.command is not None:
            ctx.db = self.db
        return ctx

    # ── Events ────────────────────────────────────────────────────────────

    async def on_ready(self) -> None:
        self.startup_phase = "ready"
        sl = _sl.get()
        if sl:
            guilds_str = ", ".join(f"{g.name}({g.id})" for g in self.guilds)
            sl.startup(
                f"Ready as {self.user} ({self.user.id})  "
                f"guilds={len(self.guilds)} [{guilds_str}]  "
                f"cogs={len(self.cogs)}"
            )
        log.print_ready(str(self.user), self.user.id, len(self.guilds), len(self.cogs))
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{Config.PREFIX}help | economy bot",
        )
        await self.change_presence(activity=activity)

        # Ensure every guild the bot is in has a guild_settings row.
        # Without this, background tasks (staking_tick, drift_task, mining_tick)
        # raise ForeignKeyViolationError on fresh installs before any command
        # has been run in a guild.  ON CONFLICT DO NOTHING makes this idempotent.
        for guild in self.guilds:
            try:
                await self.db.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    guild.id,
                )
            except Exception:
                pass

        # Start the self-heal scheduler (Hydra-ported: periodic health checks
        # + exponential-backoff recovery for Redis and failed task loops).
        from core.framework.self_heal import SelfHealScheduler
        if not hasattr(self, "self_heal"):
            self.self_heal = SelfHealScheduler(self)
        self.self_heal.start()
        log.ok("Self-heal scheduler started")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Seed guild_settings row immediately when the bot joins a new guild."""
        try:
            await self.db.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                guild.id,
            )
        except Exception:
            pass

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        msg = str(error.original) if isinstance(error, app_commands.CommandInvokeError) else str(error)
        embed = card(description=f"\u274c  {msg}", color=C_ERROR).build()
        if interaction.response.is_done():
            try:
                from core.framework.links import sanitize_embed

                sanitize_embed(embed)
            except Exception:
                pass
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_message(self, message: discord.Message) -> None:
        """Override to strip spaces around the prefix and command name.
        Allows '. balance', '.  help', '  ,fish inv', etc. to work like
        '.balance', '.help', ',fish inv'.

        Combined with the GroupMixin case-insensitive monkey-patch at the
        top of this module, the user can also mix capitalization at any
        token level: ',  Fish Inv' resolves the same as ',fish inv'.
        """
        if message.author.bot:
            await self.process_commands(message)
            return
        if not message.content and not _has_image(message):
            await self.process_commands(message)
            return

        # Strip leading whitespace BEFORE the prefix so '   ,fish' parses
        # the same as ',fish'. We rebuild the message with a copy so we
        # don't mutate the original (other listeners may inspect it).
        if message.content and message.content[0].isspace() and message.content.lstrip():
            import copy
            new_message = copy.copy(message)
            object.__setattr__(new_message, "content", message.content.lstrip())
            message = new_message

        # Determine the prefix for this message
        prefix = await _get_prefix(self, message)

        # Plain messages inside a registered Disco AI chat thread continue
        # the conversation without a fresh @mention. Prefix commands
        # (",thread save", ",balance", ...) still dispatch normally, so we
        # only intercept content that does not start with a real prefix.
        ai_thread_ids = getattr(self, "_ai_thread_ids", None)
        if (ai_thread_ids and message.guild
                and isinstance(message.channel, discord.Thread)
                and message.channel.id in ai_thread_ids):
            _pfxs = prefix if isinstance(prefix, list) else [prefix]
            if not any(p and message.content.startswith(p) for p in _pfxs):
                help_cog = self.get_cog("Help")
                if help_cog:
                    await help_cog.handle_ai_reply(message)
                    return

        # Bot channels return a list of prefixes  -  process_commands handles
        # list prefixes natively via discord.py, so skip the space-stripping
        # and mention-interception logic below (it only works with str prefixes).
        # But we still need to check for AI replies and @mentions.
        if isinstance(prefix, list):
            if message.guild and _is_reply_to_bot(message, self):
                help_cog = self.get_cog("Help")
                if help_cog:
                    await help_cog.handle_ai_reply(message)
                    return
            if message.guild and _bot_is_mentioned(self, message):
                help_cog = self.get_cog("Help")
                if help_cog:
                    await help_cog.handle_ai_mention(message)
                    return
            await self.process_commands(message)
            return

        bot_mentioned = _bot_is_mentioned(self, message)

        if message.content.startswith(prefix):
            # If the bot is mentioned as a command argument (e.g. .sell @bot all,
            # .group invite @bot), intercept and send a funny in-character rejection.
            # Exempt .ask (mention is part of the question) and .admin (config commands).
            if bot_mentioned:
                after = message.content[len(prefix):]
                cmd_name = after.split()[0].lower() if after.split() else ""
                if cmd_name not in ("ask", "admin"):
                    help_cog = self.get_cog("Help")
                    if help_cog:
                        await help_cog.handle_bot_arg_mention(message)
                        return

            after_prefix = message.content[len(prefix):]
            # If there are leading spaces after the prefix, strip them
            if after_prefix and after_prefix[0] == " " and after_prefix.lstrip():
                import copy
                stripped_content = prefix + after_prefix.lstrip()
                new_message = copy.copy(message)
                # Patch the content attribute directly (discord.py stores it as a simple attr)
                object.__setattr__(new_message, "content", stripped_content)
                await self.process_commands(new_message)
                return
        else:
            # Reply to ANY of the bot's messages routes to the rich help-cog
            # pipeline. Discord's reply feature auto-pings the bot, which
            # means ``bot_mentioned`` is True too -- so this MUST run before
            # ``internal_commands.maybe_handle``. Otherwise admins / beta
            # users whose first reply word matches a registered command
            # (help, balance, buy, ask, ...) would silently get the bare
            # command instead of the AI follow-up they actually wanted.
            if message.guild and _is_reply_to_bot(message, self):
                help_cog = self.get_cog("Help")
                if help_cog:
                    await help_cog.handle_ai_reply(message)
                    return

            # If the bot is @mentioned (but it's not a command), respond via AI.
            # Same ordering reason as above: ``maybe_handle`` strips the mention
            # and treats the first word as a command name, which intercepts
            # plain "@Disco how do I X?" pings for admins / beta users.
            if bot_mentioned and message.guild:
                help_cog = self.get_cog("Help")
                if help_cog:
                    await help_cog.handle_ai_mention(message)
                    return

            # Internal commands ("bot help", "disco balance", ...) -- now only
            # reachable via the explicit text invokers, since @mentions and
            # replies were handled above.
            handled = await self.internal_commands.maybe_handle(message)
            if handled:
                return

        await self.process_commands(message)

    async def _record_command_usage(self, ctx: DiscoContext) -> None:
        """Persist a usage row for ``,admin commandstats``.

        Captures the qualified command path (so subcommands count as
        their own bucket) plus whatever the user typed after the
        command name, then upserts an all-time roll-up so the count
        survives any future pruning of the detail table.
        """
        cmd = ctx.command
        if cmd is None:
            return
        qual = cmd.qualified_name or cmd.name or ""
        if not qual:
            return
        args_text = ""
        msg = getattr(ctx, "message", None)
        content = (getattr(msg, "content", "") or "").strip()
        prefix = ctx.prefix or ""
        if prefix and content.startswith(prefix):
            content = content[len(prefix):].lstrip()
        depth = len(qual.split())
        if depth > 0:
            tokens = content.split(maxsplit=depth)
            if len(tokens) > depth:
                args_text = tokens[depth].strip()
        if len(args_text) > 200:
            args_text = args_text[:200]
        guild_id = ctx.guild.id if ctx.guild else None
        user_id = ctx.author.id if ctx.author else 0
        db = getattr(self, "db", None)
        if db is None:
            return
        await db.execute(
            "INSERT INTO command_usage (guild_id, user_id, command_path, args_text) "
            "VALUES ($1, $2, $3, $4)",
            guild_id, user_id, qual, args_text,
        )
        await db.execute(
            "INSERT INTO command_usage_totals "
            "    (guild_id, command_path, args_text, total_count, first_seen, last_seen) "
            "VALUES ($1, $2, $3, 1, NOW(), NOW()) "
            "ON CONFLICT (guild_id, command_path, args_text) DO UPDATE SET "
            "    total_count = command_usage_totals.total_count + 1, "
            "    last_seen = NOW()",
            guild_id or 0, qual, args_text,
        )

    async def on_command(self, ctx: DiscoContext) -> None:
        """Log every command invocation and auto-delete if configured."""
        sl = _sl.get()
        if sl is not None:
            sl.cmd(ctx)
        try:
            await self._record_command_usage(ctx)
        except Exception as exc:
            log.warning("[commandstats] usage record failed: %s", exc)
        if not ctx.guild or not ctx.message:
            return
        try:
            s = await self.db.get_guild_settings(ctx.guild.id)
            # AI commands (.ask, ,disco gif/image/video) have their own delete setting.
            cmd_name = getattr(ctx.command, "name", None) if ctx.command else None
            qual_name = getattr(ctx.command, "qualified_name", "") if ctx.command else ""
            _AI_CMDS = {"ask", "disco image"}
            if cmd_name == "ask" or qual_name in _AI_CMDS:
                d = int(s.get("ai_cmd_delete_after", 0) or 0)
            else:
                d = int(s.get("cmd_delete_after", 0) or 0)
            if d > 0:
                # Use a managed asyncio task instead of discord.py's
                # message.delete(delay=…)  -  the latter silently swallows ALL
                # HTTPExceptions (including Forbidden) inside its background
                # task, making permission errors invisible.  A managed task
                # lets us log failures so admins can diagnose missing
                # "Manage Messages" permission (required to delete other
                # users' command messages, unlike bot-own reply deletion).
                msg = ctx.message

                async def _auto_delete(m=msg, delay=d):
                    await asyncio.sleep(delay)
                    # Mark before delete so on_message_delete fires after the ID
                    # is already in the set (no race condition).
                    self._autodelete_done.add(m.id)
                    async def _cleanup(mid=m.id):
                        await asyncio.sleep(10)
                        self._autodelete_done.discard(mid)
                    asyncio.create_task(_cleanup())
                    try:
                        await m.delete()
                    except discord.NotFound:
                        pass  # already deleted
                    except discord.Forbidden:
                        log.warn(
                            "[autodelete] Cannot delete command in #%s "
                            " -  bot needs Manage Messages permission"
                            % getattr(m.channel, "name", "?"),
                        )
                    except Exception as exc:
                        log.warn(f"[autodelete] Command delete failed: {exc}")

                task = asyncio.create_task(_auto_delete())
                self._autodelete_tasks.add(task)
                task.add_done_callback(self._autodelete_tasks.discard)
                self._autodelete_by_msg[msg.id] = task
                task.add_done_callback(lambda _, mid=msg.id: self._autodelete_by_msg.pop(mid, None))
        except Exception as exc:
            log.warning("[autodelete] Failed to schedule command delete: %s", exc)

    async def on_command_error(self, ctx: DiscoContext, error) -> None:
        # Log every error to the session log regardless of type
        sl = _sl.get()
        if sl is not None and not isinstance(error, commands.CommandNotFound):
            orig = error.original if isinstance(error, commands.CommandInvokeError) else error
            sl.error(ctx, orig)

        if isinstance(error, commands.CommandNotFound):
            # Bot channels (no-prefix mode): silently ignore non-commands.
            if ctx.prefix == "":
                return

            # Chain-replayed steps: silently ignore  -  the chain engine owns error reporting.
            if getattr(ctx.message, "_chain_step", False):
                return

            # Fuzzy command matching  -  preserve original args so the replay is complete
            import difflib
            invoked = getattr(ctx, "invoked_with", None) or ""
            if invoked:
                all_names = [c.name for c in self.commands]
                for cmd in self.commands:
                    all_names.extend(cmd.aliases)
                matches = difflib.get_close_matches(invoked.lower(), all_names, n=1, cutoff=0.6)
                if matches:
                    suggestion = matches[0]
                    # Extract everything the user typed after the mistyped command name
                    raw = ctx.message.content if ctx.message else ""
                    prefix = ctx.prefix or "."
                    after_prefix = raw[len(prefix):] if raw.startswith(prefix) else raw
                    parts = after_prefix.split(None, 1)
                    args_str = parts[1] if len(parts) > 1 else ""
                    await _send_fuzzy_suggestion(ctx, invoked, suggestion, args_str)
            return

        import traceback as _tb_mod
        orig = error.original if isinstance(error, commands.CommandInvokeError) else error
        tb_str = "".join(_tb_mod.format_exception(type(orig), orig, orig.__traceback__))

        guild_id = ctx.guild.id if ctx.guild else 0
        user_id = ctx.author.id if ctx.author else 0
        cmd_name = ctx.command.qualified_name if ctx.command else (getattr(ctx, "invoked_with", "") or "")

        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        # Bot channels (no-prefix mode): silently ignore argument errors.
        # "buy this laptop its a good one" → triggers `buy` command, "this"
        # fails to parse as amount → BadArgument → suppress.  Legitimate
        # commands like `buy 10 arc` parse fine and never reach this path.
        _bot_ch = ctx.prefix == ""
        if _bot_ch and isinstance(error, (
            commands.MissingRequiredArgument,
            commands.BadArgument, commands.TooManyArguments,
            commands.BadUnionArgument, commands.BadLiteralArgument,
        )):
            # Cancel any pending autodelete  -  the message wasn't really a command
            if ctx.message and ctx.message.id in self._autodelete_by_msg:
                self._autodelete_by_msg.pop(ctx.message.id).cancel()
            return
        # Also suppress CheckFailure in bot channels  -  an unregistered user
        # chatting shouldn't see "You must register first" just because they
        # typed a word that happens to be a command.
        if _bot_ch and isinstance(error, commands.CheckFailure):
            # Cancel any pending autodelete  -  the user isn't registered, this wasn't intentional
            if ctx.message and ctx.message.id in self._autodelete_by_msg:
                self._autodelete_by_msg.pop(ctx.message.id).cancel()
            return

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            self.errors.record(
                ErrorSource.CMD, str(error), severity=Severity.INFO,
                guild_id=guild_id, user_id=user_id, command=cmd_name,
                error_type=type(error).__name__, traceback_str=tb_str,
            )
            # Build a human-readable hint with a working command example
            _hint = ""
            _cmd = cmd_name or ""
            if isinstance(error, commands.MissingRequiredArgument):
                _hint = f"{ctx.prefix or '.'}{_cmd} " if _cmd else ""
            await ctx.reply_error_hint(str(error), hint=_hint, command_name=_cmd)
            await self._post_error(ctx.guild, error, ctx=ctx)
            return
        if isinstance(error, commands.CommandOnCooldown):
            # Silently skip cooldown for system-initiated follow-up re-runs
            if getattr(ctx.message, "_bypass_cooldown", False):
                return
            # Cooldowns are not errors  -  log as warning, show amber notice
            self.errors.record(
                ErrorSource.CMD, f"Cooldown {error.retry_after:.0f}s", severity=Severity.WARNING,
                guild_id=guild_id, user_id=user_id, command=cmd_name,
                error_type="CommandOnCooldown",
            )
            await ctx.reply_cooldown(error.retry_after)
            return
        if isinstance(error, commands.CheckFailure):
            # Premium gate -- render the standard locked-feature card instead
            # of the generic error embed. PremiumGateFailure is a CheckFailure
            # subclass so it lands here.
            from core.framework.premium import PremiumGateFailure
            if isinstance(error, PremiumGateFailure):
                self.errors.record(
                    ErrorSource.CMD, f"Premium gate: {error.feature_key}",
                    severity=Severity.INFO,
                    guild_id=guild_id, user_id=user_id, command=cmd_name,
                    error_type="PremiumGateFailure",
                )
                try:
                    await ctx.reply_premium_required(error.feature_key)
                except Exception:
                    log.exception("failed to render premium-required reply")
                return
            self.errors.record(
                ErrorSource.CMD, str(error), severity=Severity.WARNING,
                guild_id=guild_id, user_id=user_id, command=cmd_name,
                error_type="CheckFailure",
            )
            await ctx.reply_error(str(error) or "You can't use this command here.")
            await self._post_error(ctx.guild, error, ctx=ctx)
            return

        # Discord-side service rate limits (429 / 40062) are NOT a command bug --
        # echoing "429 Too Many Requests..." back to the user as a red error embed
        # both confuses players and risks compounding the rate limit (the error
        # reply is itself an HTTP send). Log + record, but stay silent on chat.
        if isinstance(error, discord.HTTPException) and (
            error.status == 429 or getattr(error, "code", 0) == 40062
        ):
            self.errors.record(
                ErrorSource.CMD, f"Discord rate limit: {error}",
                severity=Severity.WARNING,
                guild_id=guild_id, user_id=user_id, command=cmd_name,
                error_type="HTTPRateLimit",
            )
            log.warning("[on_command_error] suppressed Discord rate limit on %s: %s", cmd_name, error)
            return

        # Unexpected errors  -  high severity
        self.errors.record(
            ErrorSource.CMD, str(error), severity=Severity.HIGH,
            guild_id=guild_id, user_id=user_id, command=cmd_name,
            error_type=type(error).__name__, traceback_str=tb_str,
        )
        await ctx.reply_error(str(error))
        await self._post_error(ctx.guild, error, ctx=ctx)
        raise error

    def _track_error_freq(self, error_type: str) -> int:
        """Track error frequency. Returns count in last 10 minutes."""
        import time
        now = time.time()
        window = 600
        timestamps = self._error_freq.setdefault(error_type, [])
        timestamps.append(now)
        self._error_freq[error_type] = [t for t in timestamps if now - t < window]
        return len(self._error_freq[error_type])

    async def _post_error(
        self,
        guild: discord.Guild | None,
        error: Exception,
        ctx=None,
    ) -> None:
        """Post an error embed to the guild's error_channel if configured."""
        import traceback
        if not guild:
            return
        try:
            settings = await self.db.get_guild_settings(guild.id)
            ch_id = settings.get("error_channel")
            if not ch_id:
                return
            ch = guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await self.fetch_channel(ch_id)
                except Exception:
                    return
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                return

            error_type = type(error).__name__
            error_lower = error_type.lower()

            # Severity detection
            critical_keywords = ("database", "connection", "pool", "timeout")
            high_keywords = ("permission", "forbidden", "notfound")
            info_keywords = ("badargument", "missingrequired")
            warning_keywords = ("check", "cooldown")

            if any(k in error_lower for k in critical_keywords):
                severity_label = "CRITICAL"
                color = C_CRIMSON
                emoji = "\U0001f534"  # red circle
            elif any(k in error_lower for k in high_keywords):
                severity_label = "HIGH"
                color = C_ERROR
                emoji = "\U0001f534"  # red circle
            elif any(k in error_lower for k in warning_keywords):
                severity_label = "WARNING"
                color = C_WARNING
                emoji = "\U0001f7e0"  # orange circle
            elif any(k in error_lower for k in info_keywords):
                severity_label = "INFO"
                color = C_INFO
                emoji = "\U0001f535"  # blue circle
            else:
                severity_label = "MEDIUM"
                color = C_ERROR
                emoji = "\U0001f7e1"  # yellow circle

            # Check if this severity level is enabled for the error feed
            feed_levels = settings.get("error_feed_levels") or "INFO,WARNING,LOW,MEDIUM,HIGH,CRITICAL"
            allowed = {s.strip().upper() for s in feed_levels.split(",")}
            if severity_label not in allowed:
                return

            # Frequency tracking
            freq = self._track_error_freq(error_type)
            freq_note = ""
            if freq >= 5:
                freq_note = f"\nRepeating -- {freq}x in the last 10 minutes"
            elif freq >= 3:
                freq_note = f"\nSeen {freq}x in the last 10 minutes"

            tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            tb_short = tb[-1800:] if len(tb) > 1800 else tb

            builder = card(f"{emoji} {error_type}", color=color).timestamp()

            if ctx is not None:
                invoke = getattr(ctx, "invoked_with", None) or "unknown"
                author = getattr(ctx, "author", None)
                msg = getattr(ctx, "message", None)
                input_text = ""
                if msg and getattr(msg, "content", None):
                    input_text = f"\n`{msg.content[:150]}`"
                builder = builder.field("Command", f"`{invoke}`{input_text}", False)
                if author:
                    builder = builder.field("User", author.mention, True)

            error_msg = f"```\n{str(error)[:300]}\n```{freq_note}"
            embed = (
                builder
                .field("Error", error_msg, False)
                .field("Traceback", f"```py\n{tb_short}\n```", False)
                .footer(f"{severity_label} | Use .admin diag errors for details")
                .build()
            )

            if isinstance(ch, discord.Thread) and ch.archived:
                try:
                    await ch.edit(archived=False)
                except Exception:
                    pass
            await ch.send(embed=embed)
        except Exception:
            pass  # Never let error reporting crash the bot

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Global event error handler  -  posts to error_channel for all guilds that have one."""
        import traceback, sys
        exc_type, exc_val, exc_tb = sys.exc_info()
        if exc_val is None:
            return

        tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        # Record as a bot-level error for every guild
        for guild in self.guilds:
            self.errors.record(
                ErrorSource.BOT, str(exc_val), severity=Severity.HIGH,
                guild_id=guild.id, error_type=type(exc_val).__name__,
                traceback_str=tb_str,
                context={"event_method": event_method},
            )
            await self._post_error(guild, exc_val)
