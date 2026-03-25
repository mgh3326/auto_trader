"""
KIS (한국투자증권) WebSocket Client for Execution Data

국내/해외 체결 데이터를 실시간으로 수신하여 Redis pub/sub으로 발행합니다.
"""

import asyncio
import base64
import json
import logging
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import redis.asyncio as redis
import websockets
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings

logger = logging.getLogger(__name__)

from app.services.kis_websocket_internal import approval_keys
from app.services.kis_websocket_internal.constants import (
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_TTL_SECONDS,
    RECOVERABLE_APPROVAL_MSG_CODES,
)

# Public re-exports
get_approval_key = approval_keys.get_approval_key
_cache_approval_key = approval_keys._cache_approval_key
_get_cached_approval_key = approval_keys._get_cached_approval_key
_is_valid_approval_key = approval_keys._is_valid_approval_key
_issue_approval_key = approval_keys._issue_approval_key
close_approval_key_redis = approval_keys.close_approval_key_redis

DOMESTIC_EXECUTION_TR_REAL = "H0STCNI0"
OVERSEAS_EXECUTION_TR_REAL = "H0GSCNI0"
DOMESTIC_EXECUTION_TR_MOCK = "H0STCNI9"
OVERSEAS_EXECUTION_TR_MOCK = "H0GSCNI9"

DOMESTIC_EXECUTION_TR = DOMESTIC_EXECUTION_TR_REAL
OVERSEAS_EXECUTION_TR = OVERSEAS_EXECUTION_TR_REAL

DOMESTIC_EXECUTION_TR_CODES = {
    DOMESTIC_EXECUTION_TR_REAL,
    DOMESTIC_EXECUTION_TR_MOCK,
}
OVERSEAS_EXECUTION_TR_CODES = {
    OVERSEAS_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_MOCK,
}
EXECUTION_TR_CODES = DOMESTIC_EXECUTION_TR_CODES | OVERSEAS_EXECUTION_TR_CODES
RECOVERABLE_APPROVAL_MSG_CODES = {"OPSP0011", "OPSP8996"}

_SIDE_MAP = {
    "01": "ask",
    "1": "ask",
    "S": "ask",
    "SELL": "ask",
    "ASK": "ask",
    "매도": "ask",
    "02": "bid",
    "2": "bid",
    "B": "bid",
    "BUY": "bid",
    "BID": "bid",
    "매수": "bid",
}

_US_SYMBOL_RESERVED_TOKENS = {
    "PROD",
    "RESERVED",
    "ENV",
    "HTS",
    "NASD",
    "NASDAQ",
    "NYSE",
    "AMEX",
    "KRX",
}

OVERSEAS_FILL_FIELDS = {
    "side": 4,
    "rctf_cls": 5,
    "filled_at": 6,
    "symbol": 7,
    "filled_qty": 8,
    "filled_price": 9,
    "order_qty": 10,
    "cntg_yn": 11,
    "fill_yn": 11,
    "rfus_yn": 12,
    "acpt_yn": 13,
}

OVERSEAS_SIDE_MAP = {
    "01": "ask",
    "1": "ask",
    "S": "ask",
    "02": "bid",
    "2": "bid",
    "B": "bid",
}

DOMESTIC_OFFICIAL_FILL_FIELDS = {
    "order_id": 2,
    "side": 4,
    "symbol": 8,
    "filled_qty": 9,
    "filled_price": 10,
    "filled_at": 11,
    "fill_yn": 13,
}

DOMESTIC_COMPACT_FILL_FIELDS = {
    "symbol": 0,
    "side": 1,
    "order_id": 2,
    "first_numeric": 3,
    "second_numeric": 4,
    "filled_at": 5,
}


