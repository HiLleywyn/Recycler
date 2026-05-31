"""
core/framework/redis_bus.py  -  Redis-backed pub/sub EventBus for Discoin v2.

Drop-in replacement for the in-memory EventBus (core/framework/bot.py).
Publishes events to Redis channels so the FastAPI server can receive them,
and subscribes to Redis channels for events originating from the API.

Usage
─────
    from core.framework.redis_bus import RedisBus

    bus = RedisBus(redis_url="redis://localhost:6379")
    await bus.connect()

    # Subscribe (same API as EventBus)
    bus.subscribe("trade_executed", my_callback)

    # Publish (same API, but also publishes to Redis)
    await bus.publish("trade_executed", guild_id=123, data={...})

    # Cleanup
    await bus.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Awaitable

log = logging.getLogger("discoin.redis_bus")

# Channel prefix for all Discoin Redis pub/sub
_PREFIX = "discoin"

# Events that should be broadcast to Redis (bot → API / WebSocket)
REDIS_BROADCAST_EVENTS = frozenset({
    "prices_updated",
    "trade_executed",
    "swap_executed",
    "block_bundled",
    "block_mined",
    "stake_created",
    "stake_removed",
    "stake_reward",
    "delegation_created",
    "delegation_removed",
    "lp_added",
    "lp_removed",
    "savings_deposit",
    "savings_withdraw",
    "loan_created",
    "loan_repaid",
    "loan_liquidated",
    "transfer_sent",
    "drop_spawned",
    "drop_claimed",
    "game_result",
    "contract_event",
    "badge_earned",
    "admin_action",
    "settings_changed",
    "token_halted",
    "token_resumed",
    "security_event",
    "security_enforcement",
    "security_score_update",
    "security_alert",
    "market_event_started",
    "market_event_ended",
    "market_event_phase",
})


class RedisBus:
    """Redis-backed pub/sub bus with in-memory fallback for local listeners."""

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        self._redis_url = redis_url
        self._redis = None  # redis.asyncio.Redis
        self._pubsub = None  # redis.asyncio.client.PubSub
        self._listener_task: asyncio.Task | None = None

        # In-memory listeners (same as original EventBus)
        self._listeners: dict[str, list[Callable[..., Awaitable[Any]]]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Redis and start listening for subscribed channels."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            log.warning("redis package not installed  -  falling back to in-memory bus only")
            return

        # redis-py 5.x handles SSL automatically from the rediss:// scheme.
        # To skip cert verification (Railway self-signed certs) pass
        # ssl_cert_reqs="none".  Do NOT pass ssl=  -  that is not accepted by
        # AbstractConnection.__init__() in redis-py 5.x.
        import os
        # ``max_connections`` is per-client. 10 is the redis-py default;
        # bumping to a sane bot-scale ceiling here so concurrent ops
        # (market cache writes, prediction polling, AI queue lookups,
        # the $status probe) don't trip ``MaxConnectionsError`` on the
        # shared pool. Override with REDIS_MAX_CONNECTIONS if needed.
        max_conns = max(10, int(os.getenv("REDIS_MAX_CONNECTIONS", "50") or 50))
        kwargs: dict = {"decode_responses": True, "max_connections": max_conns}
        if self._redis_url.startswith("rediss://"):
            if os.getenv("REDIS_SSL_VERIFY", "0") != "1":
                kwargs["ssl_cert_reqs"] = "none"

        self._redis = aioredis.from_url(self._redis_url, **kwargs)
        # Verify connection
        await self._redis.ping()
        log.info("RedisBus connected to %s", self._redis_url)

        # Create pub/sub and start listener
        self._pubsub = self._redis.pubsub()
        self._listener_task = asyncio.create_task(self._listen_loop())

    async def close(self) -> None:
        """Clean up Redis connections and null out all state so is_connected returns False."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
        # Null out all state so is_connected returns False after close
        self._redis = None
        self._pubsub = None
        self._listener_task = None
        log.info("RedisBus closed")

    async def ping(self) -> bool:
        """Send a PING to Redis and return True if the connection is alive."""
        if self._redis is None:
            return False
        try:
            await asyncio.wait_for(self._redis.ping(), timeout=2.0)
            return True
        except Exception:
            return False

    # ── Subscribe / Unsubscribe ───────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable[..., Awaitable[Any]]) -> None:
        """Subscribe a local callback to an event. Also subscribes to Redis channel."""
        self._listeners.setdefault(event, []).append(callback)

        # Subscribe to the Redis pattern for this event (all guilds)
        if self._pubsub is not None:
            pattern = f"{_PREFIX}:{event}:*"
            asyncio.ensure_future(self._pubsub.psubscribe(pattern))

    def unsubscribe(self, event: str, callback: Callable[..., Awaitable[Any]]) -> None:
        """Remove a local callback."""
        try:
            self._listeners.get(event, []).remove(callback)
        except ValueError:
            pass

    # ── Publish ───────────────────────────────────────────────────────────

    async def publish(self, event: str, **kwargs: Any) -> None:
        """
        Publish an event to both local listeners and Redis.

        Local listeners are called directly (same as original EventBus).
        If the event is in REDIS_BROADCAST_EVENTS, it's also published
        to the Redis channel `discoin:{event}:{guild_id}`.
        """
        from core.framework import session_log as _sl
        sl = _sl.get()
        if sl is not None:
            sl.event(event, **kwargs)

        # 1. Call local listeners
        for cb in list(self._listeners.get(event, [])):
            try:
                await cb(**kwargs)
            except Exception as exc:
                log.error(
                    "RedisBus listener %s raised on %r: %s",
                    cb.__qualname__, event, exc,
                )

        # 2. Publish to Redis if applicable
        if self._redis is not None and event in REDIS_BROADCAST_EVENTS:
            # Cogs pass guild= (discord.Guild object); extract the id
            guild = kwargs.get("guild") or kwargs.get("guild_id")
            guild_id = getattr(guild, "id", guild) or "*"
            channel = f"{_PREFIX}:{event}:{guild_id}"
            payload = json.dumps({
                "event": event,
                "guild_id": str(guild_id),
                "data": _serialize_kwargs(kwargs),
                "ts": time.time(),
                "source": "bot",
            })
            try:
                await self._redis.publish(channel, payload)
            except Exception as exc:
                log.error("Failed to publish to Redis channel %s: %s", channel, exc)

    # ── Redis Listener Loop ───────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Listen for Redis messages from the API server and dispatch to local listeners."""
        if self._pubsub is None:
            return

        # Subscribe to API-originated events
        await self._pubsub.psubscribe(f"{_PREFIX}:api:*")

        try:
            async for message in self._pubsub.listen():
                if message["type"] not in ("pmessage", "message"):
                    continue
                try:
                    data = json.loads(message["data"])
                    event = data.get("event", "")
                    source = data.get("source", "")

                    # Only dispatch events from the API (avoid echo)
                    if source == "api":
                        event_data = data.get("data", {})
                        for cb in list(self._listeners.get(event, [])):
                            try:
                                await cb(**event_data)
                            except Exception as exc:
                                log.error(
                                    "Redis listener %s raised on %r: %s",
                                    cb.__qualname__, event, exc,
                                )
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning("Invalid Redis message: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception:
            # Log the full traceback so the self-healer and error logs show the root cause.
            # After this the task is "done", so is_connected → False and the healer will
            # schedule a reconnect automatically.
            log.exception("Redis listener loop crashed  -  is_connected will flip False")

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True only if the Redis client exists AND the listener loop is still alive."""
        return (
            self._redis is not None
            and self._listener_task is not None
            and not self._listener_task.done()
        )


def _serialize_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Make kwargs JSON-serializable by converting non-basic types to strings."""
    result = {}
    for k, v in kwargs.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            result[k] = v
        elif isinstance(v, (list, tuple)):
            result[k] = [_serialize_kwargs({"_": item})["_"] if isinstance(item, dict) else str(item) for item in v]
        elif isinstance(v, dict):
            result[k] = _serialize_kwargs(v)
        else:
            result[k] = str(v)
    return result
