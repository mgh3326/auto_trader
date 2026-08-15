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

Commit-before-notify ordering (load-bearing): each branch (``_handle_deny``,
both branches of ``_handle_approve``, and the early-return paths) calls
``session.commit()`` for its mutating work *before* making any Telegram
``edit_message``/``send_approval_message`` call. A Telegram API failure
(rate limit, "message not found", network blip) must never roll back a
DB-recorded broker-order outcome -- nonce consumption, the commit lease,
``record_approval``, and any acked/resting/unverified/rejected rung state
from ``revalidate_and_submit`` are all committed first. All notify calls
(``edit_message``/``send_approval_message``, in addition to the existing
``answer_callback``) are themselves best-effort and never raise, as
belt-and-suspenders on top of the commit ordering.

Every broker/Telegram/DB dependency is injectable (``notifier``,
``revalidate_fn``, ``service_factory``) so tests can supply fakes; real
broker/Telegram/httpx calls are never exercised by this module's test suite.

Nonce replay prevention is load-bearing: every manual, batch, auto-veto, and
loss-cut action crosses the shared published-snapshot gate before it can consume
a nonce or reach submit/cancel. After that non-consuming snapshot gate,
approval-window checks run before nonce consumption. An expired proposal
converges through the authoritative expiry transition while leaving the nonce
unconsumed. Deny retains its existing consume-before-reject ordering.

``handle_callback_update`` never raises: Telegram's webhook contract expects a
bounded result for every update, so any unexpected exception is caught, logged,
and turned into a failure result dict. Callback queries are answered
best-effort only after the non-consuming published-binding preflight succeeds;
an invalid callback causes no external Telegram/provider/broker side effect.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.caller_identity import caller_agent_id_var
from app.services.order_proposals.alerts import send_approval_dispatch_alert
from app.services.order_proposals.approval_message import (
    _escape_markdown,
    build_approval_dispatch_messages,
    build_batch_result_message,
    build_buying_power_shortfall_text,
    build_loss_cut_confirmation_message,
    parse_callback_data,
)
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    ApprovalWindowDecision,
    WindowEvaluator,
    approval_window_operator_text,
    approval_window_rung_result,
    evaluate_approval_window,
    evaluate_approval_window_boundary,
    recheck_approval_window_decision,
    valid_until_block,
)
from app.services.order_proposals.auto_veto import (
    TargetCancelFn,
    TargetFetchFn,
    TossVetoReconcileFn,
    acquire_auto_veto_locks,
    cancel_auto_submitted_rungs,
    reconcile_toss_auto_veto_terminal,
)
from app.services.order_proposals.broker_gateway import (
    cancel_target_order,
    fetch_target_order,
)
from app.services.order_proposals.dispatch import publish_approval_messages
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    CallbackEnvelope,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import (
    OrderProposalsService,
    batch_member_block_reason,
)
from app.telegram_contract import (
    TelegramErrorClassification,
    TelegramMethodResult,
    telegram_text_length,
)

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]
RevalidateFn = Callable[..., Any]
Clock = Callable[[], datetime]

# Rung states from which a direct transition to "rejected" is legal (see
# app/services/order_proposals/state_machine.py). A Telegram deny only ever
# acts on rungs that are still awaiting/undergoing submission -- rungs already
# past "submitting" (acked/resting/partially_filled) cannot be rejected
# directly and are left untouched by this handler.
_DENY_REJECTABLE_STATES = frozenset(
    {"pending_approval", "needs_reconfirm", "submitting", "unverified"}
)

_RESULT_LABELS: dict[str, str] = {
    "submitted_acked": "체결 대기(접수)",
    "submitted_resting": "주문 유지(대기)",
    "guard_blocked": "가드에 의해 차단됨",
    "not_delivered": "승인됨, 주문 미도달",
    "unverified": "확인 불가(수동 확인 필요)",
    "error": "오류",
    "needs_reconfirm": "재확인 필요",
    "cancelled": "취소 확인",
    "expired": "제안 만료",
    "invalid_valid_until": "유효기간 오류",
    "defer_session_closed": "주문 가능 세션 아님",
    "calendar_unknown": "시장 세션 확인 불가",
    "no_executable_window": "다음 가능 세션 전 만료",
}

_BATCH_SUCCESS_RESULTS = frozenset(
    {"submitted_acked", "submitted_resting", "cancelled"}
)
_BATCH_SKIP_RESULTS = frozenset({"guard_blocked", "approval_required"})
_WINDOW_BLOCK_RESULTS = frozenset(
    {
        "expired",
        "invalid_valid_until",
        "defer_session_closed",
        "calendar_unknown",
        "no_executable_window",
    }
)
_BATCH_SKIP_RESULTS = _BATCH_SKIP_RESULTS | _WINDOW_BLOCK_RESULTS


async def _evaluate_bound_window(
    group: Any,
    *,
    window_evaluator: WindowEvaluator,
    now_fn: Clock | None = None,
    now: datetime | None = None,
) -> ApprovalWindowDecision:
    if now_fn is None:
        if now is None:
            raise ValueError("approval-window boundary requires a clock")

        def now_fn() -> datetime:
            return now

    expected = (group.source_asof or {}).get("approval_window_policy_stamp")
    return await evaluate_approval_window_boundary(
        group,
        window_evaluator=window_evaluator,
        now_fn=now_fn,
        expected_policy_stamp=str(expected) if expected is not None else None,
    )


async def _reject_window_callback(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    decision: ApprovalWindowDecision,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None,
) -> dict[str, Any]:
    if decision.code is ApprovalWindowCode.EXPIRED:
        await service.expire_mutable_rungs_if_needed(proposal_id, now=now)
    await session.commit()
    text = approval_window_operator_text(decision)
    if message_id is not None:
        await _safe_edit_message(
            notifier,
            chat_id,
            message_id,
            text,
            reply_markup={"inline_keyboard": []},
        )
    await _safe_answer(notifier, callback_query_id, text)
    return {
        "handled": False,
        "reason": decision.code.value,
        "proposal_id": str(proposal_id),
        "approval_window": decision.to_dict(),
    }


def _serialize_rung_outcomes(outcomes: list[RungOutcome]) -> list[dict[str, Any]]:
    """Preserve the original rung index for batch summaries and audits."""
    return [
        {"rung_index": outcome.rung_index, "result": outcome.result}
        for outcome in outcomes
    ]


def _batch_result_values(approval_result: Mapping[str, Any]) -> list[str]:
    """Read structured rung results, falling back to the legacy value list."""
    structured = approval_result.get("rung_results")
    if isinstance(structured, list):
        values = [
            str(item.get("result"))
            for item in structured
            if isinstance(item, Mapping) and item.get("result") is not None
        ]
        if values:
            return values
    return [str(value) for value in approval_result.get("results") or []]


def _outcome_error_summary(outcome: RungOutcome, *, limit: int = 240) -> str | None:
    error = str((outcome.detail or {}).get("error") or "").strip()
    if not error:
        return None
    compact = " ".join(error.split())
    if len(compact) > limit:
        compact = compact[: limit - 1] + "…"
    return _escape_markdown(compact)


