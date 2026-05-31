from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("discoin.ws")


class ConnectionManager:
    """Manages WebSocket connections, per-user tracking, and channel subscriptions.

    Connections are tracked by ``user_id`` and can subscribe to named
    channels (e.g. ``market:MTA``, ``portfolio:123``).  The ``broadcast``
    method pushes JSON messages to all subscribers of a given channel.
    """

    def __init__(self) -> None:
        # user_id -> set of WebSocket connections (one user can have multiple tabs)
        self._connections: dict[str, set[WebSocket]] = {}
        # channel -> set of WebSocket connections
        self._subscriptions: dict[str, set[WebSocket]] = {}
        # reverse lookup: ws -> user_id
        self._ws_user: dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """Accept a WebSocket and register it under the given user."""
        await websocket.accept()
        self._connections.setdefault(user_id, set()).add(websocket)
        self._ws_user[websocket] = user_id
        log.info("WS connected: user=%s", user_id)

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket from all tracking structures."""
        user_id = self._ws_user.pop(websocket, None)
        if user_id and user_id in self._connections:
            self._connections[user_id].discard(websocket)
            if not self._connections[user_id]:
                del self._connections[user_id]

        # Remove from all subscriptions
        for channel, subs in list(self._subscriptions.items()):
            subs.discard(websocket)
            if not subs:
                del self._subscriptions[channel]

        log.info("WS disconnected: user=%s", user_id)

    def subscribe(self, websocket: WebSocket, channel: str) -> None:
        """Subscribe a WebSocket to a named channel."""
        self._subscriptions.setdefault(channel, set()).add(websocket)
        log.debug(
            "WS subscribe: user=%s channel=%s",
            self._ws_user.get(websocket),
            channel,
        )

    def unsubscribe(self, websocket: WebSocket, channel: str) -> None:
        """Unsubscribe a WebSocket from a named channel."""
        if channel in self._subscriptions:
            self._subscriptions[channel].discard(websocket)
            if not self._subscriptions[channel]:
                del self._subscriptions[channel]

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        """Send a JSON message to all WebSocket subscribers of a channel.

        Broken connections are silently removed.
        """
        subscribers = self._subscriptions.get(channel, set()).copy()
        if not subscribers:
            return

        dead: list[WebSocket] = []
        for ws in subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

    async def send_to_user(self, user_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to all connections of a specific user."""
        connections = self._connections.get(user_id, set()).copy()
        dead: list[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

    @property
    def active_connections(self) -> int:
        """Total number of active WebSocket connections."""
        return sum(len(s) for s in self._connections.values())


# Singleton instance
manager = ConnectionManager()
