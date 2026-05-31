"""WebSocket endpoint  -  real-time feeds for prices, trades, and notifications."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from api.v2.config import get_settings
from api.v2.ws.manager import manager

log = logging.getLogger("discoin.ws")

router = APIRouter(tags=["ws"])

# Shared Redis listener task  -  started once, fans out to all WS clients
_redis_listener_task: asyncio.Task | None = None

HEARTBEAT_INTERVAL = 30  # seconds
VALID_CHANNELS = {
    "market",         # market:<symbol>  -  price updates
    "trades",         # trades:<symbol>  -  trade events
    "portfolio",      # portfolio:<user_id>  -  portfolio changes
    "notifications",  # notifications:<user_id>  -  user notifications
    "blocks",         # blocks:<network>  -  block events
    "pools",          # pools:<pool_id>  -  pool updates
}


def _decode_token(token: str) -> dict[str, Any] | None:
    """Attempt to decode a JWT. Returns payload dict or None."""
    if not token:
        return None
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        if payload.get("partial") or payload.get("tfa_pending"):
            return None
        return {
            "user_id": payload["sub"],
            "guild_id": payload.get("guild_id"),
            "username": payload.get("username"),
            "is_admin": payload.get("is_admin", False),
        }
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


_EVENT_TO_WS_CHANNEL = {
    "prices_updated": "market",
    "trade_executed": "trades",
    "swap_executed": "trades",
    "block_bundled": "blocks",
    "block_mined": "blocks",
    "stake_created": "portfolio",
    "stake_removed": "portfolio",
    "stake_reward": "portfolio",
    "delegation_created": "portfolio",
    "delegation_removed": "portfolio",
    "pos_validator_slashed": "validators",
    "lp_added": "pools",
    "lp_removed": "pools",
    "savings_deposit": "portfolio",
    "savings_withdraw": "portfolio",
    "loan_created": "portfolio",
    "loan_repaid": "portfolio",
    "loan_liquidated": "portfolio",
    "transfer_sent": "notifications",
    "drop_spawned": "notifications",
    "drop_claimed": "notifications",
    "game_result": "notifications",
    "contract_event": "notifications",
    "badge_earned": "notifications",
    "admin_action": "notifications",
    "settings_changed": "notifications",
    "token_halted": "market",
    "token_resumed": "market",
}


async def _redis_listener(redis) -> None:
    """Subscribe to all RedisBus events and fan out to WebSocket clients.

    Runs as a single background task for the lifetime of each WebSocket
    connection's app.  Listens to ``discoin:*`` patterns and routes
    messages to the appropriate ConnectionManager channels.
    """
    pubsub = redis.pubsub()
    try:
        await pubsub.psubscribe("discoin:*")

        async for message in pubsub.listen():
            if message["type"] not in ("pmessage", "message"):
                continue
            try:
                data = json.loads(message["data"])
                event = data.get("event", "")
                ws_prefix = _EVENT_TO_WS_CHANNEL.get(event)
                if not ws_prefix:
                    continue

                # Build the WS channel name (e.g. "market:MTA", "trades:ARC")
                event_data = data.get("data", {})
                # Try symbol/token first, then network, then guild_id as suffix
                suffix = (
                    event_data.get("symbol")
                    or event_data.get("token")
                    or event_data.get("network")
                    or data.get("guild_id", "")
                )
                ws_channel = f"{ws_prefix}:{suffix}" if suffix else ws_prefix

                payload = {
                    "type": "event",
                    "channel": ws_channel,
                    "event": event,
                    "data": event_data,
                    "ts": data.get("ts"),
                }

                # Broadcast to exact channel subscribers
                await manager.broadcast(ws_channel, payload)

                # Also broadcast to wildcard subscribers (e.g. "market:*")
                wildcard = f"{ws_prefix}:*"
                await manager.broadcast(wildcard, payload)

                # For portfolio/notification events, send to specific user
                user_id = event_data.get("user") or event_data.get("user_id")
                if user_id and ws_prefix in ("portfolio", "notifications"):
                    user_channel = f"{ws_prefix}:{user_id}"
                    await manager.broadcast(user_channel, payload)

            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("Invalid Redis pub/sub message: %s", exc)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.error("Redis WS listener crashed: %s", exc)
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()


async def _heartbeat(websocket: WebSocket) -> None:
    """Send periodic ping frames to keep the connection alive."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await websocket.send_json({"type": "ping"})
    except (asyncio.CancelledError, Exception):
        pass


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None, description="Bearer token for auth"),
):
    """WebSocket endpoint for real-time data feeds.

    Authentication:
    - Pass `token` query parameter, OR
    - Send `{"type": "auth", "token": "..."}` as first message.

    Subscribe/unsubscribe:
    - `{"type": "subscribe", "channel": "market:MTA"}`
    - `{"type": "unsubscribe", "channel": "market:MTA"}`

    Heartbeat:
    - Server sends `{"type": "ping"}` every 30s.
    - Client should reply `{"type": "pong"}`.
    """
    # --- Authenticate ---
    user = _decode_token(token) if token else None
    user_id = user["user_id"] if user else f"anon-{id(websocket)}"

    await manager.connect(websocket, str(user_id))

    # Start shared Redis listener on first WS connection
    global _redis_listener_task
    redis = getattr(websocket.app.state, 'redis', None)
    if redis and (_redis_listener_task is None or _redis_listener_task.done()):
        _redis_listener_task = asyncio.create_task(_redis_listener(redis))

    # Start background tasks
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))

    try:
        # Send welcome message
        await websocket.send_json({
            "type": "connected",
            "user_id": str(user_id),
            "authenticated": user is not None,
        })

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON."})
                continue

            msg_type = msg.get("type")

            if msg_type == "auth":
                # Late authentication
                auth_token = msg.get("token", "")
                user = _decode_token(auth_token)
                if user:
                    # Re-register with real user_id
                    manager.disconnect(websocket)
                    user_id = user["user_id"]
                    await manager.connect(websocket, str(user_id))
                    await websocket.send_json({
                        "type": "authenticated",
                        "user_id": str(user_id),
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Authentication failed.",
                    })

            elif msg_type == "subscribe":
                channel = msg.get("channel", "")
                prefix = channel.split(":")[0] if ":" in channel else channel
                if prefix not in VALID_CHANNELS:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Invalid channel: {channel}",
                    })
                    continue
                manager.subscribe(websocket, channel)
                await websocket.send_json({
                    "type": "subscribed",
                    "channel": channel,
                })

            elif msg_type == "unsubscribe":
                channel = msg.get("channel", "")
                manager.unsubscribe(websocket, channel)
                await websocket.send_json({
                    "type": "unsubscribed",
                    "channel": channel,
                })

            elif msg_type == "pong":
                # Client heartbeat response  -  no action needed
                pass

            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        log.info("WS client disconnected: user=%s", user_id)
    except Exception as exc:
        log.error("WS error: user=%s err=%s", user_id, exc)
    finally:
        heartbeat_task.cancel()
        manager.disconnect(websocket)
