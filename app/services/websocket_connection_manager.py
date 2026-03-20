"""WebSocket connection manager for broadcasting market data to clients."""

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts messages to all clients."""

    def __init__(self):
        """Initialize connection manager."""
        self.active_connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept and register a new WebSocket connection.

        Args:
            websocket: The WebSocket connection to accept
        """
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(
            f"New WebSocket connection established. "
            f"Total connections: {len(self.active_connections)}"
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a WebSocket connection from active connections.

        Args:
            websocket: The WebSocket connection to remove
        """
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.discard(websocket)
        logger.info(
            f"WebSocket connection disconnected. "
            f"Total connections: {len(self.active_connections)}"
        )

    async def broadcast(self, message: dict | str) -> None:
        """
        Broadcast a message to all active WebSocket connections.

        Args:
            message: The message to broadcast (dict or JSON string)
        """
        if isinstance(message, dict):
            message = json.dumps(message)

        if not self.active_connections:
            return

        disconnected = []

        async with self._lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.warning(
                        f"Failed to send message to client: {e}. "
                        f"Marking for disconnection."
                    )
                    disconnected.append(connection)

            for connection in disconnected:
                self.active_connections.discard(connection)

        if disconnected:
            logger.info(
                f"Removed {len(disconnected)} disconnected client(s). "
                f"Active connections: {len(self.active_connections)}"
            )

    async def get_connection_count(self) -> int:
        """
        Get the current number of active connections.

        Returns:
            int: Number of active WebSocket connections
        """
        async with self._lock:
            return len(self.active_connections)


manager = ConnectionManager()
