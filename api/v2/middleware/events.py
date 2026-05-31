"""
Event Publishing Middleware
===========================

Automatically publishes events to Redis after successful state-changing
API requests (POST, PATCH, DELETE).  The bot's RedisBus picks these up
so the economy security monitor, WebSocket feeds, and whale alerts can
see API-originating activity.

Event type is inferred from the request path and method.
"""
from __future__ import annotations

import json
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("discoin.api.events")

# Map (method, path_prefix) → event name
# Only state-changing operations (POST/PATCH/DELETE) are published.
_EVENT_MAP: list[tuple[str, str, str]] = [
    # Games
    ("POST", "/api/v2/games/coinflip",    "game_result"),
    ("POST", "/api/v2/games/slots",       "game_result"),
    ("POST", "/api/v2/games/dice",        "game_result"),
    ("POST", "/api/v2/games/roulette",    "game_result"),
    ("POST", "/api/v2/games/blackjack",   "game_result"),
    ("POST", "/api/v2/games/mines",       "game_result"),
    ("POST", "/api/v2/games/crash",       "game_result"),
    # Trading
    ("POST", "/api/v2/trading/buy",       "trade_executed"),
    ("POST", "/api/v2/trading/sell",      "trade_executed"),
    ("POST", "/api/v2/trading/swap",      "swap_executed"),
    ("POST", "/api/v2/trading/transfer",  "transfer_sent"),
    # Pools
    ("POST", "/api/v2/pools/add",         "lp_added"),
    ("POST", "/api/v2/pools/remove",      "lp_removed"),
    # Staking
    ("POST", "/api/v2/staking/stake",     "stake_created"),
    ("POST", "/api/v2/staking/unstake",   "stake_removed"),
    ("POST", "/api/v2/staking/delegate",  "delegation_created"),
    ("POST", "/api/v2/staking/undelegate","delegation_removed"),
    # Savings
    ("POST", "/api/v2/savings/deposit",   "savings_deposit"),
    ("POST", "/api/v2/savings/withdraw",  "savings_withdraw"),
    # Lending
    ("POST", "/api/v2/lending/borrow",    "loan_created"),
    ("POST", "/api/v2/lending/repay",     "loan_repaid"),
    # Mining
    ("POST", "/api/v2/mining/buy",        "trade_executed"),
    ("POST", "/api/v2/mining/sell",       "trade_executed"),
    ("POST", "/api/v2/mining/set-network","admin_action"),
    # Shop
    ("POST", "/api/v2/shop/buy",          "trade_executed"),
    ("POST", "/api/v2/shop/sell",         "trade_executed"),
    # Admin
    ("POST",  "/api/v2/admin/",           "admin_action"),
    ("PATCH", "/api/v2/admin/",           "settings_changed"),
]


def _match_event(method: str, path: str) -> str | None:
    """Return the event name if the request matches, else None."""
    for m, prefix, event in _EVENT_MAP:
        if method == m and path.startswith(prefix):
            return event
    return None


class EventPublishingMiddleware(BaseHTTPMiddleware):
    """Publish events to Redis after successful state-changing API requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Only publish on successful state changes (2xx responses)
        if response.status_code < 200 or response.status_code >= 300:
            return response

        event = _match_event(request.method, request.url.path)
        if not event:
            return response

        # Extract guild_id and user_id from the JWT (stored by auth middleware)
        # These are set by get_current_user dependency; we read from request state
        # if available, otherwise try to decode from the Authorization header.
        guild_id = None
        user_id = None
        try:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                import jwt
                from api.v2.config import get_settings
                token = auth.removeprefix("Bearer ").strip()
                payload = jwt.decode(
                    token, get_settings().JWT_SECRET,
                    algorithms=["HS256"],
                    options={"verify_exp": False},
                )
                guild_id = int(payload.get("guild_id", 0))
                user_id = int(payload.get("user_id", 0))
        except Exception as exc:
            log.debug("EventPublishingMiddleware: JWT decode failed (%s)", exc)

        if not guild_id:
            return response

        # Publish to Redis
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return response

        try:
            payload_json = json.dumps({
                "event": event,
                "guild_id": str(guild_id),
                "data": {
                    "user_id": user_id,
                    "path": request.url.path,
                    "method": request.method,
                },
                "ts": time.time(),
                "source": "api",
            })
            # Publish to discoin:api:{guild_id}  -  the bot's RedisBus listener
            # subscribes to discoin:api:* and dispatches events where source == "api"
            await redis.publish(f"discoin:api:{guild_id}", payload_json)
        except Exception as exc:
            log.warning(
                "EventPublishingMiddleware: Redis publish failed for event=%s guild=%s: %s",
                event, guild_id, exc,
            )

        return response
