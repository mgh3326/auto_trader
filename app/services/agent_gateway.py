import json
import logging
from typing import Any
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

AGENT_GATEWAY_RETRY_STOP = stop_after_attempt(4)
AGENT_GATEWAY_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=4)


def _build_agent_retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=AGENT_GATEWAY_RETRY_STOP,
        wait=AGENT_GATEWAY_RETRY_WAIT,
        reraise=False,
    )


class AgentGatewayClient:
    """External AI agent gateway (formerly OpenClaw) webhook (POST /hooks/agent)."""

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self._webhook_url: str = webhook_url or settings.AGENT_GATEWAY_URL
        self._token: str = token if token is not None else settings.AGENT_GATEWAY_TOKEN
        self._callback_url: str = settings.AGENT_GATEWAY_CALLBACK_URL

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
                sent = await notifier.notify_agent_message(message)
            else:
                sent = await notifier.notify_agent_message(message, **notifier_kwargs)
            if sent:
                logger.debug(
                    "Agent gateway %s alert mirror invoked: correlation_id=%s result=success",
                    alert_type,
                    correlation_id,
                )
            else:
                logger.debug(
                    "Agent gateway %s alert mirror invoked: correlation_id=%s result=failed",
                    alert_type,
                    correlation_id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to invoke Agent gateway %s alert mirror: correlation_id=%s error=%s",
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
        """Send an analysis request to the agent gateway.

        Returns
        -------
        str
            request_id to correlate the callback payload.
        """
        if not settings.AGENT_GATEWAY_ENABLED:
            raise RuntimeError(
                "Agent gateway integration is disabled (AGENT_GATEWAY_ENABLED=false)"
            )

        request_id = request_id or str(uuid4())

        message = _build_agent_message(
            request_id=request_id,
            prompt=prompt,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            callback_url=callback_url or self._callback_url,
            callback_token=settings.AGENT_GATEWAY_CALLBACK_TOKEN,
            include_model_name=include_model_name,
        )

        payload = {
            "message": message,
            "name": "auto-trader:analysis",
            "sessionKey": f"auto-trader:agent:{request_id}",
            "wakeMode": "now",
        }

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        async with httpx.AsyncClient(timeout=10) as cli:
            res = await cli.post(self._webhook_url, json=payload, headers=headers)
            _ = res.raise_for_status()

        logger.info(
            "Agent gateway analysis requested: request_id=%s symbol=%s instrument_type=%s status=%s",
            request_id,
            symbol,
            instrument_type,
            res.status_code,
        )
        return request_id

    async def _send_market_alert(
        self,
        message: str,
        category: str,
        *,
        mirror_to_telegram: bool = True,
    ) -> str | None:
        if not settings.AGENT_GATEWAY_ENABLED:
            logger.debug("Agent gateway disabled, skipping %s alert", category)
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

        delivered_to_agent = False
        try:
            async for attempt in _build_agent_retrying():
                with attempt:
                    async with httpx.AsyncClient(timeout=10) as cli:
                        res = await cli.post(
                            self._webhook_url, json=payload, headers=headers
                        )
                        _ = res.raise_for_status()
                    logger.info(
                        "Agent gateway %s alert sent: request_id=%s",
                        category,
                        request_id,
                    )
                    delivered_to_agent = True
                    break

        except RetryError as e:
            logger.error(
                "Agent gateway %s alert failed after retries: request_id=%s error=%s",
                category,
                request_id,
                e,
            )
        except Exception as e:
            logger.error(
                "Agent gateway %s alert error: request_id=%s error=%s",
                category,
                request_id,
                e,
            )
        finally:
            if mirror_to_telegram:
                await self._forward_to_telegram(message, alert_type=category)

        if delivered_to_agent:
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


def _build_agent_message(
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
