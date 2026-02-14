import json
import logging
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings

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

    async def send_execution_notification(self, event: dict[str, Any]) -> bool:
        """Send a single execution notification message to OpenClaw Gateway."""
        if not settings.OPENCLAW_ENABLED:
            logger.debug("OpenClaw execution notification skipped: disabled")
            return False

        if not self._webhook_url:
            logger.warning("OpenClaw execution notification skipped: webhook URL missing")
            return False

        market = str(event.get("market") or "unknown")
        symbol = str(event.get("symbol") or "unknown")

        payload = {
            "message": self._build_execution_message(event),
            "name": "auto-trader:execution",
            "sessionKey": f"auto-trader:execution:{market}:{symbol}",
            "wakeMode": "now",
        }

        headers = {"Content-Type": "application/json"}
        token = self._token.strip() if self._token else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                res = await cli.post(self._webhook_url, json=payload, headers=headers)
                res.raise_for_status()

            logger.info(
                "OpenClaw execution notification sent: symbol=%s market=%s status=%s",
                symbol,
                market,
                res.status_code,
            )
            return True
        except Exception as e:
            logger.warning(
                "OpenClaw execution notification failed: symbol=%s market=%s error=%s",
                symbol,
                market,
                e,
            )
            return False

    def _build_execution_message(self, event: dict[str, Any]) -> str:
        """Build human-readable execution message with safe fallbacks."""
        symbol = str(event.get("symbol") or "UNKNOWN")
        name = str(event.get("name") or symbol)
        market = str(event.get("market") or "unknown").lower()
        side = str(event.get("side") or "").lower()
        side_text = {"buy": "매수", "sell": "매도"}.get(side, "체결")

        filled_price = self._to_float(event.get("filled_price"))
        filled_qty = self._to_float(event.get("filled_qty"))
        order_id = str(event.get("order_id") or "").strip()
        timestamp = str(event.get("timestamp") or event.get("exec_time") or "").strip()

        lines = [f"🔔 {name}({symbol}) {side_text}"]

        detail_parts: list[str] = []
        if filled_price is not None and filled_qty is not None:
            detail_parts.append(
                f"{self._format_quantity(filled_qty, market)} × "
                f"{self._format_price(filled_price, market)} = "
                f"{self._format_amount(filled_price * filled_qty, market)}"
            )
        elif filled_price is not None:
            detail_parts.append(f"가격 {self._format_price(filled_price, market)}")
        elif filled_qty is not None:
            detail_parts.append(f"수량 {self._format_quantity(filled_qty, market)}")

        if order_id:
            detail_parts.append(f"주문번호 {order_id}")
        if timestamp:
            detail_parts.append(f"시각 {timestamp}")

        if detail_parts:
            lines.append(" | ".join(detail_parts))
        else:
            lines.append("상세 체결 정보가 부족하여 종목 기준으로 알림합니다.")

        next_step = event.get("dca_next_step")
        if isinstance(next_step, dict):
            step_number = next_step.get("step_number")
            target_price = self._to_float(next_step.get("target_price"))
            target_qty = next_step.get("target_quantity", next_step.get("quantity"))

            step_label = (
                f"{step_number}차" if step_number is not None else "다음 단계"
            )
            price_text = (
                self._format_price(target_price, market)
                if target_price is not None
                else "가격 미정"
            )
            qty_text = (
                self._format_quantity(target_qty, market)
                if target_qty not in (None, "")
                else "수량 미정"
            )

            lines.append("")
            lines.append(f"📋 DCA {step_label}: {price_text} / {qty_text}")
            lines.append("주문 실행 여부를 확인해 주세요.")

        return "\n".join(lines)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_price(price: float, market: str) -> str:
        if market == "us":
            return f"${price:,.2f}"
        return f"{price:,.0f}원"

    @staticmethod
    def _format_amount(amount: float, market: str) -> str:
        if market == "us":
            return f"${amount:,.2f}"
        return f"{amount:,.0f}원"

    @staticmethod
    def _format_quantity(qty: Any, market: str) -> str:
        try:
            qty_float = float(qty)
            qty_text = f"{qty_float:g}"
        except (TypeError, ValueError):
            qty_text = str(qty)

        unit = "주" if market in {"kr", "us"} else "개"
        return f"{qty_text}{unit}"


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
