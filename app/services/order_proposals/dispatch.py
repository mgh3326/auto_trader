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
from app.services.fill_notification import resolve_display_name_db
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
from app.services.order_proposals.errors import OrderProposalError
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

_SOURCE_ASOF_ABSENT = object()
_AUDITABLE_REVALIDATION_FALLBACK_REASONS = frozenset(
    {"eligibility_error", "multi_rung_requires_approval"}
)
# ROB-1281: prefix for "no card was rendered because the proposal is no longer
# approvable", kept distinct from card-rendering/transport failure codes.
APPROVAL_NOT_DISPATCHABLE_PREFIX = "approval_not_dispatchable:"
# ROB-1281 r2: the *only* ``set_approval_nonce`` refusals that mean "this
# proposal can never carry an approval card again" -- the two verdicts of
# ``service.proposal_approval_block_reason``.
#
# Deliberately an allowlist, never a denylist.  ``set_approval_nonce`` also
# raises ``approval_dispatch_already_pending``/``approval_dispatch_already_
# current`` under ``require_redispatchable``, and those are the opposite kind
# of answer: they are the guard *protecting a live card* from being replaced.
# Ledgering one of them would run ``_record_proposal_not_dispatchable``, which
# supersedes the current attempt, moves group ownership to a new attempt, and
# clears the nonce on failure -- destroying the very card the guard just
# refused to disturb.  Anything not listed here therefore re-raises to the
# caller untouched, which is also the pre-ROB-1281 behaviour.
_NOT_DISPATCHABLE_REASON_PREFIXES = (
    "proposal_terminal:",
    "proposal_superseded_by:",
)


def _auto_approve_rejection_card_block_for_group(group: Any) -> str | None:
    """Read optional audit provenance without inventing a missing value.

    ORM groups always expose ``source_asof``.  Some narrow dispatch callers
    intentionally use shape-only group objects, though, where absence means
    there is no provenance column at all; that is distinct from an ORM value
    explicitly stored as ``None``.  Neither case may create a rejection
    record while rendering a card.  The latter still flows through the safe
    projector so its ``None`` contract remains exercised.
    """
    source_asof = getattr(group, "source_asof", _SOURCE_ASOF_ABSENT)
    if source_asof is _SOURCE_ASOF_ABSENT:
        return None
    return build_auto_approve_rejection_card_block(source_asof)


# §141차: what "the auto lane completed the whole proposal" looks like depends
# on the action. `place` and `replace` both end at a broker submit, so their
# success is a submit outcome; an auto-approved `cancel` never submits anything
# and ends at `cancelled`. Getting this wrong is not cosmetic -- revalidation
# has already performed the broker mutation by the time this is read, so an
# action whose terminal result is missing here would be executed and *then*
# reported as "falling back to human approval", sending an approval card for
# work that is already done. Every action in the classifier's supported set
# must therefore appear here, and unknown actions deliberately map to the
# empty set (never auto-complete) rather than to a permissive default.
_AUTO_COMPLETED_RESULTS: dict[str, frozenset[str]] = {
    "place": frozenset({"submitted_acked", "submitted_resting"}),
    "replace": frozenset({"submitted_acked", "submitted_resting"}),
    "cancel": frozenset({"cancelled"}),
}


def _auto_completed_results(action: str | None) -> frozenset[str]:
    return _AUTO_COMPLETED_RESULTS.get(action or "place", frozenset())


def _manual_fallback_decisions(
    *,
    rungs: list[Any],
    reason: str,
    policy_version: str,
    **inputs: Any,
) -> list[dict[str, Any]]:
    """Describe a pre-classifier fail-closed fallback using safe primitives."""
    return [
        {
            "rung_index": rung.rung_index,
            "eligible": False,
            "reason": reason,
            "policy_version": policy_version,
            **inputs,
        }
        for rung in rungs
        if rung.state == "pending_approval"
    ]


