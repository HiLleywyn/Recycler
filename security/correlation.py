"""
security/correlation.py  -  Cross-platform event correlation.

Tracks when the same user is active on both Discord bot and Dashboard API
within a short window.  Legitimate users typically use one at a time;
simultaneous heavy usage from both platforms is suspicious.
"""
from __future__ import annotations

import logging

from security.models import SecurityEvent
from security.redis_cache import SecurityRedisCache

log = logging.getLogger("discoin.security.correlation")


class CrossPlatformCorrelator:
    """Correlates events across bot and API for the same user."""

    def __init__(self, cache: SecurityRedisCache) -> None:
        self.cache = cache

    async def record_and_get(self, event: SecurityEvent) -> dict:
        """Record an event source and return current correlation state.

        Returns dict with:
            bot_events: int
            api_events: int
            last_bot_ts: float
            last_api_ts: float
        """
        return await self.cache.update_correlation(
            event.guild_id,
            event.user_id,
            event.source.value,
        )

    async def get_correlation(self, guild_id: int, user_id: int) -> dict:
        """Get current correlation data without recording."""
        return await self.cache.get_correlation_events(guild_id, user_id)
