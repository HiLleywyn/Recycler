"""
security/behavior_profile.py  -  Per-user behavioral baselines and anomaly detection.

Maintains rolling statistical baselines for each user, stored in Redis.
Anomalies are detected when current behavior deviates significantly from
the user's established baseline (> N standard deviations).
"""
from __future__ import annotations

import math
import time
import logging
from typing import Any

from security.config import (
    ANOMALY_STDDEV_THRESHOLD,
    BASELINE_MIN_SAMPLES,
)
from security.models import (
    SecurityEvent,
    BehaviorBaseline,
    UserSecurityProfile,
    RiskLevel,
)
from security.redis_cache import SecurityRedisCache

log = logging.getLogger("discoin.security.profiler")


class BehaviorProfiler:
    """Manages user behavior baselines and detects anomalies."""

    def __init__(self, cache: SecurityRedisCache) -> None:
        self.cache = cache

    async def get_profile(self, guild_id: int, user_id: int) -> UserSecurityProfile:
        """Load or create a user's security profile."""
        raw = await self.cache.get_profile(guild_id, user_id)
        if raw:
            try:
                return UserSecurityProfile(**raw)
            except Exception:
                pass
        return UserSecurityProfile(user_id=user_id, guild_id=guild_id)

    async def save_profile(self, profile: UserSecurityProfile) -> None:
        """Persist profile to Redis."""
        await self.cache.set_profile(
            profile.guild_id,
            profile.user_id,
            profile.model_dump(mode="json"),
        )

    async def update_from_event(self, event: SecurityEvent) -> UserSecurityProfile:
        """Update the user's behavior profile with a new event.

        This is called on every event to build up the baseline over time.
        The actual baseline recalculation happens periodically (not every event).
        """
        profile = await self.get_profile(event.guild_id, event.user_id)
        baseline = profile.baseline

        # Track the event in the rolling window
        baseline.sample_count += 1

        # Update IP tracking
        if event.ip_address and event.ip_address not in profile.known_ips:
            profile.known_ips.append(event.ip_address)
            # Keep only last 20 IPs
            if len(profile.known_ips) > 20:
                profile.known_ips = profile.known_ips[-20:]

        # Update active hours
        hour = time.gmtime(event.timestamp).tm_hour
        if hour not in baseline.active_hours:
            baseline.active_hours.append(hour)
            baseline.active_hours.sort()

        # Update transaction type frequency (incremental mean)
        if event.tx_type:
            current_mean = baseline.tx_per_hour.get(event.tx_type, 0.0)
            n = baseline.sample_count
            # Simple incremental mean update
            baseline.tx_per_hour[event.tx_type] = current_mean + (1.0 - current_mean) / max(n, 1)

        # Update amount baselines (incremental Welford's algorithm)
        if event.amount_usd is not None and event.tx_type:
            self._update_amount_stats(baseline, event.tx_type, event.amount_usd)

        # API request tracking
        if event.source.value == "api":
            baseline.api_requests_per_hour = (
                baseline.api_requests_per_hour * 0.95 + 0.05  # EMA
            )

        baseline.last_updated = time.time()
        profile.baseline = baseline

        await self.save_profile(profile)
        return profile

    def _update_amount_stats(
        self, baseline: BehaviorBaseline, tx_type: str, amount: float,
    ) -> None:
        """Incremental update of mean and stddev for transaction amounts."""
        old_mean = baseline.avg_amount.get(tx_type, 0.0)
        old_std = baseline.avg_amount_std.get(tx_type, 0.0)
        n = baseline.sample_count

        if n <= 1:
            baseline.avg_amount[tx_type] = amount
            baseline.avg_amount_std[tx_type] = 0.0
            return

        # Welford's online algorithm for running mean/variance
        new_mean = old_mean + (amount - old_mean) / n
        # Approximate stddev update (simplified  -  full Welford needs M2 accumulator,
        # but this is good enough for anomaly detection)
        delta = amount - old_mean
        delta2 = amount - new_mean
        variance = max(0, old_std ** 2 + (delta * delta2 - old_std ** 2) / n)
        new_std = math.sqrt(variance)

        baseline.avg_amount[tx_type] = new_mean
        baseline.avg_amount_std[tx_type] = new_std

    def check_anomalies(
        self, event: SecurityEvent, profile: UserSecurityProfile,
    ) -> list[dict[str, Any]]:
        """Check if the current event is anomalous relative to the user's baseline.

        Returns a list of anomaly descriptions (empty = normal behavior).
        """
        baseline = profile.baseline
        anomalies: list[dict[str, Any]] = []

        # Need minimum samples before we can detect anomalies
        if baseline.sample_count < BASELINE_MIN_SAMPLES:
            return []

        # Check transaction amount anomaly
        if event.amount_usd is not None and event.tx_type:
            mean = baseline.avg_amount.get(event.tx_type, 0.0)
            std = baseline.avg_amount_std.get(event.tx_type, 0.0)

            if std > 0 and mean > 0:
                z_score = abs(event.amount_usd - mean) / std
                if z_score > ANOMALY_STDDEV_THRESHOLD:
                    anomalies.append({
                        "type": "amount_anomaly",
                        "tx_type": event.tx_type,
                        "amount": event.amount_usd,
                        "mean": mean,
                        "std": std,
                        "z_score": z_score,
                        "description": (
                            f"Unusual amount: ${event.amount_usd:,.2f} for {event.tx_type} "
                            f"(baseline: ${mean:,.2f} +/- ${std:,.2f}, z={z_score:.1f})"
                        ),
                    })

        # Check unusual hour
        if baseline.active_hours:
            hour = time.gmtime(event.timestamp).tm_hour
            if hour not in baseline.active_hours and len(baseline.active_hours) >= 5:
                anomalies.append({
                    "type": "unusual_hour",
                    "hour": hour,
                    "typical_hours": baseline.active_hours,
                    "description": (
                        f"Activity at unusual hour: {hour}:00 UTC "
                        f"(typical: {', '.join(f'{h}:00' for h in baseline.active_hours[:5])})"
                    ),
                })

        # Check unknown IP
        if event.ip_address and profile.known_ips:
            if event.ip_address not in profile.known_ips and len(profile.known_ips) >= 3:
                anomalies.append({
                    "type": "new_ip",
                    "ip": event.ip_address,
                    "known_ips_count": len(profile.known_ips),
                    "description": (
                        f"New IP address: {event.ip_address} "
                        f"(user has {len(profile.known_ips)} known IPs)"
                    ),
                })

        return anomalies

    async def update_risk_level(
        self, profile: UserSecurityProfile, threat_score: float,
    ) -> UserSecurityProfile:
        """Update the profile's risk level based on current threat score."""
        if threat_score >= 81:
            profile.risk_level = RiskLevel.CRITICAL
        elif threat_score >= 61:
            profile.risk_level = RiskLevel.HIGH
        elif threat_score >= 41:
            profile.risk_level = RiskLevel.ELEVATED
        else:
            profile.risk_level = RiskLevel.NORMAL

        await self.save_profile(profile)
        return profile