def _unrecorded_revalidation_fallbacks(
    *,
    outcomes: list[RungOutcome],
    decisions: list[dict[str, Any]],
    policy_version: str,
    pending_count: int,
) -> list[dict[str, Any]]:
    """Retain safe revalidation fallbacks that never reached ``reject()``.

    The classifier's decisions are already appended by the eligibility
    closure.  Multi-rung short-circuiting and a gate exception happen outside
    that closure, so their typed outcome reasons need a separate audit row.
    Raw exception detail is deliberately not copied.
    """
    recorded = {
        (decision.get("rung_index"), decision.get("reason"))
        for decision in decisions
        if decision.get("eligible") is False
    }
    fallbacks: list[dict[str, Any]] = []
    for outcome in outcomes:
        detail = outcome.detail if isinstance(outcome.detail, dict) else {}
        reason = detail.get("reason")
        key = (outcome.rung_index, reason)
        if reason not in _AUDITABLE_REVALIDATION_FALLBACK_REASONS or key in recorded:
            continue
        inputs: dict[str, Any] = {}
        if reason == "eligibility_error":
            inputs["eligibility_error"] = True
        elif reason == "multi_rung_requires_approval":
            inputs["pending_rung_count"] = str(pending_count)
        fallbacks.append(
            {
                "rung_index": outcome.rung_index,
                "eligible": False,
                "reason": reason,
                "policy_version": policy_version,
                **inputs,
            }
        )
    return fallbacks


def _mirror_decimal_text(value: Any) -> str:
    """Keep Discord card quantities/prices legible after DB numeric coercion."""
    try:
        normalized = format(Decimal(str(value)).normalize(), "f")
    except Exception:  # noqa: BLE001 - display degradation must not raise
        return str(value)
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


async def _resolve_card_display_name(group: Any) -> str | None:
    """Reuse the notification name resolver; rendering remains fail-open to code."""
    symbol = str(getattr(group, "symbol", "") or "").strip()
    market = {"equity_kr": "kr", "equity_us": "us"}.get(
        str(getattr(group, "market", "") or ""),
        str(getattr(group, "market", "") or ""),
    )
    if not symbol:
        return None
    try:
        name = await resolve_display_name_db(market, symbol)
    except Exception:  # noqa: BLE001 - alerts must still reach the operator
        logger.debug("order proposal display-name resolution failed", exc_info=True)
        return None
    normalized = " ".join(str(name or "").split())
    return normalized or None


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
                action=getattr(group, "action", None) or "place",
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


def approval_not_dispatchable_failure_code(reason: str) -> str:
    """Return the stable ledger code for a proposal that stopped being approvable."""
    return f"{APPROVAL_NOT_DISPATCHABLE_PREFIX}{reason}"


