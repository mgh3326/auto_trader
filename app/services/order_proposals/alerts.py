"""Discord-first operational alerts for non-sent approval dispatches."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.trade_notifier.transports import send_discord_embed_single
from app.services.order_proposals.service import OrderProposalsService

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]
AlertState = Literal["sent", "failed"]

_FAILURE_COLOR = 0xE74C3C


@dataclass(frozen=True, slots=True)
class ApprovalDispatchAlertResult:
    state: AlertState
    channel: str
    failure_code: str | None
    recorded: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _recommended_action(failure_code: str) -> str:
    upper = failure_code.upper()
    if "EXPIRED" in upper or "INVALID_VALID_UNTIL" in upper:
        return "재발송 금지. 제안을 재평가한 뒤 void + 새 제안을 생성하세요."
    if "CALENDAR_UNKNOWN" in upper or "NXT_CAPABILITY_STALE" in upper:
        return (
            "세션/NXT 데이터를 신선화한 뒤 order_proposal_redispatch(dry_run=true)로 "
            "확인하고, 통과 시 dry_run=false로 재발송하세요. 또는 정규장에서 재시도하세요."
        )
    if "DEFER_SESSION_CLOSED" in upper or "NO_EXECUTABLE_WINDOW" in upper:
        return (
            "다음 주문 가능 세션에서 order_proposal_redispatch(dry_run=true)로 "
            "재검증한 뒤 재발송하세요."
        )
    if "TELEGRAM" in upper or "APPROVAL_" in upper:
        return (
            "Telegram 설정/전송 상태를 복구한 뒤 order_proposal_redispatch"
            "(dry_run=true → false)로 재발송하세요."
        )
    return (
        "원인을 확인한 뒤 order_proposal_redispatch(dry_run=true)로 재검증하고, "
        "통과한 경우에만 dry_run=false로 재발송하세요."
    )


async def _record_alert_outcome(
    proposal_id: uuid.UUID,
    *,
    state: AlertState,
    alert_failure_code: str | None,
    dispatch_failure_code: str,
    now: datetime,
    service_factory: ServiceFactory,
) -> bool:
    try:
        async with service_factory() as session:
            service = OrderProposalsService(session)
            await service.record_approval_dispatch_alert(
                proposal_id,
                state=state,
                alert_failure_code=alert_failure_code,
                dispatch_failure_code=dispatch_failure_code,
                now=now,
            )
            await session.commit()
        return True
    except Exception:  # noqa: BLE001 - the primary dispatch outcome is already durable
        logger.exception(
            "order_proposals.approval_dispatch_alert_record_failed",
            extra={
                "proposal_id": str(proposal_id),
                "dispatch_failure_code": dispatch_failure_code,
                "alert_failure_code": alert_failure_code,
            },
        )
        return False


async def send_approval_dispatch_alert(
    proposal_id: uuid.UUID,
    *,
    dispatch_state: str,
    dispatch_failure_code: str,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> ApprovalDispatchAlertResult:
    """Alert Discord and durably record delivery success/failure.

    This boundary never changes the already-committed proposal or dispatch
    outcome. Alert delivery failures are returned, logged at error level, and
    written to ``source_asof.approval_dispatch_alert`` when the DB is available.
    """
    symbol = "unknown"
    side = "unknown"
    attempt_id: str | None = None
    try:
        async with service_factory() as session:
            service = OrderProposalsService(session)
            group, _rungs = await service.get_proposal(proposal_id)
            symbol = group.symbol
            side = group.side
            attempt_id = (
                str(group.approval_dispatch_attempt_id)
                if group.approval_dispatch_attempt_id is not None
                else None
            )
    except Exception:  # noqa: BLE001 - still try the operator channel
        logger.exception(
            "order_proposals.approval_dispatch_alert_context_failed",
            extra={
                "proposal_id": str(proposal_id),
                "dispatch_failure_code": dispatch_failure_code,
            },
        )

    embed: dict[str, Any] = {
        "title": "🚨 approval_dispatch가 승인창 없이 종료됨",
        "color": _FAILURE_COLOR,
        "fields": [
            {"name": "proposal_id", "value": str(proposal_id), "inline": False},
            {"name": "symbol", "value": symbol, "inline": True},
            {"name": "side", "value": side, "inline": True},
            {"name": "dispatch_state", "value": dispatch_state, "inline": True},
            {
                "name": "failure_code",
                "value": dispatch_failure_code,
                "inline": False,
            },
            {
                "name": "operator_action",
                "value": _recommended_action(dispatch_failure_code),
                "inline": False,
            },
        ],
    }
    if attempt_id is not None:
        embed["fields"].append(
            {"name": "dispatch_attempt_id", "value": attempt_id, "inline": False}
        )

    webhook = settings.discord_webhook_alerts
    alert_failure_code: str | None = None
    delivered = False
    if not webhook:
        alert_failure_code = "discord_webhook_unconfigured"
    else:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                delivered = await send_discord_embed_single(
                    http_client=client,
                    webhook_url=webhook,
                    embed=embed,
                )
            if not delivered:
                alert_failure_code = "discord_delivery_failed"
        except Exception:  # noqa: BLE001 - convert to observable failure
            alert_failure_code = "discord_alert_exception"
            logger.exception(
                "order_proposals.approval_dispatch_alert_exception",
                extra={
                    "proposal_id": str(proposal_id),
                    "dispatch_failure_code": dispatch_failure_code,
                },
            )

    state: AlertState = "sent" if delivered else "failed"
    if not delivered:
        logger.error(
            "order_proposals.approval_dispatch_alert_failed",
            extra={
                "proposal_id": str(proposal_id),
                "dispatch_state": dispatch_state,
                "dispatch_failure_code": dispatch_failure_code,
                "alert_failure_code": alert_failure_code,
            },
        )
    recorded = await _record_alert_outcome(
        proposal_id,
        state=state,
        alert_failure_code=alert_failure_code,
        dispatch_failure_code=dispatch_failure_code,
        now=now,
        service_factory=service_factory,
    )
    return ApprovalDispatchAlertResult(
        state=state,
        channel="discord",
        failure_code=alert_failure_code,
        recorded=recorded,
    )


__all__ = [
    "ApprovalDispatchAlertResult",
    "send_approval_dispatch_alert",
]
