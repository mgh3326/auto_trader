import json
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.fill_notification import (
    FillOrderLike,
    coerce_fill_order,
    format_fill_message,
)

logger = logging.getLogger(__name__)

OPENCLAW_RETRY_STOP = stop_after_attempt(4)
OPENCLAW_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=4)
OPENCLAW_FILL_AGENT_NAME = "TradeAlert"
OPENCLAW_FILL_AGENT_CHANNEL = "discord"
OPENCLAW_FILL_AGENT_MODEL = "gpt"
OPENCLAW_FILL_AGENT_TIMEOUT_SECONDS = 60
OPENCLAW_FILL_MARKET_LABELS = {
    "kr": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}
OPENCLAW_FILL_AGENT_INSTRUCTIONS = (
    "반드시 `get_holdings`와 `analyze_stock`를 실행하세요.\n"
    "최종 판단은 `buy`, `hold`, `sell` 중 정확히 하나만 선택하세요.\n"
    "Discord 댓글 첫 줄은 반드시 `판단: buy`, `판단: hold`, `판단: sell` 중 하나로 시작하고, "
    "그 다음 줄부터 현재 보유 상태, 이번 체결의 의미, 핵심 근거를 한국어로 간결하게 정리하세요."
)

FillNotificationDeliveryStatus = Literal["success", "skipped", "failed"]


@dataclass(slots=True, frozen=True)
class FillNotificationDeliveryResult:
    status: FillNotificationDeliveryStatus
    reason: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.status == "success" and self.request_id is None:
            raise ValueError("success results require a request_id")
        if self.status != "success" and self.request_id is not None:
            raise ValueError("request_id is only allowed for success results")


def _build_openclaw_retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=OPENCLAW_RETRY_STOP,
        wait=OPENCLAW_RETRY_WAIT,
        reraise=False,
    )


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _format_fill_side_text(side: str, fill_status: str | None) -> str:
    side_text = {
        "bid": "매수",
        "ask": "매도",
    }.get(side, "미확인")
    fill_label = "부분체결" if fill_status == "partial" else "체결"
    return f"{side_text} {fill_label}"


