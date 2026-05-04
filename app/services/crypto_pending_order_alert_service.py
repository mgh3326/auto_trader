"""Read-only crypto pending-order reminder service (ROB-99)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.mcp_server.tooling.orders_history import get_order_history_impl
from app.monitoring.trade_notifier.transports import send_discord_content_single
from app.services.brokers.upbit.client import fetch_multiple_current_prices

logger = logging.getLogger(__name__)

NORMAL_CHANNEL_ID = "1500719153508515870"
FAILURE_CHANNEL_ID = "1500722535678083102"
SERVICE_NAME = "crypto_pending_order_alert"
READ_ONLY_NOTICE = "읽기 전용 미체결 주문 리마인더입니다. 보유/취소 추천이 아닙니다."
MAX_DISCORD_CONTENT_CHARS = 1900
MAX_LISTED_ORDERS = 12

OrderLookup = Callable[[], Awaitable[dict[str, Any]]]
PriceLookup = Callable[[list[str]], Awaitable[dict[str, float]]]
DiscordSender = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class PendingCryptoOrder:
    exchange: str
    account: str
    symbol: str
    side: str
    status: str
    order_price: float | None
    current_price: float | None
    gap_pct: float | None
    remaining_qty: float | None
    ordered_qty: float | None
    estimated_remaining_krw: float | None
    ordered_at: str | None
    age: str | None
    order_ref: str


@dataclass(frozen=True)
class CryptoPendingOrderAlertConfig:
    enabled: bool
    normal_channel_id: str
    failure_channel_id: str
    normal_webhook_url: str | None
    failure_webhook_url: str | None
    trader_base_url: str

    @classmethod
    def from_settings(
        cls, settings_obj: Any = settings
    ) -> CryptoPendingOrderAlertConfig:
        return cls(
            enabled=bool(
                getattr(settings_obj, "crypto_pending_order_alert_enabled", False)
            ),
            normal_channel_id=str(
                getattr(
                    settings_obj,
                    "crypto_pending_order_alert_channel_id",
                    NORMAL_CHANNEL_ID,
                )
            ),
            failure_channel_id=str(
                getattr(
                    settings_obj,
                    "crypto_pending_order_failure_channel_id",
                    FAILURE_CHANNEL_ID,
                )
            ),
            normal_webhook_url=getattr(
                settings_obj, "crypto_pending_order_alert_webhook_url", None
            ),
            failure_webhook_url=getattr(
                settings_obj, "crypto_pending_order_failure_webhook_url", None
            ),
            trader_base_url=str(getattr(settings_obj, "trader_base_url", "") or ""),
        )


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"₩{value:,.0f}"


def _fmt_qty(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _short_ref(order_id: Any) -> str:
    raw = str(order_id or "").strip()
    if not raw:
        return "n/a"
    return raw[:8]


def _parse_ordered_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _human_age(ordered_at: str | None, *, now: datetime) -> str | None:
    created = _parse_ordered_at(ordered_at)
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    delta = now.astimezone(UTC) - created.astimezone(UTC)
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem_minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _position_url(config: CryptoPendingOrderAlertConfig, symbol: str) -> str | None:
    base = config.trader_base_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/portfolio?market=crypto&symbol={symbol}"


def normalize_pending_orders(
    orders: list[dict[str, Any]],
    prices: dict[str, float],
    *,
    now: datetime | None = None,
) -> list[PendingCryptoOrder]:
    now = now or datetime.now(UTC)
    normalized: list[PendingCryptoOrder] = []
    for order in orders:
        symbol = str(order.get("symbol") or "").upper()
        current_price = prices.get(symbol)
        order_price = _safe_float(order.get("ordered_price"))
        remaining_qty = _safe_float(order.get("remaining_qty"))
        ordered_qty = _safe_float(order.get("ordered_qty"))
        gap_pct = None
        if order_price is not None and current_price:
            gap_pct = ((order_price - current_price) / current_price) * 100
        estimated_remaining_krw = None
        if remaining_qty is not None and order_price is not None:
            estimated_remaining_krw = remaining_qty * order_price
        ordered_at = str(order.get("ordered_at") or "") or None
        normalized.append(
            PendingCryptoOrder(
                exchange="Upbit",
                account="default",
                symbol=symbol,
                side=str(order.get("side") or "unknown"),
                status=str(order.get("status") or "unknown"),
                order_price=order_price,
                current_price=current_price,
                gap_pct=gap_pct,
                remaining_qty=remaining_qty,
                ordered_qty=ordered_qty,
                estimated_remaining_krw=estimated_remaining_krw,
                ordered_at=ordered_at,
                age=_human_age(ordered_at, now=now),
                order_ref=_short_ref(order.get("order_id")),
            )
        )
    return normalized


def format_pending_order_message(
    orders: list[PendingCryptoOrder],
    *,
    config: CryptoPendingOrderAlertConfig,
    run_ts: datetime | None = None,
) -> str:
    run_ts = run_ts or datetime.now(UTC)
    lines = [
        f"🔔 **Crypto pending orders: {len(orders)} open**",
        READ_ONLY_NOTICE,
        f"Channel: <#{config.normal_channel_id}>",
        f"Run: {run_ts.isoformat()}",
        "",
    ]
    omitted_count = max(len(orders) - MAX_LISTED_ORDERS, 0)
    for idx, order in enumerate(orders[:MAX_LISTED_ORDERS], 1):
        link = _position_url(config, order.symbol)
        title = f"{idx}. `{order.symbol}` {order.side.upper()} ({order.status})"
        if link:
            title += f" — {link}"
        lines.extend(
            [
                title,
                f"   • order/current/gap: {_fmt_money(order.order_price)} / {_fmt_money(order.current_price)} / {_fmt_pct(order.gap_pct)}",
                f"   • remaining/original: {_fmt_qty(order.remaining_qty)} / {_fmt_qty(order.ordered_qty)}",
                f"   • remaining value: {_fmt_money(order.estimated_remaining_krw)}",
                f"   • ordered_at/age/ref: {order.ordered_at or 'n/a'} / {order.age or 'n/a'} / `{order.order_ref}`",
            ]
        )
    if omitted_count:
        lines.append(
            f"…and {omitted_count} more pending order(s) omitted to fit Discord content limits."
        )
    message = "\n".join(lines)
    if len(message) > MAX_DISCORD_CONTENT_CHARS:
        return (
            message[: MAX_DISCORD_CONTENT_CHARS - 80].rstrip()
            + "\n…truncated to fit Discord content limits."
        )
    return message


def format_failure_message(
    *,
    stage: str,
    reason: str,
    failure_class: str,
    partial: bool,
    run_ts: datetime | None = None,
    hint: str | None = None,
    config: CryptoPendingOrderAlertConfig | None = None,
) -> str:
    run_ts = run_ts or datetime.now(UTC)
    channel_id = config.failure_channel_id if config else FAILURE_CHANNEL_ID
    lines = [
        f"🚨 **{SERVICE_NAME} failure**",
        f"Channel: <#{channel_id}>",
        f"Stage: `{stage}`",
        f"Class: `{failure_class}`",
        f"Partial data: `{str(partial).lower()}`",
        f"Run: {run_ts.isoformat()}",
        f"Reason: {reason}",
    ]
    if hint:
        lines.append(f"Hint: {hint}")
    return "\n".join(lines)


def _validate_order_symbols(
    orders: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    symbols: list[str] = []
    malformed_refs: list[str] = []
    seen: set[str] = set()
    for order in orders:
        symbol = str(order.get("symbol") or "").strip().upper()
        if not symbol or "-" not in symbol:
            malformed_refs.append(_short_ref(order.get("order_id")))
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return sorted(symbols), malformed_refs


async def _default_order_lookup() -> dict[str, Any]:
    return await get_order_history_impl(status="pending", market="crypto", limit=-1)


async def _default_price_lookup(symbols: list[str]) -> dict[str, float]:
    return await fetch_multiple_current_prices(symbols, use_cache=False)


async def _default_discord_sender(webhook_url: str, content: str) -> bool:
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        return await send_discord_content_single(
            http_client=client,
            webhook_url=webhook_url,
            content=content,
        )


async def _send_failure(
    *,
    config: CryptoPendingOrderAlertConfig,
    sender: DiscordSender,
    stage: str,
    reason: str,
    failure_class: str,
    partial: bool,
    run_ts: datetime,
    dry_run: bool,
    hint: str | None = None,
) -> dict[str, Any]:
    message = format_failure_message(
        stage=stage,
        reason=reason,
        failure_class=failure_class,
        partial=partial,
        run_ts=run_ts,
        hint=hint,
        config=config,
    )
    if dry_run:
        return {"failure_alert_sent": False, "failure_message": message}
    if not config.failure_webhook_url:
        return {
            "failure_alert_sent": False,
            "failure_message": message,
            "failure_delivery_error": "missing failure webhook url",
        }
    delivered = await sender(config.failure_webhook_url, message)
    return {"failure_alert_sent": delivered, "failure_message": message}


async def run_crypto_pending_order_alert(
    *,
    execute: bool = False,
    config: CryptoPendingOrderAlertConfig | None = None,
    order_lookup: OrderLookup | None = None,
    price_lookup: PriceLookup | None = None,
    discord_sender: DiscordSender | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the read-only pending-order reminder.

    `execute=False` is dry-run mode and never posts to Discord.
    """
    config = config or CryptoPendingOrderAlertConfig.from_settings()
    order_lookup = order_lookup or _default_order_lookup
    price_lookup = price_lookup or _default_price_lookup
    discord_sender = discord_sender or _default_discord_sender
    run_ts = now or datetime.now(UTC)
    dry_run = not execute

    summary: dict[str, Any] = {
        "service": SERVICE_NAME,
        "execute": execute,
        "dry_run": dry_run,
        "enabled": config.enabled,
        "normal_channel_id": config.normal_channel_id,
        "failure_channel_id": config.failure_channel_id,
        "run_ts": run_ts.isoformat(),
    }

    if execute and not config.enabled:
        return summary | {"status": "skipped", "reason": "disabled"}

    try:
        history = await order_lookup()
    except Exception as exc:  # noqa: BLE001
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="lookup",
            reason=f"{type(exc).__name__}: {exc}",
            failure_class=type(exc).__name__,
            partial=False,
            run_ts=run_ts,
            dry_run=dry_run,
            hint="Check Upbit credentials/connectivity and order-history handler logs.",
        )
        return summary | {"status": "failed", "stage": "lookup"} | failure

    orders = list(history.get("orders") or [])
    errors = list(history.get("errors") or [])
    if errors:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="lookup",
            reason=f"order history returned errors: {errors}",
            failure_class="PartialOrderLookup",
            partial=bool(orders),
            run_ts=run_ts,
            dry_run=dry_run,
            hint="Inspect broker-specific errors before trusting the reminder output.",
        )
        return (
            summary
            | {"status": "failed", "stage": "lookup", "orders_count": len(orders)}
            | failure
        )

    if not orders:
        return summary | {
            "status": "success",
            "orders_count": 0,
            "normal_alert_sent": False,
            "reason": "no pending crypto orders",
        }

    symbols, malformed_refs = _validate_order_symbols(orders)
    if malformed_refs:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="order_validation",
            reason=f"malformed pending order rows without valid crypto symbols: {malformed_refs}",
            failure_class="MalformedOrderRows",
            partial=True,
            run_ts=run_ts,
            dry_run=dry_run,
            hint="Check the broker order-history normalization before sending a normal reminder.",
        )
        return (
            summary
            | {
                "status": "failed",
                "stage": "order_validation",
                "orders_count": len(orders),
            }
            | failure
        )

    try:
        prices = await price_lookup(symbols)
    except Exception as exc:  # noqa: BLE001
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="quote_enrichment",
            reason=f"{type(exc).__name__}: {exc}",
            failure_class=type(exc).__name__,
            partial=True,
            run_ts=run_ts,
            dry_run=dry_run,
            hint="Pending orders were found but quote enrichment failed.",
        )
        return (
            summary
            | {
                "status": "failed",
                "stage": "quote_enrichment",
                "orders_count": len(orders),
            }
            | failure
        )

    missing_prices = [symbol for symbol in symbols if symbol not in prices]
    if missing_prices:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="quote_enrichment",
            reason=f"missing current prices for: {', '.join(missing_prices)}",
            failure_class="PartialQuoteEnrichment",
            partial=True,
            run_ts=run_ts,
            dry_run=dry_run,
            hint="Verify Upbit market codes and ticker endpoint availability.",
        )
        return (
            summary
            | {
                "status": "failed",
                "stage": "quote_enrichment",
                "orders_count": len(orders),
            }
            | failure
        )

    normalized = normalize_pending_orders(orders, prices, now=run_ts)
    message = format_pending_order_message(normalized, config=config, run_ts=run_ts)
    if dry_run:
        return summary | {
            "status": "success",
            "orders_count": len(normalized),
            "normal_alert_sent": False,
            "message": message,
            "orders": [asdict(order) for order in normalized],
        }

    if not config.normal_webhook_url:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="discord_delivery",
            reason="missing normal webhook url",
            failure_class="MissingDiscordWebhook",
            partial=True,
            run_ts=run_ts,
            dry_run=False,
            hint="Configure the dedicated CRYPTO_PENDING_ORDER_ALERT_WEBHOOK_URL for channel 1500719153508515870.",
        )
        return (
            summary
            | {
                "status": "failed",
                "stage": "discord_delivery",
                "orders_count": len(normalized),
            }
            | failure
        )

    delivered = await discord_sender(config.normal_webhook_url, message)
    if not delivered:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="discord_delivery",
            reason="normal Discord webhook returned unsuccessful delivery",
            failure_class="DiscordDeliveryFailed",
            partial=True,
            run_ts=run_ts,
            dry_run=False,
            hint="Check normal webhook URL/channel and Discord webhook permissions.",
        )
        return (
            summary
            | {
                "status": "failed",
                "stage": "discord_delivery",
                "orders_count": len(normalized),
            }
            | failure
        )

    return summary | {
        "status": "success",
        "orders_count": len(normalized),
        "normal_alert_sent": True,
        "orders": [asdict(order) for order in normalized],
    }
