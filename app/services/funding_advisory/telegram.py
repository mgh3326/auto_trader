"""Read-only Telegram card formatter and claimed-delivery adapter.

Every button is a URL. There is no callback data, proposal create, approval
nonce, broker mutation, or money-movement action in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.invest_deep_links import (
    build_funding_advisory_url,
    build_funding_declaration_url,
)
from app.services.funding_advisory.service import FundingAdvisoryService

logger = logging.getLogger(__name__)

CARD_HEADER = (
    "자금 조달 권고 — 조회/검토 전용 · "
    "이 카드는 입금·환전·차입·매도를 실행하지 않습니다"
)
HANDOFF_LABEL = "경로 설명 · 이 화면에서 주문 안 만듦"


@dataclass(frozen=True)
class FundingAdvisoryCard:
    text: str
    inline_keyboard: dict[str, list[list[dict[str, str]]]]


def _amount(route: dict[str, Any]) -> str:
    value = route.get("route_fundable_amount")
    return str(value) if value is not None else "금액 미상"


def render_funding_advisory_card(view: dict[str, Any]) -> FundingAdvisoryCard:
    target = view["target"]
    trigger = view["trigger"]
    need = view["need"]
    lines = [
        CARD_HEADER,
        "",
        f"대상: {target['market']} · {target['account_mode']} · {target['symbol']}",
        f"상류 gate 통과: {trigger['gate_name']} {trigger['gate_version']} "
        f"@ {trigger['gate_evaluated_at']}",
        f"필요 현금: {need['required_cash']} {target['currency']}",
        f"broker 주문가능액: {need['target_buying_power']} {target['currency']}",
        f"이 후보 shortfall: {need['shortfall']} {target['currency']}",
        f"다른 pending 매수: {need['other_pending_required']} {target['currency']}",
        f"별도 reserved: {need['reserved_cash']} {target['currency']}",
        "pending/reserved 포함 운영상 gap: "
        f"{need['operational_gap_including_other_pending']} {target['currency']}",
        "",
        "조달 경로:",
    ]
    for route in view["routes"]:
        comparison = route.get("comparison", "unavailable")
        lines.append(
            f"- {route['label']}: {_amount(route)} · {comparison} · "
            f"ETA {route.get('deadline_status', 'unknown')}"
        )
        if route["route_id"] in {"PROFITABLE_TRIM", "LOSS_CUT_ROTATION"}:
            lines.append(f"  {HANDOFF_LABEL}")
    combination = view["combination"]
    lines.extend(
        [
            "",
            "부분 조달: 참고 시나리오이며 자동 선택 아님",
            f"남은 gap: {combination['remaining_gap']} {target['currency']}",
            "조달 완료 판정은 target broker buying power 재조회 후",
            "자동 실행 없음 · 선언 갱신도 실제 이체가 아닙니다",
        ]
    )
    detail_url = build_funding_advisory_url(view["advisory_id"])
    keyboard_rows: list[list[dict[str, str]]] = []
    if detail_url is not None:
        keyboard_rows.append([{"text": "상세 보기 · 읽기 전용", "url": detail_url}])
    keyboard_rows.append(
        [
            {
                "text": "외부 잔고 선언 갱신 · 돈 이동 아님",
                "url": build_funding_declaration_url(),
            }
        ]
    )
    return FundingAdvisoryCard(
        text="\n".join(lines),
        inline_keyboard={"inline_keyboard": keyboard_rows},
    )


async def _record_delivery(
    *,
    delivery_id: UUID,
    revision_id: UUID,
    action: str,
    state: str,
    now: datetime,
    chat_id: str | None,
    message_id: int | None,
    failure_code: str | None,
) -> None:
    async with AsyncSessionLocal() as session:
        service = FundingAdvisoryService(session)
        await service.record_delivery_result(
            delivery_id=delivery_id,
            revision_id=revision_id,
            action=action,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            now=now,
            chat_id=chat_id,
            message_id=message_id,
            failure_code=failure_code,
        )


def _notifier() -> Any:
    from app.monitoring.trade_notifier.notifier import get_trade_notifier

    return get_trade_notifier()


async def deliver_claimed_advisory(
    view: dict[str, Any], *, now: datetime, notifier: Any | None = None
) -> dict[str, Any]:
    """Send/edit only when an upstream candidate event already claimed a row."""

    delivery = view.get("delivery") or {}
    action = delivery.get("action")
    if action not in {"send", "edit"}:
        return {"status": "not_sent", "reason": delivery.get("reason", "no_claim")}

    delivery_id = UUID(delivery["delivery_id"])
    revision_id = UUID(view["revision_id"])
    allowlist = settings.order_proposals_telegram_chat_allowlist
    if not settings.ORDER_PROPOSALS_TELEGRAM_ENABLED or not allowlist:
        state = "send_failed" if action == "send" else "edit_failed"
        failure = (
            "funding_telegram_disabled"
            if not settings.ORDER_PROPOSALS_TELEGRAM_ENABLED
            else "telegram_allowlist_empty"
        )
        await _record_delivery(
            delivery_id=delivery_id,
            revision_id=revision_id,
            action=action,
            state=state,
            now=now,
            chat_id=delivery.get("chat_id"),
            message_id=delivery.get("message_id"),
            failure_code=failure,
        )
        return {"status": state, "failure_code": failure}

    card = render_funding_advisory_card(view)
    sender = notifier or _notifier()
    chat_id = delivery.get("chat_id") or allowlist[0]
    message_id = delivery.get("message_id")
    try:
        if action == "edit":
            if not chat_id or message_id is None:
                result = None
                state = "edit_failed"
                failure = "existing_message_identity_missing"
            else:
                result = await sender.edit_message(
                    str(chat_id),
                    int(message_id),
                    card.text,
                    reply_markup=card.inline_keyboard,
                )
                state = "sent" if result.ok else "edit_failed"
                failure = result.failure_code
        else:
            result = await sender.send_approval_message(
                card.text,
                card.inline_keyboard,
                chat_id=str(chat_id),
                parse_mode=None,
            )
            state = "sent" if result.ok else "send_failed"
            failure = result.failure_code
            if result.ok:
                message_id = result.message_id
        await _record_delivery(
            delivery_id=delivery_id,
            revision_id=revision_id,
            action=action,
            state=state,
            now=now,
            chat_id=str(chat_id) if chat_id else None,
            message_id=int(message_id) if message_id is not None else None,
            failure_code=failure,
        )
        return {
            "status": state,
            "message_id": message_id,
            "failure_code": failure,
        }
    except Exception as exc:  # result may be unknown; never retry automatically
        logger.exception("funding advisory Telegram delivery outcome unknown")
        try:
            await _record_delivery(
                delivery_id=delivery_id,
                revision_id=revision_id,
                action=action,
                state="delivery_unknown",
                now=now,
                chat_id=str(chat_id) if chat_id else None,
                message_id=int(message_id) if message_id is not None else None,
                failure_code=type(exc).__name__,
            )
        except Exception:
            logger.exception("funding advisory delivery ledger update failed")
        return {"status": "delivery_unknown", "failure_code": type(exc).__name__}


__all__ = [
    "CARD_HEADER",
    "HANDOFF_LABEL",
    "FundingAdvisoryCard",
    "deliver_claimed_advisory",
    "render_funding_advisory_card",
]
