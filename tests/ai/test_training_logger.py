"""TrainingLogger append-only write + feedback update."""
from __future__ import annotations

import json

import pytest

from ai.training_logger import TrainingLogger


class _FakeDB:
    """Minimal asyncpg-shape stub good enough for TrainingLogger tests."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next_id = 1

    async def fetch_one(self, query: str, *args) -> dict | None:
        # `,ask` calls log_turn -> INSERT ... RETURNING id.
        (
            user_id, guild_id, channel_id,
            system_prompt, user_message, assistant_reply,
            messages_json, tool_calls_json,
            model, rounds, latency_ms, finish_reason,
        ) = args
        row = {
            "id": self._next_id,
            "user_id": user_id, "guild_id": guild_id, "channel_id": channel_id,
            "system_prompt": system_prompt, "user_message": user_message,
            "assistant_reply": assistant_reply,
            "messages_json": json.loads(messages_json),
            "tool_calls_json": json.loads(tool_calls_json),
            "model": model, "rounds": rounds, "latency_ms": latency_ms,
            "finish_reason": finish_reason,
            "feedback_score": None,
        }
        self._next_id += 1
        self.rows.append(row)
        return {"id": row["id"]}

    async def execute(self, query: str, *args) -> str:
        # UPDATE disco_training_turns SET feedback_score = $2 WHERE id = $1
        row_id, score = args
        for r in self.rows:
            if r["id"] == row_id:
                r["feedback_score"] = score
                return "UPDATE 1"
        return "UPDATE 0"

    async def fetch_all(self, *a, **k):
        return list(self.rows)


@pytest.mark.asyncio
async def test_log_turn_minimal_args_writes_row():
    db = _FakeDB()
    logger = TrainingLogger(db)
    trace_id = await logger.log_turn(
        user_id=7, guild_id=1, channel_id=2,
        user_message="hey", assistant_reply="hi",
    )
    assert trace_id == 1
    assert db.rows[0]["user_message"] == "hey"
    assert db.rows[0]["assistant_reply"] == "hi"
    # Default messages reconstruction is a 2-row [user, assistant] pair.
    assert len(db.rows[0]["messages_json"]) == 2
    assert db.rows[0]["tool_calls_json"] == []


@pytest.mark.asyncio
async def test_log_turn_full_messages_round_trip():
    db = _FakeDB()
    logger = TrainingLogger(db)
    full_messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    await logger.log_turn(
        user_id=7, guild_id=1, channel_id=2,
        user_message="hi", assistant_reply="yo",
        messages=full_messages,
        tool_calls=[{"name": "get_price", "args": {"token": "DSC"}}],
        model="google/gemini-2.5-flash",
    )
    row = db.rows[0]
    assert row["system_prompt"] == "be terse"
    assert row["messages_json"] == full_messages
    assert row["tool_calls_json"][0]["name"] == "get_price"
    assert row["model"] == "google/gemini-2.5-flash"


@pytest.mark.asyncio
async def test_record_feedback_updates_score():
    db = _FakeDB()
    logger = TrainingLogger(db)
    trace_id = await logger.log_turn(
        user_id=1, guild_id=None, channel_id=None,
        user_message="a", assistant_reply="b",
    )
    ok = await logger.record_feedback(trace_id, 1)
    assert ok is True
    assert db.rows[0]["feedback_score"] == 1


@pytest.mark.asyncio
async def test_record_feedback_clamps_to_smallint_window():
    db = _FakeDB()
    logger = TrainingLogger(db)
    trace_id = await logger.log_turn(
        user_id=1, guild_id=None, channel_id=None,
        user_message="a", assistant_reply="b",
    )
    await logger.record_feedback(trace_id, 99)
    assert db.rows[0]["feedback_score"] == 3
    await logger.record_feedback(trace_id, -99)
    assert db.rows[0]["feedback_score"] == -3
