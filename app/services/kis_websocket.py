"""
KIS (한국투자증권) WebSocket Client for Execution Data

국내/해외 체결 데이터를 실시간으로 수신하여 Redis pub/sub으로 발행합니다.
"""

import asyncio
import logging
import ssl
from typing import Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings

logger = logging.getLogger(__name__)


async def get_approval_key() -> str:
    """
    KIS WebSocket Approval Key 발급

    Approval Key는 24시간 유효하며, 23시간 캐시하여 재발급을 줄입니다.

    Returns:
        str: Approval Key

    Raises:
        Exception: Approval Key 발급 실패 시
    """
    approval_key = await _get_cached_approval_key()

    if approval_key is None:
        approval_key = await _issue_approval_key()
        await _cache_approval_key(approval_key)

    return approval_key


async def _get_cached_approval_key() -> str | None:
    """캐시된 Approval Key 조회 (만료 체크 포함)"""
    raise NotImplementedError("Redis caching to be implemented")


async def _cache_approval_key(approval_key: str) -> None:
    """Approval Key 캐싱 (23시간 TTL)"""
    raise NotImplementedError("Redis caching to be implemented")


async def _issue_approval_key() -> str:
    """
    KIS Approval Key 발급 API 호출

    Returns:
        str: 발급된 Approval Key

    Raises:
        Exception: HTTP 요청 실패 또는 응답에 approval_key 없음
    """
    base_url = "https://openapi.koreainvestment.com:9443"
    path = "/oauth2/Approval"
    url = f"{base_url}{path}"

    headers = {
        "Content-Type": "application/json",
    }

    request_body = {
        "grant_type": "client_credentials",
        "appkey": settings.kis_app_key,
        "secretkey": settings.kis_app_secret,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, headers=headers, json=request_body, timeout=10
        )
        response.raise_for_status()
        data = response.json()

    issued_key = data.get("approval_key")
    if not issued_key:
        raise Exception("Approval Key not found in response")

    logger.info("KIS Approval Key issued successfully")
    return issued_key


