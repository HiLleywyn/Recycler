"""DiscoAI memory facade.

Three layers:
  - Short-term: per-(guild, channel, user) Redis list of recent turns.
  - Long-term facts: Postgres table `disco_facts`, keyed by (scope, key).
  - Episodic: Postgres table `disco_episodes`, full-text-ish recall by tags.

The facts and episodes tables are in migration 0123_disco_ai.sql. They
are the only tables the ai/ module touches directly -- everything else
goes through the FastAPI surface per the architectural contract.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class Turn:
    """One short-term conversation turn."""

    role: str           # "user" or "assistant"
    content: str
    ts: float           # epoch seconds


@dataclass
class Fact:
    """One row from disco_facts."""

    scope: str
    key: str
    value: str
    confidence: float
    source: str
    updated_at: float   # epoch seconds


@dataclass
class Episode:
    """One row from disco_episodes."""

    id: int
    scope: str
    summary: str
    tags: list[str]
    created_at: float


# ── Facade ──────────────────────────────────────────────────────────────

class MemoryService:
    """Facade over Redis (short-term) + Postgres (long-term + episodic)."""

    def __init__(
        self,
        *,
        db: Any,
        redis: Any,
        short_term_turns: int = 12,
        short_term_ttl_s: int = 3600,
    ) -> None:
        self._db = db
        self._redis = redis
        self._cap = short_term_turns
        self._ttl = short_term_ttl_s

    # ── Short-term (Redis) ─────────────────────────────────────────────

    @staticmethod
    def _short_key(guild_id: int | None, channel_id: int | None, user_id: int) -> str:
        gid = guild_id if guild_id is not None else 0
        cid = channel_id if channel_id is not None else 0
        return f"disco:mem:{gid}:{cid}:{user_id}"

    async def append_turn(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        turn: Turn,
    ) -> None:
        if self._redis is None:
            return
        key = self._short_key(guild_id, channel_id, user_id)
        payload = json.dumps({"role": turn.role, "content": turn.content, "ts": turn.ts})
        try:
            await self._redis.lpush(key, payload)
            await self._redis.ltrim(key, 0, self._cap - 1)
            await self._redis.expire(key, self._ttl)
        except Exception as exc:
            log.debug("MemoryService.append_turn redis error: %s", exc)

    async def get_recent_turns(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> list[Turn]:
        if self._redis is None:
            return []
        key = self._short_key(guild_id, channel_id, user_id)
        try:
            raw = await self._redis.lrange(key, 0, self._cap - 1)
        except Exception as exc:
            log.debug("MemoryService.get_recent_turns redis error: %s", exc)
            return []
        out: list[Turn] = []
        # Redis stores newest-first via LPUSH; reverse to chronological order.
        for item in reversed(raw or []):
            try:
                d = json.loads(item if isinstance(item, str) else item.decode("utf-8"))
                out.append(Turn(role=d.get("role", "user"), content=d.get("content", ""), ts=float(d.get("ts", 0.0))))
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue
        return out

    async def clear(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> int:
        """Drop every short-term turn for the (guild, channel, user) tuple."""
        if self._redis is None:
            return 0
        key = self._short_key(guild_id, channel_id, user_id)
        try:
            n = await self._redis.delete(key)
            return int(n or 0)
        except Exception as exc:
            log.debug("MemoryService.clear redis error: %s", exc)
            return 0

    async def clear_user_in_guild(self, guild_id: int, user_id: int) -> int:
        """Drop short-term turns for this user across EVERY channel in a guild.

        ``clear`` only wipes the (guild, channel, user) bucket for the channel
        the user ran the command in -- which is wrong for ``,ai recontext``
        because the model keeps replaying turns from other rooms when the
        same user posts there again. This walks the keyspace via SCAN
        looking for ``disco:mem:<gid>:*:<uid>`` and deletes whatever it
        finds. Returns the number of keys removed.
        """
        return await self._scan_delete(f"disco:mem:{guild_id or 0}:*:{user_id}")

    async def clear_guild(self, guild_id: int) -> int:
        """Drop short-term turns for EVERY user in EVERY channel of a guild.

        Backs the ``,ai recontext server`` admin path. Same SCAN-based walk
        as :meth:`clear_user_in_guild`, just with a wider pattern.
        """
        return await self._scan_delete(f"disco:mem:{guild_id or 0}:*")

    async def clear_channel(self, guild_id: int, channel_id: int) -> int:
        """Drop short-term turns for every user in ONE channel of a guild.

        Backs ``,ai recontext channel``: cheaper than a full guild wipe
        when the loop is localised to the room the admin is standing in.
        """
        return await self._scan_delete(
            f"disco:mem:{guild_id or 0}:{channel_id or 0}:*",
        )

    async def _scan_delete(self, pattern: str) -> int:
        """SCAN-and-delete helper. Cooperative -- never blocks the Redis loop."""
        if self._redis is None:
            return 0
        removed = 0
        try:
            async for key in self._redis.scan_iter(match=pattern, count=100):
                try:
                    n = await self._redis.delete(key)
                    removed += int(n or 0)
                except Exception:
                    continue
        except Exception as exc:
            log.debug("MemoryService._scan_delete(%s) error: %s", pattern, exc)
        return removed

    # ── Long-term facts (Postgres) ─────────────────────────────────────

    async def upsert_fact(
        self,
        scope: str,
        key: str,
        value: str,
        confidence: float = 0.7,
        source: str = "tool",
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO disco_facts (scope, key, value, confidence, source, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (scope, key) DO UPDATE
            SET value = EXCLUDED.value,
                confidence = EXCLUDED.confidence,
                source = EXCLUDED.source,
                updated_at = NOW()
            """,
            scope, key, value, float(confidence), source,
        )

    async def get_facts(self, scope: str, limit: int = 20) -> list[Fact]:
        rows = await self._db.fetch_all(
            """
            SELECT scope, key, value, confidence, source,
                   EXTRACT(EPOCH FROM updated_at) AS updated_at
            FROM disco_facts
            WHERE scope = $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            scope, int(limit),
        )
        return [_row_to_fact(r) for r in rows]

    async def search_facts(
        self,
        scope: str,
        query: str,
        limit: int = 5,
    ) -> list[Fact]:
        # Hybrid recall: ILIKE for exact substrings, pg_trgm similarity for
        # fuzzy/typo matches, ordered by relevance with recency as tiebreaker.
        # The 0123 trigram GIN index accelerates the ILIKE branch. pgvector
        # cosine search remains future work once an embedding column lands.
        rows = await self._db.fetch_all(
            """
            SELECT scope, key, value, confidence, source,
                   EXTRACT(EPOCH FROM updated_at) AS updated_at
            FROM disco_facts
            WHERE scope = $1
              AND (value ILIKE $2 OR key ILIKE $2
                   OR similarity(value, $3) > 0.2
                   OR similarity(key, $3) > 0.2)
            ORDER BY GREATEST(similarity(value, $3), similarity(key, $3)) DESC,
                     updated_at DESC
            LIMIT $4
            """,
            scope, f"%{query}%", query, int(limit),
        )
        return [_row_to_fact(r) for r in rows]

    async def forget_fact(self, scope: str, key: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM disco_facts WHERE scope = $1 AND key = $2",
            scope, key,
        )
        # asyncpg returns "DELETE n"
        return isinstance(status, str) and status.endswith(" 1")

    # ── Episodic (Postgres) ────────────────────────────────────────────

    async def record_episode(
        self,
        scope: str,
        summary: str,
        tags: list[str] | None = None,
    ) -> int | None:
        row = await self._db.fetch_one(
            """
            INSERT INTO disco_episodes (scope, summary, tags)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            scope, summary, list(tags or []),
        )
        return int(row["id"]) if row else None

    async def search_episodes(
        self,
        scope: str,
        query: str,
        limit: int = 3,
    ) -> list[Episode]:
        # Hybrid recall: ILIKE for substrings, exact tag match, and pg_trgm
        # similarity for fuzzy summary matches. Ordered by similarity with
        # recency as tiebreaker. pgvector cosine search remains future work.
        rows = await self._db.fetch_all(
            """
            SELECT id, scope, summary, tags,
                   EXTRACT(EPOCH FROM created_at) AS created_at
            FROM disco_episodes
            WHERE scope = $1
              AND (summary ILIKE $2
                   OR $3 = ANY (tags)
                   OR similarity(summary, $3) > 0.2)
            ORDER BY similarity(summary, $3) DESC, created_at DESC
            LIMIT $4
            """,
            scope, f"%{query}%", query, int(limit),
        )
        return [
            Episode(
                id=int(r["id"]),
                scope=r["scope"],
                summary=r["summary"],
                tags=list(r["tags"] or []),
                created_at=float(r.get("created_at") or 0.0),
            )
            for r in rows
        ]


def _row_to_fact(r: dict) -> Fact:
    return Fact(
        scope=r["scope"],
        key=r["key"],
        value=r["value"],
        confidence=float(r.get("confidence") or 0.0),
        source=r.get("source", ""),
        updated_at=float(r.get("updated_at") or 0.0),
    )


# ── Scope helpers ──────────────────────────────────────────────────────

def user_scope(user_id: int, guild_id: int | None = None) -> str:
    """Canonical scope string for a user (optionally namespaced by guild)."""
    if guild_id is None:
        return f"user:{user_id}"
    return f"user:{user_id}:guild:{guild_id}"


def guild_scope(guild_id: int) -> str:
    return f"guild:{guild_id}"


def lore_scope() -> str:
    return "lore"


def now_ts() -> float:
    return time.time()