def _window_outcome_text(outcome: RungOutcome) -> str | None:
    payload = (outcome.detail or {}).get("approval_window")
    if not isinstance(payload, Mapping):
        return None
    code = str(payload.get("code") or "")
    if code == ApprovalWindowCode.EXPIRED.value:
        return "제안 만료"
    if code == ApprovalWindowCode.INVALID_VALID_UNTIL.value:
        return "제안 유효기간 오류"
    if code == ApprovalWindowCode.CALENDAR_UNKNOWN.value:
        return "시장 세션 확인 불가"
    if code in {
        ApprovalWindowCode.NO_EXECUTABLE_WINDOW.value,
        ApprovalWindowCode.DEFER_SESSION_CLOSED.value,
    }:
        evidence = payload.get("session_evidence")
        next_at = (
            evidence.get("next_allowed_at") if isinstance(evidence, Mapping) else None
        )
        prefix = (
            "다음 주문 가능 세션 전에 만료"
            if code == ApprovalWindowCode.NO_EXECUTABLE_WINDOW.value
            else "주문 가능 세션 아님"
        )
        return f"{prefix} — 다음 허용 세션: {next_at or '확인 불가'}"
    return None


def _generate_nonce() -> str:
    return secrets.token_urlsafe(8)


async def _safe_answer(
    notifier: Any, callback_query_id: str | None, text: str | None = None
) -> None:
    """Best-effort ``answer_callback`` that never raises.

    Called only after the published-binding preflight succeeds. A notifier
    failure must not crash the handler.
    """
    if not callback_query_id:
        return
    try:
        await notifier.answer_callback(callback_query_id, text)
    except Exception as exc:  # noqa: BLE001 - best-effort, never propagate
        logger.error(
            "order_proposals.telegram.answer_callback_failed",
            extra={"exception_type": type(exc).__name__},
        )


async def _safe_edit_message(
    notifier: Any,
    chat_id: Any,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> TelegramMethodResult:
    """Best-effort ``edit_message`` that never raises.

    Belt-and-suspenders alongside the commit-before-notify ordering in
    ``_handle_deny``/``_handle_approve``: by the time this is called the
    mutating DB work for this branch is already committed, so a Telegram
    failure here must not surface as an uncaught exception (which would hit
    the top-level ``except Exception`` and misreport a successful trade
    action as ``"internal_error"``).
    """
    try:
        return await notifier.edit_message(
            chat_id, message_id, text, reply_markup=reply_markup
        )
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        logger.error(
            "order_proposals.telegram.edit_message_failed",
        )
        return TelegramMethodResult.failed(
            payload_chars=telegram_text_length(text),
            failure_code="telegram_transport_error",
            error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
        )


async def _alert_non_sent_callback_dispatch(
    proposal_id: uuid.UUID,
    *,
    dispatch_state: str,
    dispatch_failure_code: str,
    now: datetime,
    service_factory: ServiceFactory,
) -> dict[str, Any]:
    """Alert without hiding an already-committed callback dispatch result."""
    try:
        return (
            await send_approval_dispatch_alert(
                proposal_id,
                dispatch_state=dispatch_state,
                dispatch_failure_code=dispatch_failure_code,
                now=now,
                service_factory=service_factory,
            )
        ).as_dict()
    except Exception as exc:  # noqa: BLE001 - preserve the durable dispatch outcome
        logger.error(
            "order_proposals.approval_dispatch_alert_boundary_failed",
            extra={
                "proposal_id": str(proposal_id),
                "dispatch_state": dispatch_state,
                "dispatch_failure_code": dispatch_failure_code,
                "alert_failure_code": "approval_dispatch_alert_internal_error",
                "exception_type": type(exc).__name__,
            },
        )
        return {
            "state": "failed",
            "channel": "discord",
            "failure_code": "approval_dispatch_alert_internal_error",
            "recorded": False,
        }


async def _resolve_proposal_id(service: Any, proposal_short: str) -> uuid.UUID | None:
    """Resolve a full ``proposal_id`` from its 8-char callback-data prefix.

    The candidate pool includes terminal/superseded groups so stale Telegram
    buttons resolve to the real proposal and reach the lifecycle guard, which
    can return an explicit ``proposal_superseded_by:<id>`` reason. Zero or
    multiple prefix matches are both treated as unresolved -- fail closed
    rather than guess.
    """
    return await service.resolve_proposal_id_prefix(proposal_short)


async def _preflight_proposal_callback(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    callback: CallbackEnvelope,
    notifier: Any,
    callback_query_id: str | None,
) -> dict[str, Any] | None:
    """Run the shared non-consuming gate before any callback-side external I/O."""
    try:
        await service.preflight_published_proposal_callback(
            proposal_id, callback=callback
        )
    except OrderProposalError as exc:
        # End the read transaction without writing. Invalid bindings must not
        # trigger even a Telegram callback answer, preview, provider fetch, or
        # dry-run broker path.
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}
    await _safe_answer(notifier, callback_query_id, "처리 중")
    return None


def _build_result_summary(outcomes: list[RungOutcome]) -> str:
    if not outcomes:
        return "처리할 대기 단계가 없습니다."
    lines = ["*처리 결과*"]
    for outcome in outcomes:
        label = _RESULT_LABELS.get(outcome.result, outcome.result)
        window_text = _window_outcome_text(outcome)
        if window_text is not None:
            label = window_text
        # Merged with main's parallel fix: PR-3a's summarizer (compacted,
        # length-capped, markdown-escaped) + main's "submit_rejected"
        # fallback for error outcomes whose detail carries no error text.
        reason = _outcome_error_summary(outcome)
        if outcome.result == "error" and not reason:
            reason = "submit\\_rejected"
        if (
            reason
            and window_text is None
            and outcome.result
            in {
                "guard_blocked",
                "not_delivered",
                "error",
                *_WINDOW_BLOCK_RESULTS,
            }
        ):
            label = f"{label} — {reason}"
        lines.append(f"- #{outcome.rung_index + 1}: {label}")
    return "\n".join(lines)


def _classify_batch_approval_result(
    approval_result: dict[str, Any],
) -> tuple[str, str | None]:
    """Map the reused single-approval result without hiding rung failures."""
    if not approval_result.get("handled"):
        return "skipped", str(approval_result.get("reason") or "approval_skipped")
    if approval_result.get("reason") == "needs_reconfirm":
        return "needs_reconfirm", None

    outcomes = _batch_result_values(approval_result)
    if outcomes and all(value in _BATCH_SUCCESS_RESULTS for value in outcomes):
        return "approved", None
    if outcomes and all(value in _BATCH_SKIP_RESULTS for value in outcomes):
        return "skipped", ",".join(dict.fromkeys(outcomes))
    if outcomes:
        non_success = [
            value for value in outcomes if value not in _BATCH_SUCCESS_RESULTS
        ]
        return "failed", ",".join(dict.fromkeys(non_success or outcomes))
    return "skipped", "no_rung_outcomes"


