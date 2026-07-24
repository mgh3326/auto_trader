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
from app.services.order_proposals.auto_approve import (
    build_auto_approved_message,
    evaluate_auto_approve_eligibility,
    limits_for_market,
)
from app.services.order_proposals.auto_veto import (
    TargetCancelFn,
    TargetFetchFn,
    acquire_auto_veto_locks,
    cancel_auto_submitted_rungs,
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


def _generate_nonce() -> str:
    # Duplicated from telegram_callback.py::_generate_nonce (2 lines) rather
    # than imported -- that name is `_`-prefixed/module-private, and this
    # module is a peer top-level caller, not a consumer of that module.
    return secrets.token_urlsafe(8)


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


async def _register_and_publish_batch_summary(
    *,
    session: AsyncSession,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    message_id: int,
    chat_id: str,
    now: datetime,
    notifier: Any,
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
    batch, proposals = await service.get_approval_batch_display(
        registration.batch.batch_id
    )
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
    # Freeze + pending owner must be durable before the external publication.
    await session.commit()
    publication = await publish_approval_messages(
        notifier=notifier,
        messages=messages,
        chat_id=chat_id,
    )
    await service.finish_approval_batch_dispatch(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=publication,
        now=now,
    )


async def send_proposal_for_approval(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> TelegramDispatchResult:
    """Mint a fresh approval nonce, render the message, and send it.

    Sends to the FIRST entry in
    ``settings.order_proposals_telegram_chat_allowlist`` -- the return type
    is a single workflow result. An empty allowlist is recorded as a durable
    local failure without minting a nonce.
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

        fresh_nonce = _generate_nonce()
        await service.set_approval_nonce(proposal_id, fresh_nonce)

        group, rungs = await service.get_proposal(proposal_id)
        binding = _proposal_binding(
            group=group,
            nonce=fresh_nonce,
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.MANUAL,
        )
        messages = build_approval_dispatch_messages(
            group=group, rungs=rungs, binding=binding
        )
        await service.start_approval_dispatch(
            proposal_id,
            attempt_id=attempt_id,
            binding=binding,
            now=now,
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
            now=now,
        )
        await session.commit()
        if result.approvable and result.message_id is not None:
            await _register_and_publish_batch_summary(
                session=session,
                service=service,
                proposal_id=proposal_id,
                message_id=result.message_id,
                chat_id=chat_id,
                now=now,
                notifier=notifier,
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
) -> TelegramDispatchResult:
    """Auto-submit an eligible resting proposal, otherwise send for approval."""
    if not settings.ORDER_PROPOSALS_AUTO_APPROVE:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
        )

    auto_submitted = False
    messages: ApprovalDispatchMessages | None = None
    attempt_id: uuid.UUID | None = None
    async with service_factory() as session:
        service = OrderProposalsService(session)
        await service.acquire_auto_dispatch_lock(proposal_id)
        group, initial_rungs = await service.get_proposal(proposal_id)
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

            outcomes: list[RungOutcome] = await revalidate_fn(
                service=service,
                proposal_id=proposal_id,
                now=now,
                eligibility_gate=eligibility_gate,
            )
            submitted_results = {"submitted_acked", "submitted_resting"}
            auto_submitted = (
                bool(outcomes)
                and len(outcomes) == pending_count
                and all(outcome.result in submitted_results for outcome in outcomes)
            )
            if auto_submitted:
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
        # Persist broker outcomes and the audit/nonce before Telegram I/O.
        await session.commit()

    if not auto_submitted or messages is None or attempt_id is None:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
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
            )
            await service.record_auto_notification_failure(
                proposal_id,
                error=result.failure_code or "telegram_dispatch_failed",
                outcomes=outcomes,
                now=now,
            )
        await session.commit()
    return result


__all__ = [
    "dispatch_proposal",
    "publish_approval_messages",
    "record_approval_dispatch_failure",
    "send_proposal_for_approval",
]
