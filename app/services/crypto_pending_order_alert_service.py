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
# hk 2603 decision (2026-11-04): only symbols that need attention get a line.
# A symbol is flagged when any of its orders is at least this old ...
FLAG_MIN_AGE_DAYS = 3
# ... or sits at least this far (absolute %) from the current price.
FLAG_MIN_ABS_GAP_PCT = 10.0

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
    discord_bot_token: str | None
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
            discord_bot_token=_resolve_secret_setting(
                getattr(settings_obj, "crypto_pending_order_discord_bot_token", None)
            ),
            trader_base_url=str(getattr(settings_obj, "trader_base_url", "") or ""),
        )


def _resolve_secret_setting(value: Any) -> str | None:
    if value in (None, ""):
        return None
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        value = getter()
    raw = str(value).strip()
    return raw or None


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
    return f"{base}/invest/stocks/crypto/{symbol}"


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


@dataclass(frozen=True)
class PendingSymbolSummary:
    symbol: str
    sides: tuple[str, ...]
    order_count: int
    nearest_gap_pct: float | None
    max_abs_gap_pct: float | None
    total_remaining_krw: float | None
    oldest_age: str | None
    flag_reasons: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.flag_reasons)


def _age_days(ordered_at: str | None, *, now: datetime) -> float | None:
    created = _parse_ordered_at(ordered_at)
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now.astimezone(UTC) - created.astimezone(UTC)).total_seconds() / 86400


def summarize_pending_orders_by_symbol(
    orders: list[PendingCryptoOrder], *, now: datetime
) -> list[PendingSymbolSummary]:
    """Group pending orders per symbol and flag the ones that need attention.

    Flagged symbols come first, largest absolute gap first; the rest follow in
    symbol order. Boundaries are inclusive (exactly 3 days / exactly 10%).
    """
    grouped: dict[str, list[PendingCryptoOrder]] = {}
    for order in orders:
        grouped.setdefault(order.symbol, []).append(order)

    summaries: list[PendingSymbolSummary] = []
    for symbol, group in grouped.items():
        gaps = [o.gap_pct for o in group if o.gap_pct is not None]
        remaining = [
            o.estimated_remaining_krw
            for o in group
            if o.estimated_remaining_krw is not None
        ]
        ages = [
            (days, o.age)
            for o in group
            if (days := _age_days(o.ordered_at, now=now)) is not None
        ]
        reasons: list[str] = []
        if any(days >= FLAG_MIN_AGE_DAYS for days, _ in ages):
            reasons.append(f"age≥{FLAG_MIN_AGE_DAYS}d")
        if any(abs(gap) >= FLAG_MIN_ABS_GAP_PCT for gap in gaps):
            reasons.append(f"gap≥{FLAG_MIN_ABS_GAP_PCT:g}%")
        summaries.append(
            PendingSymbolSummary(
                symbol=symbol,
                sides=tuple(sorted({o.side.upper() for o in group})),
                order_count=len(group),
                nearest_gap_pct=min(gaps, key=abs) if gaps else None,
                max_abs_gap_pct=max((abs(g) for g in gaps), default=None),
                total_remaining_krw=sum(remaining) if remaining else None,
                oldest_age=max(ages)[1] if ages else None,
                flag_reasons=tuple(reasons),
            )
        )

    flagged = sorted(
        (s for s in summaries if s.flagged),
        key=lambda s: (-(s.max_abs_gap_pct or 0.0), s.symbol),
    )
    rest = sorted((s for s in summaries if not s.flagged), key=lambda s: s.symbol)
    return flagged + rest


def _symbol_line(
    idx: int, summary: PendingSymbolSummary, config: CryptoPendingOrderAlertConfig
) -> str:
    line = (
        f"{idx}. `{summary.symbol}` {'/'.join(summary.sides)} ×{summary.order_count}"
        f" · nearest {_fmt_pct(summary.nearest_gap_pct)}"
        f" · remaining {_fmt_money(summary.total_remaining_krw)}"
        f" · oldest {summary.oldest_age or 'n/a'}"
        f" · ⚠️ {', '.join(summary.flag_reasons)}"
    )
    link = _position_url(config, summary.symbol)
    if link:
        line += f" — {link}"
    return line


