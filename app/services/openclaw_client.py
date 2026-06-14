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
from app.monitoring.trade_notifier import get_trade_notifier

logger = logging.getLogger(__name__)

OPENCLAW_RETRY_STOP = stop_after_attempt(4)
OPENCLAW_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=4)

WatchAlertDeliveryStatus = Literal["success", "skipped", "failed"]


@dataclass(slots=True, frozen=True)
class WatchAlertDeliveryResult:
    status: WatchAlertDeliveryStatus
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


def _resolve_watch_alert_url() -> str:
    """Resolve the watch-alert router URL.

    WATCH_ALERT_ROUTER_URL wins when set; otherwise falls through to the
    deprecated N8N_WATCH_ALERT_WEBHOOK_URL for backward compatibility.
    """
    router = settings.WATCH_ALERT_ROUTER_URL.strip()
    if router:
        return router
    return settings.N8N_WATCH_ALERT_WEBHOOK_URL.strip()


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



    async def send_watch_alert_to_router(
        self,
        *,
        message: str,
        market: str,
        triggered: list[dict[str, Any]],
        as_of: str,
        correlation_id: str | None = None,
        intents: list[dict[str, Any]] | None = None,
    ) -> WatchAlertDeliveryResult:
        request_id = str(uuid4())
        router_url = _resolve_watch_alert_url()

        if not router_url:
            logger.debug(
                "Watch alert router skipped: correlation_id=%s market=%s reason=router_not_configured",
                correlation_id,
                market,
            )
            return WatchAlertDeliveryResult(
                status="skipped",
                reason="router_not_configured",
            )

        payload = {
            "alert_type": "watch",
            "correlation_id": correlation_id,
            "as_of": as_of,
            "market": market,
            "triggered": triggered,
            "message": message,
            "intents": intents or [],
        }
        headers = {"Content-Type": "application/json"}

        try:
            async for attempt in _build_openclaw_retrying():
                attempt_number = attempt.retry_state.attempt_number
                with attempt:
                    logger.info(
                        "Watch alert router send start: correlation_id=%s request_id=%s market=%s attempt=%s",
                        correlation_id,
                        request_id,
                        market,
                        attempt_number,
                    )
                    try:
                        async with httpx.AsyncClient(timeout=10) as cli:
                            res = await cli.post(
                                router_url,
                                json=payload,
                                headers=headers,
                            )
                            _ = res.raise_for_status()
                    except Exception as exc:
                        logger.warning(
                            "Watch alert router attempt failed: correlation_id=%s request_id=%s market=%s attempt=%s error=%s",
                            correlation_id,
                            request_id,
                            market,
                            attempt_number,
                            exc,
                        )
                        raise
                    logger.info(
                        "Watch alert router sent: correlation_id=%s request_id=%s market=%s attempt=%s status=%s",
                        correlation_id,
                        request_id,
                        market,
                        attempt_number,
                        res.status_code,
                    )
                    return WatchAlertDeliveryResult(
                        status="success",
                        request_id=request_id,
                    )
        except RetryError as exc:
            logger.error(
                "Watch alert router failed after retries: correlation_id=%s request_id=%s market=%s error=%s",
                correlation_id,
                request_id,
                market,
                exc,
            )
        except Exception as exc:
            logger.error(
                "Watch alert router error: correlation_id=%s request_id=%s market=%s error=%s",
                correlation_id,
                request_id,
                market,
                exc,
            )

        return WatchAlertDeliveryResult(status="failed", reason="request_failed")

    async def _send_market_alert(
        self,
        message: str,
        category: str,
        *,
        mirror_to_telegram: bool = True,
    ) -> str | None:
        # Deprecated: watch category is replaced by send_watch_alert_to_router (ROB-171)
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
        # Deprecated: replaced by send_watch_alert_to_router (ROB-171)
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
