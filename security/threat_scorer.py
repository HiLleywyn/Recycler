"""
security/threat_scorer.py  -  Composite threat score with exponential decay.

Score range: 0-100.  Each detection adds points based on severity weight.
Score decays exponentially with a configurable half-life (default 1 hour).
"""
from __future__ import annotations

import math
import time
import logging

from security.config import (
    SCORE_DECAY_HALF_LIFE,
    LEVEL_1_THRESHOLD,
    LEVEL_2_THRESHOLD,
    LEVEL_3_THRESHOLD,
    LEVEL_4_THRESHOLD,
    LEVEL_5_THRESHOLD,
)
from security.models import (
    ThreatDetection,
    ResponseLevel,
)
from security.redis_cache import SecurityRedisCache

log = logging.getLogger("discoin.security.scorer")


class ThreatScorer:
    """Manages per-user threat scores with decay."""

    def __init__(self, cache: SecurityRedisCache) -> None:
        self.cache = cache

    async def get_current_score(self, guild_id: int, user_id: int) -> float:
        """Get the current threat score after applying decay."""
        raw = await self.cache.get("score", guild_id, user_id)
        if raw is None:
            return 0.0

        if isinstance(raw, (int, float)):
            return max(0.0, min(100.0, float(raw)))

        stored_score = float(raw.get("score", 0.0))
        updated_at = float(raw.get("updated_at", time.time()))

        return self._apply_decay(stored_score, updated_at)

    def _apply_decay(self, score: float, updated_at: float) -> float:
        """Apply exponential decay to a score based on elapsed time."""
        elapsed = time.time() - updated_at
        if elapsed <= 0 or score <= 0:
            return max(0.0, score)

        # Exponential decay: score * 0.5^(elapsed / half_life)
        decay_factor = math.pow(0.5, elapsed / SCORE_DECAY_HALF_LIFE)
        decayed = score * decay_factor

        # Floor very small scores to 0
        if decayed < 0.5:
            return 0.0
        return min(100.0, decayed)

    async def add_detections(
        self,
        guild_id: int,
        user_id: int,
        detections: list[ThreatDetection],
    ) -> tuple[float, float]:
        """Add detection scores to user's threat score.

        Returns (previous_score, new_score).
        """
        if not detections:
            current = await self.get_current_score(guild_id, user_id)
            return current, current

        previous_score = await self.get_current_score(guild_id, user_id)

        # Sum up score deltas from all detections
        total_delta = sum(d.score_delta for d in detections)

        new_score = min(100.0, previous_score + total_delta)

        # Persist updated score
        now = time.time()
        await self.cache.set_threat_score(guild_id, user_id, new_score, updated_at=now)

        log.info(
            "Threat score update: guild=%d user=%d %.1f → %.1f (+%.1f from %d detections)",
            guild_id, user_id, previous_score, new_score, total_delta, len(detections),
        )

        return previous_score, new_score

    def determine_response_level(self, score: float) -> ResponseLevel:
        """Map a threat score to the appropriate response level."""
        if score >= LEVEL_5_THRESHOLD:
            return ResponseLevel.LOCKDOWN
        if score >= LEVEL_4_THRESHOLD:
            return ResponseLevel.FLAG
        if score >= LEVEL_3_THRESHOLD:
            return ResponseLevel.FREEZE
        if score >= LEVEL_2_THRESHOLD:
            return ResponseLevel.THROTTLE
        if score >= LEVEL_1_THRESHOLD:
            return ResponseLevel.LOG
        return ResponseLevel.NONE

    async def reset_score(self, guild_id: int, user_id: int) -> None:
        """Admin action: reset a user's threat score to 0."""
        await self.cache.set_threat_score(guild_id, user_id, 0.0)
        log.info("Threat score reset: guild=%d user=%d", guild_id, user_id)
