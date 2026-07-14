"""Send the initial Telegram approval message for a proposal (ROB-816 PR 2).

``send_proposal_for_approval`` is a top-level caller module, same as
``telegram_callback.py`` -- it opens and COMMITS its own DB session rather
than being constructor-injected, because it (a) is invoked from
``order_proposal_create`` after that tool's own session has already closed
and committed, and (b) calls the Telegram notifier, which
``OrderProposalsService``/``OrderProposalRepository`` never do (they only
flush -- see ``service.py``'s module docstring).

Commit-before-notify is not a live risk here the way it is in
``telegram_callback.py`` (there is no notify call *after* this function's
mutating work), but the nonce mint + ``source_asof`` merge are still
committed explicitly before returning, matching that module's established
discipline rather than relying on implicit ``async with`` behavior.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.approval_message import (
    build_approval_message,
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
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import OrderProposalsService

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]
RevalidateFn = Callable[..., Any]


def _generate_nonce() -> str:
    # Duplicated from telegram_callback.py::_generate_nonce (2 lines) rather
    # than imported -- that name is `_`-prefixed/module-private, and this
    # module is a peer top-level caller, not a consumer of that module.
    return secrets.token_urlsafe(12)


async def _register_and_publish_batch_summary(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    message_id: int,
    chat_id: str,
    now: datetime,
    notifier: Any,
) -> None:
    """Best-effort batch registration after the individual message succeeds."""
    registration = await service.register_approval_batch_member(
        proposal_id,
        chat_id=chat_id,
        approval_message_id=message_id,
        now=now,
    )
    if registration is None or registration.summary_action == "none":
        return
    batch, proposals = await service.get_approval_batch_display(
        registration.batch.batch_id
    )
    text, keyboard = build_batch_approval_message(batch=batch, proposals=proposals)
    if registration.summary_action == "send":
        try:
            summary_id = await notifier.send_approval_message(
                text, keyboard, chat_id=chat_id
            )
        except Exception:  # noqa: BLE001 - individual dispatch remains valid
            logger.exception("order proposal batch summary send failed")
            await service.release_approval_batch_summary_claim(batch.batch_id, now=now)
            return
        if summary_id is None:
            await service.release_approval_batch_summary_claim(batch.batch_id, now=now)
            return
        await service.record_approval_batch_summary(
            batch.batch_id, message_id=summary_id, now=now
        )
        return
    if batch.summary_message_id is None:
        return
    try:
        await notifier.edit_message(
            chat_id,
            batch.summary_message_id,
            text,
            reply_markup=keyboard,
        )
    except Exception:  # noqa: BLE001 - summary is a best-effort surface
        logger.exception("order proposal batch summary edit failed")


async def send_proposal_for_approval(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> int | None:
    """Mint a fresh approval nonce, render the message, and send it.

    Sends to the FIRST entry in
    ``settings.order_proposals_telegram_chat_allowlist`` -- the return type
    is a single ``int | None`` message_id, which only makes sense for a
    single-chat send, not a broadcast. An empty allowlist is a no-op (no
    nonce mint, no send, returns ``None``) -- callers (the MCP wiring) should
    already gate on a non-empty allowlist before calling this, but this
    function defends independently.
    """
    allowlist = settings.order_proposals_telegram_chat_allowlist
    if not allowlist:
        return None
    chat_id = allowlist[0]

    async with service_factory() as session:
        service = OrderProposalsService(session)

        fresh_nonce = _generate_nonce()
        await service.set_approval_nonce(proposal_id, fresh_nonce)

        group, rungs = await service.get_proposal(proposal_id)
        text, keyboard = build_approval_message(group=group, rungs=rungs)

        message_id = await notifier.send_approval_message(
            text, keyboard, chat_id=chat_id
        )

        if message_id is not None:
            await service.record_approval_dispatch(
                proposal_id, message_id=message_id, chat_id=chat_id, now=now
            )
            await _register_and_publish_batch_summary(
                service=service,
                proposal_id=proposal_id,
                message_id=message_id,
                chat_id=chat_id,
                now=now,
                notifier=notifier,
            )

        # Commit explicitly before returning -- see module docstring. The
        # nonce mint above is committed even when message_id is None (send
        # failed): a fresh nonce with no message sent is not a correctness
        # problem, it just means the operator can't approve yet.
        await session.commit()
        return message_id


async def dispatch_proposal(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
    revalidate_fn: RevalidateFn = revalidate_and_submit,
    cancel_target_fn: TargetCancelFn = cancel_target_order,
    fetch_target_fn: TargetFetchFn = fetch_target_order,
) -> int | None:
    """Auto-submit an eligible resting proposal, otherwise send for approval."""
    if not settings.ORDER_PROPOSALS_AUTO_APPROVE:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
        )

    auto_submitted = False
    message: tuple[str, dict[str, Any]] | None = None
    async with service_factory() as session:
        service = OrderProposalsService(session)
        await service.acquire_auto_dispatch_lock(proposal_id)
        group, initial_rungs = await service.get_proposal(proposal_id)
        pending_count = sum(rung.state == "pending_approval" for rung in initial_rungs)
        if pending_count == 0:
            await session.commit()
            return None
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
                message = build_auto_approved_message(
                    group=group,
                    rungs=rungs,
                    nonce=veto_nonce,
                    policy_version=limits.policy_version,
                )
        # Persist broker outcomes and the audit/nonce before Telegram I/O.
        await session.commit()

    if not auto_submitted or message is None:
        return await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=service_factory,
        )

    allowlist = settings.order_proposals_telegram_chat_allowlist
    text, keyboard = message
    notify_error = "telegram_allowlist_empty"
    message_id = None
    chat_id = allowlist[0] if allowlist else None
    if chat_id is not None:
        notify_error = "telegram_message_not_sent"
        try:
            message_id = await notifier.send_approval_message(
                text, keyboard, chat_id=chat_id
            )
        except Exception as exc:  # noqa: BLE001 - compensate live order below
            logger.exception("auto-approved proposal veto notification failed")
            notify_error = str(exc)
            message_id = None
    if message_id is None:
        async with service_factory() as session:
            service = OrderProposalsService(session)
            await service.acquire_auto_dispatch_lock(proposal_id)
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
                error=notify_error,
                outcomes=outcomes,
                now=now,
            )
            await session.commit()
        return None
    if chat_id is None:
        raise AssertionError("a sent Telegram message requires a destination chat")
    async with service_factory() as session:
        service = OrderProposalsService(session)
        await service.record_approval_dispatch(
            proposal_id, message_id=message_id, chat_id=chat_id, now=now
        )
        await session.commit()
    return message_id


__all__ = ["dispatch_proposal", "send_proposal_for_approval"]
