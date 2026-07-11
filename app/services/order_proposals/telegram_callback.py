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

Principle #5 (nonce replay prevention is load-bearing): ``consume_approval_nonce``
is always called -- and its exceptions handled -- before any other mutation in
both the approve and deny branches.

``handle_callback_update`` never raises: Telegram's webhook contract expects a
response for every update, so any unexpected exception is caught, logged, and
turned into a failure result dict. Callback queries are answered best-effort as
soon as their metadata is available, before validation or order processing.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.approval_message import (
    _escape_markdown,
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
    "cancelled": "취소 확인",
}


def _outcome_error_summary(outcome: RungOutcome, *, limit: int = 240) -> str | None:
    error = str((outcome.detail or {}).get("error") or "").strip()
    if not error:
        return None
    compact = " ".join(error.split())
    if len(compact) > limit:
        compact = compact[: limit - 1] + "…"
    return _escape_markdown(compact)


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


async def _safe_edit_message(
    notifier: Any, chat_id: Any, message_id: int, text: str
) -> None:
    """Best-effort ``edit_message`` that never raises.

    Belt-and-suspenders alongside the commit-before-notify ordering in
    ``_handle_deny``/``_handle_approve``: by the time this is called the
    mutating DB work for this branch is already committed, so a Telegram
    failure here must not surface as an uncaught exception (which would hit
    the top-level ``except Exception`` and misreport a successful trade
    action as ``"internal_error"``).
    """
    try:
        await notifier.edit_message(chat_id, message_id, text)
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        logger.exception("order_proposals telegram edit_message failed")


async def _safe_send_approval_message(
    notifier: Any, text: str, keyboard: dict, *, chat_id: str
) -> int | None:
    """Best-effort ``send_approval_message`` that never raises.

    See ``_safe_edit_message`` docstring for why this must never propagate.
    """
    try:
        return await notifier.send_approval_message(text, keyboard, chat_id=chat_id)
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        logger.exception("order_proposals telegram send_approval_message failed")
        return None


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
        # Merged with main's parallel fix: PR-3a's summarizer (compacted,
        # length-capped, markdown-escaped) + main's "submit_rejected"
        # fallback for error outcomes whose detail carries no error text.
        reason = _outcome_error_summary(outcome)
        if outcome.result == "error" and not reason:
            reason = "submit\\_rejected"
        if reason and outcome.result in {"guard_blocked", "error"}:
            label = f"{label} — {reason}"
        lines.append(f"- #{outcome.rung_index + 1}: {label}")
    return "\n".join(lines)


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
        before = detail.get("before")
        after = detail.get("after")
        lines.append(f"- #{outcome.rung_index + 1}: 변경 전 {before} → 변경 후 {after}")
    return "\n".join(lines)