def _build_extra_reconfirm_block(reconfirm_outcomes: list[RungOutcome]) -> str:
    """Render before/after diffs for reconfirming rungs beyond the first.

    ``build_approval_message`` only accepts a single ``diff`` and renders an
    explicit before/after highlight for it (see
    ``app/services/order_proposals/approval_message.py``) -- when more than
    one rung in the same ``revalidate_and_submit`` batch comes back
    ``needs_reconfirm``, every rung after the first would otherwise have no
    visible before/after in the outgoing message. This composes a
    supplementary block (in ``telegram_callback.py``, not inside
    ``build_approval_message``, to keep that function's single-diff contract
    unchanged) listing each remaining reconfirming rung's before/after.
    """
    lines = ["*추가 재확인 필요 단계*"]
    for outcome in reconfirm_outcomes:
        detail = outcome.detail or {}
        shortfall_text = build_buying_power_shortfall_text(detail)
        if shortfall_text is not None:
            lines.append(f"- #{outcome.rung_index + 1}: {shortfall_text}")
            continue
        before = detail.get("before")
        after = detail.get("after")
        lines.append(f"- #{outcome.rung_index + 1}: 변경 전 {before} → 변경 후 {after}")
    return "\n".join(lines)


async def _handle_deny(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    callback: CallbackEnvelope,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None,
) -> dict[str, Any]:
    preflight_failure = await _preflight_proposal_callback(
        session=session,
        service=service,
        proposal_id=proposal_id,
        callback=callback,
        notifier=notifier,
        callback_query_id=callback_query_id,
    )
    if preflight_failure is not None:
        return preflight_failure
    try:
        await service.consume_published_proposal_callback(
            proposal_id, callback=callback, now=now
        )
    except OrderProposalError as exc:
        # No mutation happened above (mismatch/replay both raise before any
        # flush) -- commit anyway to release the row lock taken by
        # the common callback gate's `for_update=True` SELECT.
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    _group, rungs = await service.get_proposal(proposal_id)
    rejected_rungs: list[int] = []
    for rung in rungs:
        if rung.state in _DENY_REJECTABLE_STATES:
            await service.record_rejected(
                proposal_id, rung.rung_index, reason="telegram_deny", now=now
            )
            rejected_rungs.append(rung.rung_index)

    # Commit the reject transitions before any Telegram call -- a notify
    # failure below must never roll back an already-recorded deny.
    await session.commit()

    if message_id is not None:
        await _safe_edit_message(notifier, chat_id, message_id, "❌ 거부됨")
    return {
        "handled": True,
        "reason": "denied",
        "proposal_id": str(proposal_id),
        "rejected_rungs": rejected_rungs,
    }


def _classify_veto_outcome(outcome: Mapping[str, Any]) -> str:
    """Map one ``cancel_auto_submitted_rungs`` outcome to a display bucket.

    ``result: "cancel_failed"`` covers two evidentially distinct cases (see
    ``auto_veto.cancel_auto_submitted_rungs``): (1) the fresh broker fetch
    already reports the target ``cancelled`` but the Toss-only second-stage
    ledger reconcile hasn't confirmed yet (``broker_status == "cancelled"``),
    and (2) the fetch itself never produced a status (broker read failure ->
    ``broker_status is None``). Neither is proof the cancel did NOT take
    effect -- ROB-1246: the real Acceptance A run hit case (1) and the
    operator saw a false "취소 실패" for a cancel that had already succeeded
    at the broker and converged moments later via a follow-up reconcile.
    Only a fetched, non-cancelled broker status is a confirmed failure.
    """
    result = outcome.get("result")
    if result in {"filled", "cancelled"}:
        return result
    if result == "cancel_failed":
        broker_status = outcome.get("broker_status")
        if broker_status == "cancelled" or broker_status is None:
            return "unconfirmed"
        return "failed"
    # "not_cancellable": the rung was never in a broker-cancellable state
    # (already resolved via another path, or missing a broker_order_id) --
    # there is no pending broker evidence to wait on, so this is a definite
    # failure bucket rather than "unconfirmed".
    return "failed"


async def _handle_auto_veto(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    callback: CallbackEnvelope,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None = None,
    telegram_user_id: str,
    cancel_fn: TargetCancelFn,
    fetch_fn: TargetFetchFn,
    toss_reconcile_fn: TossVetoReconcileFn = reconcile_toss_auto_veto_terminal,
) -> dict[str, Any]:
    """Cancel every still-open auto-submitted rung and converge evidence."""
    preflight_failure = await _preflight_proposal_callback(
        session=session,
        service=service,
        proposal_id=proposal_id,
        callback=callback,
        notifier=notifier,
        callback_query_id=callback_query_id,
    )
    if preflight_failure is not None:
        return preflight_failure
    group, rungs = await service.get_proposal(proposal_id)
    # Match replace/cancel lock ordering: broker target advisory locks before
    # the proposal-row nonce lock, with stable ordering for multi-rung groups.
    await acquire_auto_veto_locks(service=service, group=group, rungs=rungs)
    try:
        await service.consume_published_proposal_callback(
            proposal_id, callback=callback, now=now
        )
    except OrderProposalError as exc:
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    group, rungs = await service.get_proposal(proposal_id)
    outcomes = await cancel_auto_submitted_rungs(
        service=service,
        group=group,
        rungs=rungs,
        now=now,
        cancel_fn=cancel_fn,
        fetch_fn=fetch_fn,
        toss_reconcile_fn=toss_reconcile_fn,
    )
    outcome_kinds = {_classify_veto_outcome(outcome) for outcome in outcomes}

    await service.record_auto_veto(
        proposal_id,
        telegram_user_id=telegram_user_id,
        outcomes=outcomes,
        now=now,
    )
    await session.commit()

    # Priority order: a fill always wins (nothing left to cancel); a confirmed
    # failure (broker still shows the target open after the cancel attempt,
    # or nothing was cancellable) must not be masked by an unconfirmed rung
    # elsewhere in the same batch; "unconfirmed" only wins over the default
    # success text when no rung is a definite fill/failure.
    if "filled" in outcome_kinds:
        reason = "auto_veto_filled"
        text = "✅ 체결됨 — 취소 시점에 이미 체결된 주문입니다."
    elif "failed" in outcome_kinds:
        reason = "auto_veto_failed"
        text = "⚠️ 취소 실패 — 브로커 주문 상태를 확인해 주세요."
    elif "unconfirmed" in outcome_kinds:
        reason = "auto_veto_unconfirmed"
        text = (
            "🔍 취소 확인 중 — 브로커 취소 처리를 아직 확정하지 못했습니다. "
            "잠시 후 다시 확인해 주세요."
        )
    else:
        reason = "auto_veto_cancelled"
        text = "🛑 취소됨"
    if message_id is not None:
        await _safe_edit_message(
            notifier,
            chat_id,
            message_id,
            text,
            reply_markup={"inline_keyboard": []},
        )
    return {
        "handled": True,
        "reason": reason,
        "proposal_id": str(proposal_id),
        "outcomes": outcomes,
    }


