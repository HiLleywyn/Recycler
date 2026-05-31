"""
core/framework/cooldowns.py  -  Decorator-based cooldown helpers for Discoin.

Provides friendlier remaining-time messages than discord.py's default.
"""
from __future__ import annotations

from typing import Callable

from discord.ext import commands

_COOLDOWN_MULTIPLIER = 0.5  # Global cooldown scaling factor


def user_cooldown(seconds: float) -> Callable:
    """
    Per-user cooldown decorator. On violation, replies with a friendly
    '⏱ You can do this again in X' message instead of raising CooldownMapping.

    Usage::

        @mine.command()
        @user_cooldown(300)
        async def work(self, ctx): ...
    """
    return commands.cooldown(1, max(1.0, seconds * _COOLDOWN_MULTIPLIER), commands.BucketType.user)