class KISSubscriptionAckError(RuntimeError):
    """Structured ACK failure for KIS subscription."""

    def __init__(self, tr_id: str, rt_cd: str, msg_cd: str, msg1: str):
        self.tr_id = tr_id
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg1 = msg1
        super().__init__(
            f"Subscription failed: tr_id={tr_id} rt_cd={rt_cd} "
            f"msg_cd={msg_cd} msg1={msg1}"
        )


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
        parsed = self._parse_response(response)
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
                received_at = self._now_iso()
                self.messages_received += 1
                self.last_message_at = received_at
                try:
                    data = self._parse_message(message)

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
                    data.setdefault("correlation_id", self._new_correlation_id())

                    if self._is_execution_event(data):
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
        if data.get("type") in {"error", "ack"}:
            return False
        tr_code = str(data.get("tr_code", ""))
        if tr_code in OVERSEAS_EXECUTION_TR_CODES:
            status = str(data.get("execution_status", "")).strip().lower()
            if status:
                is_executable = status in {"filled", "partial"}
                if not is_executable:
                    logger.error(
                        "Overseas execution event REJECTED (possible field index mismatch): "
                        "tr_code=%s fill_yn=%r cntg_yn_raw=%r filled_qty=%s filled_price=%s "
                        "execution_status=%s raw_fields_count=%d",
                        tr_code,
                        data.get("fill_yn"),
                        data.get("cntg_yn"),
                        data.get("filled_qty"),
                        data.get("filled_price"),
                        data.get("execution_status"),
                        int(data.get("raw_fields_count", 0)),
                    )
                return is_executable
            is_filled = str(data.get("fill_yn", "")).strip() == "2"
            has_qty = self._to_float(data.get("filled_qty")) > 0
            has_price = self._to_float(data.get("filled_price")) > 0
            if not (is_filled and has_qty and has_price):
                logger.error(
                    "Overseas execution event REJECTED (possible field index mismatch): "
                    "tr_code=%s fill_yn=%r cntg_yn_raw=%r filled_qty=%s filled_price=%s "
                    "execution_status=%s raw_fields_count=%d",
                    tr_code,
                    data.get("fill_yn"),
                    data.get("cntg_yn"),
                    data.get("filled_qty"),
                    data.get("filled_price"),
                    data.get("execution_status"),
                    int(data.get("raw_fields_count", 0)),
                )
            return is_filled and has_qty and has_price
        if tr_code in DOMESTIC_EXECUTION_TR_CODES:
            fill_yn = str(data.get("fill_yn") or data.get("cntg_yn") or "").strip()
            if fill_yn:
                return fill_yn == "2"
            status = str(data.get("execution_status", "")).strip().lower()
            if status:
                return status in {"filled", "partial"}
            logger.info(
                "Drop domestic execution event without fill_yn: correlation_id=%s "
                "tr_code=%s symbol=%s execution_type=%s",
                data.get("correlation_id"),
                tr_code,
                data.get("symbol"),
                data.get("execution_type"),
            )
            return False
        if data.get("execution_type") == 1:
            return True
        return tr_code in EXECUTION_TR_CODES

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
        if rfus_yn == "1":
            return "rejected"
        if rctf_cls == "2" or acpt_yn == "3":
            return "canceled"
        if cntg_yn != "2":
            return "order_notice"
        if (
            filled_qty > 0
            and filled_price > 0
            and order_qty > 0
            and filled_qty < order_qty
        ):
            return "partial"
        if filled_qty > 0 and filled_price > 0:
            return "filled"
        return "invalid_fill"

    def _parse_message(self, message: str | bytes) -> dict[str, Any] | None:
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
                parsed = json.loads(message)
                if (
                    isinstance(parsed, dict)
                    and isinstance(parsed.get("header"), dict)
                    and str(parsed["header"].get("tr_id", "")).upper() == "PINGPONG"
                ):
                    return {"system": "pingpong"}
                return parsed
            except Exception as e:
                logger.error(f"JSON parse error: {e}, message: {message}")
                return None

        if "pingpong" in message.lower():
            return {"system": "pingpong"}

        parts = message.split("|")
        if len(parts) < 3:
            logger.warning(f"Invalid message format: {message}")
            return None

        envelope = self._extract_envelope(parts)
        if envelope is None:
            logger.warning(f"Unsupported message envelope: {message}")
            return None

        parsed = {
            "tr_code": envelope["tr_code"],
            "execution_type": envelope["execution_type"],
            "market": "kr"
            if envelope["tr_code"] in DOMESTIC_EXECUTION_TR_CODES
            else "us"
            if envelope["tr_code"] in OVERSEAS_EXECUTION_TR_CODES
            else "unknown",
        }

        payload_source = envelope["payload_source"]
        payload_fields: list[str]
        if envelope.get("encrypted"):
            decrypted_payload = self._decrypt_execution_payload(
                envelope["tr_code"], payload_source
            )
            if not decrypted_payload:
                return None
            payload_fields = self._split_payload(decrypted_payload)
        else:
            payload_fields = self._split_payload(payload_source)

        if payload_fields:
            parsed["raw_fields_count"] = len(payload_fields)
            parsed.update(
                self._parse_execution_payload(
                    payload_fields,
                    parsed["market"],
                    parsed["tr_code"],
                )
            )

        if not parsed.get("symbol"):
            allow_symbol_fallback = not (
                parsed["tr_code"] in OVERSEAS_EXECUTION_TR_CODES
                and len(payload_fields) > OVERSEAS_FILL_FIELDS["symbol"]
            )
            if allow_symbol_fallback:
                parsed["symbol"] = (
                    self._extract_symbol(payload_fields, parsed["market"]) or ""
                )
            else:
                parsed["symbol"] = ""

        if parsed["tr_code"] in EXECUTION_TR_CODES and (
            not parsed.get("filled_price") or not parsed.get("filled_qty")
        ):
            logger.debug(
                "KIS execution payload parsed with fallback values: raw=%s", message
            )

        return parsed

    def _extract_envelope(self, parts: list[str]) -> dict[str, Any] | None:
        first = parts[0]
        second = parts[1] if len(parts) > 1 else ""

        if first in {"0", "1"} and second in EXECUTION_TR_CODES:
            is_encrypted = first == "1"
            if is_encrypted:
                payload_source = (
                    "|".join(parts[3:]) if len(parts) > 3 else "|".join(parts[2:])
                )
            else:
                payload_source = (
                    "|".join(parts[3:])
                    if len(parts) > 3 and parts[2].isdigit() and len(parts[2]) <= 3
                    else "|".join(parts[2:])
                )
            return {
                "tr_code": second,
                "execution_type": 1,
                "payload_source": payload_source,
                "encrypted": is_encrypted,
            }

        if first in EXECUTION_TR_CODES:
            execution_type = int(second) if second.isdigit() else 1
            return {
                "tr_code": first,
                "execution_type": execution_type,
                "payload_source": "|".join(parts[2:]),
                "encrypted": False,
            }

        execution_type = int(second) if second.isdigit() else None
        return {
            "tr_code": first,
            "execution_type": execution_type,
            "payload_source": "|".join(parts[2:]),
            "encrypted": False,
        }

    def _decrypt_execution_payload(self, tr_code: str, payload: str) -> str | None:
        if not payload:
            logger.warning("Encrypted payload is empty: tr_id=%s", tr_code)
            return None

        crypto = self._encryption_keys_by_tr.get(tr_code)
        if not crypto:
            logger.warning(
                "Encrypted payload received but key/iv is missing: tr_id=%s", tr_code
            )
            return None

        key_raw, iv_raw = crypto
        try:
            key_bytes = self._decode_aes_material(key_raw, iv=False)
            iv_bytes = self._decode_aes_material(iv_raw, iv=True)
            cipher_bytes = base64.b64decode(payload)

            decryptor = Cipher(
                algorithms.AES(key_bytes), modes.CBC(iv_bytes)
            ).decryptor()
            padded_plain = decryptor.update(cipher_bytes) + decryptor.finalize()

            unpadder = padding.PKCS7(128).unpadder()
            plain = unpadder.update(padded_plain) + unpadder.finalize()
            return plain.decode("utf-8").strip()
        except Exception as e:
            logger.warning(
                "Failed to decrypt execution payload: tr_id=%s error=%s", tr_code, e
            )
            return None

    def _decode_aes_material(self, raw: str, *, iv: bool) -> bytes:
        expected_lengths = {16} if iv else {16, 24, 32}

        utf8_bytes = raw.encode("utf-8")
        if len(utf8_bytes) in expected_lengths:
            return utf8_bytes

        decoded_bytes = base64.b64decode(raw, validate=True)
        if len(decoded_bytes) in expected_lengths:
            return decoded_bytes

        kind = "iv" if iv else "key"
        raise ValueError(f"Invalid AES {kind} length")

    def _split_payload(self, payload: str) -> list[str]:
        if not payload:
            return []
        if "^" in payload:
            return payload.split("^")
        return [part for part in payload.split("|") if part]

    def _parse_execution_payload(
        self,
        payload_fields: list[str],
        market: str,
        tr_code: str,
    ) -> dict[str, Any]:
        raw_fields = [field.strip() for field in payload_fields]
        compact_fields = [field for field in raw_fields if field]
        if not compact_fields:
            return {}

        if market == "us":
            parsed_overseas = self._parse_overseas_execution(raw_fields)
            if parsed_overseas is not None:
                return parsed_overseas
            logger.error(
                "Overseas execution payload parse FAILED (returned None): "
                "tr_code=%s field_count=%d raw_fields=%r",
                tr_code,
                len(raw_fields),
                raw_fields[:16],
            )

        if market == "kr":
            parsed_domestic = self._parse_domestic_execution(raw_fields)
            if parsed_domestic is not None:
                return parsed_domestic

        kv: dict[str, str] = {}
        for token in compact_fields:
            if "=" in token:
                key, value = token.split("=", 1)
                kv[key.strip().lower()] = value.strip()

        if market in {"kr", "us"} and payload_fields and not kv:
            return {}

        fields = compact_fields
        symbol = self._extract_symbol(fields, market)

        side_token = self._first_token(
            kv,
            fields,
            ["side", "sll_buy_dvsn_cd", "buy_sell", "bsop_gb"],
            lambda v: v.upper() in _SIDE_MAP,
        )
        side = _SIDE_MAP.get(side_token.upper(), "unknown") if side_token else "unknown"

        order_id = self._first_token(
            kv,
            fields,
            ["order_id", "ord_no", "odno", "orgn_ord_no"],
            lambda v: len(v) >= 6 and not (v.isdigit() and len(v) == 6),
        )
        filled_price = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_price", "ccld_unpr", "ft_ccld_unpr3", "price", "trade_price"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )
        filled_qty = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_qty", "ccld_qty", "ft_ccld_qty", "qty", "trade_volume"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )
        filled_amount = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_amount", "ccld_amt", "ft_ccld_amt3", "amount", "trade_amount"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )

        if filled_price <= 0 and len(fields) >= 4:
            filled_price = self._to_float(fields[3])
        if filled_qty <= 0 and len(fields) >= 5:
            filled_qty = self._to_float(fields[4])
        if filled_amount <= 0 and len(fields) >= 6:
            filled_amount = self._to_float(fields[5])

        if filled_price <= 0 or filled_qty <= 0:
            numeric_candidates = []
            for token in fields:
                if token in {symbol, side_token, order_id}:
                    continue
                numeric_value = self._to_float(token)
                if numeric_value > 0:
                    numeric_candidates.append(numeric_value)
            if filled_price <= 0 and numeric_candidates:
                filled_price = max(numeric_candidates)
            if filled_qty <= 0 and numeric_candidates:
                filled_qty = min(numeric_candidates)

        if filled_amount <= 0 and filled_price > 0 and filled_qty > 0:
            filled_amount = filled_price * filled_qty

        filled_at = self._extract_timestamp(
            self._first_token(
                kv,
                fields,
                ["filled_at", "timestamp", "exec_time", "ord_tmd", "ccld_time"],
                lambda v: bool(v.strip()),
            )
        )

        return {
            "symbol": symbol or "",
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "filled_at": filled_at,
        }

    def _parse_overseas_execution(self, fields: list[str]) -> dict[str, Any] | None:
        if len(fields) <= max(OVERSEAS_FILL_FIELDS.values()):
            logger.error(
                "Overseas execution payload has insufficient fields: field_count=%d required=%d",
                len(fields),
                max(OVERSEAS_FILL_FIELDS.values()) + 1,
            )
            return None

        symbol = fields[OVERSEAS_FILL_FIELDS["symbol"]].strip()
        if not symbol:
            logger.error(
                "Overseas execution payload missing symbol at index %d: field_count=%d",
                OVERSEAS_FILL_FIELDS["symbol"],
                len(fields),
            )
            return None

        side_token = fields[OVERSEAS_FILL_FIELDS["side"]].strip().upper()
        side = OVERSEAS_SIDE_MAP.get(side_token, "unknown")
        order_qty = self._to_float(fields[OVERSEAS_FILL_FIELDS["order_qty"]])
        rctf_cls = fields[OVERSEAS_FILL_FIELDS["rctf_cls"]].strip()
        acpt_yn = fields[OVERSEAS_FILL_FIELDS["acpt_yn"]].strip()
        rfus_yn = fields[OVERSEAS_FILL_FIELDS["rfus_yn"]].strip()
        cntg_yn = fields[OVERSEAS_FILL_FIELDS["cntg_yn"]].strip()

        filled_qty = self._to_float(fields[OVERSEAS_FILL_FIELDS["filled_qty"]])
        filled_price = self._to_float(fields[OVERSEAS_FILL_FIELDS["filled_price"]])

        order_id = fields[2].strip() if len(fields) > 2 else ""
        if not order_id:
            order_id = None

        filled_at = self._extract_timestamp(fields[OVERSEAS_FILL_FIELDS["filled_at"]])
        execution_status = self._classify_overseas_execution_status(
            rfus_yn=rfus_yn,
            rctf_cls=rctf_cls,
            acpt_yn=acpt_yn,
            cntg_yn=cntg_yn,
            filled_qty=filled_qty,
            filled_price=filled_price,
            order_qty=order_qty,
        )

        filled_amount = (
            filled_price * filled_qty if filled_price > 0 and filled_qty > 0 else 0
        )

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "filled_at": filled_at,
            "currency": "USD",
            "order_qty": order_qty,
            "rctf_cls": rctf_cls,
            "acpt_yn": acpt_yn,
            "rfus_yn": rfus_yn,
            "cntg_yn": cntg_yn,
            "fill_yn": cntg_yn,
            "execution_status": execution_status,
        }

    def _parse_domestic_execution(self, fields: list[str]) -> dict[str, Any] | None:
        parsed = self._parse_domestic_execution_by_official_index(fields)
        if parsed is not None:
            return parsed
        return self._parse_domestic_execution_compact(fields)

    def _parse_domestic_execution_by_official_index(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        if len(fields) <= max(DOMESTIC_OFFICIAL_FILL_FIELDS.values()):
            return None

        symbol = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["symbol"]].strip()
        if not (symbol.isdigit() and len(symbol) == 6):
            return None

        side_token = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["side"]].strip().upper()
        side = _SIDE_MAP.get(side_token, "unknown")
        order_id = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["order_id"]].strip() or None

        filled_qty = self._to_float(fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_qty"]])
        filled_price = self._to_float(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_price"]]
        )
        if filled_qty <= 0 or filled_price <= 0:
            return None

        filled_at = self._extract_timestamp(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_at"]]
        )
        fill_yn = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]].strip()
        if not self._is_supported_timestamp_token(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_at"]]
        ):
            return None
        if fill_yn not in {"1", "2"}:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_price * filled_qty,
            "filled_at": filled_at,
            "fill_yn": fill_yn,
        }

    def _parse_domestic_execution_compact(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        if len(fields) <= max(DOMESTIC_COMPACT_FILL_FIELDS.values()):
            return None

        symbol = fields[DOMESTIC_COMPACT_FILL_FIELDS["symbol"]].strip()
        if not (symbol.isdigit() and len(symbol) == 6):
            return None

        side_token = fields[DOMESTIC_COMPACT_FILL_FIELDS["side"]].strip().upper()
        side = _SIDE_MAP.get(side_token, "unknown")
        order_id = fields[DOMESTIC_COMPACT_FILL_FIELDS["order_id"]].strip() or None

        first_numeric = self._to_float(
            fields[DOMESTIC_COMPACT_FILL_FIELDS["first_numeric"]]
        )
        second_numeric = self._to_float(
            fields[DOMESTIC_COMPACT_FILL_FIELDS["second_numeric"]]
        )
        if first_numeric <= 0 or second_numeric <= 0:
            return None

        if first_numeric <= second_numeric:
            filled_qty = first_numeric
            filled_price = second_numeric
        else:
            filled_qty = second_numeric
            filled_price = first_numeric

        filled_at_token = self._find_hhmmss_token(
            fields, exclude={symbol, order_id or ""}
        )
        if (
            not filled_at_token
            and len(fields) > DOMESTIC_COMPACT_FILL_FIELDS["filled_at"]
        ):
            fallback_token = fields[DOMESTIC_COMPACT_FILL_FIELDS["filled_at"]].strip()
            if self._is_hhmmss(fallback_token):
                filled_at_token = fallback_token
        if not filled_at_token:
            return None
        filled_at = self._extract_timestamp(filled_at_token)

        fill_yn = ""
        if len(fields) > DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]:
            fill_yn = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]].strip()

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_price * filled_qty,
            "filled_at": filled_at,
            "fill_yn": fill_yn,
        }

    def _first_token(
        self,
        kv: dict[str, str],
        fields: list[str],
        preferred_keys: list[str],
        predicate: Callable[[str], bool],
        *,
        scan_fields: bool = True,
    ) -> str | None:
        for key in preferred_keys:
            value = kv.get(key.lower())
            if value and predicate(value):
                return value

        if not scan_fields:
            return None

        for token in fields:
            if predicate(token):
                return token
        return None

    def _extract_symbol(self, fields: list[str], market: str) -> str | None:
        for token in fields:
            stripped = token.strip()
            if market == "kr" and stripped.isdigit() and len(stripped) == 6:
                return stripped
            if market != "us":
                continue

            normalized = stripped.upper()
            cleaned = normalized.replace(".", "").replace("-", "").replace("/", "")
            if not cleaned or not cleaned.isalnum() or not (1 <= len(cleaned) <= 10):
                continue
            if not cleaned[0].isalpha() or cleaned.isdigit():
                continue
            if normalized != stripped:
                continue
            if cleaned in _US_SYMBOL_RESERVED_TOKENS:
                continue
            if cleaned.startswith(("ORDER", "ACNT", "ACCOUNT", "CUST", "USER")):
                continue
            if any(ch.isdigit() for ch in cleaned) and len(cleaned) >= 8:
                continue
            return stripped
        return None

    def _extract_timestamp(self, value: str | None) -> str:
        if not value:
            return datetime.now(UTC).replace(microsecond=0).isoformat()

        cleaned = value.strip()
        if "T" in cleaned:
            return cleaned
        if cleaned.isdigit():
            if len(cleaned) == 6:
                today = datetime.now(UTC).strftime("%Y%m%d")
                return datetime.strptime(today + cleaned, "%Y%m%d%H%M%S").isoformat()
            if len(cleaned) == 14:
                return datetime.strptime(cleaned, "%Y%m%d%H%M%S").isoformat()
        return cleaned

    def _find_hhmmss_token(
        self, fields: list[str], *, exclude: set[str] | None = None
    ) -> str | None:
        excluded = {token.strip() for token in (exclude or set()) if token}
        for token in fields:
            stripped = token.strip()
            if stripped in excluded:
                continue
            if self._is_hhmmss(stripped):
                return stripped
        return None

    def _is_hhmmss(self, value: str) -> bool:
        if len(value) != 6 or not value.isdigit():
            return False
        hour = int(value[:2])
        minute = int(value[2:4])
        second = int(value[4:6])
        return hour < 24 and minute < 60 and second < 60

    def _is_supported_timestamp_token(self, value: str | None) -> bool:
        if not value:
            return False
        cleaned = value.strip()
        if self._is_hhmmss(cleaned):
            return True
        if len(cleaned) == 14 and cleaned.isdigit():
            try:
                datetime.strptime(cleaned, "%Y%m%d%H%M%S")
                return True
            except ValueError:
                return False
        return False

    def _to_float(self, value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _parse_response(self, message: str | bytes) -> dict[str, Any]:
        """
        구독 응답 메시지 파싱

        Args:
            message: 응답 메시지

        Returns:
            dict: 파싱된 데이터
        """
        if isinstance(message, bytes):
            message = message.decode("utf-8")

        message = message.strip()

        if message.startswith("{"):
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

    def get_runtime_snapshot(self) -> dict[str, int | str | None]:
        return {
            "messages_received": self.messages_received,
            "execution_events_received": self.execution_events_received,
            "last_message_at": self.last_message_at,
            "last_execution_at": self.last_execution_at,
            "last_pingpong_at": self.last_pingpong_at,
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _new_correlation_id() -> str:
        return uuid4().hex

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
