"""Telegram callback-query handler for order_proposals approvals (ROB-816 PR 2).

Orchestrates the whole click-to-submit flow for a single Telegram webhook
update: chat-allowlist authz -> callback-data parse -> short-prefix proposal
resolution -> nonce replay guard -> commit lease -> approve/deny dispatch ->
fresh re-validate & submit -> Telegram message update.

This module owns the DB session it opens via ``service_factory`` (default
``AsyncSessionLocal``) and DOES commit -- unlike ``OrderProposalsService``/
``OrderProposalRepository``, which only flush and never commit -- because
this handler is the top-level caller, same as any MCP tool handler in this
codebase.

Every broker/Telegram/DB dependency is injectable (``notifier``,
``revalidate_fn``, ``service_factory``) so tests can supply fakes; real
broker/Telegram/httpx calls are never exercised by this module's test suite.

Principle #5 (nonce replay prevention is load-bearing): ``consume_approval_nonce``
is always called -- and its exceptions handled -- before any other mutation in
both the approve and deny branches.

``handle_callback_update`` never raises: Telegram's webhook contract expects a
response for every update, so any unexpected exception is caught, logged, and
turned into a best-effort ``answer_callback`` + a failure result dict.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.approval_message import (
    build_approval_message,
    parse_callback_data,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import OrderProposalsService

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]
RevalidateFn = Callable[..., Any]

# Rung states from which a direct transition to "rejected" is legal (see
# app/services/order_proposals/state_machine.py). A Telegram deny only ever
# acts on rungs that are still awaiting/undergoing submission -- rungs already
# past "submitting" (acked/resting/partially_filled) cannot be rejected
# directly and are left untouched by this handler.
_DENY_REJECTABLE_STATES = frozenset(
    {"pending_approval", "needs_reconfirm", "submitting", "unverified"}
)

_CANDIDATE_POOL_LIMIT = 200

_RESULT_LABELS: dict[str, str] = {
    "submitted_acked": "체결 대기(접수)",
    "submitted_resting": "주문 유지(대기)",
    "guard_blocked": "가드에 의해 차단됨",
    "unverified": "확인 불가(수동 확인 필요)",
    "error": "오류",
    "needs_reconfirm": "재확인 필요",
}


def _generate_nonce() -> str:
    return secrets.token_urlsafe(12)


async def _safe_answer(
    notifier: Any, callback_query_id: str | None, text: str | None = None
) -> None:
    """Best-effort ``answer_callback`` that never raises.

    Used both on the happy/known-failure paths and from the top-level
    exception handler, where a second failure (e.g. the notifier itself
    raising) must not crash the handler.
    """
    if not callback_query_id:
        return
    try:
        await notifier.answer_callback(callback_query_id, text)
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        logger.exception("order_proposals telegram answer_callback failed")


async def _resolve_proposal_id(service: Any, proposal_short: str) -> uuid.UUID | None:
    """Resolve a full ``proposal_id`` from its 8-char callback-data prefix.

    ``pending_approval``/``needs_reconfirm`` are rung-level states, not valid
    group-level ``lifecycle_state`` values (see state_machine.GROUP_STATES) --
    the group rollup currently buckets all of those (plus "unverified") into
    "proposed" (see ``OrderProposalsService._recompute_group_state``). So the
    candidate pool is fetched by group lifecycle_state="proposed" and then
    filtered in Python by prefix. Zero or multiple matches are both treated
    as an unresolved reference -- fail closed rather than guess.
    """
    candidates = await service.list_recent(
        lifecycle_state="proposed", limit=_CANDIDATE_POOL_LIMIT
    )
    matches = [
        group.proposal_id
        for group, _rungs in candidates
        if str(group.proposal_id).startswith(proposal_short)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _build_result_summary(outcomes: list[RungOutcome]) -> str:
    if not outcomes:
        return "처리할 대기 단계가 없습니다."
    lines = ["*처리 결과*"]
    for outcome in outcomes:
        label = _RESULT_LABELS.get(outcome.result, outcome.result)
        lines.append(f"- #{outcome.rung_index + 1}: {label}")
    return "\n".join(lines)


async def _handle_deny(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    nonce: str,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None,
) -> dict[str, Any]:
    try:
        await service.consume_approval_nonce(proposal_id, nonce, now=now)
    except OrderProposalError as exc:
        await _safe_answer(
            notifier, callback_query_id, "이미 처리되었거나 유효하지 않은 요청입니다"
        )
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    _group, rungs = await service.get_proposal(proposal_id)
    rejected_rungs: list[int] = []
    for rung in rungs:
        if rung.state in _DENY_REJECTABLE_STATES:
            await service.record_rejected(
                proposal_id, rung.rung_index, reason="telegram_deny", now=now
            )
            rejected_rungs.append(rung.rung_index)

    if message_id is not None:
        await notifier.edit_message(chat_id, message_id, "❌ 거부됨")
    await _safe_answer(notifier, callback_query_id, "거부되었습니다")
    return {
        "handled": True,
        "reason": "denied",
        "proposal_id": str(proposal_id),
        "rejected_rungs": rejected_rungs,
    }


async def _handle_approve(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    nonce: str,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None,
    telegram_user_id: str,
    revalidate_fn: RevalidateFn,
) -> dict[str, Any]:
    try:
        await service.consume_approval_nonce(proposal_id, nonce, now=now)
    except OrderProposalError as exc:
        await _safe_answer(
            notifier, callback_query_id, "이미 처리되었거나 유효하지 않은 요청입니다"
        )
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    acquired = await service.acquire_commit_lease(proposal_id, now=now)
    if not acquired:
        await _safe_answer(notifier, callback_query_id, "처리 중")
        return {
            "handled": False,
            "reason": "lease_held",
            "proposal_id": str(proposal_id),
        }

    await service.record_approval(
        proposal_id, telegram_user_id=telegram_user_id, now=now
    )

    outcomes: list[RungOutcome] = await revalidate_fn(
        service=service, proposal_id=proposal_id, now=now
    )

    reconfirm_outcomes = [o for o in outcomes if o.result == "needs_reconfirm"]
    if reconfirm_outcomes:
        fresh_nonce = _generate_nonce()
        await service.set_approval_nonce(proposal_id, fresh_nonce)
        group, rungs = await service.get_proposal(proposal_id)
        text, keyboard = build_approval_message(
            group=group, rungs=rungs, diff=reconfirm_outcomes[0].detail
        )
        if message_id is not None:
            await notifier.edit_message(
                chat_id, message_id, "⚠️ 재확인 필요 — 아래 새 메시지를 확인해 주세요."
            )
        new_message_id = await notifier.send_approval_message(
            text, keyboard, chat_id=str(chat_id)
        )
        await _safe_answer(notifier, callback_query_id, "재확인이 필요합니다")
        return {
            "handled": True,
            "reason": "needs_reconfirm",
            "proposal_id": str(proposal_id),
            "new_message_id": new_message_id,
        }

    summary = _build_result_summary(outcomes)
    if message_id is not None:
        await notifier.edit_message(chat_id, message_id, summary)
    await _safe_answer(notifier, callback_query_id, "처리되었습니다")
    return {
        "handled": True,
        "reason": "approved",
        "proposal_id": str(proposal_id),
        "results": [outcome.result for outcome in outcomes],
    }


async def handle_callback_update(
    update: dict[str, Any],
    *,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
    notifier: Any = None,
    revalidate_fn: RevalidateFn = revalidate_and_submit,
) -> dict[str, Any]:
    """Handle one Telegram webhook update. Never raises (fail-closed)."""
    callback_query_id: str | None = None
    active_notifier = notifier
    try:
        if active_notifier is None:
            from app.monitoring.trade_notifier.notifier import get_trade_notifier

            active_notifier = get_trade_notifier()

        callback_query = update.get("callback_query")
        if not isinstance(callback_query, dict):
            return {"handled": False, "reason": "not_callback"}

        callback_query_id = callback_query.get("id")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        from_user = callback_query.get("from") or {}
        telegram_user_id = from_user.get("id")
        data = callback_query.get("data")

        if str(chat_id) not in settings.order_proposals_telegram_chat_allowlist:
            await _safe_answer(active_notifier, callback_query_id, "허용되지 않은 채팅")
            return {"handled": False, "reason": "chat_not_allowed"}

        try:
            action, proposal_short, nonce = parse_callback_data(data)
        except ValueError:
            await _safe_answer(active_notifier, callback_query_id, "잘못된 요청입니다")
            return {"handled": False, "reason": "malformed_callback_data"}

        async with service_factory() as session:
            service = OrderProposalsService(session)
            proposal_id = await _resolve_proposal_id(service, proposal_short)
            if proposal_id is None:
                await session.commit()
                await _safe_answer(
                    active_notifier, callback_query_id, "제안을 찾을 수 없습니다"
                )
                return {"handled": False, "reason": "proposal_not_found"}

            if action == "dn":
                result = await _handle_deny(
                    service=service,
                    proposal_id=proposal_id,
                    nonce=nonce,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                )
            else:
                result = await _handle_approve(
                    service=service,
                    proposal_id=proposal_id,
                    nonce=nonce,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                    telegram_user_id=(
                        str(telegram_user_id) if telegram_user_id is not None else ""
                    ),
                    revalidate_fn=revalidate_fn,
                )
            await session.commit()
            return result
    except Exception:  # noqa: BLE001 - fail-closed webhook contract
        logger.exception("order_proposals telegram callback handling failed")
        await _safe_answer(
            active_notifier, callback_query_id, "처리 중 오류가 발생했습니다"
        )
        return {"handled": False, "reason": "internal_error"}