def format_pending_order_message(
    orders: list[PendingCryptoOrder],
    *,
    config: CryptoPendingOrderAlertConfig,
    run_ts: datetime | None = None,
) -> str:
    run_ts = run_ts or datetime.now(UTC)
    summaries = summarize_pending_orders_by_symbol(orders, now=run_ts)
    flagged = [s for s in summaries if s.flagged]
    rest = [s for s in summaries if not s.flagged]

    head = [
        f"🔔 **Crypto pending orders: {len(orders)} open** "
        f"({len(summaries)} symbol(s), {len(flagged)} need attention)",
        READ_ONLY_NOTICE,
        f"Channel: <#{config.normal_channel_id}>",
        f"Run: {run_ts.isoformat()}",
        "",
    ]
    tail: list[str] = []
    if rest:
        tail.append(
            f"Other {len(rest)} symbol(s) · {sum(s.order_count for s in rest)} order(s):"
            f" resting normally (<{FLAG_MIN_AGE_DAYS}d old, <{FLAG_MIN_ABS_GAP_PCT:g}% gap)."
        )

    body = [_symbol_line(i, s, config) for i, s in enumerate(flagged, 1)]
    while body:
        cut = len(flagged) - len(body)
        note = (
            [f"…{cut} flagged line(s) cut to fit Discord content limits."]
            if cut
            else []
        )
        message = "\n".join(head + body + note + tail)
        if len(message) <= MAX_DISCORD_CONTENT_CHARS:
            return message
        body.pop()
    note = (
        [f"…{len(flagged)} flagged line(s) cut to fit Discord content limits."]
        if flagged
        else []
    )
    return "\n".join(head + note + tail)[:MAX_DISCORD_CONTENT_CHARS]


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


async def _send_discord_channel_message(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    channel_id: str,
    content: str,
) -> bool:
    try:
        response = await http_client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}"},
            json={"content": content},
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Discord channel message to %s", channel_id)
        return False


async def _default_discord_sender(delivery_target: str, content: str) -> bool:
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        if delivery_target.startswith("discord-bot://"):
            _, rest = delivery_target.split("://", maxsplit=1)
            channel_id, bot_token = rest.split("/", maxsplit=1)
            return await _send_discord_channel_message(
                http_client=client,
                bot_token=bot_token,
                channel_id=channel_id,
                content=content,
            )
        return await send_discord_content_single(
            http_client=client,
            webhook_url=delivery_target,
            content=content,
        )


def _delivery_target(
    webhook_url: str | None, channel_id: str, bot_token: str | None
) -> str | None:
    if webhook_url:
        return webhook_url
    if bot_token:
        return f"discord-bot://{channel_id}/{bot_token}"
    return None


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
    target = _delivery_target(
        config.failure_webhook_url,
        config.failure_channel_id,
        config.discord_bot_token,
    )
    if not target:
        return {
            "failure_alert_sent": False,
            "failure_message": message,
            "failure_delivery_error": "missing failure Discord webhook URL or bot token",
        }
    delivered = await sender(target, message)
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

    target = _delivery_target(
        config.normal_webhook_url,
        config.normal_channel_id,
        config.discord_bot_token,
    )
    if not target:
        failure = await _send_failure(
            config=config,
            sender=discord_sender,
            stage="discord_delivery",
            reason="missing normal Discord webhook URL or bot token",
            failure_class="MissingDiscordDeliveryTarget",
            partial=True,
            run_ts=run_ts,
            dry_run=False,
            hint="Configure CRYPTO_PENDING_ORDER_ALERT_WEBHOOK_URL or CRYPTO_PENDING_ORDER_DISCORD_BOT_TOKEN for channel 1500719153508515870.",
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

    delivered = await discord_sender(target, message)
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
