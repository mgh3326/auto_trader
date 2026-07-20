"""ROB-265 Plan 4 — watch-trigger review notification payload + delivery.

Carries the closed payload (see :class:`ReviewTriggerPayload`) built from
the full immutable trigger identity snapshot in ``investment_watch_events``
plus the linkage back to the source report / item / alert, and delivers it
in-process via :class:`~app.monitoring.trade_notifier.TradeNotifier`
(ROB-566 — the earlier HTTP webhook delivery to an external Hermes gateway
has been removed; ROB-986).

The agent gateway (formerly OpenClaw) is the legacy notification surface
and is intentionally not touched from this module. The agent-gateway
watch-alert path has now been removed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas.investment_reports import (
    ItemIntentLiteral,
    ItemSideLiteral,
    MarketLiteral,
    TargetKindLiteral,
    WatchActionModeLiteral,
    WatchClauseOpLiteral,
    WatchInvalidation,
    WatchMetricLiteral,
    WatchPriceRange,
)

logger = logging.getLogger(__name__)

WatchEventOutcomeLiteral = Literal[
    "notified",
    "review_required",
    "preview_attached",
    "executed",
    "expired",
    "ignored",
    "failed",
]


class InvestLinks(BaseModel):
    """ROB-500 — operator-facing Invest deep links (path only, no host).

    Hermes prepends its configured Invest base URL when rendering.
    """

    report_path: str
    stock_path: str
    event_anchor: str | None = None
    alert_anchor: str | None = None

    model_config = ConfigDict(extra="forbid")


class OperatorActionGuidance(BaseModel):
    """ROB-500 — what this notification means for the operator.

    Deterministically derived from action_mode/outcome; rendered at the
    top of the Discord card so the operator doesn't have to decode UUIDs.
    """

    headline: str
    requires_operator_review: bool
    order_behavior: Literal["none", "preview_only", "mock_only"]

    model_config = ConfigDict(extra="forbid")


class PriceGuidance(BaseModel):
    """ROB-500 — advisory price thresholds copied **verbatim** from the
    source item's ``watch_recommendation``. Never derived or invented in
    this path; absence means '가격 가이드 없음'. No take-profit / sell
    targets — the stored schema doesn't have them (locked scope).
    """

    entry_review_below_price: Decimal | None = None
    suggested_limit_price_range: WatchPriceRange | None = None
    max_chase_price: Decimal | None = None
    invalidation: WatchInvalidation | None = None

    model_config = ConfigDict(extra="forbid")


class PlannedAction(BaseModel):
    """ROB-514 - operator-facing execution plan derived from max_action."""

    side: ItemSideLiteral
    qty: Decimal | None = None
    amount_krw: Decimal | None = None
    limit_price_hint: Decimal | None = None
    ladder_level: str | None = None

    model_config = ConfigDict(extra="forbid")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def planned_action_from_max_action(
    max_action: dict[str, Any] | None,
) -> PlannedAction | None:
    """Project stored max_action into the Hermes planned_action contract.

    Fail-open: malformed optional context is omitted instead of blocking alert
    delivery.
    """
    if not isinstance(max_action, dict) or not max_action:
        return None
    try:
        return PlannedAction(
            side=max_action.get("side"),
            qty=_decimal_or_none(max_action.get("qty", max_action.get("quantity"))),
            amount_krw=_decimal_or_none(max_action.get("amount_krw")),
            limit_price_hint=_decimal_or_none(
                max_action.get("limit_price_hint", max_action.get("limit_price"))
            ),
            ladder_level=(
                str(max_action["ladder_level"])
                if max_action.get("ladder_level") not in (None, "")
                else None
            ),
        )
    except Exception:  # noqa: BLE001 - notification context is advisory
        logger.warning("max_action planned_action projection failed; omitting context")
        return None


def trigger_checklist_from_raw(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def build_invest_links(
    *,
    market: str,
    symbol: str,
    source_report_uuid: Any,
    event_uuid: Any | None = None,
    alert_uuid: Any | None = None,
) -> InvestLinks:
    report_path = f"/invest/reports/{source_report_uuid}"
    stock_path = (
        f"/invest/stocks/{quote(str(market).lower(), safe='')}"
        f"/{quote(str(symbol), safe='')}"
    )
    return InvestLinks(
        report_path=report_path,
        stock_path=stock_path,
        event_anchor=(
            f"{report_path}#watch-event-{event_uuid}"
            if event_uuid is not None
            else None
        ),
        alert_anchor=(
            f"{report_path}#watch-alert-{alert_uuid}"
            if alert_uuid is not None
            else None
        ),
    )


_GUIDANCE_BY_ACTION_MODE: dict[str, OperatorActionGuidance] = {
    "notify_only": OperatorActionGuidance(
        headline="알림 전용 — 자동 주문 없음, 필요 시 수동 검토",
        requires_operator_review=False,
        order_behavior="none",
    ),
    "approval_required": OperatorActionGuidance(
        headline="운영자 검토 필요 — 승인 전 주문 없음",
        requires_operator_review=True,
        order_behavior="none",
    ),
    "preview_only": OperatorActionGuidance(
        headline="주문 프리뷰 첨부 — 실제 주문 없음",
        requires_operator_review=False,
        order_behavior="preview_only",
    ),
    "auto_execute_mock": OperatorActionGuidance(
        headline="모의계좌 자동 실행 — 실계좌 주문 없음",
        requires_operator_review=False,
        order_behavior="mock_only",
    ),
}

_FALLBACK_GUIDANCE = OperatorActionGuidance(
    headline="알림 — 자동 주문 없음",
    requires_operator_review=False,
    order_behavior="none",
)

_REVIEW_REQUIRED_GUIDANCE = OperatorActionGuidance(
    headline="운영자 검토 필요 — 승인 전 주문 없음",
    requires_operator_review=True,
    order_behavior="none",
)


def build_operator_action_guidance(
    *, action_mode: str, outcome: str
) -> OperatorActionGuidance:
    base = _GUIDANCE_BY_ACTION_MODE.get(action_mode, _FALLBACK_GUIDANCE)
    if outcome == "review_required" and not base.requires_operator_review:
        # validity-review path reuses the trigger contract with
        # outcome='review_required' regardless of the watch's action_mode.
        return _REVIEW_REQUIRED_GUIDANCE
    return base


_PRICE_GUIDANCE_KEYS = (
    "entry_review_below_price",
    "suggested_limit_price_range",
    "max_chase_price",
    "invalidation",
)


def price_guidance_from_watch_recommendation(
    recommendation: dict[str, Any] | None,
) -> PriceGuidance | None:
    """Extract the advisory price subset, or ``None`` for '가격 가이드 없음'.

    Fail-open: malformed stored JSON logs a warning and returns ``None``
    rather than blocking the trigger notification.
    """
    if not isinstance(recommendation, dict):
        return None
    subset = {key: recommendation.get(key) for key in _PRICE_GUIDANCE_KEYS}
    if all(value is None for value in subset.values()):
        return None
    try:
        return PriceGuidance.model_validate(subset)
    except ValidationError:
        logger.warning(
            "watch_recommendation price-guidance subset failed validation — "
            "omitting guidance"
        )
        return None


class ReviewTriggerPayload(BaseModel):
    """Closed contract sent to Hermes when a watch fires.

    Carries every field the operator needs to re-evaluate the watch
    without round-tripping back to auto_trader: full trigger identity
    snapshot + source report/item/alert linkage + correlation_id
    semantics preserved from the event row.
    """

    event_uuid: UUID
    alert_uuid: UUID
    source_report_uuid: UUID
    source_item_uuid: UUID
    correlation_id: str
    kst_date: str
    market: MarketLiteral
    target_kind: TargetKindLiteral
    symbol: str
    metric: WatchMetricLiteral
    operator: WatchClauseOpLiteral
    threshold: Decimal
    threshold_high: Decimal | None = None
    threshold_key: str
    intent: ItemIntentLiteral
    action_mode: WatchActionModeLiteral
    current_value: Decimal | None
    scanner_snapshot: dict[str, Any]
    outcome: WatchEventOutcomeLiteral

    # ROB-500 — operator-facing additions. Optional + additive so older
    # constructors keep working; populated by both send paths.
    invest_links: InvestLinks | None = None
    operator_action_guidance: OperatorActionGuidance | None = None
    price_guidance: PriceGuidance | None = None

    # ROB-514 — watch alert execution plan & trigger checklist
    planned_action: PlannedAction | None = None
    trigger_checklist: list[str] | None = None

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class HermesDeliveryResult:
    status: Literal["success", "skipped", "failed"]
    http_status: int | None = None
    reason: str | None = None


class HermesNotificationClient:
    """Delivers :class:`ReviewTriggerPayload` in-process via TradeNotifier.

    ROB-566 replaced the original HTTP webhook delivery (to an external
    Hermes gateway) with in-process rendering through
    :class:`~app.monitoring.trade_notifier.TradeNotifier`; ROB-986 removed
    the now-dead webhook code path entirely.
    """

    async def send_review_trigger(
        self, payload: ReviewTriggerPayload
    ) -> HermesDeliveryResult:
        from app.monitoring.trade_notifier import get_trade_notifier

        notifier = get_trade_notifier()
        try:
            sent = await notifier.notify_investment_watch(payload)
        except Exception as exc:
            logger.warning(
                "watch in-process render raised: event_uuid=%s error=%s",
                payload.event_uuid,
                exc,
            )
            return HermesDeliveryResult(status="failed", reason="render_exception")
        if sent:
            return HermesDeliveryResult(status="success")
        # dispatch가 False면 skipped(=alert active 유지, 재시도)
        return HermesDeliveryResult(status="skipped", reason="discord_not_configured")

    async def close(self) -> None:
        """No-op — retained so callers' symmetric close() calls stay valid."""
