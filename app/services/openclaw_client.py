import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.kr_symbols import KR_SYMBOLS
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.fill_notification import (
    FillOrder,
    FillOrderLike,
    coerce_fill_order,
    format_fill_message,
)

logger = logging.getLogger(__name__)

OPENCLAW_RETRY_STOP = stop_after_attempt(4)
OPENCLAW_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=4)

# Module-level cache for KR_SYMBOLS reverse mapping
_KR_SYMBOLS_REVERSE: dict[str, str] | None = None

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


def _get_kr_symbol_reverse() -> dict[str, str]:
    """Get cached reverse mapping of KR_SYMBOLS (code -> name)."""
    global _KR_SYMBOLS_REVERSE
    if _KR_SYMBOLS_REVERSE is None:
        _KR_SYMBOLS_REVERSE = {v: k for k, v in KR_SYMBOLS.items()}
    return _KR_SYMBOLS_REVERSE


def _resolve_fill_display_name(order: FillOrder) -> str:
    """Resolve display name based on market type and symbol.

    - KR: KR_SYMBOLS 역매핑으로 조회, 미존재 시 심볼 코드 그대로
    - US: 심볼 그대로
    - Crypto: KRW-BTC -> BTC, USDT-BTC -> BTC
    """
    if order.market_type == "kr":
        reverse_map = _get_kr_symbol_reverse()
        return reverse_map.get(order.symbol, order.symbol)

    if order.market_type == "us":
        return order.symbol

    if order.market_type == "crypto":
        # KRW-BTC -> BTC, USDT-BTC -> BTC
        if "-" in order.symbol:
            return order.symbol.split("-")[-1]
        return order.symbol

    return order.symbol


def _build_n8n_fill_payload(
    order: FillOrder, *, correlation_id: str | None = None
) -> dict[str, Any]:
    """Build n8n webhook payload for fill notification."""
    return {
        "symbol": order.symbol,
        "display_name": _resolve_fill_display_name(order),
        "side": order.side,
        "filled_price": order.filled_price,
        "filled_qty": order.filled_qty,
        "filled_amount": order.filled_amount,
        "filled_at": order.filled_at,
        "account": order.account,
        "market_type": order.market_type,
        "order_price": order.order_price,
        "fill_status": order.fill_status or "filled",
        "currency": order.currency or ("KRW" if order.market_type == "kr" else "USD"),
        "correlation_id": correlation_id,
    }


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
        skip_discord: bool = False,
    ) -> None:
        try:
            notifier = get_trade_notifier()
            notifier_kwargs: dict[str, Any] = {}
            if correlation_id is not None:
                notifier_kwargs["correlation_id"] = correlation_id
            if market_type is not None:
                notifier_kwargs["market_type"] = market_type
            if skip_discord:
                notifier_kwargs["skip_discord"] = True

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
        체결 알림을 N8N webhook으로 전송

        Fire-and-forget JSON payload로 전송하며, 최대 4회 시도합니다
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

        result = FillNotificationDeliveryResult(
            status="failed",
            reason="request_failed",
        )
        discord_fill_message = format_fill_message(normalized_order)

        try:
            # Skip if n8n webhook not configured
            n8n_webhook_url = settings.N8N_FILL_WEBHOOK_URL.strip()
            if not n8n_webhook_url:
                logger.debug(
                    "N8N fill notification skipped: correlation_id=%s symbol=%s account=%s reason=n8n_webhook_not_configured",
                    correlation_id,
                    normalized_order.symbol,
                    normalized_order.account,
                )
                result = FillNotificationDeliveryResult(
                    status="skipped",
                    reason="n8n_webhook_not_configured",
                )
            # Skip if below minimum amount (50,000)
            elif normalized_order.filled_amount < 50_000:
                logger.debug(
                    "N8N fill notification skipped: correlation_id=%s symbol=%s account=%s reason=below_minimum_notify_amount amount=%s",
                    correlation_id,
                    normalized_order.symbol,
                    normalized_order.account,
                    normalized_order.filled_amount,
                )
                result = FillNotificationDeliveryResult(
                    status="skipped",
                    reason="below_minimum_notify_amount",
                )
            else:
                payload = _build_n8n_fill_payload(
                    normalized_order, correlation_id=correlation_id
                )
                async for attempt in _build_openclaw_retrying():
                    attempt_number = attempt.retry_state.attempt_number
                    with attempt:
                        logger.info(
                            "N8N fill notification send start: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s",
                            correlation_id,
                            request_id,
                            normalized_order.symbol,
                            normalized_order.account,
                            attempt_number,
                        )
                        try:
                            async with httpx.AsyncClient(timeout=10) as cli:
                                res = await cli.post(
                                    n8n_webhook_url,
                                    json=payload,
                                    headers=headers,
                                )
                                _ = res.raise_for_status()
                        except Exception as exc:
                            logger.warning(
                                "N8N fill notification attempt failed: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s error=%s",
                                correlation_id,
                                request_id,
                                normalized_order.symbol,
                                normalized_order.account,
                                attempt_number,
                                exc,
                            )
                            raise
                        logger.info(
                            "N8N fill notification sent: correlation_id=%s request_id=%s symbol=%s account=%s attempt=%s status=%s",
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
                "N8N fill notification failed after retries: correlation_id=%s request_id=%s symbol=%s account=%s error=%s",
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
                "N8N fill notification error: correlation_id=%s request_id=%s symbol=%s account=%s error=%s",
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
            if result.reason != "below_minimum_notify_amount":
                await self._forward_to_telegram(
                    discord_fill_message,
                    alert_type="fill",
                    correlation_id=correlation_id,
                    market_type=normalized_order.market_type,
                    skip_discord=True,
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