async def _record_proposal_not_dispatchable(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    reason: str,
    attempt_id: uuid.UUID,
    now: datetime,
) -> TelegramDispatchResult:
    """Ledger a typed refusal instead of an opaque dispatch-internal error.

    ROB-1281: a proposal can stop being approvable between creation and the
    approval card -- most commonly because the auto-approve lane already sent
    the order, the broker rejected it explicitly, and revalidation
    terminalized the rung (``record_rejected`` -> group ``rejected``).  The
    nonce mint then refuses, and before this the refusal escaped as a bare
    ``OrderProposalError`` that the MCP post-commit boundary flattened into
    ``approval_dispatch_internal_error`` with ``payload_chars=0`` -- which
    reads to an operator as "the card renderer produced an empty card".  No
    card is rendered on this path at all; recording the real reason keeps the
    two failure modes distinguishable.
    """
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
        payload_chars=0,
        context_message_count=0,
    )
    return await service.finish_approval_dispatch(
        proposal_id,
        attempt_id=attempt_id,
        publication=ApprovalPublication.failed(
            payload_chars=0,
            failure_code=approval_not_dispatchable_failure_code(reason),
        ),
        chat_id=None,
        now=now,
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

    display_names = {
        str(group.proposal_id): name
        for group, _rungs in proposals
        if (name := await _resolve_card_display_name(group)) is not None
    }
    text, keyboard = build_batch_approval_message(
        batch=batch,
        proposals=proposals,
        binding=registration.binding,
        display_names=display_names,
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
        try:
            if redispatch:
                await service.set_approval_nonce(
                    proposal_id,
                    fresh_nonce,
                    require_redispatchable=True,
                )
            else:
                await service.set_approval_nonce(proposal_id, fresh_nonce)
        except OrderProposalError as exc:
            # ROB-1281: the row-locked mint is the authoritative approvability
            # gate, so catching here (rather than pre-checking the unlocked
            # group above) is race-free.  Ledger the refusal it carries --
            # ``proposal_terminal:rejected`` after a broker-rejected
            # auto-submit is the production case -- instead of letting a bare
            # domain error reach the caller's generic exception containment.
            reason = str(exc)
            if not reason.startswith(_NOT_DISPATCHABLE_REASON_PREFIXES):
                # A redispatch guard (or any future refusal) that is protecting
                # existing state, not reporting a dead proposal.  Recording it
                # would supersede the live attempt and burn its nonce, so hand
                # it back to the caller exactly as before ROB-1281.
                raise
            result = await _record_proposal_not_dispatchable(
                service=service,
                proposal_id=proposal_id,
                reason=reason,
                attempt_id=attempt_id,
                now=publish_now,
            )
            await session.commit()
            return result

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
            display_name=await _resolve_card_display_name(group),
            suffix_blocks=(
                (rejection_block,)
                if (
                    rejection_block := _auto_approve_rejection_card_block_for_group(
                        group
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
    """Publish the compact button card after payload validation."""
    all_messages = (*messages.context_messages, messages.approval_text)
    if any(
        telegram_text_length(text) > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
        for text in all_messages
    ):
        return ApprovalPublication.failed(
            payload_chars=messages.payload_chars,
            failure_code="approval_payload_too_long",
        )
    # ROB-1281 AC2: never hand Telegram a card with no visible body.  A blank
    # card would either be rejected by the API or -- worse -- delivered as an
    # unreadable button with no symbol/side/quantity/price, which is exactly
    # the failure operators must never have to approve blind.  Fail closed
    # with a code that names the emptiness instead.
    if any(not text.strip() for text in all_messages):
        return ApprovalPublication.failed(
            payload_chars=messages.payload_chars,
            failure_code="approval_payload_empty",
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
        fallback_decisions: list[dict[str, Any]] = []
        if limits is not None and group.account_mode == "toss_live":
            toss_freeze = await service.active_toss_auto_submission_freeze(
                group, now=gate_now
            )
            if toss_freeze is not None:
                # A partial, unexpected, or not-yet-cleanly-reconciled Toss
                # fill closes the same-session auto lane.  Do not even enter
                # revalidation (which can reach a broker) here; the ordinary
                # approval card is the intentional fail-closed continuation
                # while the operator considers a cancel proposal.
                fallback_decisions.extend(
                    _manual_fallback_decisions(
                        rungs=initial_rungs,
                        reason="toss_auto_submission_frozen",
                        policy_version=limits.policy_version,
                        toss_auto_submission_frozen=True,
                    )
                )
                limits = None
        if limits is not None and auto_veto_thesis_summary(group) is None:
            # This is deliberately outside the revalidation callback.  The
            # production callback consults the same gate, but this second
            # boundary prevents a future revalidation regression from
            # submitting first and discovering an unrenderable veto card only
            # after the broker accepted it.
            fallback_decisions.extend(
                _manual_fallback_decisions(
                    rungs=initial_rungs,
                    reason="auto_veto_thesis_missing",
                    policy_version=limits.policy_version,
                    thesis_present=False,
                )
            )
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
            submitted_results = _auto_completed_results(group.action)
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
                    policy_content_hash=getattr(limits, "policy_content_hash", None),
                    eligibility=decisions,
                    outcomes=[outcome.result for outcome in outcomes],
                    now=now,
                    evaluated_at=gate_now,
                )
                # §141차: an auto-approved `cancel` is told, not offered.
                #
                # Everything below the `vetoable` branch is the *approval card*
                # machinery: mint a single-use nonce, bind it to a published
                # card, open a dispatch attempt that can later be superseded or
                # tapped. A completed cancel has none of those needs -- there is
                # no order left to pull, so the card carries no button -- and it
                # cannot use that machinery even if we wanted to: cancelling the
                # last rung drives the group to `lifecycle_state="terminal"`, and
                # both `set_approval_nonce` (`proposal_terminal:terminal`) and
                # `finish_approval_dispatch` (`approval_dispatch_snapshot_missing`
                # -- a published card with no nonce is unauthorizable) fail closed
                # on exactly that. Those guards are right; the receipt simply is
                # not an approval card. So it publishes as a plain notification
                # with no nonce, no binding and no attempt row, and its delivery
                # outcome is reported from the publication itself. The auto-
                # approval decision remains durable either way -- `record_auto_
                # approval` above already stamped it.
                vetoable = (group.action or "place") != "cancel"
                veto_nonce: str | None = None
                if vetoable:
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
                else:
                    group, rungs = await service.get_proposal(proposal_id)
                    binding = None
                text, keyboard = build_auto_approved_message(
                    group=group,
                    rungs=rungs,
                    nonce=veto_nonce,
                    policy_version=limits.policy_version,
                    display_name=await _resolve_card_display_name(group),
                    binding=binding,
                )
                messages = ApprovalDispatchMessages(
                    (),
                    text,
                    keyboard,
                    telegram_text_length(text),
                )
                if binding is not None and attempt_id is not None:
                    await service.start_approval_dispatch(
                        proposal_id,
                        attempt_id=attempt_id,
                        binding=binding,
                        now=now,
                        payload_chars=messages.payload_chars,
                        context_message_count=0,
                    )
            else:
                fallback_decisions.extend(
                    _unrecorded_revalidation_fallbacks(
                        outcomes=outcomes,
                        decisions=decisions,
                        policy_version=limits.policy_version,
                        pending_count=pending_count,
                    )
                )
        if not auto_submitted:
            rejected_decisions = [
                decision for decision in decisions if decision["eligible"] is False
            ]
            rejected_decisions.extend(fallback_decisions)
            if rejected_decisions:
                await service.record_auto_approve_rejections(
                    proposal_id,
                    decisions=rejected_decisions,
                    now=gate_now,
                )
        # Persist broker outcomes and the audit/nonce before Telegram I/O.
        await session.commit()

    if not auto_submitted or messages is None:
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
        if attempt_id is not None:
            result = await service.finish_approval_dispatch(
                proposal_id,
                attempt_id=attempt_id,
                publication=publication,
                chat_id=chat_id,
                now=now,
            )
        else:
            # §141차 receipt (see the `vetoable` branch above): no attempt row
            # was opened, so there is no current-owner fence to resolve. Report
            # the delivery outcome the publication itself observed. The states
            # below still drive the same compensation/mirror branches.
            result = TelegramDispatchResult.from_publication(
                publication,
                state=(
                    ApprovalDispatchState.SENT_CURRENT
                    if publication.card_published
                    else ApprovalDispatchState.PARTIAL_FAILED
                    if publication.partial
                    else ApprovalDispatchState.FAILED
                ),
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
    "APPROVAL_NOT_DISPATCHABLE_PREFIX",
    "approval_not_dispatchable_failure_code",
    "dispatch_proposal",
    "publish_approval_messages",
    "record_approval_dispatch_failure",
    "send_proposal_for_approval",
]