class KISExecutionWebSocket:
    """
    KIS 체결 WebSocket 클라이언트

    국내/해외 체결 데이터를 실시간으로 수신하고 콜백으로 전달합니다.
    체결 타입(1)만 필터링하여 상위 콜백에 전달합니다.
    """

    def __init__(
        self,
        on_execution: Callable[[dict], None],
        mock_mode: bool = False,
    ):
        """
        Args:
            on_execution: 체결 이벤트 발생 시 호출되는 콜백 함수
            mock_mode: Mock 모드 (테스트용)
        """
        self.on_execution = on_execution
        self.mock_mode = mock_mode

        self.websocket = None
        self.is_running = False
        self.is_connected = False

        self.reconnect_delay = settings.kis_ws_reconnect_delay_seconds
        self.max_reconnect_attempts = settings.kis_ws_max_reconnect_attempts
        self.current_attempt = 0

        self.ping_interval = settings.kis_ws_ping_interval
        self.ping_timeout = settings.kis_ws_ping_timeout

        self.approval_key: str | None = None

        self._create_ssl_context()

    def _create_ssl_context(self):
        """SSL context 생성 (프로덕션 인증서 검사 비활성화)"""
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    async def connect_and_subscribe(self):
        """
        국내/해외 체결 TR 동시 구독 연결

        Raises:
            Exception: 연결 실패 시 (재연결 루프로 전파)
        """
        if self.mock_mode:
            logger.info("KIS WebSocket running in mock mode - no actual connection")
            self.is_connected = True
            return

        await self._issue_approval_key_if_needed()

        self.websocket_url = await self._build_websocket_url()

        while self.current_attempt < self.max_reconnect_attempts and self.is_running:
            try:
                await self._connect_and_subscribe_internal()
                self.current_attempt = 0

            except Exception as e:
                self.current_attempt += 1
                logger.error(
                    f"KIS WebSocket connection failed (attempt {self.current_attempt}/{self.max_reconnect_attempts}): {e}"
                )

                if self.current_attempt >= self.max_reconnect_attempts:
                    logger.error("Max reconnection attempts reached. Stopping.")
                    self.is_connected = False
                    break

                if self.is_running:
                    logger.info(f"Retrying in {self.reconnect_delay} seconds...")
                    await asyncio.sleep(self.reconnect_delay)

    async def _issue_approval_key_if_needed(self):
        """Approval Key 발급 (캐시 미스 시)"""
        self.approval_key = await get_approval_key()

    async def _build_websocket_url(self) -> str:
        """
        KIS WebSocket URL 빌드

        Returns:
            str: WebSocket URL (wss://)
        """
        base_url = "ws://ops.koreainvestment.com:21000"
        path = "/unknown"
        return f"{base_url}{path}"

    async def _connect_and_subscribe_internal(self):
        """
        WebSocket 연결 및 구독

        Raises:
            Exception: 연결/구독 실패 시
        """
        self.websocket = await websockets.connect(
            self.websocket_url,
            ssl=self.ssl_context,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            close_timeout=10,
        )

        self.is_connected = True
        logger.info("KIS WebSocket connected successfully")

        await self._subscribe_execution_tr()

    async def _subscribe_execution_tr(self):
        """
        국내/해외 체결 TR 구독 요청 전송

        Subscription request format example:
        0|H0STCNI0|...|...|...
        0|H0GSCNI0|...|...|...

        Raises:
            Exception: 구독 요청 실패 시
        """
        domestic_tr = "H0STCNI0"
        overseas_tr = "H0GSCNI0"

        request_domestic = f"0|{domestic_tr}|{self.approval_key}|..."
        request_overseas = f"0|{overseas_tr}|{self.approval_key}|..."

        await self._send_subscription_request(request_domestic)
        await self._send_subscription_request(request_overseas)

        logger.info("Subscribed to KIS execution TRs (domestic + overseas)")

    async def _send_subscription_request(self, request: str):
        """
        구독 요청 전송 및 응답 검증

        Args:
            request: 구독 요청 문자열

        Raises:
            Exception: 구독 실패 (에러 응답) 시
        """
        await self.websocket.send(request)

        response = await self.websocket.recv()
        parsed = self._parse_response(response)

        if parsed.get("type") == "error":
            error_msg = parsed.get("message", "Unknown error")
            raise Exception(f"Subscription failed: {error_msg}")

    async def listen(self):
        """
        WebSocket 메시지 수신 루프

        체결 메시지를 파싱하여 on_execution 콜백에 전달합니다.
        체결 타입(1)만 필터링하고, 그 외는 무시합니다.
        pingpong 시스템 메시지도 재전송 처리합니다.
        """
        if self.mock_mode:
            logger.info("Mock mode: skipping message listening")
            return

        try:
            async for message in self.websocket:
                try:
                    data = self._parse_message(message)

                    if data is None:
                        continue

                    if data.get("system") == "pingpong":
                        await self._handle_pingpong()
                        continue

                    if data.get("execution_type") == 1:
                        logger.debug(f"Execution received: {data}")
                        if self.on_execution:
                            await self.on_execution(data)

                except Exception as e:
                    logger.error(f"Message processing error: {e}", exc_info=True)

        except ConnectionClosed:
            logger.warning("KIS WebSocket connection closed")
            self.is_connected = False
        except WebSocketException as e:
            logger.error(f"KIS WebSocket error: {e}", exc_info=True)
            self.is_connected = False

    def _parse_message(self, message: str | bytes) -> dict | None:
        """
        KIS WebSocket 메시지 파싱

        |/^ 구분자로 파싱하며 인덱스 안전 처리를 수행합니다.

        Args:
            message: 원본 메시지 (문자열 또는 바이트)

        Returns:
            dict | None: 파싱된 데이터 (파싱 실패 시 None)

        Examples:
            "0|H0STCNI0|01|005930|..."
            JSON: {"type": "error", "message": "..."}
        """
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except Exception as e:
                logger.error(f"UTF-8 decode error: {e}")
                return None

        message = message.strip()

        if not message:
            return None

        if message.startswith("{"):
            try:
                import json

                return json.loads(message)
            except Exception as e:
                logger.error(f"JSON parse error: {e}, message: {message}")
                return None

        parts = message.split("|")
        if len(parts) < 3:
            logger.warning(f"Invalid message format: {message}")
            return None

        try:
            return {
                "tr_code": parts[0] if len(parts) > 0 else "",
                "execution_type": int(parts[1])
                if len(parts) > 1 and parts[1].isdigit()
                else None,
                "symbol": parts[2] if len(parts) > 2 else "",
                "market": "kr"
                if "H0STCNI0" in message
                else "us"
                if "H0GSCNI0" in message
                else "unknown",
            }

        except (ValueError, IndexError) as e:
            logger.error(f"Parse error: {e}, message: {message}")
            return None

    def _parse_response(self, message: str) -> dict:
        """
        구독 응답 메시지 파싱

        Args:
            message: 응답 메시지

        Returns:
            dict: 파싱된 데이터
        """
        if message.startswith("{"):
            import json

            return json.loads(message)

        return {"type": "ack", "message": message}

    async def _handle_pingpong(self):
        """
        Ping/Pong 시스템 메시지 처리

        KIS 서버가 보낸 pingpong 메시지를 클라이언트로 재전송합니다.
        """
        if self.websocket:
            await self.websocket.send("0|pingpong")
            logger.debug("Pingpong message echoed")

    async def stop(self):
        """
        WebSocket 연결 종료 (Graceful Shutdown)
        """
        if not self.is_running:
            logger.warning("KIS WebSocket not running")
            return

        logger.info("Stopping KIS WebSocket client...")
        self.is_running = False

        if self.websocket:
            await self.websocket.close()
            self.is_connected = False

        logger.info("KIS WebSocket stopped")

    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        await self.stop()
