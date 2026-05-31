"""
security/redis_cache.py  -  Redis caching layer for all security state.

Every method gracefully degrades to in-memory fallback when Redis is
unavailable.  All keys are namespaced under ``discoin:sec:`` with explicit
TTLs to prevent unbounded growth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

from security.config import (
    REDIS_PREFIX,
    PROFILE_TTL,
    BASELINE_TTL,
    ALERT_DEDUP_TTL,
)

log = logging.getLogger("discoin.security.cache")


class SecurityRedisCache:
    """Redis-backed security state with in-memory fallback."""

    def __init__(self, redis=None) -> None:
        self._redis = redis
        # In-memory fallback stores
        self._mem_store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, value)
        self._mem_sorted: dict[str, list[tuple[float, str]]] = defaultdict(list)  # key → [(score, member)]
        self._mem_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._redis is not None

    def _key(self, *parts: str | int) -> str:
        return f"{REDIS_PREFIX}:{':'.join(str(p) for p in parts)}"

    # ── Generic GET / SET ────────────────────────────────────────────────────

    async def get(self, *key_parts: str | int) -> Any | None:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return json.loads(raw) if raw else None
            except Exception as exc:
                log.debug("Redis GET failed (%s), using memory fallback", exc)
        # Memory fallback
        async with self._mem_lock:
            entry = self._mem_store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at and time.time() > expires_at:
                del self._mem_store[key]
                return None
            return value

    async def set(self, *key_parts_and_value: Any, ttl: int = 3600) -> None:
        """set("score", guild_id, user_id, value, ttl=3600)"""
        *key_parts, value = key_parts_and_value
        key = self._key(*key_parts)
        serialized = json.dumps(value, default=str)

        if self._redis is not None:
            try:
                await self._redis.setex(key, ttl, serialized)
                return
            except Exception as exc:
                log.debug("Redis SET failed (%s), using memory fallback", exc)
        # Memory fallback
        async with self._mem_lock:
            self._mem_store[key] = (time.time() + ttl, value)

    async def delete(self, *key_parts: str | int) -> None:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
                return
            except Exception as exc:
                log.debug("Redis DEL failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            self._mem_store.pop(key, None)

    async def incr(self, *key_parts: str | int, ttl: int = 60) -> int:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                count = await self._redis.incr(key)
                if count == 1:
                    await self._redis.expire(key, ttl)
                return count
            except Exception as exc:
                log.debug("Redis INCR failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            entry = self._mem_store.get(key)
            if entry is None or (entry[0] and time.time() > entry[0]):
                self._mem_store[key] = (time.time() + ttl, 1)
                return 1
            _, val = entry
            new_val = (val or 0) + 1
            self._mem_store[key] = (entry[0], new_val)
            return new_val

    # ── Sorted Set Operations (for time-series events) ───────────────────────

    async def zadd(self, key_parts: tuple[str | int, ...], score: float, member: str, ttl: int = 3600) -> None:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                await self._redis.zadd(key, {member: score})
                await self._redis.expire(key, ttl)
                return
            except Exception as exc:
                log.debug("Redis ZADD failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            entries = self._mem_sorted[key]
            entries.append((score, member))
            # Keep sorted and bounded
            entries.sort(key=lambda x: x[0])
            if len(entries) > 500:
                self._mem_sorted[key] = entries[-500:]

    async def zrangebyscore(self, key_parts: tuple[str | int, ...], min_score: float, max_score: float) -> list[str]:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                return await self._redis.zrangebyscore(key, min_score, max_score)
            except Exception as exc:
                log.debug("Redis ZRANGEBYSCORE failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            entries = self._mem_sorted.get(key, [])
            return [m for s, m in entries if min_score <= s <= max_score]

    async def zcard(self, key_parts: tuple[str | int, ...]) -> int:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                return await self._redis.zcard(key)
            except Exception as exc:
                log.debug("Redis ZCARD failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            return len(self._mem_sorted.get(key, []))

    async def zremrangebyscore(self, key_parts: tuple[str | int, ...], min_score: float, max_score: float) -> None:
        key = self._key(*key_parts)
        if self._redis is not None:
            try:
                await self._redis.zremrangebyscore(key, min_score, max_score)
                return
            except Exception as exc:
                log.debug("Redis ZREMRANGEBYSCORE failed (%s), falling back to memory", exc)
        async with self._mem_lock:
            entries = self._mem_sorted.get(key, [])
            self._mem_sorted[key] = [e for e in entries if not (min_score <= e[0] <= max_score)]

    # ── High-Level Security Operations ───────────────────────────────────────

    async def get_threat_score(self, guild_id: int, user_id: int) -> float:
        result = await self.get("score", guild_id, user_id)
        if result is None:
            return 0.0
        if isinstance(result, dict):
            return float(result.get("score", 0.0))
        return float(result)

    async def set_threat_score(self, guild_id: int, user_id: int, score: float, updated_at: float | None = None) -> None:
        await self.set("score", guild_id, user_id, {
            "score": score,
            "updated_at": updated_at or time.time(),
        }, ttl=3600)

    async def record_event(self, guild_id: int, user_id: int, event_data: dict) -> None:
        """Add a security event to the user's recent events sorted set."""
        ts = event_data.get("timestamp", time.time())
        await self.zadd(
            ("events", guild_id, user_id),
            score=ts,
            member=json.dumps(event_data, default=str),
            ttl=3600,
        )

    async def get_recent_events(self, guild_id: int, user_id: int, window_seconds: int = 300) -> list[dict]:
        now = time.time()
        raw = await self.zrangebyscore(
            ("events", guild_id, user_id),
            min_score=now - window_seconds,
            max_score=now,
        )
        events = []
        for item in raw:
            try:
                events.append(json.loads(item) if isinstance(item, str) else item)
            except (json.JSONDecodeError, TypeError):
                pass
        return events

    async def get_profile(self, guild_id: int, user_id: int) -> dict | None:
        return await self.get("profile", guild_id, user_id)

    async def set_profile(self, guild_id: int, user_id: int, profile: dict) -> None:
        await self.set("profile", guild_id, user_id, profile, ttl=PROFILE_TTL)

    async def get_enforcement(self, guild_id: int, user_id: int) -> dict | None:
        """Get active enforcement for a user. Returns None if no active enforcement."""
        result = await self.get("enforce", guild_id, user_id)
        if result is None:
            return None
        # Check expiry
        expires_at = result.get("expires_at")
        if expires_at and time.time() > expires_at:
            await self.delete("enforce", guild_id, user_id)
            return None
        return result

    async def set_enforcement(self, guild_id: int, user_id: int, enforcement: dict) -> None:
        expires_at = enforcement.get("expires_at")
        if expires_at:
            ttl = max(1, int(expires_at - time.time()))
        else:
            ttl = 86400  # 24h for permanent enforcements
        await self.set("enforce", guild_id, user_id, enforcement, ttl=ttl)

    async def clear_enforcement(self, guild_id: int, user_id: int) -> None:
        await self.delete("enforce", guild_id, user_id)

    async def check_enforcement(self, guild_id: int, user_id: int, scope: str) -> dict | None:
        """Check if user has an active enforcement that blocks a given scope."""
        enforcement = await self.get_enforcement(guild_id, user_id)
        if enforcement is None:
            return None
        enf_scope = enforcement.get("scope", "")
        if enf_scope == "all" or enf_scope == scope:
            return enforcement
        return None

    async def get_circuit_breaker(self, guild_id: int, feature: str) -> dict | None:
        return await self.get("circuit", guild_id, feature)

    async def set_circuit_breaker(self, guild_id: int, feature: str, data: dict, ttl: int = 1800) -> None:
        await self.set("circuit", guild_id, feature, data, ttl=ttl)

    async def clear_circuit_breaker(self, guild_id: int, feature: str) -> None:
        await self.delete("circuit", guild_id, feature)

    async def is_alert_duplicate(self, alert_hash: str) -> bool:
        result = await self.get("alert_dedup", alert_hash)
        return result is not None

    async def mark_alert_sent(self, alert_hash: str) -> None:
        await self.set("alert_dedup", alert_hash, True, ttl=ALERT_DEDUP_TTL)

    async def get_ip_reputation(self, ip: str) -> dict | None:
        return await self.get("ip", ip)

    async def update_ip_reputation(self, ip: str, data: dict) -> None:
        await self.set("ip", ip, data, ttl=86400)

    async def record_api_request(self, guild_id: int, user_id: int, endpoint: str) -> int:
        """Record an API request and return the count in the current window."""
        window = int(time.time()) // 60  # 1-minute window
        return await self.incr("apireq", guild_id, user_id, window, ttl=120)

    async def record_auth_failure(self, ip: str) -> int:
        """Record a failed auth attempt and return count in window."""
        window = int(time.time()) // 300  # 5-minute window
        return await self.incr("authfail", ip, window, ttl=600)

    async def record_command(self, guild_id: int, user_id: int, command: str) -> tuple[int, int]:
        """Record a bot command. Returns (total_commands, identical_commands) in window."""
        window = int(time.time()) // 60
        total = await self.incr("cmd", guild_id, user_id, window, ttl=120)
        identical = await self.incr("cmd_id", guild_id, user_id, command, window, ttl=120)
        return total, identical

    async def get_correlation_events(self, guild_id: int, user_id: int) -> dict:
        """Get cross-platform correlation data."""
        result = await self.get("correlation", guild_id, user_id)
        return result or {"bot_events": 0, "api_events": 0, "last_bot_ts": 0, "last_api_ts": 0}

    async def update_correlation(self, guild_id: int, user_id: int, source: str) -> dict:
        """Update and return cross-platform correlation counters."""
        data = await self.get_correlation_events(guild_id, user_id)
        now = time.time()
        if source == "bot":
            data["bot_events"] = data.get("bot_events", 0) + 1
            data["last_bot_ts"] = now
        else:
            data["api_events"] = data.get("api_events", 0) + 1
            data["last_api_ts"] = now
        await self.set("correlation", guild_id, user_id, data, ttl=300)
        return data

    async def get_baseline(self, guild_id: int) -> dict | None:
        return await self.get("baseline", guild_id)

    async def set_baseline(self, guild_id: int, baseline: dict) -> None:
        await self.set("baseline", guild_id, baseline, ttl=BASELINE_TTL)

    async def get_session_fingerprint(self, token_hash: str) -> dict | None:
        return await self.get("session", token_hash)

    async def set_session_fingerprint(self, token_hash: str, fingerprint: dict, ttl: int = 604800) -> None:
        await self.set("session", token_hash, fingerprint, ttl=ttl)

    async def clear_all_enforcements(self) -> int:
        """Remove all cached enforcement entries across all guilds/users.

        Called once on startup to reset locks. Returns count removed.
        """
        prefix = self._key("enforce")  # e.g. "discoin:sec:enforce"
        removed = 0
        if self._redis is not None:
            try:
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(cursor, match=f"{prefix}:*", count=100)
                    if keys:
                        await self._redis.delete(*keys)
                        removed += len(keys)
                    if cursor == 0:
                        break
                return removed
            except Exception as exc:
                log.warning("Redis enforcement clear failed: %s", exc)
        # Memory fallback
        async with self._mem_lock:
            pattern = f"{prefix}:"
            keys_to_del = [k for k in self._mem_store if k.startswith(pattern)]
            for k in keys_to_del:
                del self._mem_store[k]
                removed += 1
        return removed

    # ── Cleanup ──────────────────────────────────────────────────────────────

    async def cleanup_expired(self) -> int:
        """Clean up expired in-memory entries. Returns count removed."""
        now = time.time()
        removed = 0
        async with self._mem_lock:
            expired = [k for k, (exp, _) in self._mem_store.items() if exp and now > exp]
            for k in expired:
                del self._mem_store[k]
                removed += 1
        return removed
