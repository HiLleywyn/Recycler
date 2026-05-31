"""Persists DiscoAI chat turns for later curation / offline training.

We store the full (system, user, assistant) trace plus any tool calls
in the `disco_training_turns` table so `scripts/export_training_data.py`
can replay each exchange as a ShareGPT example.

`log_turn` accepts the fields directly rather than an inference-trace
object so callers don't have to import any specific backend.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TrainingTurn:
    """One row from disco_training_turns ready for export."""

    id: int
    created_at: float
    user_id: int
    guild_id: int | None
    channel_id: int | None
    system_prompt: str
    user_message: str
    assistant_reply: str
    messages_json: list[dict]
    tool_calls_json: list[dict]
    model: str
    rounds: int
    latency_ms: int
    finish_reason: str
    feedback_score: int | None


class TrainingLogger:
    """Append-only writer for disco_training_turns + reaction-based feedback."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def log_turn(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        user_message: str,
        assistant_reply: str,
        messages: list[dict] | None = None,
        tool_calls: list[dict] | None = None,
        model: str = "",
        rounds: int = 1,
        latency_ms: int = 0,
        finish_reason: str = "stop",
    ) -> int:
        """Append a single (user, assistant) exchange to disco_training_turns.

        `messages` is the full chat stack the model saw (system + history +
        user). If omitted we reconstruct a minimal [system, user, assistant]
        triplet so the row stays self-contained. Returns the new row id.
        """
        if messages is None:
            messages = [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_reply},
            ]
        system_prompt = ""
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m.get("content") or ""
                break

        row = await self._db.fetch_one(
            """
            INSERT INTO disco_training_turns (
                user_id, guild_id, channel_id,
                system_prompt, user_message, assistant_reply,
                messages_json, tool_calls_json,
                model, rounds, latency_ms, finish_reason
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, $12)
            RETURNING id
            """,
            int(user_id),
            int(guild_id) if guild_id is not None else None,
            int(channel_id) if channel_id is not None else None,
            system_prompt,
            user_message,
            assistant_reply or "",
            json.dumps(messages),
            json.dumps(tool_calls or []),
            model or "",
            int(rounds),
            int(latency_ms),
            finish_reason or "",
        )
        return int(row["id"]) if row else 0

    async def record_feedback(self, trace_id: int, score: int) -> bool:
        """Set feedback_score for a trace. Returns True if a row was updated."""
        # Clamp to a small SMALLINT range -- thumbs up / thumbs down are
        # +1 / -1 today, but leave headroom in case we add a 5-star UI.
        score = max(-3, min(3, int(score)))
        status = await self._db.execute(
            "UPDATE disco_training_turns SET feedback_score = $2 WHERE id = $1",
            int(trace_id), score,
        )
        return isinstance(status, str) and status.endswith(" 1")

    # ── Export ────────────────────────────────────────────────────────

    async def export_sharegpt(
        self,
        since: datetime,
        min_score: int | None = None,
    ) -> list[dict]:
        """Return turns as ShareGPT-format conversations.

        ShareGPT shape per row:
            {
              "conversations": [
                {"from": "system", "value": "..."},
                {"from": "human",  "value": "..."},
                {"from": "gpt",    "value": "..."}
              ]
            }

        Tool messages are flattened into the `gpt` turn as a JSON code
        fence so the fine-tuned model still learns when (and what) to
        call. This is intentionally opinionated -- different trainers
        want different shapes; rewrite this if you target axolotl /
        unsloth schemas instead.
        """
        if min_score is not None:
            rows = await self._db.fetch_all(
                """
                SELECT *
                FROM disco_training_turns
                WHERE created_at >= $1
                  AND feedback_score IS NOT NULL
                  AND feedback_score >= $2
                ORDER BY created_at ASC
                """,
                since, int(min_score),
            )
        else:
            # Drop explicitly-negative feedback even when no threshold is set.
            rows = await self._db.fetch_all(
                """
                SELECT *
                FROM disco_training_turns
                WHERE created_at >= $1
                  AND (feedback_score IS NULL OR feedback_score >= 0)
                ORDER BY created_at ASC
                """,
                since,
            )

        out: list[dict] = []
        for r in rows:
            convo = [
                {"from": "system", "value": r["system_prompt"]},
                {"from": "human", "value": r["user_message"]},
            ]
            assistant_value = r["assistant_reply"] or ""
            tool_calls = _coerce_jsonb(r.get("tool_calls_json")) or []
            if tool_calls:
                tool_block = json.dumps(tool_calls, indent=2)
                assistant_value = (
                    f"{assistant_value}\n\n```tool_calls\n{tool_block}\n```"
                )
            convo.append({"from": "gpt", "value": assistant_value})
            out.append({"conversations": convo})
        return out


def _coerce_jsonb(raw: Any) -> Any:
    """asyncpg returns JSONB as either a Python object or a JSON string
    depending on driver/codec config. Normalize to Python."""
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw
