"""Send the initial Telegram approval message for a proposal (ROB-816 PR 2).

``send_proposal_for_approval`` is a top-level caller module, same as
``telegram_callback.py`` -- it opens and COMMITS its own DB session rather
than being constructor-injected, because it (a) is invoked from
``order_proposal_create`` after that tool's own session has already closed
and committed, and (b) calls the Telegram notifier, which
``OrderProposalsService``/``OrderProposalRepository`` never do (they only
flush -- see ``service.py``'s module docstring).

Each individual dispatch commits its fresh nonce and pending attempt before
Telegram I/O, then finalizes that attempt in a separate transaction. The
Telegram message ID necessarily arrives between those transactions. Derived
batch membership is frozen and committed before publication. Published batches
are immutable; later proposals always stage a new card.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.approval_message import (
    ApprovalDispatchMessages,
    build_approval_dispatch_messages,
    build_batch_approval_message,
)
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    ApprovalWindowDecision,
    WindowEvaluator,
    evaluate_approval_window,
    evaluate_approval_window_boundary,
    recheck_approval_window_decision,
)
from app.services.order_proposals.auto_approve import (
    auto_veto_thesis_summary,
    build_auto_approved_message,
    evaluate_auto_approve_eligibility,
    limits_for_market,
)
from app.services.order_proposals.auto_approve_audit import (
    build_auto_approve_rejection_card_block,
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
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    ApprovalPublication,
    DispatchBinding,
    TelegramDispatchResult,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import OrderProposalsService
from app.telegram_contract import (
    TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
    TelegramErrorClassification,
    TelegramMethodResult,
    telegram_text_length,
)

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]
RevalidateFn = Callable[..., Any]
Clock = Callable[[], datetime]


def _mirror_decimal_text(value: Any) -> str:
    """Keep Discord card quantities/prices legible after DB numeric coercion."""
    try:
        normalized = format(Decimal(str(value)).normalize(), "f")
    except Exception:  # noqa: BLE001 - display degradation must not raise
        return str(value)
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


async def _mirror_auto_veto_card(
    *,
    notifier: Any,
    group: Any,
    rungs: list[Any],
    policy_version: str,
) -> bool:
    """Mirror the already-published auto-veto card through TradeNotifier.

    Telegram remains the action surface.  The Discord delivery is a best-effort
    operational mirror and cannot turn an otherwise valid order into a false
    cancellation state.  A missing thesis is treated as a failed mirror too,
    although the eligibility gate prevents that case before broker submit.
    """
    thesis_summary = auto_veto_thesis_summary(group)
    if thesis_summary is None:
        logger.error(
            "order_proposals.auto_veto_discord_mirror_skipped_missing_thesis",
            extra={"proposal_id": str(group.proposal_id)},
        )
        return False
    sender = getattr(notifier, "send_auto_veto_card_mirror", None)
    if not callable(sender):
        logger.warning(
            "order_proposals.auto_veto_discord_mirror_unavailable",
            extra={"proposal_id": str(group.proposal_id)},
        )
        return False
    try:
        return bool(
            await sender(
                symbol=group.symbol,
                market=group.market,
                quantities=[_mirror_decimal_text(rung.quantity) for rung in rungs],
                prices=[_mirror_decimal_text(rung.limit_price) for rung in rungs],
                thesis_summary=thesis_summary,
                policy_version=policy_version,
            )
        )
    except Exception:  # noqa: BLE001 - an alert mirror cannot roll back a card
        logger.exception(
            "order_proposals.auto_veto_discord_mirror_failed",
            extra={"proposal_id": str(group.proposal_id)},
        )
        return False


def _generate_nonce() -> str:
    # Duplicated from telegram_callback.py::_generate_nonce (2 lines) rather
    # than imported -- that name is `_`-prefixed/module-private, and this
    # module is a peer top-level caller, not a consumer of that module.
    return secrets.token_urlsafe(8)


def approval_window_failure_code(decision: ApprovalWindowDecision) -> str:
    """Return the stable dispatch-ledger code for a blocked approval window."""
    evidence_detail = (
        decision.evidence.detail if decision.evidence is not None else None
    )
    detail = evidence_detail or decision.detail
    return f"{decision.code.value}/{detail or 'unspecified'}"


def _proposal_binding(
    *,
    group: Any,
    nonce: str | None,
    attempt_id: uuid.UUID,
    card_kind: ApprovalCardKind,
) -> DispatchBinding:
    return build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=nonce,
        attempt_id=attempt_id,
        card_kind=card_kind,
        current_membership_revision=group.approval_dispatch_membership_revision,
    )


async def _record_approval_window_block(
    *,
    service: OrderProposalsService,
    group: Any,
    decision: ApprovalWindowDecision,
    attempt_id: uuid.UUID,
    binding: DispatchBinding | None = None,
) -> TelegramDispatchResult:
    """Ledger a fail-closed window decision before returning it to the caller."""
    resolved_binding = binding or _proposal_binding(
        group=group,
        nonce=group.approval_nonce,
        attempt_id=attempt_id,
        card_kind=ApprovalCardKind.MANUAL,
    )
    publication = ApprovalPublication.failed(
        payload_chars=0,
        failure_code=approval_window_failure_code(decision),
    )
    await service.start_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        binding=resolved_binding,
        now=decision.observed_at,
        payload_chars=0,
        context_message_count=0,
    )
    return await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        publication=publication,
        chat_id=None,
        now=decision.observed_at,
        approval_window_policy_stamp=decision.policy_stamp,
    )


async def _register_and_publish_batch_summary(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    message_id: int,
    chat_id: str,
    now: datetime,
    notifier: Any,
    window_evaluator: WindowEvaluator,
    now_fn: Clock,
) -> None:
    """Publish a new immutable batch card after freezing its exact members."""
    registration = await service.register_approval_batch_member(
        proposal_id,
        chat_id=chat_id,
        approval_message_id=message_id,
        now=now,
    )
    if (
        registration is None
        or registration.summary_action == "none"
        or registration.binding is None
    ):
        return
    # Make the frozen membership and summary-delivery claim visible before
    # publishing a button that depends on both rows.
    await session.commit()
    batch, proposals = await service.get_approval_batch_display(
        registration.batch.batch_id,
        for_update=True,
    )
    decisions: list[tuple[Any, ApprovalWindowDecision]] = []
    for group, _rungs in proposals:
        expected = (group.source_asof or {}).get("approval_window_policy_stamp")
        decision = await evaluate_approval_window_boundary(
            group,
            window_evaluator=window_evaluator,
            now_fn=now_fn,
            expected_policy_stamp=str(expected) if expected is not None else None,
        )
        if not decision.allowed:
            if decision.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    group.proposal_id,
                    now=decision.observed_at,
                )
            await service.release_approval_batch_summary_claim(
                batch.batch_id,
                now=decision.observed_at,
            )
            await session.commit()
            return
        decisions.append((group, decision))

    final_now = now_fn()
    for group, decision in decisions:
        final_decision = recheck_approval_window_decision(
            group,
            decision,
            now=final_now,
        )
        if not final_decision.allowed:
            if final_decision.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    group.proposal_id,
                    now=final_decision.observed_at,
                )
            await service.release_approval_batch_summary_claim(
                batch.batch_id,
                now=final_decision.observed_at,
            )
            await session.commit()
            return

    text, keyboard = build_batch_approval_message(
        batch=batch,
        proposals=proposals,
        binding=registration.binding,
    )
    messages = ApprovalDispatchMessages(
        context_messages=(),
        approval_text=text,
        inline_keyboard=keyboard,
        payload_chars=telegram_text_length(text),
    )
    await service.record_approval_batch_payload(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        payload_chars=messages.payload_chars,
    )
    # The immutable member set, pending owner, and exact payload are durable
    # before publication, matching the individual dispatch attempt contract.
    await session.commit()

    publish_now = now_fn()
    for group, decision in decisions:
        final_decision = recheck_approval_window_decision(
            group,
            decision,
            now=publish_now,
        )
        if not final_decision.allowed:
            if final_decision.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    group.proposal_id,
                    now=final_decision.observed_at,
                )
            await service.release_approval_batch_summary_claim(
                batch.batch_id,
                now=final_decision.observed_at,
            )
            await session.commit()
            return

    publication = await publish_approval_messages(
        notifier=notifier,
        messages=messages,
        chat_id=chat_id,
    )
    await service.finish_approval_batch_dispatch(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=publication,
        now=publish_now,
    )


async def send_proposal_for_approval(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
    window_evaluator: WindowEvaluator | None = None,
    now_fn: Clock | None = None,
    redispatch: bool = False,
) -> TelegramDispatchResult | ApprovalWindowDecision:
    """Mint a fresh approval nonce, render the message, and send it.

    Sends to the FIRST entry in
    ``settings.order_proposals_telegram_chat_allowlist``. A fail-closed
    approval-window preflight returns its typed decision without minting a
    nonce or publishing a card. An empty allowlist is recorded as a durable
    typed dispatch failure without minting a nonce.
    """
    allowlist = settings.order_proposals_telegram_chat_allowlist
    if not allowlist:
        publication = ApprovalPublication.failed(
            payload_chars=0,
            failure_code="telegram_allowlist_empty",
        )
        return await record_approval_dispatch_failure(
            proposal_id,
            publication=publication,
            now=now,
            service_factory=service_factory,
        )
    chat_id = allowlist[0]
    attempt_id = uuid.uuid4()

    async with service_factory() as session:
        service = OrderProposalsService(session)
        group, _rungs = await service.get_proposal(proposal_id)
        evaluate_window = window_evaluator or evaluate_approval_window
        clock = now_fn or (lambda: now)
        window = await evaluate_approval_window_boundary(
            group,
            window_evaluator=evaluate_window,
            now_fn=clock,
            require_policy_stamp=False,
        )
        observed_now = window.observed_at
        if not window.allowed:
            await _record_approval_window_block(
                service=service,
                group=group,
                decision=window,
                attempt_id=attempt_id,
            )
            if window.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    proposal_id, now=observed_now
                )
            await session.commit()
            return window

        # Calendar resolution above may await broker/exchange evidence. Sample
        # the clock and policy again at the actual nonce/card boundary so a
        # validity or session edge crossed during that I/O cannot mint a
        # nonce or publish an already-stale button.
        window = await evaluate_approval_window_boundary(
            group,
            window_evaluator=evaluate_window,
            now_fn=clock,
            require_policy_stamp=False,
        )
        publish_now = window.observed_at
        if not window.allowed:
            await _record_approval_window_block(
                service=service,
                group=group,
                decision=window,
                attempt_id=attempt_id,
            )
            if window.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    proposal_id, now=publish_now
                )
            await session.commit()
            return window

        fresh_nonce = _generate_nonce()
        if redispatch:
            await service.set_approval_nonce(
                proposal_id,
                fresh_nonce,
                require_redispatchable=True,
            )
        else:
            await service.set_approval_nonce(proposal_id, fresh_nonce)

        group, rungs = await service.get_proposal(proposal_id)
        binding = _proposal_binding(
            group=group,
            nonce=fresh_nonce,
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.MANUAL,
        )
        messages = build_approval_dispatch_messages(
            group=group,
            rungs=rungs,
            suffix_blocks=(
                (rejection_block,)
                if (
                    rejection_block := build_auto_approve_rejection_card_block(
                        group.source_asof
                    )
                )
                is not None
                else ()
            ),
            binding=binding,
        )

        send_window = await evaluate_approval_window_boundary(
            group,
            window_evaluator=evaluate_window,
            now_fn=clock,
            expected_policy_stamp=window.policy_stamp,
        )
        if not send_window.allowed:
            await _record_approval_window_block(
                service=service,
                group=group,
                decision=send_window,
                attempt_id=attempt_id,
                binding=binding,
            )
            if send_window.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(
                    proposal_id,
                    now=send_window.observed_at,
                )
            await session.commit()
            return send_window

        await service.start_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            binding=binding,
            now=send_window.observed_at,
            payload_chars=messages.payload_chars,
            context_message_count=len(messages.context_messages),
        )
        # The nonce and pending attempt become durable before Telegram I/O.
        await session.commit()

    publication = await publish_approval_messages(
        notifier=notifier,
        messages=messages,
        chat_id=chat_id,
    )

    async with service_factory() as session:
        service = OrderProposalsService(session)
        result = await service.finish_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            publication=publication,
            chat_id=chat_id,
            now=send_window.observed_at,
            approval_window_policy_stamp=send_window.policy_stamp,
        )
        await session.commit()
        if result.approvable and result.message_id is not None:
            await _register_and_publish_batch_summary(
                session=session,
                service=service,
                proposal_id=proposal_id,
                message_id=result.message_id,
                chat_id=chat_id,
                now=send_window.observed_at,
                notifier=notifier,
                window_evaluator=evaluate_window,
                now_fn=clock,
            )
            await session.commit()
    return result


async def publish_approval_messages(
    *,
    notifier: Any,
    messages: ApprovalDispatchMessages,
    chat_id: str,
) -> ApprovalPublication:
    """Send every context successfully before publishing the button card."""
    all_messages = (*messages.context_messages, messages.approval_text)
    if any(
        telegram_text_length(text) > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
        for text in all_messages
    ):
        return ApprovalPublication.failed(
            payload_chars=messages.payload_chars,
            failure_code="approval_payload_too_long",
        )

    successful_contexts = 0
    for context_text in messages.context_messages:
        try:
            context_result = await notifier.send_approval_message(
                context_text,
                None,
                chat_id=chat_id,
                parse_mode=None,
            )
        except Exception:  # noqa: BLE001 - converted to a closed safe result
            context_result = TelegramMethodResult.failed(
                payload_chars=telegram_text_length(context_text),
                failure_code="telegram_transport_error",
                error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
            )
        if not context_result.ok:
            return ApprovalPublication.failed(
                payload_chars=messages.payload_chars,
                failure_code="approval_context_dispatch_failed",
                partial=successful_contexts > 0,
                method_result=context_result,
            )
        successful_contexts += 1

    try:
        card_result = await notifier.send_approval_message(
            messages.approval_text,
            messages.inline_keyboard,
            chat_id=chat_id,
        )
    except Exception:  # noqa: BLE001 - converted to a closed safe result
        card_result = TelegramMethodResult.failed(
            payload_chars=telegram_text_length(messages.approval_text),
            failure_code="telegram_transport_error",
            error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
        )
    if not card_result.ok:
        return ApprovalPublication.failed(
            payload_chars=messages.payload_chars,
            failure_code="approval_card_dispatch_failed",
            partial=successful_contexts > 0,
            method_result=card_result,
        )
    return ApprovalPublication.published(
        payload_chars=messages.payload_chars,
        method_result=card_result,
    )


async def record_approval_dispatch_failure(
    proposal_id: uuid.UUID,
    *,
    publication: ApprovalPublication,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> TelegramDispatchResult:
    """Ledger a local/preflight dispatch failure with no Telegram I/O."""
    if publication.card_published:
        raise ValueError("record_approval_dispatch_failure requires a failed receipt")
    attempt_id = uuid.uuid4()
    async with service_factory() as session:
        service = OrderProposalsService(session)
        group, _rungs = await service.get_proposal(proposal_id)
        binding = _proposal_binding(
            group=group,
            nonce=group.approval_nonce,
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.MANUAL,
        )
        await service.start_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            binding=binding,
            now=now,
            payload_chars=publication.payload_chars,
            context_message_count=0,
        )
        result = await service.finish_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            publication=publication,
            chat_id=None,
            now=now,
        )
        await session.commit()
    return result


async def dispatch_proposal(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
    revalidate_fn: RevalidateFn = revalidate_and_submit,
    cancel_target_fn: TargetCancelFn = cancel_target_order,
    fetch_target_fn: TargetFetchFn = fetch_target_order,
    toss_veto_reconcile_fn: TossVetoReconcileFn = reconcile_toss_auto_veto_terminal,
    window_evaluator: WindowEvaluator | None = None,
    now_fn: Clock | None = None,
) -> TelegramDispatchResult | ApprovalWindowDecision:
    """Auto-submit an eligible resting proposal, otherwise send for approval."""
    if not settings.ORDER_PROPOSALS_AUTO_APPROVE:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
            window_evaluator=window_evaluator,
            now_fn=now_fn,
        )

    clock = now_fn or (lambda: now)
    auto_submitted = False
    messages: ApprovalDispatchMessages | None = None
    attempt_id: uuid.UUID | None = None
    mirror_card: tuple[Any, list[Any], str] | None = None
    auto_policy_version: str | None = None
    async with service_factory() as session:
        service = OrderProposalsService(session)
        await service.acquire_auto_dispatch_lock(proposal_id)
        group, initial_rungs = await service.get_proposal(proposal_id)
        evaluate_window = window_evaluator or evaluate_approval_window
        window = await evaluate_approval_window_boundary(
            group,
            window_evaluator=evaluate_window,
            now_fn=clock,
            require_policy_stamp=False,
        )
        gate_now = window.observed_at
        if not window.allowed:
            if window.code is ApprovalWindowCode.EXPIRED:
                await service.expire_mutable_rungs_if_needed(proposal_id, now=gate_now)
            await session.commit()
            return window
        pending_count = sum(rung.state == "pending_approval" for rung in initial_rungs)
        if pending_count == 0:
            await session.commit()
            return TelegramDispatchResult(
                state=ApprovalDispatchState.FAILED,
                message_id=None,
                status_code=None,
                error_code=None,
                error_classification=None,
                payload_chars=0,
                failure_code="proposal_not_pending_approval",
            )
        limits = limits_for_market(group.market)
        decisions: list[dict[str, Any]] = []
        if limits is not None and group.account_mode == "toss_live":
            toss_freeze = await service.active_toss_auto_submission_freeze(
                group, now=gate_now
            )
            if toss_freeze is not None:
                # A verified Toss fill closes the same-session auto lane.  Do
                # not even enter revalidation (which can reach a broker) here;
                # the ordinary approval card is the intentional fail-closed
                # continuation while the operator considers a cancel proposal.
                limits = None
        if limits is not None and auto_veto_thesis_summary(group) is None:
            # This is deliberately outside the revalidation callback.  The
            # production callback consults the same gate, but this second
            # boundary prevents a future revalidation regression from
            # submitting first and discovering an unrenderable veto card only
            # after the broker accepted it.
            limits = None
        if limits is not None:
            daily_notional = await service.auto_approved_daily_notional(group, now=now)

            async def eligibility_gate(**kwargs: Any) -> Any:
                nonlocal daily_notional
                decision = evaluate_auto_approve_eligibility(
                    group=kwargs["group"],
                    rung=kwargs["rung"],
                    preview=kwargs["preview"],
                    limits=limits,
                    daily_notional=daily_notional,
                )
                decisions.append(
                    {
                        "rung_index": kwargs["rung"].rung_index,
                        "eligible": decision.eligible,
                        "reason": decision.reason,
                        **decision.details,
                    }
                )
                if decision.eligible:
                    daily_notional = Decimal(decision.details["daily_notional_after"])
                return decision

            revalidate_kwargs: dict[str, Any] = {
                "service": service,
                "proposal_id": proposal_id,
                "now": gate_now,
                "eligibility_gate": eligibility_gate,
            }
            if revalidate_fn is revalidate_and_submit:
                revalidate_kwargs.update(
                    window_evaluator=evaluate_window,
                    expected_policy_stamp=window.policy_stamp,
                    now_fn=clock,
                )
            outcomes: list[RungOutcome] = await revalidate_fn(**revalidate_kwargs)
            submitted_results = {"submitted_acked", "submitted_resting"}
            auto_submitted = (
                bool(outcomes)
                and len(outcomes) == pending_count
                and all(outcome.result in submitted_results for outcome in outcomes)
            )
            if auto_submitted:
                auto_policy_version = limits.policy_version
                await service.record_auto_approval(
                    proposal_id,
                    policy_version=limits.policy_version,
                    eligibility=decisions,
                    outcomes=[outcome.result for outcome in outcomes],
                    now=now,
                )
                veto_nonce = _generate_nonce()
                await service.set_approval_nonce(proposal_id, veto_nonce)
                group, rungs = await service.get_proposal(proposal_id)
                attempt_id = uuid.uuid4()
                binding = _proposal_binding(
                    group=group,
                    nonce=veto_nonce,
                    attempt_id=attempt_id,
                    card_kind=ApprovalCardKind.AUTO_VETO,
                )
                text, keyboard = build_auto_approved_message(
                    group=group,
                    rungs=rungs,
                    nonce=veto_nonce,
                    policy_version=limits.policy_version,
                    binding=binding,
                )
                messages = ApprovalDispatchMessages(
                    (),
                    text,
                    keyboard,
                    telegram_text_length(text),
                )
                await service.start_approval_dispatch(
                    proposal_id,
                    attempt_id=attempt_id,
                    binding=binding,
                    now=now,
                    payload_chars=messages.payload_chars,
                    context_message_count=0,
                )
            else:
                rejected_decisions = [
                    decision for decision in decisions if decision["eligible"] is False
                ]
                if rejected_decisions:
                    await service.record_auto_approve_rejections(
                        proposal_id,
                        decisions=rejected_decisions,
                        now=gate_now,
                    )
        # Persist broker outcomes and the audit/nonce before Telegram I/O.
        await session.commit()

    if not auto_submitted or messages is None or attempt_id is None:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
            window_evaluator=window_evaluator,
            now_fn=clock,
        )

    allowlist = settings.order_proposals_telegram_chat_allowlist
    chat_id = allowlist[0] if allowlist else None
    publication = (
        await publish_approval_messages(
            notifier=notifier,
            messages=messages,
            chat_id=chat_id,
        )
        if chat_id is not None
        else ApprovalPublication.failed(
            payload_chars=messages.payload_chars,
            failure_code="telegram_allowlist_empty",
        )
    )
    async with service_factory() as session:
        service = OrderProposalsService(session)
        # Preserve the established auto-dispatch lock order: advisory lock
        # first, then proposal/attempt row locks inside finalization.
        await service.acquire_auto_dispatch_lock(proposal_id)
        result = await service.finish_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            publication=publication,
            chat_id=chat_id,
            now=now,
        )
        if result.state in {
            ApprovalDispatchState.FAILED,
            ApprovalDispatchState.PARTIAL_FAILED,
        }:
            group, rungs = await service.get_proposal(proposal_id)
            await acquire_auto_veto_locks(service=service, group=group, rungs=rungs)
            outcomes = await cancel_auto_submitted_rungs(
                service=service,
                group=group,
                rungs=rungs,
                now=now,
                cancel_fn=cancel_target_fn,
                fetch_fn=fetch_target_fn,
                toss_reconcile_fn=toss_veto_reconcile_fn,
            )
            await service.record_auto_notification_failure(
                proposal_id,
                error=result.failure_code or "telegram_dispatch_failed",
                outcomes=outcomes,
                now=now,
            )
        elif result.state is ApprovalDispatchState.SENT_CURRENT:
            group, rungs = await service.get_proposal(proposal_id)
            # This branch is reachable only after `auto_submitted` above set
            # the policy version.  Keep the guard defensive so a future
            # refactor cannot emit an unversioned mirror card.
            if auto_policy_version is not None:
                mirror_card = (group, rungs, auto_policy_version)
        await session.commit()
    if mirror_card is not None:
        group, rungs, policy_version = mirror_card
        await _mirror_auto_veto_card(
            notifier=notifier,
            group=group,
            rungs=rungs,
            policy_version=policy_version,
        )
    return result


__all__ = [
    "dispatch_proposal",
    "publish_approval_messages",
    "record_approval_dispatch_failure",
    "send_proposal_for_approval",
]