async def _handle_approve(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    callback: CallbackEnvelope,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None,
    telegram_user_id: str,
    revalidate_fn: RevalidateFn,
    window_evaluator: WindowEvaluator | None = None,
    now_fn: Clock | None = None,
    loss_cut_confirmation: bool = False,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> dict[str, Any]:
    window_evaluator = window_evaluator or evaluate_approval_window
    now_fn = now_fn or (lambda: now)
    preflight_failure = await _preflight_proposal_callback(
        session=session,
        service=service,
        proposal_id=proposal_id,
        callback=callback,
        notifier=notifier,
        callback_query_id=callback_query_id,
    )
    if preflight_failure is not None:
        return preflight_failure
    # Lock the broker target before taking any proposal row lock. Independently
    # created proposals may point at the same manual/session order, so the
    # proposal-scoped commit lease alone cannot prevent a double mutation.
    target_group, _ = await service.get_proposal(proposal_id)
    await service.acquire_target_mutation_lock(target_group)

    window = await _evaluate_bound_window(
        target_group, window_evaluator=window_evaluator, now_fn=now_fn
    )
    approval_now = window.observed_at
    if not window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=window,
            now=approval_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
        )

    # Session resolution may await broker/calendar I/O. Re-sample and
    # re-evaluate at the nonce boundary so an edge crossed during that lookup
    # cannot consume the single-use approval token.
    window = await _evaluate_bound_window(
        target_group, window_evaluator=window_evaluator, now_fn=now_fn
    )
    approval_now = window.observed_at
    if not window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=window,
            now=approval_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
        )

    try:
        if loss_cut_confirmation:
            await service.consume_published_proposal_callback(
                proposal_id,
                callback=callback,
                telegram_user_id=telegram_user_id,
                now=approval_now,
            )
        else:
            await service.consume_published_proposal_callback(
                proposal_id,
                callback=callback,
                now=approval_now,
            )
    except OrderProposalError as exc:
        # See `_handle_deny`'s matching comment: no mutation happened above,
        # but commit anyway to release the row lock.
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    acquired = await service.acquire_commit_lease(proposal_id, now=approval_now)
    if not acquired:
        # Same rationale -- release the `for_update=True` lock before return.
        await session.commit()
        return {
            "handled": False,
            "reason": "lease_held",
            "proposal_id": str(proposal_id),
        }

    await service.record_approval(
        proposal_id, telegram_user_id=telegram_user_id, now=approval_now
    )

    # A rung that came back `needs_reconfirm` on a previous approve click is
    # NOT `pending_approval` -- `revalidate_and_submit` only re-enters rungs
    # currently in `pending_approval` (see revalidation.py's module
    # docstring). Without this transition, a second Approve click on the
    # reconfirm message would find every rung still parked in
    # `needs_reconfirm`, skip all of them, and silently no-op forever (ROB-816
    # final-review Finding 2). `needs_reconfirm -> pending_approval` is
    # already a legal transition in state_machine.py; nothing before this fix
    # ever triggered it.
    _current_group, current_rungs = await service.get_proposal(proposal_id)
    for current_rung in current_rungs:
        if current_rung.state == "needs_reconfirm":
            await service.transition_rung(
                proposal_id, current_rung.rung_index, new_state="pending_approval"
            )

    submit_agent_id = settings.ORDER_PROPOSALS_SUBMIT_AGENT_ID.strip() or None
    caller_agent_id_token = caller_agent_id_var.set(submit_agent_id)
    try:
        revalidate_kwargs: dict[str, Any] = {
            "service": service,
            "proposal_id": proposal_id,
            "now": approval_now,
        }
        if revalidate_fn is revalidate_and_submit:
            revalidate_kwargs.update(
                window_evaluator=window_evaluator,
                expected_policy_stamp=window.policy_stamp,
                now_fn=now_fn,
            )
        outcomes: list[RungOutcome] = await revalidate_fn(**revalidate_kwargs)
    finally:
        caller_agent_id_var.reset(caller_agent_id_token)

    window_blocked = [
        outcome for outcome in outcomes if outcome.result in _WINDOW_BLOCK_RESULTS
    ]
    all_window_blocked = bool(outcomes) and len(window_blocked) == len(outcomes)
    target_was_cancelled = any(
        bool((outcome.detail or {}).get("target_cancelled"))
        for outcome in window_blocked
    )
    zero_send_results = _WINDOW_BLOCK_RESULTS | {
        "needs_reconfirm",
        "guard_blocked",
        "approval_required",
    }
    zero_send_window_block = bool(window_blocked) and all(
        outcome.result in zero_send_results for outcome in outcomes
    )
    if zero_send_window_block and not target_was_cancelled:
        expired = any(outcome.result == "expired" for outcome in window_blocked)
        if expired:
            await service.expire_mutable_rungs_if_needed(
                proposal_id,
                now=now_fn(),
            )
        await service.restore_approval_after_window_block(
            proposal_id,
            nonce=callback.nonce,
            expired=expired,
        )
        summary = _build_result_summary(outcomes)
        await session.commit()
        if message_id is not None:
            await _safe_edit_message(
                notifier,
                chat_id,
                message_id,
                summary,
                reply_markup={"inline_keyboard": []},
            )
        first_window = window_blocked[0]
        operator_reason = _window_outcome_text(first_window)
        return {
            "handled": False,
            "reason": str(
                (first_window.detail or {}).get("error") or "approval_window_blocked"
            ),
            "operator_reason": operator_reason,
            "proposal_id": str(proposal_id),
            "results": [outcome.result for outcome in outcomes],
            "rung_results": _serialize_rung_outcomes(outcomes),
        }

    reconfirm_outcomes = [o for o in outcomes if o.result == "needs_reconfirm"]
    if reconfirm_outcomes:
        reconfirm_group, _ = await service.get_proposal(proposal_id)
        reconfirm_window = await _evaluate_bound_window(
            reconfirm_group,
            window_evaluator=window_evaluator,
            now_fn=now_fn,
        )
        reconfirm_now = reconfirm_window.observed_at
        if not reconfirm_window.allowed:
            return await _reject_window_callback(
                session=session,
                service=service,
                proposal_id=proposal_id,
                decision=reconfirm_window,
                now=reconfirm_now,
                notifier=notifier,
                chat_id=chat_id,
                message_id=message_id,
                callback_query_id=callback_query_id,
            )
        reconfirm_window = await _evaluate_bound_window(
            reconfirm_group,
            window_evaluator=window_evaluator,
            now_fn=now_fn,
        )
        reconfirm_now = reconfirm_window.observed_at
        if not reconfirm_window.allowed:
            return await _reject_window_callback(
                session=session,
                service=service,
                proposal_id=proposal_id,
                decision=reconfirm_window,
                now=reconfirm_now,
                notifier=notifier,
                chat_id=chat_id,
                message_id=message_id,
                callback_query_id=callback_query_id,
            )
        fresh_nonce = _generate_nonce()
        await service.set_approval_nonce(proposal_id, fresh_nonce)
        group, rungs = await service.get_proposal(proposal_id)
        dispatch_attempt_id = uuid.uuid4()
        binding = build_proposal_dispatch_binding(
            proposal_id=group.proposal_id,
            nonce=fresh_nonce,
            attempt_id=dispatch_attempt_id,
            card_kind=ApprovalCardKind.RECONFIRM,
            current_membership_revision=(group.approval_dispatch_membership_revision),
        )
        suffix_blocks: list[str] = []
        # `build_approval_message` only renders an explicit diff for the
        # first reconfirming rung -- surface every other reconfirming rung's
        # before/after here so a multi-rung reconfirm batch never silently
        # drops a rung's change (Finding 2, gap #1).
        if len(reconfirm_outcomes) > 1:
            suffix_blocks.append(_build_extra_reconfirm_block(reconfirm_outcomes[1:]))
        # Rungs in the same batch that did NOT come back `needs_reconfirm`
        # (e.g. one rung submitted while another needs reconfirmation) would
        # otherwise never be reported anywhere, since this branch
        # short-circuits before `_build_result_summary` runs below (Finding
        # 2, gap #2).
        other_outcomes = [o for o in outcomes if o.result != "needs_reconfirm"]
        if other_outcomes:
            suffix_blocks.append(_build_result_summary(other_outcomes))
        messages = build_approval_dispatch_messages(
            group=group,
            rungs=rungs,
            diff=reconfirm_outcomes[0].detail,
            suffix_blocks=suffix_blocks,
            binding=binding,
        )
        send_window = await _evaluate_bound_window(
            group,
            window_evaluator=window_evaluator,
            now_fn=now_fn,
        )
        if not send_window.allowed:
            if send_window.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    proposal_id,
                    now=send_window.observed_at,
                )
            await service.restore_approval_after_window_block(
                proposal_id,
                nonce=fresh_nonce,
                expired=send_window.code is ApprovalWindowCode.EXPIRED,
            )
            if send_window.code is not ApprovalWindowCode.EXPIRED:
                await service.clear_approval_nonce(
                    proposal_id,
                    expected_nonce=fresh_nonce,
                )
            await session.commit()
            rejection_text = approval_window_operator_text(send_window)
            if message_id is not None:
                await _safe_edit_message(
                    notifier,
                    chat_id,
                    message_id,
                    rejection_text,
                    reply_markup={"inline_keyboard": []},
                )
            return {
                "handled": False,
                "reason": send_window.code.value,
                "proposal_id": str(proposal_id),
                "approval_window": send_window.to_dict(),
                "results": [outcome.result for outcome in outcomes],
                "rung_results": _serialize_rung_outcomes(outcomes),
            }
        await service.start_approval_dispatch(
            proposal_id,
            attempt_id=dispatch_attempt_id,
            binding=binding,
            now=send_window.observed_at,
            payload_chars=messages.payload_chars,
            context_message_count=len(messages.context_messages),
        )

        # Commit the fresh nonce + record_approval + revalidate_and_submit's
        # rung-state transitions before any Telegram call -- a notify
        # failure below must never roll back real broker-order evidence.
        await session.commit()

        if message_id is not None:
            shortfall_notice = build_buying_power_shortfall_text(
                reconfirm_outcomes[0].detail or {}
            )
            await _safe_edit_message(
                notifier,
                chat_id,
                message_id,
                (
                    f"⚠️ 재확인 필요 — {shortfall_notice}"
                    if shortfall_notice is not None
                    else "⚠️ 재확인 필요 — 아래 새 메시지를 확인해 주세요."
                ),
            )
        publication = await publish_approval_messages(
            notifier=notifier,
            messages=messages,
            chat_id=str(chat_id),
        )
        dispatch_result = await service.finish_approval_dispatch(
            proposal_id,
            attempt_id=dispatch_attempt_id,
            publication=publication,
            chat_id=str(chat_id),
            now=send_window.observed_at,
            approval_window_policy_stamp=send_window.policy_stamp,
        )
        await session.commit()
        operator_alert = None
        if not dispatch_result.ok:
            operator_alert = await _alert_non_sent_callback_dispatch(
                proposal_id,
                dispatch_state=dispatch_result.state.value,
                dispatch_failure_code=(
                    dispatch_result.failure_code or "approval_dispatch_failed"
                ),
                now=send_window.observed_at,
                service_factory=service_factory,
            )
        new_message_id = dispatch_result.message_id if dispatch_result.ok else None
        return {
            "handled": True,
            "reason": "needs_reconfirm",
            "proposal_id": str(proposal_id),
            "new_message_id": new_message_id,
            "approval_dispatch": dispatch_result.as_dict(),
            "operator_alert": operator_alert,
            "results": [outcome.result for outcome in outcomes],
            "rung_results": _serialize_rung_outcomes(outcomes),
        }

    summary = _build_result_summary(outcomes)

    # Commit record_approval + revalidate_and_submit's rung-state
    # transitions (acked/resting/unverified/rejected) before any Telegram
    # call -- same rationale as the reconfirm branch above.
    await session.commit()

    if message_id is not None:
        await _safe_edit_message(notifier, chat_id, message_id, summary)
    return {
        "handled": not all_window_blocked,
        "reason": (
            str(
                (window_blocked[0].detail or {}).get("error")
                or "approval_window_blocked"
            )
            if all_window_blocked
            else "approved_with_window_block"
            if window_blocked
            else "approved"
        ),
        "proposal_id": str(proposal_id),
        "results": [outcome.result for outcome in outcomes],
        "rung_results": _serialize_rung_outcomes(outcomes),
    }


