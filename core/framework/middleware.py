"""
Middleware decorators for Discoin commands.

Stacking order (outermost = runs first):
    @bot.command()
    @guild_only
    @no_bots
    @ensure_registered
    async def cmd(ctx): ...
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from functools import wraps

from discord.ext import commands

log = logging.getLogger(__name__)

def guild_only(func):
    """Reject commands invoked outside a guild (DMs)."""
    @commands.check(lambda ctx: ctx.guild is not None or _raise(
        commands.NoPrivateMessage("This command can only be used in a server.")
    ))
    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await func(*args, **kwargs)
    return wrapper

def no_bots(func):
    """Reject commands from bot accounts."""
    @commands.check(lambda ctx: not ctx.author.bot or _raise(
        commands.CheckFailure("Bots cannot use economy commands.")
    ))
    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await func(*args, **kwargs)
    return wrapper

async def _twin_join_flag(db, gid: int, uid: int) -> None:
    """V3 anti-alt: check if another account in the same guild registered
    within 60s of this one, and if so flag the pair as a soft
    'twin_join' signal. Best-effort, swallows every error."""
    try:
        from services import anti_alt as _anti_alt
        other = await _anti_alt.twin_join_check(db, gid, uid, window_secs=60)
        if other is not None:
            await _anti_alt.flag_pair(
                db, gid, uid, int(other), "twin_join", severity=2,
            )
    except Exception:
        pass


def ensure_registered(func):
    """
    Auto-register user in the database before command runs.
    Sets ctx.user_row so the command body can read it without a DB round-trip.

    Works for both free-function commands and Cog method commands (detects 'self').

    Also fires a best-effort one-time welcome DM the first time we see a
    user. ``services.onboarding.try_send_welcome_dm`` self-dedupes via
    the ``welcomed_users`` table and never raises, so the spawned task
    is fire-and-forget.
    """
    sig = inspect.signature(func)
    params = list(sig.parameters.keys())
    is_method = params and params[0] == "self"

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Locate ctx: first arg for functions, second arg for methods
        ctx = args[1] if is_method else args[0]
        ctx.user_row = await ctx.db.ensure_user(ctx.author.id, ctx.guild.id, ctx.author.display_name)
        try:
            from services.onboarding import was_welcomed, try_send_welcome_dm
            if not await was_welcomed(ctx.db, int(ctx.author.id)):
                prefix = getattr(ctx, "prefix", None) or ","
                asyncio.create_task(
                    try_send_welcome_dm(
                        ctx.bot, ctx.author, ctx.guild, prefix=prefix,
                    ),
                    name=f"welcome-dm-{ctx.author.id}",
                )
                # V3 anti-alt: piggyback on the same first-touch path.
                # twin_join_check returns the other user_id when two
                # accounts registered within 60s of each other; if so we
                # flag the pair as a soft signal that the staff dashboard
                # surfaces. Strictly observational -- no auto-bans, no
                # user-facing change. Wrapped in its own try so a missing
                # security_signals table never blocks command flow.
                try:
                    asyncio.create_task(
                        _twin_join_flag(
                            ctx.db, int(ctx.guild.id), int(ctx.author.id),
                        ),
                        name=f"anti-alt-twin-{ctx.author.id}",
                    )
                except Exception:
                    pass
        except Exception:
            log.debug("welcome-dm dispatch failed", exc_info=True)
        return await func(*args, **kwargs)

    return wrapper

def _is_guild_admin(ctx) -> bool:
    """True if the invoking member has Manage Guild (admin) permission."""
    member = ctx.author
    return bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False))


async def module_allowed(ctx, module_name: str) -> bool:
    """Return True if the module is enabled OR the caller is a guild admin."""
    if _is_guild_admin(ctx):
        return True
    if ctx.guild:
        return await ctx.bot.db.module_enabled(ctx.guild.id, module_name)
    return True


async def module_cog_check(bot, ctx, module_name: str) -> bool:
    """Shared cog_check logic: reject if the given module is disabled for the guild.
    Guild admins (Manage Guild) always bypass module toggles."""
    if ctx.guild and not await module_allowed(ctx, module_name):
        raise commands.CheckFailure(f"The **{module_name}** module is disabled on this server.")
    return True

# Features that require explicit opt-in even for server admins.
# Admins must be granted access via ,gm beta grant just like everyone else.
_EXPLICIT_OPT_IN: frozenset[str] = frozenset({"command_chains"})

async def check_beta_access(bot, guild, member, feature_name: str) -> bool:
    """Check if a member has beta access to a feature.

    Access is granted if:
    1. Feature is NOT in _EXPLICIT_OPT_IN AND member has Manage Server permission, OR
    2. Member has a direct user grant for this feature, OR
    3. Member has a role that was granted access to this feature.

    Returns True if allowed, False otherwise.
    """
    if not guild or not member:
        return False
    # Admins get automatic access unless the feature requires explicit opt-in
    if member.guild_permissions.manage_guild and feature_name not in _EXPLICIT_OPT_IN:
        return True
    # Check DB for beta grants (required for opt-in features; fallback for others)
    try:
        role_ids = [r.id for r in member.roles]
        return await bot.db.guilds.has_beta_access(guild.id, feature_name, member.id, role_ids)
    except Exception:
        return False  # fail closed for beta features

# All registered beta feature names
BETA_FEATURES = {
    "command_chains": "Multi-command chains (&&, >, ;, ||, |, +)",
    "internal_commands": "Internal bot commands (bot <cmd>, /discoin)",
    "price_alerts": "DM notifications when tokens hit price targets",
    "drs_commands": "DRS Terminal (.drs) command group  -  manage DRS operators and player assist tools",
}

def _raise(exc: Exception):
    raise exc