async def _handle_deny(
    *,
    session: AsyncSession,
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
        # No mutation happened above (mismatch/replay both raise before any
        # flush) -- commit anyway to release the row lock taken by
        # `consume_approval_nonce`'s `for_update=True` SELECT.
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


async def _handle_approve(
    *,
    session: AsyncSession,
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
    if await service.expire_if_needed(proposal_id, now=now):
        await session.commit()
        if message_id is not None:
            await _safe_edit_message(notifier, chat_id, message_id, "⌛ 제안 만료")
        await _safe_answer(notifier, callback_query_id, "제안이 만료되었습니다")
        return {
            "handled": False,
            "reason": "proposal_expired",
            "proposal_id": str(proposal_id),
        }

    try:
        await service.consume_approval_nonce(proposal_id, nonce, now=now)
    except OrderProposalError as exc:
        # See `_handle_deny`'s matching comment: no mutation happened above,
        # but commit anyway to release the row lock.
        await session.commit()
        return {"handled": False, "reason": str(exc), "proposal_id": str(proposal_id)}

    acquired = await service.acquire_commit_lease(proposal_id, now=now)
    if not acquired:
        # Same rationale -- release the `for_update=True` lock before return.
        await session.commit()
        return {
            "handled": False,
            "reason": "lease_held",
            "proposal_id": str(proposal_id),
        }

    await service.record_approval(
        proposal_id, telegram_user_id=telegram_user_id, now=now
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
        # `build_approval_message` only renders an explicit diff for the
        # first reconfirming rung -- surface every other reconfirming rung's
        # before/after here so a multi-rung reconfirm batch never silently
        # drops a rung's change (Finding 2, gap #1).
        if len(reconfirm_outcomes) > 1:
            text = f"{text}\n\n{_build_extra_reconfirm_block(reconfirm_outcomes[1:])}"
        # Rungs in the same batch that did NOT come back `needs_reconfirm`
        # (e.g. one rung submitted while another needs reconfirmation) would
        # otherwise never be reported anywhere, since this branch
        # short-circuits before `_build_result_summary` runs below (Finding
        # 2, gap #2).
        other_outcomes = [o for o in outcomes if o.result != "needs_reconfirm"]
        if other_outcomes:
            text = f"{text}\n\n{_build_result_summary(other_outcomes)}"

        # Commit the fresh nonce + record_approval + revalidate_and_submit's
        # rung-state transitions before any Telegram call -- a notify
        # failure below must never roll back real broker-order evidence.
        await session.commit()

        if message_id is not None:
            await _safe_edit_message(
                notifier,
                chat_id,
                message_id,
                "⚠️ 재확인 필요 — 아래 새 메시지를 확인해 주세요.",
            )
        new_message_id = await _safe_send_approval_message(
            notifier, text, keyboard, chat_id=str(chat_id)
        )
        if new_message_id is not None:
            # Mirror dispatch.py's send_proposal_for_approval: keep
            # source_asof.approval_message_id pointing at the NEWEST
            # outstanding Telegram message, not the original one from
            # dispatch.py -- otherwise a later reader of source_asof would
            # see a stale/superseded message_id for this reconfirm cycle
            # (ROB-816 final-review Finding 4). A failed send
            # (new_message_id is None) has nothing to persist.
            await service.record_approval_dispatch(
                proposal_id,
                message_id=new_message_id,
                chat_id=str(chat_id),
                now=now,
            )
            await session.commit()
        return {
            "handled": True,
            "reason": "needs_reconfirm",
            "proposal_id": str(proposal_id),
            "new_message_id": new_message_id,
        }

    summary = _build_result_summary(outcomes)

    # Commit record_approval + revalidate_and_submit's rung-state
    # transitions (acked/resting/unverified/rejected) before any Telegram
    # call -- same rationale as the reconfirm branch above.
    await session.commit()

    if message_id is not None:
        await _safe_edit_message(notifier, chat_id, message_id, summary)
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

        await _safe_answer(active_notifier, callback_query_id, "처리 중")

        if str(chat_id) not in settings.order_proposals_telegram_chat_allowlist:
            return {"handled": False, "reason": "chat_not_allowed"}

        try:
            action, proposal_short, nonce = parse_callback_data(data)
        except ValueError:
            return {"handled": False, "reason": "malformed_callback_data"}

        async with service_factory() as session:
            service = OrderProposalsService(session)
            proposal_id = await _resolve_proposal_id(service, proposal_short)
            if proposal_id is None:
                await session.commit()
                return {"handled": False, "reason": "proposal_not_found"}

            if action == "dn":
                result = await _handle_deny(
                    session=session,
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
                    session=session,
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
            # `_handle_deny`/`_handle_approve` each commit their own
            # mutating work internally before making any Telegram notify
            # call (see module docstring: commit-before-notify ordering) --
            # no end-of-function commit here.
            return result
    except Exception:  # noqa: BLE001 - fail-closed webhook contract
        logger.exception("order_proposals telegram callback handling failed")
        return {"handled": False, "reason": "internal_error"}