def _format_fill_value(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-12:
        return f"{int(rounded):,}"
    return f"{value:,.12f}".rstrip("0").rstrip(".")


def _resolve_fill_agent_market(market_type: str | None) -> str | None:
    if market_type is None:
        return None
    return OPENCLAW_FILL_MARKET_LABELS.get(market_type)


def _resolve_fill_analysis_thread_id(market_type: str | None) -> str | None:
    if market_type == "kr":
        return _normalize_optional_text(settings.OPENCLAW_THREAD_KR)
    if market_type == "us":
        return _normalize_optional_text(settings.OPENCLAW_THREAD_US)
    if market_type == "crypto":
        return _normalize_optional_text(settings.OPENCLAW_THREAD_CRYPTO)
    return None


def _build_fill_agent_message(
    normalized_order: FillOrderLike,
    *,
    agent_market: str,
) -> str:
    order = coerce_fill_order(normalized_order)
    return (
        "다음 체결 내역을 분석하고 Discord 판단 스레드에 전달할 메시지를 작성하세요.\n\n"
        f"종목: {order.symbol}\n"
        f"구분: {_format_fill_side_text(order.side, order.fill_status)}\n"
        f"수량: {_format_fill_value(order.filled_qty)}\n"
        f"체결가: {_format_fill_value(order.filled_price)}\n"
        f"금액: {_format_fill_value(order.filled_amount)}\n"
        f"시간: {order.filled_at}\n"
        f"계좌: {order.account}\n"
        f"마켓: {agent_market}\n\n"
        "출력 규칙:\n"
        f"{OPENCLAW_FILL_AGENT_INSTRUCTIONS}"
    )


class OpenClawClient:
    """Client for OpenClaw Gateway webhook (POST /hooks/agent)."""

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self._webhook_url: str = webhook_url or settings.OPENCLAW_WEBHOOK_URL
        self._token: str = token if token is not None else settings.OPENCLAW_TOKEN
        self._callback_url: str = settings.OPENCLAW_CALLBACK_URL

    async def _forward_to_telegram(
        self,
        message: str,
        alert_type: str,
        *,
        correlation_id: str | None = None,
        market_type: str | None = None,
    ) -> None:
        try:
            notifier = get_trade_notifier()
            notifier_kwargs: dict[str, str] = {}
            if correlation_id is not None:
                notifier_kwargs["correlation_id"] = correlation_id
            if market_type is not None:
                notifier_kwargs["market_type"] = market_type

            if not notifier_kwargs:
                sent = await notifier.notify_openclaw_message(message)
            else:
                sent = await notifier.notify_openclaw_message(
                    message, **notifier_kwargs
                )
            if sent:
                logger.debug(
                    "OpenClaw %s alert mirror invoked: correlation_id=%s result=success",
                    alert_type,
                    correlation_id,
                )
            else:
                logger.debug(
                    "OpenClaw %s alert mirror invoked: correlation_id=%s result=failed",
                    alert_type,
                    correlation_id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to invoke OpenClaw %s alert mirror: correlation_id=%s error=%s",
                alert_type,
                correlation_id,
                exc,
            )

    async def request_analysis(
        self,
        prompt: str,
        symbol: str,
        name: str,
        instrument_type: str,
        callback_url: str | None = None,
        include_model_name: bool = True,
        request_id: str | None = None,
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

        request_id = request_id or str(uuid4())

        message = _build_openclaw_message(
            request_id=request_id,
            prompt=prompt,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            callback_url=callback_url or self._callback_url,
            callback_token=settings.OPENCLAW_CALLBACK_TOKEN,
            include_model_name=include_model_name,
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
            _ = res.raise_for_status()

        logger.info(
            "OpenClaw analysis requested: request_id=%s symbol=%s instrument_type=%s status=%s",
            request_id,
            symbol,
            instrument_type,
            res.status_code,
        )
        return request_id

    async def send_fill_notification(
        self, order: FillOrderLike, *, correlation_id: str | None = None
    ) -> FillNotificationDeliveryResult:
        """
        체결 알림을 OpenClaw Gateway로 전송

        Fire-and-forget 텍스트 알림으로 전송하며, 최대 4회 시도합니다
        (초기 1회 + 재시도 3회, 1s -> 2s -> 4s 백오프).
        모든 재시도 실패 시 failed 결과를 반환하고 예외를 삼킵니다.

        Args:
            order: 정규화된 체결 데이터

        Returns:
            FillNotificationDeliveryResult: 성공/스킵/실패 결과
        """
        normalized_order = coerce_fill_order(order)
        request_id = str(uuid4())
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        result = FillNotificationDeliveryResult(
            status="failed",
            reason="request_failed",
        )
        discord_fill_message = format_fill_message(normalized_order)
        try:
            agent_market = _resolve_fill_agent_market(normalized_order.market_type)
            analysis_thread_id = _resolve_fill_analysis_thread_id(
                normalized_order.market_type
            )

            if not settings.OPENCLAW_ENABLED:
                logger.debug(
                    "OpenClaw fill agent skipped: correlation_id=%s symbol=%s account=%s reason=openclaw_disabled",
                    correlation_id,
                    normalized_order.symbol,
                    normalized_order.account,
                )
                result = FillNotificationDeliveryResult(
                    status="skipped",
                    reason="openclaw_disabled",
                )
            elif agent_market is None:
                logger.debug(
                    "OpenClaw fill agent skipped: correlation_id=%s symbol=%s account=%s reason=unsupported_market market_type=%s",
                    correlation_id,
                    normalized_order.symbol,
                    normalized_order.account,
                    normalized_order.market_type,
                )
                result = FillNotificationDeliveryResult(
                    status="skipped",
                    reason="unsupported_market",
                )
            elif analysis_thread_id is None:
                logger.debug(
                    "OpenClaw fill agent skipped: correlation_id=%s symbol=%s account=%s reason=missing_analysis_thread market_type=%s",
                    correlation_id,
                    normalized_order.symbol,
                    normalized_order.account,
                    normalized_order.market_type,
                )
                result = FillNotificationDeliveryResult(
                    status="skipped",
                    reason="missing_analysis_thread",
                )
            else:
                payload = {
                    "message": _build_fill_agent_message(
                        normalized_order,
                        agent_market=agent_market,
                    ),
                    "name": OPENCLAW_FILL_AGENT_NAME,
                    "deliver": True,
                    "channel": OPENCLAW_FILL_AGENT_CHANNEL,
                    "to": analysis_thread_id,
                    "model": OPENCLAW_FILL_AGENT_MODEL,
                    "timeoutSeconds": OPENCLAW_FILL_AGENT_TIMEOUT_SECONDS,
                }
                async for attempt in _build_openclaw_retrying():
                    attempt_number = attempt.retry_state.attempt_number
                    with attempt:
                        logger.info(
                            "OpenClaw fill notification send start: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s thread_id=%s",
                            correlation_id,
                            request_id,
                            normalized_order.symbol,
                            normalized_order.account,
                            attempt_number,
                            analysis_thread_id,
                        )
                        try:
                            async with httpx.AsyncClient(timeout=10) as cli:
                                res = await cli.post(
                                    self._webhook_url,
                                    json=payload,
                                    headers=headers,
                                )
                                _ = res.raise_for_status()
                        except Exception as exc:
                            logger.warning(
                                "OpenClaw fill notification attempt failed: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s error=%s",
                                correlation_id,
                                request_id,
                                normalized_order.symbol,
                                normalized_order.account,
                                attempt_number,
                                exc,
                            )
                            raise
                        logger.info(
                            "OpenClaw fill notification sent: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s status=%s",
                            correlation_id,
                            request_id,
                            normalized_order.symbol,
                            normalized_order.account,
                            attempt_number,
                            res.status_code,
                        )
                        result = FillNotificationDeliveryResult(
                            status="success",
                            request_id=request_id,
                        )
                        break

        except RetryError as e:
            logger.error(
                "OpenClaw fill notification failed after retries: correlation_id=%s request_id=%s symbol=%s account=%s error=%s",
                correlation_id,
                request_id,
                normalized_order.symbol,
                normalized_order.account,
                e,
            )
            result = FillNotificationDeliveryResult(
                status="failed",
                reason="request_failed",
            )
        except Exception as e:
            logger.error(
                "OpenClaw fill notification error: correlation_id=%s request_id=%s symbol=%s account=%s error=%s",
                correlation_id,
                request_id,
                normalized_order.symbol,
                normalized_order.account,
                e,
            )
            result = FillNotificationDeliveryResult(
                status="failed",
                reason="request_failed",
            )
        finally:
            await self._forward_to_telegram(
                discord_fill_message,
                alert_type="fill",
                correlation_id=correlation_id,
                market_type=normalized_order.market_type,
            )

        return result

    async def _send_market_alert(
        self,
        message: str,
        category: str,
        *,
        mirror_to_telegram: bool = True,
    ) -> str | None:
        if not settings.OPENCLAW_ENABLED:
            logger.debug("OpenClaw disabled, skipping %s alert", category)
            return None

        request_id = str(uuid4())
        payload = {
            "message": message,
            "name": f"auto-trader:{category}",
            "sessionKey": f"auto-trader:{category}:{request_id}",
            "wakeMode": "now",
        }

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        delivered_to_openclaw = False
        try:
            async for attempt in _build_openclaw_retrying():
                with attempt:
                    async with httpx.AsyncClient(timeout=10) as cli:
                        res = await cli.post(
                            self._webhook_url, json=payload, headers=headers
                        )
                        _ = res.raise_for_status()
                    logger.info(
                        "OpenClaw %s alert sent: request_id=%s",
                        category,
                        request_id,
                    )
                    delivered_to_openclaw = True
                    break

        except RetryError as e:
            logger.error(
                "OpenClaw %s alert failed after retries: request_id=%s error=%s",
                category,
                request_id,
                e,
            )
        except Exception as e:
            logger.error(
                "OpenClaw %s alert error: request_id=%s error=%s",
                category,
                request_id,
                e,
            )
        finally:
            if mirror_to_telegram:
                await self._forward_to_telegram(message, alert_type=category)

        if delivered_to_openclaw:
            return request_id
        return None

    async def send_scan_alert(
        self,
        message: str,
        *,
        mirror_to_telegram: bool = True,
    ) -> str | None:
        return await self._send_market_alert(
            message,
            category="scan",
            mirror_to_telegram=mirror_to_telegram,
        )

    async def send_watch_alert(self, message: str) -> str | None:
        return await self._send_market_alert(message, category="watch")


def _build_openclaw_message(
    *,
    request_id: str,
    prompt: str,
    symbol: str,
    name: str,
    instrument_type: str,
    callback_url: str,
    callback_token: str | None,
    include_model_name: bool = True,
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
    }
    if include_model_name:
        callback_schema["model_name"] = "..."

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
