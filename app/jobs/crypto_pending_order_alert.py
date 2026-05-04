"""Read-only Upbit KRW crypto pending-order reminder job.

This module intentionally performs no broker mutations.  It only reads open Upbit
orders, enriches them with current ticker prices, and optionally posts a Discord
message when pending orders exist.  Failures are routed to a separate Discord
channel so a silent normal channel still means "no pending orders" rather than
"the check did not run".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.core.config import settings
from app.services.brokers.upbit.client import fetch_multiple_current_prices
from app.services.brokers.upbit.orders import fetch_open_orders

logger = logging.getLogger(__name__)

DEFAULT_POSITION_BASE_URL = "https://trader.robinco.dev/portfolio/positions/crypto"


@dataclass(frozen=True)
class PendingCryptoOrder:
    """Normalized, notification-safe view of an Upbit pending order."""

    exchange: str
    symbol: str
    side: str
    order_id_prefix: str
    ordered_price: Decimal | None
    current_price: Decimal | None
    distance_pct: Decimal | None
    remaining_qty: Decimal
    original_qty: Decimal
    remaining_value_krw: Decimal | None
    ordered_at: str
    age_minutes: int | None
    status: str
    detail_url: str

    def to_jsonable(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = str(value)
        return payload


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _coerce_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid crypto pending-order timezone %r; falling back to Asia/Seoul",
            timezone_name,
        )
        return ZoneInfo("Asia/Seoul")


def _fmt_krw(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value.quantize(Decimal('1')):,.0f}원"


def _fmt_qty(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value.quantize(Decimal('0.01'))}%"


def _side_label(side: str) -> str:
    return {"bid": "매수", "ask": "매도", "buy": "매수", "sell": "매도"}.get(
        side, side or "unknown"
    )


def _safe_order_id_prefix(order_id: Any) -> str:
    order_id_str = str(order_id or "").strip()
    if not order_id_str:
        return "unknown"
    return f"{order_id_str[:8]}..."


def _parse_ordered_at(value: Any) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    if not raw:
        return "N/A", None

    parsed_raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(parsed_raw)
    except ValueError:
        return raw, None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    age_minutes = max(int((now - dt.astimezone(UTC)).total_seconds() // 60), 0)
    return raw, age_minutes


def _normalize_order(
    raw_order: dict[str, Any],
    *,
    current_prices: dict[str, float],
    position_base_url: str,
) -> PendingCryptoOrder:
    symbol = str(raw_order.get("market") or "").strip().upper()
    if not symbol.startswith("KRW-"):
        raise ValueError(f"non-KRW Upbit pending order is out of scope: {symbol}")

    remaining_qty = _to_decimal(raw_order.get("remaining_volume"))
    executed_qty = _to_decimal(raw_order.get("executed_volume")) or Decimal("0")
    ordered_price = _to_decimal(raw_order.get("price"))
    current_price = _to_decimal(current_prices.get(symbol))

    if remaining_qty is None:
        raise ValueError(f"missing remaining_volume for {symbol}")

    original_qty = remaining_qty + executed_qty
    remaining_value_krw: Decimal | None = None
    if ordered_price is not None:
        remaining_value_krw = ordered_price * remaining_qty

    distance_pct: Decimal | None = None
    if ordered_price is not None and current_price and current_price != 0:
        distance_pct = ((ordered_price - current_price) / current_price) * Decimal(
            "100"
        )

    ordered_at, age_minutes = _parse_ordered_at(raw_order.get("created_at"))

    return PendingCryptoOrder(
        exchange="Upbit",
        symbol=symbol,
        side=_side_label(str(raw_order.get("side") or "")),
        order_id_prefix=_safe_order_id_prefix(raw_order.get("uuid")),
        ordered_price=ordered_price,
        current_price=current_price,
        distance_pct=distance_pct,
        remaining_qty=remaining_qty,
        original_qty=original_qty,
        remaining_value_krw=remaining_value_krw,
        ordered_at=ordered_at,
        age_minutes=age_minutes,
        status=str(raw_order.get("state") or "wait"),
        detail_url=f"{position_base_url.rstrip('/')}/{symbol}",
    )


def _format_order_line(order: PendingCryptoOrder) -> str:
    age = "N/A" if order.age_minutes is None else f"{order.age_minutes}분"
    return (
        f"• **{order.symbol} {order.side}** | 주문가 {_fmt_krw(order.ordered_price)} "
        f"/ 현재가 {_fmt_krw(order.current_price)} ({_fmt_pct(order.distance_pct)})\n"
        f"  잔량 {_fmt_qty(order.remaining_qty)} / 원주문 {_fmt_qty(order.original_qty)} "
        f"· 잔여금액 {_fmt_krw(order.remaining_value_krw)} · 경과 {age}\n"
        f"  주문 {order.order_id_prefix} · [상세]({order.detail_url})"
    )


def format_pending_order_message(
    orders: list[PendingCryptoOrder],
    *,
    checked_at: datetime | None = None,
    timezone_name: str = "Asia/Seoul",
) -> str:
    tz = _coerce_timezone(timezone_name)
    checked = (checked_at or datetime.now(UTC)).astimezone(tz)
    total_value = sum((order.remaining_value_krw or Decimal("0")) for order in orders)
    lines = [
        "🔔 **코인 미체결 주문 알림**",
        "",
        f"조회 시각: {checked.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"계좌/거래소: Upbit · 대상: KRW 코인 · 미체결 {len(orders)}건",
        f"추정 잔여 주문금액: {_fmt_krw(total_value)}",
        "",
        *[_format_order_line(order) for order in orders],
        "",
        "ℹ️ read-only 현황 공유입니다. 유지/취소 추천, 자동 취소, 주문 변경은 수행하지 않았습니다.",
    ]
    return "\n".join(lines)


def format_failure_message(
    *,
    reason: str,
    checked_at: datetime | None = None,
    timezone_name: str = "Asia/Seoul",
    details: dict[str, Any] | None = None,
) -> str:
    tz = _coerce_timezone(timezone_name)
    checked = (checked_at or datetime.now(UTC)).astimezone(tz)
    lines = [
        "⚠️ **코인 미체결 주문 알림 실패**",
        "",
        f"시간: {checked.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"사유: {reason}",
        "",
        "정상 채널 무알림은 '미체결 없음'이어야 하므로, 실패/부분 실패는 이 채널로 별도 보고합니다.",
    ]
    if details:
        safe_details = json.dumps(details, ensure_ascii=False, default=str)[:1200]
        lines.extend(["", f"```json\n{safe_details}\n```"])
    return "\n".join(lines)


def _resolve_discord_bot_token(explicit_token: str | None) -> str:
    return (
        str(explicit_token or "").strip()
        or str(settings.CRYPTO_PENDING_ORDER_DISCORD_BOT_TOKEN or "").strip()
        or str(os.getenv("DISCORD_TC_BRIEFING_BOT_TOKEN") or "").strip()
        or str(os.getenv("N8N_DISCORD_BOT_TOKEN_ALFRED") or "").strip()
    )


async def send_discord_channel_message(
    *,
    bot_token: str,
    channel_id: str,
    content: str,
    timeout_seconds: float = 10.0,
) -> bool:
    """Send a plain Discord channel message using Bot auth."""

    bot_token = str(bot_token or "").strip()
    channel_id = str(channel_id or "").strip()
    if not bot_token or not channel_id:
        return False

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    payload = {"content": content[:1900]}
    async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
    return True


async def collect_pending_crypto_orders(
    *,
    position_base_url: str = DEFAULT_POSITION_BASE_URL,
) -> list[PendingCryptoOrder]:
    """Fetch and normalize Upbit KRW pending orders without side effects."""

    raw_orders = await fetch_open_orders(market=None)
    krw_orders = [
        order
        for order in raw_orders
        if str(order.get("market") or "").upper().startswith("KRW-")
    ]
    if not krw_orders:
        return []

    symbols = sorted({str(order.get("market") or "").upper() for order in krw_orders})
    current_prices = await fetch_multiple_current_prices(symbols, use_cache=False)
    missing_prices = [symbol for symbol in symbols if symbol not in current_prices]
    if missing_prices:
        raise RuntimeError(
            "partial price lookup failure for symbols: " + ", ".join(missing_prices)
        )

    return [
        _normalize_order(
            order,
            current_prices=current_prices,
            position_base_url=position_base_url,
        )
        for order in krw_orders
    ]


async def run_crypto_pending_order_alert(
    *,
    execute: bool = False,
    enabled: bool | None = None,
    bot_token: str | None = None,
    alert_channel_id: str | None = None,
    failure_channel_id: str | None = None,
    timezone_name: str | None = None,
    position_base_url: str = DEFAULT_POSITION_BASE_URL,
) -> dict[str, Any]:
    """Run one read-only pending-order alert cycle.

    Args:
        execute: When False, never sends Discord messages and returns a dry-run
            preview. When True, sends only if enabled and a message is required.
        enabled: Explicit feature gate override. Defaults to settings.
    """

    effective_enabled = (
        settings.CRYPTO_PENDING_ORDER_ALERT_ENABLED if enabled is None else enabled
    )
    effective_bot_token = _resolve_discord_bot_token(bot_token)
    effective_alert_channel = (
        alert_channel_id or settings.CRYPTO_PENDING_ORDER_ALERT_CHANNEL_ID
    )
    effective_failure_channel = (
        failure_channel_id or settings.CRYPTO_PENDING_ORDER_FAILURE_CHANNEL_ID
    )
    effective_timezone = timezone_name or settings.CRYPTO_PENDING_ORDER_TIMEZONE
    checked_at = datetime.now(UTC)

    if execute and not effective_enabled:
        return {
            "success": True,
            "status": "disabled",
            "sent": False,
            "orders_count": 0,
            "message": "CRYPTO_PENDING_ORDER_ALERT_ENABLED is false",
        }

    try:
        orders = await collect_pending_crypto_orders(
            position_base_url=position_base_url
        )
    except Exception as exc:
        logger.exception("Crypto pending-order alert collection failed")
        failure_message = format_failure_message(
            reason=str(exc),
            checked_at=checked_at,
            timezone_name=effective_timezone,
        )
        failure_sent = False
        if execute:
            try:
                failure_sent = await send_discord_channel_message(
                    bot_token=effective_bot_token,
                    channel_id=effective_failure_channel,
                    content=failure_message,
                )
            except Exception:
                logger.exception("Crypto pending-order failure notification failed")
        return {
            "success": False,
            "status": "failed",
            "sent": False,
            "failure_sent": failure_sent,
            "orders_count": 0,
            "error": str(exc),
        }

    if not orders:
        return {
            "success": True,
            "status": "no_orders",
            "sent": False,
            "orders_count": 0,
        }

    normal_message = format_pending_order_message(
        orders,
        checked_at=checked_at,
        timezone_name=effective_timezone,
    )
    if not execute:
        return {
            "success": True,
            "status": "dry_run_orders_found",
            "sent": False,
            "orders_count": len(orders),
            "target_channel_id": effective_alert_channel,
            "message_preview": normal_message,
            "orders": [order.to_jsonable() for order in orders],
        }

    try:
        sent = await send_discord_channel_message(
            bot_token=effective_bot_token,
            channel_id=effective_alert_channel,
            content=normal_message,
        )
        if not sent:
            raise RuntimeError(
                "normal Discord notification was not delivered; "
                "missing bot token/channel config or transport returned false"
            )
    except Exception as exc:
        logger.exception("Crypto pending-order normal notification failed")
        failure_message = format_failure_message(
            reason="normal Discord notification failed",
            checked_at=checked_at,
            timezone_name=effective_timezone,
            details={"error": str(exc), "orders_count": len(orders)},
        )
        failure_sent = False
        try:
            failure_sent = await send_discord_channel_message(
                bot_token=effective_bot_token,
                channel_id=effective_failure_channel,
                content=failure_message,
            )
        except Exception:
            logger.exception("Crypto pending-order failure notification failed")
        return {
            "success": False,
            "status": "notification_failed",
            "sent": False,
            "failure_sent": failure_sent,
            "orders_count": len(orders),
            "error": str(exc),
        }

    return {
        "success": True,
        "status": "orders_found",
        "sent": True,
        "orders_count": len(orders),
        "target_channel_id": effective_alert_channel,
        "orders": [order.to_jsonable() for order in orders],
    }


__all__ = [
    "PendingCryptoOrder",
    "collect_pending_crypto_orders",
    "format_failure_message",
    "format_pending_order_message",
    "run_crypto_pending_order_alert",
    "send_discord_channel_message",
]