async def _handle_batch_approve(
    *,
    service_factory: ServiceFactory,
    batch_short: str,
    callback: CallbackEnvelope,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None = None,
    telegram_user_id: str,
    revalidate_fn: RevalidateFn,
    window_evaluator: WindowEvaluator,
    now_fn: Clock,
) -> dict[str, Any]:
    """Consume one batch trigger and process every frozen member independently."""
    async with service_factory() as session:
        service = OrderProposalsService(session)
        await service.acquire_approval_batch_chat_lock(str(chat_id))
        batch_id = await service.resolve_approval_batch_id_prefix(batch_short)
        if batch_id is None:
            await session.commit()
            return {"handled": False, "reason": "approval_batch_not_found"}

        gate_now = now_fn()
        try:
            # Validate the #1646 publication owner/membership/nonce snapshot
            # before any calendar lookup or Telegram callback answer. An
            # expired batch may proceed only far enough to converge an expired
            # member; the consuming gate below still rejects the batch TTL.
            await service.preflight_published_batch_callback(
                batch_id,
                callback=callback,
                chat_id=str(chat_id),
                now=gate_now,
                allow_expired=True,
            )
        except OrderProposalError as exc:
            await session.commit()
            return {"handled": False, "reason": str(exc)}

        batch, proposals = await service.get_approval_batch_display(
            batch_id,
            for_update=True,
        )
        gate_now = now_fn()
        batch_expired = gate_now >= batch.expires_at
        if len(proposals) < 2:
            await session.commit()
            return {"handled": False, "reason": "approval_batch_too_small"}
        member_expired = False
        for group, _rungs in proposals:
            validity_block = valid_until_block(group.valid_until, now=gate_now)
            if (
                validity_block is not None
                and validity_block[0] is ApprovalWindowCode.EXPIRED
            ):
                member_expired = True
                break
        if batch_expired and not member_expired:
            # A stale batch TTL must not produce Telegram/provider side
            # effects. Continue only when the batch deadline was bounded by a
            # member's valid_until so that member can durably converge to
            # expired without consuming either nonce.
            await session.commit()
            return {"handled": False, "reason": "approval_batch_expired"}
        await _safe_answer(notifier, callback_query_id, "처리 중")

        preflight: list[tuple[Any, list[Any], str | None, ApprovalWindowDecision]] = []
        batch_window_blocked = False
        for group, rungs in proposals:
            block_reason = batch_member_block_reason(group, rungs, now=gate_now)
            decision = await _evaluate_bound_window(
                group, window_evaluator=window_evaluator, now_fn=now_fn
            )
            gate_now = decision.observed_at
            if block_reason is not None or not decision.allowed:
                batch_window_blocked = True
            preflight.append((group, rungs, block_reason, decision))

        if not batch_window_blocked:
            # The calendar lookups above are awaited member-by-member. Repeat
            # the exact frozen set at the nonce boundary so an open/expiry
            # boundary crossed while evaluating a large batch cannot consume
            # the batch trigger on stale evidence.
            gate_now = now_fn()
            preflight = []
            for group, rungs in proposals:
                block_reason = batch_member_block_reason(
                    group,
                    rungs,
                    now=gate_now,
                )
                decision = await _evaluate_bound_window(
                    group, window_evaluator=window_evaluator, now_fn=now_fn
                )
                gate_now = decision.observed_at
                if block_reason is not None or not decision.allowed:
                    batch_window_blocked = True
                preflight.append((group, rungs, block_reason, decision))

        gate_now = now_fn()
        preflight = [
            (
                group,
                rungs,
                batch_member_block_reason(group, rungs, now=gate_now),
                recheck_approval_window_decision(group, decision, now=gate_now),
            )
            for group, rungs, _block_reason, decision in preflight
        ]
        batch_window_blocked = any(
            block_reason is not None or not decision.allowed
            for _group, _rungs, block_reason, decision in preflight
        )

        if batch_window_blocked:
            blocked_results: list[dict[str, Any]] = []
            for group, rungs, block_reason, decision in preflight:
                if decision.code is ApprovalWindowCode.EXPIRED:
                    await service.expire_mutable_rungs_if_needed(
                        group.proposal_id,
                        now=gate_now,
                    )
                if block_reason is not None:
                    reason = block_reason
                    rung_results: list[dict[str, Any]] = []
                elif not decision.allowed:
                    reason = approval_window_operator_text(decision)
                    rung_result = approval_window_rung_result(decision)
                    rung_results = [
                        {"rung_index": rung.rung_index, "result": rung_result}
                        for rung in rungs
                        if rung.state in {"pending_approval", "needs_reconfirm"}
                    ]
                else:
                    reason = "batch_atomic_window_block"
                    rung_results = []
                blocked_results.append(
                    {
                        "proposal_id": str(group.proposal_id),
                        "status": "skipped",
                        "reason": reason,
                        "rung_results": rung_results,
                    }
                )
            await session.commit()
            if message_id is not None:
                await _safe_edit_message(
                    notifier,
                    chat_id,
                    message_id,
                    build_batch_result_message(
                        proposals=proposals, results=blocked_results
                    ),
                    reply_markup={"inline_keyboard": []},
                )
            return {
                "handled": False,
                "reason": "BATCH_WINDOW_BLOCKED",
                "batch_id": str(batch_id),
                "results": blocked_results,
            }

        if batch_expired:
            await session.commit()
            if message_id is not None:
                await _safe_edit_message(
                    notifier,
                    chat_id,
                    message_id,
                    "⌛ 일괄 승인 만료",
                    reply_markup={"inline_keyboard": []},
                )
            return {"handled": False, "reason": "approval_batch_expired"}

        try:
            _batch, members = await service.consume_approval_batch_nonce(
                batch_id,
                callback=callback,
                chat_id=str(chat_id),
                telegram_user_id=telegram_user_id,
                now=gate_now,
                expected_members=tuple(
                    (group.proposal_id, str(group.approval_nonce or ""))
                    for group, _rungs in proposals
                ),
            )
        except OrderProposalError as exc:
            await session.commit()
            if str(exc) == "approval_batch_expired" and message_id is not None:
                await _safe_edit_message(
                    notifier,
                    chat_id,
                    message_id,
                    "⌛ 일괄 승인 만료",
                    reply_markup={"inline_keyboard": []},
                )
            return {"handled": False, "reason": str(exc)}

        # Commit the single-use batch trigger before touching any member. A
        # crash or Telegram retry can then never execute the frozen set twice.
        await session.commit()

    now = gate_now
    results: list[dict[str, Any]] = []
    for member in members:
        member_result: dict[str, Any] = {
            "proposal_id": str(member.proposal_id),
            "status": "failed",
        }
        member_message: str | None = None
        async with service_factory() as member_session:
            member_service = OrderProposalsService(member_session)
            try:
                group, rungs = await member_service.get_proposal(member.proposal_id)
                block_reason = batch_member_block_reason(group, rungs, now=now)
                if block_reason is not None:
                    member_result.update(status="skipped", reason=block_reason)
                    member_message = (
                        f"⚠️ 일괄 승인 제외 — {_escape_markdown(block_reason)}"
                    )
                else:
                    approval_result = await _handle_approve(
                        session=member_session,
                        service=member_service,
                        proposal_id=member.proposal_id,
                        callback=CallbackEnvelope(
                            action="op",
                            subject_short=str(member.proposal_id)[:8],
                            attempt_id=member.dispatch_binding.attempt_id,
                            membership_revision=(
                                member.dispatch_binding.membership_revision
                            ),
                            membership_digest=(
                                member.dispatch_binding.membership_digest
                            ),
                            nonce=member.approval_nonce,
                        ),
                        now=now,
                        notifier=notifier,
                        chat_id=chat_id,
                        message_id=member.approval_message_id,
                        callback_query_id=None,
                        telegram_user_id=telegram_user_id,
                        revalidate_fn=revalidate_fn,
                        window_evaluator=window_evaluator,
                        now_fn=now_fn,
                        service_factory=service_factory,
                    )
                    rung_results = approval_result.get("rung_results")
                    if isinstance(rung_results, list):
                        member_result["rung_results"] = [
                            {
                                "rung_index": int(value["rung_index"]),
                                "result": str(value["result"]),
                            }
                            for value in rung_results
                            if isinstance(value, Mapping)
                            and "rung_index" in value
                            and "result" in value
                        ]
                    status, reason = _classify_batch_approval_result(approval_result)
                    member_result["status"] = status
                    operator_reason = approval_result.get("operator_reason")
                    if operator_reason:
                        reason = str(operator_reason)
                    if reason is not None:
                        member_result["reason"] = reason
                    if not approval_result.get("handled"):
                        member_message = (
                            f"⚠️ 일괄 승인 제외 — {_escape_markdown(reason)}"
                        )
            except Exception as exc:  # noqa: BLE001 - isolate each batch member
                logger.error(
                    "order_proposals.batch_member.approval_failed",
                    extra={
                        "proposal_id": str(member.proposal_id),
                        "exception_type": type(exc).__name__,
                    },
                )
                await member_session.rollback()
                member_result.update(status="failed", reason="internal_error")
                member_message = (
                    "❌ 일괄 승인 처리 실패 — 단건 승인을 다시 확인해 주세요."
                )

            # Record the member outcome before any batch-owned Telegram edit.
            # `_handle_approve` already commits its own broker/proposal work.
            try:
                await member_service.record_approval_batch_member_result(
                    member.member_id,
                    result=str(member_result["status"]),
                    detail={
                        "proposal_id": member_result["proposal_id"],
                        "reason": member_result.get("reason", ""),
                        "rung_results": ",".join(
                            f"{value['rung_index']}:{value['result']}"
                            for value in member_result.get("rung_results", [])
                        ),
                    },
                    now=now,
                )
                await member_session.commit()
            except Exception as exc:  # noqa: BLE001 - observation must not stop the batch
                logger.error(
                    "order_proposals.batch_member.result_record_failed",
                    extra={
                        "proposal_id": str(member.proposal_id),
                        "exception_type": type(exc).__name__,
                    },
                )
                await member_session.rollback()

        if member_message is not None:
            await _safe_edit_message(
                notifier,
                chat_id,
                member.approval_message_id,
                member_message,
                reply_markup={"inline_keyboard": []},
            )
        results.append(member_result)

    async with service_factory() as display_session:
        display_service = OrderProposalsService(display_session)
        _batch, proposals = await display_service.get_approval_batch_display(batch_id)
        await display_session.commit()
    if message_id is not None:
        await _safe_edit_message(
            notifier,
            chat_id,
            message_id,
            build_batch_result_message(proposals=proposals, results=results),
            reply_markup={"inline_keyboard": []},
        )
    return {
        "handled": True,
        "reason": "batch_approved",
        "batch_id": str(batch_id),
        "results": results,
    }


