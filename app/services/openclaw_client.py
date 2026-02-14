import json
import logging
from uuid import uuid4

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.services.fill_notification import (
    FillOrderLike,
    coerce_fill_order,
    format_fill_message,
)

logger = logging.getLogger(__name__)


class OpenClawClient:
    """Client for OpenClaw Gateway webhook (POST /hooks/agent)."""

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        callback_url: str | None = None,
    ) -> None:
        self._webhook_url = webhook_url or settings.OPENCLAW_WEBHOOK_URL
        self._token = token if token is not None else settings.OPENCLAW_TOKEN
        self._callback_url = callback_url or settings.OPENCLAW_CALLBACK_URL

    async def request_analysis(
        self,
        prompt: str,
        symbol: str,
        name: str,
        instrument_type: str,
    ) -> str:
        """Send an analysis request to OpenClaw.

        Returns
        -------
        str
            request_id to correlate the callback payload.
        """
        if not settings.OPENCLAW_ENABLED:
            raise RuntimeError(
                "OpenClaw integration is disabled (OPENCLAW_ENABLED=false)"
            )

        request_id = str(uuid4())

        message = _build_openclaw_message(
            request_id=request_id,
            prompt=prompt,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            callback_url=self._callback_url,
            callback_token=settings.OPENCLAW_CALLBACK_TOKEN,
        )

        payload = {
            "message": message,
            "name": "auto-trader:analysis",
            "sessionKey": f"auto-trader:openclaw:{request_id}",
            "wakeMode": "now",
        }

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        async with httpx.AsyncClient(timeout=10) as cli:
            res = await cli.post(self._webhook_url, json=payload, headers=headers)
            res.raise_for_status()

        logger.info(
            "OpenClaw analysis requested: request_id=%s symbol=%s instrument_type=%s status=%s",
            request_id,
            symbol,
            instrument_type,
            res.status_code,
        )
        return request_id

    async def send_fill_notification(self, order: FillOrderLike) -> str | None:
        """
        체결 알림을 OpenClaw Gateway로 전송

        Fire-and-forget 텍스트 알림으로 전송하며, 최대 4회 시도합니다
        (초기 1회 + 재시도 3회, 1s -> 2s -> 4s 백오프).
        모든 재시도 실패 시 None을 반환하고 예외를 삼킵니다.

        Args:
            order: 정규화된 체결 데이터

        Returns:
            str | None: 성공 시 request_id, 실패 시 None
        """
        if not settings.OPENCLAW_ENABLED:
            logger.debug("OpenClaw disabled, skipping fill notification")
            return None

        normalized_order = coerce_fill_order(order)
        request_id = str(uuid4())
        message = format_fill_message(normalized_order)

        order_id = normalized_order.order_id or request_id
        session_key = f"auto-trader:fill:{normalized_order.account}:{order_id}"

        payload = {
            "message": message,
            "name": "auto-trader:fill",
            "sessionKey": session_key,
            "wakeMode": "now",
        }

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                reraise=False,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=10) as cli:
                        res = await cli.post(
                            self._webhook_url, json=payload, headers=headers
                        )
                        res.raise_for_status()
                    logger.info(
                        "OpenClaw fill notification sent: request_id=%s symbol=%s account=%s",
                        request_id,
                        normalized_order.symbol,
                        normalized_order.account,
                    )
                    return request_id

        except RetryError as e:
            logger.error(
                "OpenClaw fill notification failed after retries: request_id=%s error=%s",
                request_id,
                e,
            )
            return None
        except Exception as e:
            logger.error(
                "OpenClaw fill notification error: request_id=%s error=%s",
                request_id,
                e,
            )
            return None


def _build_openclaw_message(
    *,
    request_id: str,
    prompt: str,
    symbol: str,
    name: str,
    instrument_type: str,
    callback_url: str,
    callback_token: str | None,
) -> str:
    callback_schema = {
        "request_id": request_id,
        "symbol": symbol,
        "name": name,
        "instrument_type": instrument_type,
        "decision": "buy|hold|sell",
        "confidence": 0,
        "reasons": ["..."],
        "price_analysis": {
            "appropriate_buy_range": {"min": 0, "max": 0},
            "appropriate_sell_range": {"min": 0, "max": 0},
            "buy_hope_range": {"min": 0, "max": 0},
            "sell_target_range": {"min": 0, "max": 0},
        },
        "detailed_text": "...",
        "model_name": "...",
    }

    schema_json = json.dumps(callback_schema, ensure_ascii=True)

    callback_headers = "Content-Type: application/json\n"
    token = callback_token.strip() if callback_token else ""
    if token:
        callback_headers += f"Authorization: Bearer {token}\n"

    return (
        "Analyze the following trading instrument and return a JSON result via HTTP callback.\n\n"
        f"request_id: {request_id}\n"
        f"symbol: {symbol}\n"
        f"name: {name}\n"
        f"instrument_type: {instrument_type}\n\n"
        "USER_PROMPT:\n"
        f"{prompt}\n\n"
        "CALLBACK:\n"
        f"POST {callback_url}\n"
        f"{callback_headers}\n"
        "RESPONSE_JSON_SCHEMA (example):\n"
        f"{schema_json}\n"
    )
