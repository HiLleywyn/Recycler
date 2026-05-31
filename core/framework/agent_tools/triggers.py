"""
core/framework/agent_tools/triggers.py -- event-based agent trigger engine.

Triggers live in the agent_triggers table and fire when matching events
arrive on the Redis bus (prices_updated, market_event_started, etc.). Each
fire dispatches to an agent tool through run_tool(), so triggers inherit the
framework's validation, approval, and audit guardrails.

Supported kinds and their ``condition`` shape:
  price_above / price_below  -> {"symbol": "MTA", "threshold": 50000}
  event                      -> {"event": "black_swan"}   ("*" for any)
  portfolio_drop             -> {"pct": 10}              (future)

Triggers CANNOT auto-fire DANGER tools. The run_tool() guardrail rejects
those with approval_required, which the engine records on last_result.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .core import ToolContext
from .executor import run_tool

log = logging.getLogger("discoin.agent_tools.triggers")

# Max number of triggers a single event dispatch will evaluate per guild.
# Keeps a runaway trigger list from blowing up a price-update broadcast.
_PER_EVENT_LIMIT = 500


async def create_trigger(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    kind: str,
    condition: dict,
    tool: str,
    args: dict,
    name: str = "",
    one_shot: bool = True,
) -> int:
    row = await db.fetch_one(
        """
        INSERT INTO agent_triggers
            (guild_id, user_id, kind, condition, tool, args, name, one_shot,
             enabled, created_at)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb,$7,$8,true,NOW())
        RETURNING id
        """,
        int(guild_id), int(user_id), kind,
        json.dumps(condition, default=str), tool,
        json.dumps(args, default=str), name or "",
        bool(one_shot),
    )
    return int(row["id"])


async def list_triggers(db: Any, *, guild_id: int, user_id: int) -> list[dict]:
    return await db.fetch_all(
        "SELECT * FROM agent_triggers WHERE guild_id=$1 AND user_id=$2 "
        "ORDER BY id DESC",
        int(guild_id), int(user_id),
    )


async def delete_trigger(db: Any, *, trigger_id: int, user_id: int) -> bool:
    res = await db.execute(
        "DELETE FROM agent_triggers WHERE id=$1 AND user_id=$2",
        int(trigger_id), int(user_id),
    )
    return "DELETE 1" in str(res)


class TriggerEngine:
    """Subscribes to bus events and fires matching triggers through run_tool."""

    # Names of bus topics + their bound handler attributes. Centralised so
    # ``start()`` and ``stop()`` cannot drift out of sync.
    _SUBSCRIPTIONS: tuple[tuple[str, str], ...] = (
        ("prices_updated",       "_on_prices_updated"),
        ("market_event_started", "_on_market_event"),
        ("token_halted",         "_on_token_halted"),
        ("loan_liquidated",      "_on_loan_liquidated"),
    )

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._subscribed = False

    def start(self) -> None:
        if self._subscribed:
            return
        bus = getattr(self.bot, "bus", None)
        if bus is None:
            log.warning("[agent_tools.triggers] no bus on bot; engine idle")
            return
        for topic, handler_name in self._SUBSCRIPTIONS:
            bus.subscribe(topic, getattr(self, handler_name))
        self._subscribed = True
        log.info("[agent_tools.triggers] engine started")

    def stop(self) -> None:
        """Unsubscribe from every bus topic so reload/shutdown leaves no
        dangling handlers firing into a dead engine."""
        if not self._subscribed:
            return
        bus = getattr(self.bot, "bus", None)
        if bus is not None:
            unsubscribe = getattr(bus, "unsubscribe", None)
            for topic, handler_name in self._SUBSCRIPTIONS:
                handler = getattr(self, handler_name, None)
                if callable(unsubscribe) and handler is not None:
                    try:
                        unsubscribe(topic, handler)
                    except Exception:
                        log.warning(
                            "[agent_tools.triggers] failed to unsubscribe %s",
                            topic,
                        )
        self._subscribed = False
        log.info("[agent_tools.triggers] engine stopped")

    # ── Event handlers ────────────────────────────────────────────────────

    async def _on_prices_updated(self, **kwargs: Any) -> None:
        guild_id = _extract_guild_id(kwargs)
        if not guild_id:
            return
        prices = kwargs.get("prices") or {}
        if not isinstance(prices, dict) or not prices:
            return
        # Only examine symbols that have at least one active trigger
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        rows = await db.fetch_all(
            """
            SELECT * FROM agent_triggers
            WHERE guild_id=$1 AND enabled=true
              AND kind IN ('price_above','price_below')
            LIMIT $2
            """,
            int(guild_id), _PER_EVENT_LIMIT,
        )
        for row in rows:
            cond = _load_json(row.get("condition"))
            sym = str(cond.get("symbol", "")).upper()
            thr = _as_float(cond.get("threshold"))
            if not sym or thr is None:
                continue
            px = _as_float(prices.get(sym))
            if px is None:
                continue
            if row["kind"] == "price_above" and px >= thr:
                await self._fire(row, {"symbol": sym, "price": px, "threshold": thr})
            elif row["kind"] == "price_below" and px <= thr:
                await self._fire(row, {"symbol": sym, "price": px, "threshold": thr})

    async def _on_market_event(self, **kwargs: Any) -> None:
        guild_id = _extract_guild_id(kwargs)
        event_name = str(kwargs.get("event") or kwargs.get("event_type") or "")
        if not guild_id or not event_name:
            return
        rows = await self.bot.db.fetch_all(
            "SELECT * FROM agent_triggers "
            "WHERE guild_id=$1 AND enabled=true AND kind='event' LIMIT $2",
            int(guild_id), _PER_EVENT_LIMIT,
        )
        for row in rows:
            cond = _load_json(row.get("condition"))
            want = str(cond.get("event", "")).lower()
            if want in ("*", "", event_name.lower()):
                await self._fire(row, {"event": event_name})

    async def _on_token_halted(self, **kwargs: Any) -> None:
        guild_id = _extract_guild_id(kwargs)
        if not guild_id:
            return
        symbol = str(kwargs.get("symbol") or "").upper()
        rows = await self.bot.db.fetch_all(
            "SELECT * FROM agent_triggers "
            "WHERE guild_id=$1 AND enabled=true AND kind='token_halted' LIMIT $2",
            int(guild_id), _PER_EVENT_LIMIT,
        )
        for row in rows:
            cond = _load_json(row.get("condition"))
            want = str(cond.get("symbol", "")).upper()
            if not want or want == symbol:
                await self._fire(row, {"symbol": symbol})

    async def _on_loan_liquidated(self, **kwargs: Any) -> None:
        guild_id = _extract_guild_id(kwargs)
        user_id = kwargs.get("user_id")
        if not guild_id or user_id is None:
            return
        rows = await self.bot.db.fetch_all(
            """
            SELECT * FROM agent_triggers
            WHERE guild_id=$1 AND user_id=$2 AND enabled=true
              AND kind='loan_liquidated'
            """,
            int(guild_id), int(user_id),
        )
        for row in rows:
            await self._fire(row, {"user_id": int(user_id)})

    # ── Firing ────────────────────────────────────────────────────────────

    async def _fire(self, row: dict, firing_context: dict) -> None:
        db = self.bot.db
        args = _load_json(row.get("args"))
        args["_trigger"] = firing_context
        ctx = ToolContext(
            user_id=int(row["user_id"]),
            guild_id=int(row["guild_id"]),
            db=db,
            bus=getattr(self.bot, "bus", None),
            actor="trigger",
            approved=False,
        )
        tool_name = str(row.get("tool") or "")
        result = await run_tool(tool_name, ctx, args)
        log.info(
            "[agent_tools.triggers] fired id=%s tool=%s ok=%s",
            row.get("id"), tool_name, result.ok,
        )

        if bool(row.get("one_shot", True)):
            await db.execute(
                """
                UPDATE agent_triggers
                SET enabled=false, fired_at=NOW(),
                    fire_count=fire_count+1, last_result=$2::jsonb
                WHERE id=$1
                """,
                int(row["id"]), result.to_json(),
            )
        else:
            await db.execute(
                """
                UPDATE agent_triggers
                SET fired_at=NOW(), fire_count=fire_count+1,
                    last_result=$2::jsonb
                WHERE id=$1
                """,
                int(row["id"]), result.to_json(),
            )


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_guild_id(kwargs: dict) -> int:
    g = kwargs.get("guild") or kwargs.get("guild_id")
    return int(getattr(g, "id", g) or 0)


def _load_json(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