async def _handle_loss_cut_first_click(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    callback: CallbackEnvelope,
    now: datetime,
    notifier: Any,
    chat_id: Any,
    message_id: int | None,
    callback_query_id: str | None = None,
    telegram_user_id: str,
    loss_cut_preview_fn: RevalidateFn,
    window_evaluator: WindowEvaluator | None = None,
    now_fn: Clock | None = None,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> dict[str, Any]:
    """Consume step one and edit the message into a bound confirmation."""
    window_evaluator = window_evaluator or evaluate_approval_window
    now_fn = now_fn or (lambda: now)
    preflight_failure = await _preflight_proposal_callback(
        session=session,
        service=service,
        proposal_id=proposal_id,
        callback=callback,
        notifier=notifier,
        callback_query_id=callback_query_id,
    )
    if preflight_failure is not None:
        return preflight_failure

    group = await service.preflight_published_proposal_callback(
        proposal_id,
        callback=callback,
    )
    window = await _evaluate_bound_window(
        group, window_evaluator=window_evaluator, now_fn=now_fn
    )
    click_now = window.observed_at
    if not window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=window,
            now=click_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=None,
        )

    # Repeat immediately before the external preview. The first calendar
    # resolution itself may have crossed a validity/session boundary.
    window = await _evaluate_bound_window(
        group, window_evaluator=window_evaluator, now_fn=now_fn
    )
    click_now = window.observed_at
    if not window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=window,
            now=click_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=None,
        )

    submit_agent_id = settings.ORDER_PROPOSALS_SUBMIT_AGENT_ID.strip() or None
    caller_agent_id_token = caller_agent_id_var.set(submit_agent_id)
    try:
        evidence = await loss_cut_preview_fn(
            service=service, proposal_id=proposal_id, now=click_now
        )
    finally:
        caller_agent_id_var.reset(caller_agent_id_token)
    try:
        group = await service.preflight_published_proposal_callback(
            proposal_id,
            callback=callback,
        )
    except OrderProposalError as exc:
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}
    post_preview_window = await _evaluate_bound_window(
        group,
        window_evaluator=window_evaluator,
        now_fn=now_fn,
    )
    post_preview_now = post_preview_window.observed_at
    if not post_preview_window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=post_preview_window,
            now=post_preview_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=None,
        )

    post_preview_window = await _evaluate_bound_window(
        group,
        window_evaluator=window_evaluator,
        now_fn=now_fn,
    )
    post_preview_now = post_preview_window.observed_at
    if not post_preview_window.allowed:
        return await _reject_window_callback(
            session=session,
            service=service,
            proposal_id=proposal_id,
            decision=post_preview_window,
            now=post_preview_now,
            notifier=notifier,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=None,
        )
    try:
        await service.consume_published_proposal_callback(
            proposal_id,
            callback=callback,
            now=post_preview_now,
        )
    except OrderProposalError as exc:
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    confirmation_nonce = _generate_nonce()
    await service.issue_loss_cut_confirmation(
        proposal_id,
        first_nonce=callback.nonce,
        confirmation_nonce=confirmation_nonce,
        telegram_user_id=telegram_user_id,
        now=post_preview_now,
    )
    group, rungs = await service.get_proposal(proposal_id)
    dispatch_attempt_id = uuid.uuid4()
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=confirmation_nonce,
        attempt_id=dispatch_attempt_id,
        card_kind=ApprovalCardKind.LOSS_CUT_CONFIRMATION,
        current_membership_revision=group.approval_dispatch_membership_revision,
    )
    text, keyboard = build_loss_cut_confirmation_message(
        group=group, rungs=rungs, evidence=evidence, binding=binding
    )
    publish_window = await _evaluate_bound_window(
        group,
        window_evaluator=window_evaluator,
        now_fn=now_fn,
    )
    if not publish_window.allowed:
        if publish_window.code is ApprovalWindowCode.EXPIRED:
            await service.expire_mutable_rungs_if_needed(
                proposal_id,
                now=publish_window.observed_at,
            )
        await service.restore_approval_after_window_block(
            proposal_id,
            nonce=confirmation_nonce,
            expired=publish_window.code is ApprovalWindowCode.EXPIRED,
        )
        if publish_window.code is not ApprovalWindowCode.EXPIRED:
            await service.clear_approval_nonce(
                proposal_id,
                expected_nonce=confirmation_nonce,
            )
        await session.commit()
        rejection_text = approval_window_operator_text(publish_window)
        if message_id is not None:
            await _safe_edit_message(
                notifier,
                chat_id,
                message_id,
                rejection_text,
                reply_markup={"inline_keyboard": []},
            )
        return {
            "handled": False,
            "reason": publish_window.code.value,
            "proposal_id": str(proposal_id),
            "approval_window": publish_window.to_dict(),
        }

    await service.start_approval_dispatch(
        proposal_id,
        attempt_id=dispatch_attempt_id,
        binding=binding,
        now=publish_window.observed_at,
        payload_chars=telegram_text_length(text),
        context_message_count=0,
    )
    await session.commit()

    if message_id is None:
        publication = ApprovalPublication.failed(
            payload_chars=telegram_text_length(text),
            failure_code="approval_edit_message_missing",
        )
    else:
        method_result = await _safe_edit_message(
            notifier,
            chat_id,
            message_id,
            text,
            reply_markup=keyboard,
        )
        publication = (
            ApprovalPublication.published(
                payload_chars=telegram_text_length(text),
                method_result=method_result,
            )
            if method_result.ok
            else ApprovalPublication.failed(
                payload_chars=telegram_text_length(text),
                failure_code="approval_card_edit_failed",
                method_result=method_result,
            )
        )
    dispatch_result = await service.finish_approval_dispatch(
        proposal_id,
        attempt_id=dispatch_attempt_id,
        publication=publication,
        chat_id=str(chat_id),
        now=publish_window.observed_at,
        approval_window_policy_stamp=publish_window.policy_stamp,
    )
    await session.commit()
    operator_alert = None
    if not dispatch_result.ok:
        operator_alert = await _alert_non_sent_callback_dispatch(
            proposal_id,
            dispatch_state=dispatch_result.state.value,
            dispatch_failure_code=(
                dispatch_result.failure_code or "approval_dispatch_failed"
            ),
            now=publish_window.observed_at,
            service_factory=service_factory,
        )
    return {
        "handled": dispatch_result.ok,
        "reason": (
            "loss_cut_confirmation_required"
            if dispatch_result.ok
            else "loss_cut_confirmation_dispatch_failed"
        ),
        "proposal_id": str(proposal_id),
        "approval_dispatch": dispatch_result.as_dict(),
        "operator_alert": operator_alert,
    }


