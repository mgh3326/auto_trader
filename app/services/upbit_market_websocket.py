"""Upbit public market data WebSocket client.

Connects to Upbit's public WebSocket to receive real-time market data
(ticker, orderbook, trade) and forwards it to the connection manager for broadcasting.
"""

import asyncio
import json
import logging
import ssl
import uuid
from collections.abc import Callable

from websockets.exceptions import ConnectionClosed, WebSocketException

from app.services.websocket_connection_manager import manager

logger = logging.getLogger(__name__)

UPBIT_PUBLIC_WS_URL = "wss://api.upbit.com/websocket/v1"


class UpbitPublicWebSocketClient:
    """Upbit public market data WebSocket client.

    Subscribes to public market data (ticker, orderbook, trade) from Upbit
    and broadcasts it to all connected WebSocket clients.
    """

    def __init__(
        self,
        subscription_type: str = "ticker",
        codes: list[str] | None = None,
        verify_ssl: bool = False,
        on_message: Callable | None = None,
    ):
        """
        Initialize Upbit public WebSocket client.

        Args:
            subscription_type: Type of subscription ("ticker", "orderbook", "trade")
            codes: List of market codes to subscribe (e.g., ["KRW-BTC", "KRW-ETH"])
                   None means subscribe to all markets
            verify_ssl: SSL certificate verification (default: False on macOS)
            on_message: Optional callback for received messages
        """
        self.websocket_url = UPBIT_PUBLIC_WS_URL
        self.subscription_type = subscription_type
        self.codes = codes
        self.verify_ssl = verify_ssl
        self.on_message = on_message
        self.websocket = None
        self.is_connected = False
        self.is_running = False
        self.reconnect_delay = 5
        self.max_reconnect_attempts = 10
        self.current_attempt = 0

    def _create_ssl_context(self):
        """Create SSL context for WebSocket connection."""
        if self.verify_ssl:
            ssl_context = ssl.create_default_context()
            logger.info("SSL certificate verification enabled")
        else:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            logger.info("SSL certificate verification disabled (macOS default)")
        return ssl_context

    def _create_subscribe_message(self) -> list:
        """Create subscription message for Upbit WebSocket."""
        message = [
            {"ticket": str(uuid.uuid4())},
            {
                "type": self.subscription_type,
                "codes": self.codes if self.codes else [],
            },
        ]
        return message

    async def connect_and_subscribe(self):
        """Connect to Upbit WebSocket and subscribe to market data."""
        while self.current_attempt < self.max_reconnect_attempts and self.is_running:
            try:
                await self._connect_and_subscribe_internal()
                self.current_attempt = 0
            except Exception as e:
                self.current_attempt += 1
                logger.error(
                    f"WebSocket connection failed (attempt {self.current_attempt}/{self.max_reconnect_attempts}): {e}"
                )

                if self.current_attempt >= self.max_reconnect_attempts:
                    logger.error("Max reconnection attempts reached. Stopping.")
                    break

                if self.is_running:
                    logger.info(f"Retrying in {self.reconnect_delay} seconds...")
                    await asyncio.sleep(self.reconnect_delay)

    async def _connect_and_subscribe_internal(self):
        """Internal connection and subscription logic."""
        logger.info(
            f"Connecting to Upbit public WebSocket: {self.subscription_type} "
            f"(codes: {self.codes if self.codes else 'all'})"
        )

        ssl_context = self._create_ssl_context()

        import websockets

        self.websocket = await websockets.connect(
            self.websocket_url,
            ssl=ssl_context,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=10,
        )

        self.is_connected = True
        logger.info("WebSocket connection established")

        subscribe_message = self._create_subscribe_message()
        await self.websocket.send(json.dumps(subscribe_message))
        logger.info("Subscription message sent")

        await self._listen_for_messages()

    async def _listen_for_messages(self):
        """Listen for incoming WebSocket messages."""
        logger.info("Listening for market data...")

        if self.websocket is None:
            logger.error("WebSocket not connected")
            return

        try:
            async for message in self.websocket:
                try:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8")

                    data = json.loads(message)

                    logger.debug(f"Received data: {data}")

                    await self._handle_message(data)

                except json.JSONDecodeError as e:
                    logger.error(f"JSON parse error: {e}, message: {message}")
                except Exception as e:
                    logger.error(f"Message processing error: {e}, message: {message}")

        except ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self.is_connected = False
        except WebSocketException as e:
            logger.error(f"WebSocket error: {e}")
            self.is_connected = False
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            self.is_connected = False

    async def _handle_message(self, data: dict):
        """Handle received market data message."""
        if self.on_message:
            await self.on_message(data)

        connection_count = await manager.get_connection_count()
        if connection_count > 0:
            await manager.broadcast(data)

    async def start(self):
        """Start the WebSocket client."""
        if self.is_running:
            logger.warning("WebSocket client already running")
            return

        self.is_running = True
        logger.info("Starting Upbit public WebSocket client...")

        try:
            await self.connect_and_subscribe()
        except Exception as e:
            logger.error(f"Failed to start WebSocket client: {e}")
            self.is_running = False
            raise

    async def stop(self):
        """Stop the WebSocket client."""
        if not self.is_running:
            logger.warning("WebSocket client not running")
            return

        logger.info("Stopping Upbit public WebSocket client...")
        self.is_running = False

        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("WebSocket connection closed")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
