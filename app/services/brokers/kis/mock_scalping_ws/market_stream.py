"""Read-only KIS quote/orderbook WebSocket client for the mock scalping loop.

This client streams real-time 체결가/호가 (H0STCNT0/H0STASP0) and dispatches
parsed snapshots to caller-supplied callbacks. It is **read-only by
construction**: there is no order/submit method, and the package may not import
any order/ledger module (enforced by an AST import-guard test).

Host separation is fail-closed: the WebSocket URL is built from
``WEBSOCKET_ENDPOINT_HOSTS[account_mode]`` and the resolved host:port is
asserted against that allowlist. The approval key is issued via the
account-mode-aware ``approval_keys.get_approval_key`` (live vs mock endpoints +
credentials + Redis namespaces).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings
from app.services.kis_websocket_internal import approval_keys
from app.services.kis_websocket_internal.constants import WEBSOCKET_ENDPOINT_HOSTS
from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

from .quote_parsers import OrderBookSnapshot, QuoteTick, parse_quote_frame
from .quote_protocol import DOMESTIC_ORDERBOOK_TR, DOMESTIC_TRADE_TR

logger = logging.getLogger(__name__)

TickCallback = Callable[[QuoteTick], Any | Awaitable[Any]]
BookCallback = Callable[[OrderBookSnapshot], Any | Awaitable[Any]]

# TRs this read-only client subscribes to per symbol.
_SUBSCRIBE_TRS = (DOMESTIC_TRADE_TR, DOMESTIC_ORDERBOOK_TR)


class KISQuoteWebSocket:
    """Read-only real-time quote/orderbook stream. No order surface."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        on_tick: TickCallback,
        on_book: BookCallback,
        account_mode: str = "kis_live",
    ) -> None:
        if account_mode not in WEBSOCKET_ENDPOINT_HOSTS:
            raise ValueError(
                f"account_mode must be one of {tuple(WEBSOCKET_ENDPOINT_HOSTS)}, "
                f"got {account_mode!r}"
            )
        self.symbols = [s for s in symbols if s]
        self.on_tick = on_tick
        self.on_book = on_book
        self.account_mode = account_mode

        self.websocket: Any | None = None
        self.is_running = False
        self.is_connected = False
        self.approval_key: str | None = None

        self.reconnect_delay = settings.kis_ws_reconnect_delay_seconds
        self.max_reconnect_attempts = settings.kis_ws_max_reconnect_attempts
        self.current_attempt = 0
        self.ping_interval = settings.kis_ws_ping_interval
        self.ping_timeout = settings.kis_ws_ping_timeout

        self.messages_received = 0
        self.tick_events_received = 0
        self.book_events_received = 0
        self.last_message_at: str | None = None

    # ----- URL / host allowlist (fail-closed) -----

    def _build_url(self) -> str:
        host = WEBSOCKET_ENDPOINT_HOSTS[self.account_mode]
        url = f"ws://{host}/tryitout"
        parsed = urlparse(url)
        resolved = f"{parsed.hostname}:{parsed.port}"
        if resolved != host:
            raise ValueError(
                f"websocket endpoint {resolved!r} not allowed for "
                f"{self.account_mode} (expected {host!r})"
            )
        return url

    # ----- subscription -----

    def _build_subscription_request(self, tr_id: str, tr_key: str) -> dict[str, Any]:
        if not self.approval_key:
            raise RuntimeError("Approval key is not issued")
        return {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }

    async def _subscribe_quotes(self) -> None:
        if self.websocket is None:
            raise RuntimeError("WebSocket is not connected")
        if not self.symbols:
            raise ValueError("No symbols to subscribe")

        for symbol in self.symbols:
            for tr_id in _SUBSCRIBE_TRS:
                request = self._build_subscription_request(tr_id, symbol)
                await self.websocket.send(json.dumps(request))
                response = await self.websocket.recv()
                self._validate_subscription_ack(response, tr_id, symbol)
        logger.info(
            "Subscribed to KIS quote TRs: account_mode=%s symbols=%s",
            self.account_mode,
            self.symbols,
        )

    def _validate_subscription_ack(
        self, response: str | bytes, tr_id: str, tr_key: str
    ) -> None:
        if isinstance(response, bytes):
            response = response.decode("utf-8")
        text = response.strip()
        # Real-time data frames (pipe-delimited) are not ACKs — accept them.
        if not text.startswith("{"):
            return
        parsed = json.loads(text)
        body = parsed.get("body")
        if not isinstance(body, dict):
            raise KISSubscriptionAckError(
                tr_id=tr_id, rt_cd="", msg_cd="", msg1="ACK body missing"
            )
        rt_cd = str(body.get("rt_cd", ""))
        if rt_cd != "0":
            raise KISSubscriptionAckError(
                tr_id=tr_id,
                rt_cd=rt_cd,
                msg_cd=str(body.get("msg_cd", "")),
                msg1=f"{body.get('msg1', '')} (tr_key={tr_key})",
            )

    # ----- connect / listen -----

    async def connect_and_subscribe(self) -> None:
        """Issue approval key, connect, and subscribe (bounded reconnect)."""
        self.approval_key = await approval_keys.get_approval_key(self.account_mode)
        url = self._build_url()

        while self.current_attempt < self.max_reconnect_attempts and self.is_running:
            try:
                self.websocket = await websockets.connect(
                    url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=10,
                )
                self.is_connected = True
                await self._subscribe_quotes()
                self.current_attempt = 0
                return
            except Exception as e:
                self.current_attempt += 1
                logger.error(
                    "KIS quote WebSocket connect failed: account_mode=%s attempt=%s/%s error=%s",
                    self.account_mode,
                    self.current_attempt,
                    self.max_reconnect_attempts,
                    e,
                )
                await self._close_websocket_best_effort()
                if self.current_attempt >= self.max_reconnect_attempts:
                    break
                if self.is_running:
                    await asyncio.sleep(self.reconnect_delay)

        if not self.is_connected:
            raise RuntimeError("KIS quote WebSocket connection not established")

    async def listen(self) -> None:
        """Consume messages and dispatch parsed quotes to callbacks."""
        if self.websocket is None:
            raise RuntimeError("WebSocket is not connected")
        websocket = self.websocket
        try:
            async for message in websocket:
                self.messages_received += 1
                self.last_message_at = self._now_iso()
                try:
                    await self._dispatch(message)
                except Exception as e:
                    logger.error("Quote message processing error: %s", e, exc_info=True)
        except ConnectionClosed:
            logger.warning(
                "KIS quote WebSocket closed: messages_received=%s",
                self.messages_received,
            )
            self.is_connected = False
        except WebSocketException as e:
            logger.error("KIS quote WebSocket error: %s", e, exc_info=True)
            self.is_connected = False

    async def _dispatch(self, message: str | bytes) -> None:
        if self._is_pingpong(message):
            await self._handle_pingpong()
            return
        parsed = parse_quote_frame(message)
        if parsed is None:
            return
        if isinstance(parsed, QuoteTick):
            self.tick_events_received += 1
            result = self.on_tick(parsed)
            if isawaitable(result):
                await result
        elif isinstance(parsed, OrderBookSnapshot):
            self.book_events_received += 1
            result = self.on_book(parsed)
            if isawaitable(result):
                await result

    @staticmethod
    def _is_pingpong(message: str | bytes) -> bool:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except Exception:
                return False
        return "pingpong" in message.lower()

    async def _handle_pingpong(self) -> None:
        if self.websocket:
            await self.websocket.send("0|pingpong")

    @staticmethod
    def _now_iso() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()

    async def _close_websocket_best_effort(self) -> None:
        websocket = self.websocket
        self.websocket = None
        self.is_connected = False
        if websocket is None:
            return
        try:
            await websocket.close()
        except Exception as e:
            logger.debug("Failed to close quote websocket during cleanup: %s", e)

    async def stop(self) -> None:
        self.is_running = False
        try:
            if self.websocket:
                await self.websocket.close()
        finally:
            self.websocket = None
            self.is_connected = False