async def handle_callback_update(
    update: dict[str, Any],
    *,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
    notifier: Any = None,
    revalidate_fn: RevalidateFn = revalidate_and_submit,
    loss_cut_preview_fn: RevalidateFn | None = None,
    veto_cancel_fn: TargetCancelFn = cancel_target_order,
    veto_fetch_fn: TargetFetchFn = fetch_target_order,
    veto_toss_reconcile_fn: TossVetoReconcileFn = reconcile_toss_auto_veto_terminal,
    window_evaluator: WindowEvaluator | None = None,
    now_fn: Clock | None = None,
) -> dict[str, Any]:
    """Handle one Telegram webhook update. Never raises (fail-closed)."""
    callback_query_id: str | None = None
    active_notifier = notifier
    evaluate_window = window_evaluator or evaluate_approval_window
    clock = now_fn or (lambda: now)
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
            return {"handled": False, "reason": "chat_not_allowed"}

        try:
            callback = parse_callback_data(data)
        except ValueError:
            return {"handled": False, "reason": "malformed_callback_data"}

        if callback.action == "ba":
            return await _handle_batch_approve(
                service_factory=service_factory,
                batch_short=callback.subject_short,
                callback=callback,
                now=now,
                notifier=active_notifier,
                chat_id=chat_id,
                message_id=message_id,
                callback_query_id=callback_query_id,
                telegram_user_id=(
                    str(telegram_user_id) if telegram_user_id is not None else ""
                ),
                revalidate_fn=revalidate_fn,
                window_evaluator=evaluate_window,
                now_fn=clock,
            )

        async with service_factory() as session:
            service = OrderProposalsService(session)
            proposal_id = await _resolve_proposal_id(service, callback.subject_short)
            if proposal_id is None:
                await session.commit()
                return {"handled": False, "reason": "proposal_not_found"}

            if callback.action == "vc":
                result = await _handle_auto_veto(
                    session=session,
                    service=service,
                    proposal_id=proposal_id,
                    callback=callback,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                    telegram_user_id=(
                        str(telegram_user_id) if telegram_user_id is not None else ""
                    ),
                    cancel_fn=veto_cancel_fn,
                    fetch_fn=veto_fetch_fn,
                    toss_reconcile_fn=veto_toss_reconcile_fn,
                )
            elif callback.action == "dn":
                result = await _handle_deny(
                    session=session,
                    service=service,
                    proposal_id=proposal_id,
                    callback=callback,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                )
            elif callback.action == "op":
                group, _rungs = await service.get_proposal(proposal_id)
                if group.exit_intent == "loss_cut":
                    if loss_cut_preview_fn is None:
                        from app.services.order_proposals.revalidation import (
                            preview_loss_cut_confirmation,
                        )

                        loss_cut_preview_fn = preview_loss_cut_confirmation
                    result = await _handle_loss_cut_first_click(
                        session=session,
                        service=service,
                        proposal_id=proposal_id,
                        callback=callback,
                        now=now,
                        notifier=active_notifier,
                        chat_id=chat_id,
                        message_id=message_id,
                        callback_query_id=callback_query_id,
                        telegram_user_id=(
                            str(telegram_user_id)
                            if telegram_user_id is not None
                            else ""
                        ),
                        loss_cut_preview_fn=loss_cut_preview_fn,
                        window_evaluator=evaluate_window,
                        now_fn=clock,
                        service_factory=service_factory,
                    )
                    return result
                result = await _handle_approve(
                    session=session,
                    service=service,
                    proposal_id=proposal_id,
                    callback=callback,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                    telegram_user_id=(
                        str(telegram_user_id) if telegram_user_id is not None else ""
                    ),
                    revalidate_fn=revalidate_fn,
                    window_evaluator=evaluate_window,
                    now_fn=clock,
                    service_factory=service_factory,
                )
            else:
                result = await _handle_approve(
                    session=session,
                    service=service,
                    proposal_id=proposal_id,
                    callback=callback,
                    now=now,
                    notifier=active_notifier,
                    chat_id=chat_id,
                    message_id=message_id,
                    callback_query_id=callback_query_id,
                    telegram_user_id=(
                        str(telegram_user_id) if telegram_user_id is not None else ""
                    ),
                    revalidate_fn=revalidate_fn,
                    window_evaluator=evaluate_window,
                    now_fn=clock,
                    loss_cut_confirmation=True,
                    service_factory=service_factory,
                )
            # `_handle_deny`/`_handle_approve` each commit their own
            # mutating work internally before making any Telegram notify
            # call (see module docstring: commit-before-notify ordering) --
            # no end-of-function commit here.
            return result
    except Exception as exc:  # noqa: BLE001 - fail-closed webhook contract
        logger.error(
            "order_proposals.telegram.callback_handling_failed",
            extra={"exception_type": type(exc).__name__},
        )
        return {"handled": False, "reason": "internal_error"}
