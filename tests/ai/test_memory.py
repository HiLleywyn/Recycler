"""MemoryService unit tests against fake Redis + MockDB-style db."""
from __future__ import annotations


import pytest

from ai.memory import MemoryService, Turn, guild_scope, lore_scope, user_scope


# ── Fake Redis ──────────────────────────────────────────────────────────

class FakeRedis:
    """Minimal subset of redis.asyncio used by MemoryService + RateLimiter."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def lpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        if key in self.lists:
            self.lists[key] = self.lists[key][start:stop + 1]

    async def expire(self, key: str, ttl: int) -> None:
        self.expirations[key] = ttl

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        end = None if stop == -1 else stop + 1
        return items[start:end]

    async def delete(self, key: str) -> int:
        existed = 1 if key in self.lists or key in self.kv else 0
        self.lists.pop(key, None)
        self.kv.pop(key, None)
        return existed

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = str(value)
        if ex:
            self.expirations[key] = ex

    async def incr(self, key: str) -> int:
        n = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(n)
        return n


# ── Fake DB ──────────────────────────────────────────────────────────────

class FakeDB:
    """In-memory facts/episodes table good enough for memory_service tests."""

    def __init__(self) -> None:
        self.facts: dict[tuple[str, str], dict] = {}
        self.episodes: list[dict] = []
        self._next_episode_id = 1

    async def execute(self, query: str, *args) -> str:
        q = " ".join(query.split()).lower()
        if q.startswith("insert into disco_facts"):
            scope, key, value, conf, source = args
            self.facts[(scope, key)] = {
                "scope": scope, "key": key, "value": value,
                "confidence": float(conf), "source": source,
                "updated_at": 1.0,
            }
            return "INSERT 0 1"
        if q.startswith("delete from disco_facts"):
            scope, key = args
            existed = self.facts.pop((scope, key), None) is not None
            return f"DELETE {1 if existed else 0}"
        return "OK"

    async def fetch_all(self, query: str, *args) -> list[dict]:
        q = " ".join(query.split()).lower()
        if "from disco_facts" in q and "ilike" not in q:
            scope, _limit = args
            rows = [v for (s, _), v in self.facts.items() if s == scope]
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            return rows
        if "from disco_facts" in q and "ilike" in q:
            scope, pattern, _query, _limit = args
            needle = pattern.strip("%").lower()
            rows = [
                v for (s, _), v in self.facts.items()
                if s == scope and (needle in v["value"].lower() or needle in v["key"].lower())
            ]
            return rows
        if "from disco_episodes" in q:
            scope, pattern, exact_tag, _limit = args
            needle = pattern.strip("%").lower()
            return [
                e for e in self.episodes
                if e["scope"] == scope
                and (needle in e["summary"].lower() or exact_tag in e["tags"])
            ]
        return []

    async def fetch_one(self, query: str, *args) -> dict | None:
        q = " ".join(query.split()).lower()
        if q.startswith("insert into disco_episodes"):
            scope, summary, tags = args
            row = {
                "id": self._next_episode_id, "scope": scope,
                "summary": summary, "tags": list(tags),
                "created_at": 1.0,
            }
            self._next_episode_id += 1
            self.episodes.append(row)
            return {"id": row["id"]}
        return None


# ── Tests ──────────────────────────────────────────────────────────────

@pytest.fixture
def memory():
    return MemoryService(
        db=FakeDB(), redis=FakeRedis(),
        short_term_turns=4, short_term_ttl_s=60,
    )


@pytest.mark.asyncio
async def test_short_term_trim(memory):
    for i in range(10):
        await memory.append_turn(1, 2, 3, Turn("user", f"msg{i}", float(i)))
    turns = await memory.get_recent_turns(1, 2, 3)
    # Only the cap (4) most recent turns survive
    assert len(turns) == 4
    # Order is chronological
    assert [t.content for t in turns] == ["msg6", "msg7", "msg8", "msg9"]


@pytest.mark.asyncio
async def test_short_term_clear(memory):
    await memory.append_turn(1, 2, 3, Turn("user", "hi", 1.0))
    n = await memory.clear(1, 2, 3)
    assert n == 1
    assert await memory.get_recent_turns(1, 2, 3) == []


@pytest.mark.asyncio
async def test_fact_upsert_replaces_value(memory):
    scope = user_scope(99, 5)
    await memory.upsert_fact(scope, "favorite_token", "DSC", 0.7, "tool")
    await memory.upsert_fact(scope, "favorite_token", "MTA", 0.9, "model")
    facts = await memory.get_facts(scope)
    assert len(facts) == 1
    assert facts[0].value == "MTA"
    assert facts[0].confidence == 0.9
    assert facts[0].source == "model"


@pytest.mark.asyncio
async def test_search_facts_substring(memory):
    scope = lore_scope()
    await memory.upsert_fact(scope, "k1", "DSD is the stablecoin", 0.8, "admin")
    await memory.upsert_fact(scope, "k2", "MOON is a wrapped token", 0.8, "admin")
    hits = await memory.search_facts(scope, "stablecoin")
    assert len(hits) == 1
    assert hits[0].key == "k1"


@pytest.mark.asyncio
async def test_episode_record_and_search(memory):
    scope = guild_scope(7)
    eid = await memory.record_episode(scope, "talked about LP risk", tags=["lp", "risk"])
    assert eid is not None
    found = await memory.search_episodes(scope, "lp")
    assert len(found) == 1
    assert "lp" in found[0].tags


@pytest.mark.asyncio
async def test_short_term_no_redis_does_not_raise():
    """If Redis isn't available, every short-term call must no-op cleanly."""
    mem = MemoryService(db=FakeDB(), redis=None)
    await mem.append_turn(1, 2, 3, Turn("user", "hi", 1.0))
    assert await mem.get_recent_turns(1, 2, 3) == []
    assert await mem.clear(1, 2, 3) == 0
