import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from inspect import isawaitable
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings

from . import approval_keys
from .constants import RECOVERABLE_APPROVAL_MSG_CODES
from .parsers import ExecutionMessageParser
from .protocol import (
    DOMESTIC_EXECUTION_TR_MOCK,
    DOMESTIC_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_MOCK,
    OVERSEAS_EXECUTION_TR_REAL,
    KISSubscriptionAckError,
)

logger = logging.getLogger(__name__)


class KISExecutionWebSocket:
    """
    KIS 체결 WebSocket 클라이언트

    국내/해외 체결 데이터를 실시간으로 수신하고 콜백으로 전달합니다.
    체결 타입(1)만 필터링하여 상위 콜백에 전달합니다.
    """

    def __init__(
        self,
        on_execution: Callable[[dict[str, Any]], Any],
        mock_mode: bool = False,
    ):
        """
        Args:
            on_execution: 체결 이벤트 발생 시 호출되는 콜백 함수
            mock_mode: Mock 모드 (테스트용)
        """
        self.on_execution = on_execution
        self.mock_mode = mock_mode

        self.websocket: Any | None = None
        self.websocket_url = ""
        self.is_running = False
        self.is_connected = False

        self.reconnect_delay = settings.kis_ws_reconnect_delay_seconds
        self.max_reconnect_attempts = settings.kis_ws_max_reconnect_attempts
        self.current_attempt = 0

        self.ping_interval = settings.kis_ws_ping_interval
        self.ping_timeout = settings.kis_ws_ping_timeout
        self.messages_received = 0
        self.execution_events_received = 0
        self.last_message_at: str | None = None
        self.last_execution_at: str | None = None
        self.last_pingpong_at: str | None = None

        self.approval_key: str | None = None
        self._encryption_keys_by_tr: dict[str, tuple[str, str]] = {}
        self._last_reissue_msg_code: str | None = None

        self._parser = ExecutionMessageParser(self._encryption_keys_by_tr)

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
        await self._issue_approval_key_if_needed()

        self.websocket_url = await self._build_websocket_url()

        while self.current_attempt < self.max_reconnect_attempts and self.is_running:
            try:
                await self._connect_and_subscribe_internal()
                self.current_attempt = 0
                self._last_reissue_msg_code = None
                return

            except Exception as e:
                self.current_attempt += 1
                ack_error = e if isinstance(e, KISSubscriptionAckError) else None
                recoverable_ack_failure = (
                    ack_error is not None
                    and ack_error.msg_cd in RECOVERABLE_APPROVAL_MSG_CODES
                    and self.is_running
                )
                if recoverable_ack_failure:
                    logger.warning(
                        "KIS WebSocket recoverable ACK failure: "
                        "attempt=%s/%s tr_id=%s msg_cd=%s msg1=%s last_message_at=%s "
                        "last_execution_at=%s last_pingpong_at=%s",
                        self.current_attempt,
                        self.max_reconnect_attempts,
                        ack_error.tr_id if ack_error is not None else None,
                        ack_error.msg_cd if ack_error is not None else None,
                        ack_error.msg1 if ack_error is not None else None,
                        self.last_message_at,
                        self.last_execution_at,
                        self.last_pingpong_at,
                    )
                else:
                    logger.error(
                        "KIS WebSocket connection failed: attempt=%s/%s error=%s "
                        "last_message_at=%s last_execution_at=%s last_pingpong_at=%s",
                        self.current_attempt,
                        self.max_reconnect_attempts,
                        e,
                        self.last_message_at,
                        self.last_execution_at,
                        self.last_pingpong_at,
                    )
                await self._close_websocket_best_effort()

                if recoverable_ack_failure:
                    if (
                        ack_error is not None
                        and self._last_reissue_msg_code == ack_error.msg_cd
                    ):
                        # 동일한 ACK 오류가 연속으로 반복되면 최소 1초 대기 후 재발급합니다.
                        await asyncio.sleep(1)
                    self.approval_key = await approval_keys._issue_approval_key()
                    await approval_keys._cache_approval_key(self.approval_key)
                    self._last_reissue_msg_code = (
                        ack_error.msg_cd if ack_error else None
                    )
                    logger.info(
                        "Approval key reissued after recoverable ACK failure: "
                        "msg_cd=%s attempt=%s messages_received=%s execution_events_received=%s",
                        ack_error.msg_cd if ack_error else None,
                        self.current_attempt,
                        self.messages_received,
                        self.execution_events_received,
                    )
                else:
                    self._last_reissue_msg_code = None

                if self.current_attempt >= self.max_reconnect_attempts:
                    logger.error(
                        "Max reconnection attempts reached: messages_received=%s "
                        "execution_events_received=%s last_message_at=%s last_execution_at=%s "
                        "last_pingpong_at=%s",
                        self.messages_received,
                        self.execution_events_received,
                        self.last_message_at,
                        self.last_execution_at,
                        self.last_pingpong_at,
                    )
                    self.is_connected = False
                    break

                if self.is_running:
                    logger.info(
                        "Retrying KIS WebSocket connection in %s seconds: attempt=%s/%s",
                        self.reconnect_delay,
                        self.current_attempt,
                        self.max_reconnect_attempts,
                    )
                    await asyncio.sleep(self.reconnect_delay)

        if not self.is_connected:
            raise RuntimeError("KIS WebSocket connection not established")

    async def _issue_approval_key_if_needed(self):
        """Approval Key 발급 (캐시 미스 시)"""
        self.approval_key = await approval_keys.get_approval_key()

    async def _build_websocket_url(self) -> str:
        """
        KIS WebSocket URL 빌드

        Returns:
            str: WebSocket URL
        """
        base_url = (
            "ws://ops.koreainvestment.com:31000"
            if self.mock_mode
            else "ws://ops.koreainvestment.com:21000"
        )
        path = "/tryitout"
        return f"{base_url}{path}"

    async def _connect_and_subscribe_internal(self):
        """
        WebSocket 연결 및 구독

        Raises:
            Exception: 연결/구독 실패 시
        """
        self._encryption_keys_by_tr.clear()

        connect_kwargs: dict[str, Any] = {
            "ping_interval": self.ping_interval,
            "ping_timeout": self.ping_timeout,
            "close_timeout": 10,
        }
        if urlparse(self.websocket_url).scheme == "wss":
            connect_kwargs["ssl"] = self.ssl_context

        self.websocket = await websockets.connect(self.websocket_url, **connect_kwargs)

        self.is_connected = True
        logger.info("KIS WebSocket connected successfully")

        await self._subscribe_execution_tr()

    async def _subscribe_execution_tr(self):
        if not self.approval_key:
            raise RuntimeError("Approval key is not issued")

        tr_key = settings.kis_ws_hts_id.strip()
        if not tr_key:
            raise ValueError("KIS_WS_HTS_ID must be configured")

        domestic_tr = (
            DOMESTIC_EXECUTION_TR_MOCK if self.mock_mode else DOMESTIC_EXECUTION_TR_REAL
        )
        overseas_tr = (
            OVERSEAS_EXECUTION_TR_MOCK if self.mock_mode else OVERSEAS_EXECUTION_TR_REAL
        )

        request_domestic = self._build_subscription_request(domestic_tr, tr_key)
        request_overseas = self._build_subscription_request(overseas_tr, tr_key)

        await self._send_subscription_request(request_domestic, domestic_tr)
        await self._send_subscription_request(request_overseas, overseas_tr)

        logger.info("Subscribed to KIS execution TRs (domestic + overseas)")

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
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key,
                }
            },
        }

    async def _send_subscription_request(self, request: dict[str, Any], tr_id: str):
        if self.websocket is None:
            raise RuntimeError("WebSocket is not connected")

        websocket = self.websocket
        await websocket.send(json.dumps(request))

        response = await websocket.recv()
        parsed = self._parser._parse_response(response)
        self._validate_subscription_ack(parsed, expected_tr_id=tr_id)

    def _validate_subscription_ack(
        self, parsed: dict[str, Any], expected_tr_id: str
    ) -> None:
        if parsed.get("type") == "error":
            error_msg = parsed.get("message", "Unknown error")
            raise Exception(f"Subscription failed: {error_msg}")

        body = parsed.get("body")
        if not isinstance(body, dict):
            raise Exception("Subscription failed: ACK body missing")

        rt_cd = str(body.get("rt_cd", ""))
        msg_cd = str(body.get("msg_cd", ""))
        msg1 = str(body.get("msg1", ""))
        if rt_cd != "0":
            raise KISSubscriptionAckError(
                tr_id=expected_tr_id, rt_cd=rt_cd, msg_cd=msg_cd, msg1=msg1
            )

        header = parsed.get("header")
        ack_tr_id = expected_tr_id
        if isinstance(header, dict):
            ack_tr_id = str(header.get("tr_id") or expected_tr_id)

        output = body.get("output")
        key: str | None = None
        iv: str | None = None
        if isinstance(output, dict):
            output_key = output.get("key")
            output_iv = output.get("iv")
            key = str(output_key).strip() if output_key else None
            iv = str(output_iv).strip() if output_iv else None

        if key and iv:
            self._encryption_keys_by_tr[ack_tr_id] = (key, iv)

        logger.info(
            "KIS subscription ACK received: tr_id=%s rt_cd=%s msg_cd=%s key=%s iv=%s",
            ack_tr_id,
            rt_cd,
            msg_cd,
            bool(key),
            bool(iv),
        )

    async def listen(self):
        """
        WebSocket 메시지 수신 루프

        체결 메시지를 파싱하여 on_execution 콜백에 전달합니다.
        체결 타입(1)만 필터링하고, 그 외는 무시합니다.
        pingpong 시스템 메시지도 재전송 처리합니다.
        """
        if self.websocket is None:
            raise RuntimeError("WebSocket is not connected")

        websocket = self.websocket

        try:
            async for message in websocket:
                received_at = self._parser._now_iso()
                self.messages_received += 1
                self.last_message_at = received_at
                try:
                    data = self._parser.parse_message(message)

                    if data is None:
                        continue

                    if data.get("system") == "pingpong":
                        self.last_pingpong_at = received_at
                        logger.debug(
                            "KIS pingpong received: received_at=%s messages_received=%s",
                            received_at,
                            self.messages_received,
                        )
                        await self._handle_pingpong()
                        continue

                    data["received_at"] = received_at
                    data.setdefault(
                        "correlation_id", self._parser._new_correlation_id()
                    )

                    if self._parser.is_execution_event(data):
                        self.execution_events_received += 1
                        self.last_execution_at = received_at
                        logger.info(
                            "KIS execution received: correlation_id=%s received_at=%s "
                            "tr_code=%s market=%s symbol=%s side=%s filled_qty=%s "
                            "filled_price=%s fill_yn=%s execution_status=%s",
                            data.get("correlation_id"),
                            received_at,
                            data.get("tr_code"),
                            data.get("market"),
                            data.get("symbol"),
                            data.get("side"),
                            data.get("filled_qty"),
                            data.get("filled_price"),
                            data.get("fill_yn"),
                            data.get("execution_status"),
                        )
                        if self.on_execution:
                            callback_result = self.on_execution(data)
                            if isawaitable(callback_result):
                                await callback_result

                except Exception as e:
                    logger.error(f"Message processing error: {e}", exc_info=True)

        except ConnectionClosed:
            logger.warning(
                "KIS WebSocket connection closed: messages_received=%s "
                "execution_events_received=%s last_message_at=%s last_execution_at=%s "
                "last_pingpong_at=%s",
                self.messages_received,
                self.execution_events_received,
                self.last_message_at,
                self.last_execution_at,
                self.last_pingpong_at,
            )
            self.is_connected = False
        except WebSocketException as e:
            logger.error(
                "KIS WebSocket error: error=%s messages_received=%s execution_events_received=%s "
                "last_message_at=%s last_execution_at=%s last_pingpong_at=%s",
                e,
                self.messages_received,
                self.execution_events_received,
                self.last_message_at,
                self.last_execution_at,
                self.last_pingpong_at,
                exc_info=True,
            )
            self.is_connected = False

    def _is_execution_event(self, data: dict[str, Any]) -> bool:
        return self._parser.is_execution_event(data)

    def _classify_overseas_execution_status(
        self,
        *,
        rfus_yn: str,
        rctf_cls: str,
        acpt_yn: str,
        cntg_yn: str,
        filled_qty: float,
        filled_price: float,
        order_qty: float,
    ) -> str:
        return self._parser._classify_overseas_execution_status(
            rfus_yn=rfus_yn,
            rctf_cls=rctf_cls,
            acpt_yn=acpt_yn,
            cntg_yn=cntg_yn,
            filled_qty=filled_qty,
            filled_price=filled_price,
            order_qty=order_qty,
        )

    def _parse_message(self, message: str | bytes) -> dict[str, Any] | None:
        return self._parser.parse_message(message)

    def _extract_envelope(self, parts: list[str]) -> dict[str, Any] | None:
        return self._parser._extract_envelope(parts)

    def _decrypt_execution_payload(self, tr_code: str, payload: str) -> str | None:
        return self._parser._decrypt_execution_payload(tr_code, payload)

    def _decode_aes_material(self, raw: str, *, iv: bool) -> bytes:
        return self._parser._decode_aes_material(raw, iv=iv)

    def _split_payload(self, payload: str) -> list[str]:
        return self._parser._split_payload(payload)

    def _parse_execution_payload(
        self,
        payload_fields: list[str],
        market: str,
        tr_code: str,
    ) -> dict[str, Any]:
        return self._parser._parse_execution_payload(payload_fields, market, tr_code)

    def _parse_overseas_execution(self, fields: list[str]) -> dict[str, Any] | None:
        return self._parser._parse_overseas_execution(fields)

    def _parse_domestic_execution(self, fields: list[str]) -> dict[str, Any] | None:
        return self._parser._parse_domestic_execution(fields)

    def _parse_domestic_execution_by_official_index(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        return self._parser._parse_domestic_execution_by_official_index(fields)

    def _parse_domestic_execution_compact(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        return self._parser._parse_domestic_execution_compact(fields)

    def _first_token(
        self,
        kv: dict[str, str],
        fields: list[str],
        preferred_keys: list[str],
        predicate: Callable[[str], bool],
        *,
        scan_fields: bool = True,
    ) -> str | None:
        return self._parser._first_token(
            kv, fields, preferred_keys, predicate, scan_fields=scan_fields
        )

    def _extract_symbol(self, fields: list[str], market: str) -> str | None:
        return self._parser._extract_symbol(fields, market)

    def _extract_timestamp(self, value: str | None) -> str:
        return self._parser._extract_timestamp(value)

    def _find_hhmmss_token(
        self, fields: list[str], *, exclude: set[str] | None = None
    ) -> str | None:
        return self._parser._find_hhmmss_token(fields, exclude=exclude)

    def _is_hhmmss(self, value: str) -> bool:
        return self._parser._is_hhmmss(value)

    def _is_supported_timestamp_token(self, value: str | None) -> bool:
        return self._parser._is_supported_timestamp_token(value)

    def _to_float(self, value: Any) -> float:
        return self._parser._to_float(value)

    def _parse_response(self, message: str | bytes) -> dict[str, Any]:
        return self._parser._parse_response(message)

    async def _handle_pingpong(self):
        """
        Ping/Pong 시스템 메시지 처리

        KIS 서버가 보낸 pingpong 메시지를 클라이언트로 재전송합니다.
        """
        if self.websocket:
            await self.websocket.send("0|pingpong")
            logger.debug("Pingpong message echoed")

    def get_runtime_snapshot(self) -> dict[str, int | str | None]:
        return {
            "messages_received": self.messages_received,
            "execution_events_received": self.execution_events_received,
            "last_message_at": self.last_message_at,
            "last_execution_at": self.last_execution_at,
            "last_pingpong_at": self.last_pingpong_at,
        }

    def _now_iso(self) -> str:
        return self._parser._now_iso()

    def _new_correlation_id(self) -> str:
        return self._parser._new_correlation_id()

    async def _close_websocket_best_effort(self) -> None:
        websocket = self.websocket
        self.websocket = None
        self.is_connected = False
        if websocket is None:
            return
        try:
            await websocket.close()
        except Exception as e:
            logger.debug("Failed to close websocket during reconnect cleanup: %s", e)

    async def stop(self):
        """
        WebSocket 연결 종료 (Graceful Shutdown)
        """
        logger.info("Stopping KIS WebSocket client...")
        self.is_running = False

        try:
            if self.websocket:
                await self.websocket.close()
        except Exception as e:
            logger.warning(f"Failed to close KIS WebSocket cleanly: {e}")
        finally:
            self.websocket = None
            self.is_connected = False
            self._encryption_keys_by_tr.clear()
            try:
                await approval_keys.close_approval_key_redis()
            except Exception as e:
                logger.warning(
                    f"Failed to close Approval Key Redis client cleanly: {e}"
                )

        logger.info("KIS WebSocket stopped")

    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        await self.stop()
