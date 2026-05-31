"""
core/framework/premium.py  -  @premium_required gate for Discoin commands.

Drop on top of any cog command (innermost decorator after ensure_registered)
to require an active premium subscription on the guild. Host guild and
admin grants are honoured automatically -- see services/entitlements.py.

    @commands.command(name="cast")
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("fishing")
    async def cast(self, ctx): ...

The wrapped function receives a normal call when the gate passes. When
it fails, the user gets ``ctx.reply_premium_required(feature)`` and the
underlying command body is skipped.

Cog-level gating: subclass ``PremiumCog`` (or set ``__premium_feature__``
on the cog) to gate every command in the cog with one feature key. Useful
for the AI cog where literally every subcommand is paid.
"""
from __future__ import annotations

import inspect
import logging
from functools import wraps
from typing import Callable

from discord.ext import commands

log = logging.getLogger(__name__)


def premium_required(feature_key: str) -> Callable:
    """Per-command gate. Mirrors ``ensure_registered``'s function/method
    detection so it works on both free-function commands and cog methods."""
    def decorator(func):
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        is_method = bool(params) and params[0] == "self"

        @wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = args[1] if is_method else args[0]
            # Lazy import: avoids circular import at module-load time
            # (services -> framework -> services).
            from services import entitlements
            try:
                ok = await entitlements.is_premium(ctx.guild_id, ctx.db)
            except Exception:
                log.exception("premium check failed for guild=%s feature=%s",
                              getattr(ctx, "guild_id", None), feature_key)
                # Fail open on infra errors -- we never want a DB blip to
                # lock everyone out of paid features. Audit shows the rate
                # of these so we know if it ever spikes.
                ok = True
            if not ok:
                await ctx.reply_premium_required(feature_key)
                return None
            return await func(*args, **kwargs)

        # Stash the feature key so help/admin tooling can introspect.
        wrapper.__premium_feature__ = feature_key  # type: ignore[attr-defined]
        return wrapper

    return decorator


class PremiumCog(commands.Cog):
    """Cog base class that gates every command with a single feature key.

    Use when ALL commands in a cog should be premium-only (the AI cog is
    the obvious case). Set ``__premium_feature__`` as a class attribute:

        class AI(PremiumCog):
            __premium_feature__ = "ai"
    """

    __premium_feature__: str = ""

    async def cog_check(self, ctx) -> bool:
        feature = getattr(self, "__premium_feature__", "") or ""
        if not feature:
            return True
        from services import entitlements
        try:
            ok = await entitlements.is_premium(ctx.guild_id, ctx.db)
        except Exception:
            log.exception("premium cog_check failed feature=%s", feature)
            return True  # fail open
        if not ok:
            # Raise a CheckFailure subclass so command-error handlers can
            # distinguish premium gates from other check failures.
            raise PremiumGateFailure(feature)
        return True


class PremiumGateFailure(commands.CheckFailure):
    """Raised by PremiumCog.cog_check when the guild lacks premium.
    The cog-level error handler in cogs/premium.py catches this and
    routes to ctx.reply_premium_required."""

    def __init__(self, feature_key: str) -> None:
        self.feature_key = feature_key
        super().__init__(f"Premium required: {feature_key}")
