"""The ``mpb`` Telegram callback: answering a morning batch card (hk 3795 §5.1, T3a).

A new path, separate from the order-approval callback and from the W5 inbox: none
of their code runs for an ``mpb:`` button. What this path may write is a plan
event (``approved``, ``rejected``, ``terms_rebound``, ``retired``) carrying the card's id.
It imports no order, proposal or broker module (``tests/services/mandate/
test_static_boundary.py``).

Checks, in this order, before anything is written:

1. the chat is on the approval allowlist (as for every callback);
2. the callback data is exactly ``mpb:<a|r>:<card id>:<nonce>``;
3. the sender is on ``MANDATE_APPROVER_TELEGRAM_USER_IDS`` (§3.5; empty = nobody).
   A sender who is not listed writes nothing and spends no nonce — the card stays
   answerable by the operator;
4. the nonce matches the card's stored digest, the card is the newest of its plan,
   the batch has not expired, the plan of this batch has no answer yet, the plan and
   clause hashes still match the snapshot, and the snapshot clause is still the sleeve's
   live clause.

The answer is one plan event written under the basket lock (L2, then the sleeve lock
L3 inside the event writer). The event itself spends the nonce: a plan of a batch has
at most one answer, enforced by the database trigger as well. The Telegram reply and
the message edit come after the commit and never change the result.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.models.mandate import MandateTerms
from app.models.strategy_sleeves import StrategySleeveVersion
from app.services.mandate import batch_cards as cards
from app.services.mandate import batch_rules as rules
from app.services.mandate import batches as bt
from app.services.mandate import hashing
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.locks import acquire_basket_lock, acquire_sleeve_lock

logger = logging.getLogger(__name__)

USER_ID_MIN = 1
USER_ID_MAX = 2**52 - 1
ACTOR_PREFIX = "operator:telegram:"

#: Operator-facing answer text for each closed reason.
ANSWER_TEXT: dict[str, str] = {
    "approved": "승인했습니다.",
    "rejected": "기각했습니다.",
    "chat_not_allowed": "허용되지 않은 채팅입니다.",
    "malformed_callback_data": "알 수 없는 버튼입니다.",
    "invalid_telegram_identifier": "발신자를 확인할 수 없습니다.",
    v.MandateRefusal.APPROVER_NOT_ALLOWED.value: "승인 권한이 없는 발신자입니다.",
    "card_not_found": "카드를 찾을 수 없습니다.",
    v.MandateRefusal.NONCE_MISMATCH.value: "유효하지 않은 카드입니다.",
    v.MandateRefusal.CARD_SUPERSEDED.value: "새 카드로 대체됐습니다. 최신 카드에서 응답하세요.",
    v.MandateRefusal.BATCH_ANSWERED.value: "이미 응답한 계획입니다.",
    v.MandateRefusal.BATCH_EXPIRED.value: "배치가 만료됐습니다. 다음 배치에서 다시 요청됩니다.",
    v.MandateRefusal.BINDING_BROKEN.value: "저장된 해시가 카드와 다릅니다. 처리하지 않았습니다.",
    v.MandateRefusal.TERMS_NOT_CURRENT.value: "위임장 조항이 바뀌었습니다. 다음 배치에서 다시 요청됩니다.",
    v.MandateRefusal.TERMS_EXPIRED.value: "위임장 조항이 만료됐습니다.",
    v.MandateRefusal.INVALID_TRANSITION.value: "계획 상태가 달라져 처리하지 않았습니다.",
    "internal_error": "처리하지 못했습니다.",
}


def is_mandate_update(update: object) -> bool:
    """True when the update is a button of this path (``mpb:`` data), valid or not.

    The router sends exactly these updates here and every other update to the
    unchanged order-approval callback.
    """
    if not isinstance(update, dict):
        return False
    query = update.get("callback_query")
    return isinstance(query, dict) and rules.is_mandate_callback_data(query.get("data"))


def valid_user_id(value: object) -> int | None:
    """An exact built-in int in Telegram's bound; no coercion (R37 rule)."""
    if type(value) is not int or not USER_ID_MIN <= value <= USER_ID_MAX:
        return None
    return value


@dataclass(frozen=True)
class Authorized:
    parsed: rules.ParsedCallback
    chat_id: Any
    message_id: int | None
    callback_query_id: str | None
    user_id: int
    #: the action text as published (display only; never a decision input)
    message_text: str | None = None


@dataclass(frozen=True)
class Rejected:
    reason: str
    callback_query_id: str | None = None


def authorize(update: dict[str, Any]) -> Authorized | Rejected:
    """Checks 1-3. Pure given settings: no database, no write, no nonce read."""
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return Rejected("malformed_callback_data")
    query_id = query.get("id") if isinstance(query.get("id"), str) else None
    message = query.get("message")
    message = message if isinstance(message, dict) else {}
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    chat_id = chat.get("id")
    if str(chat_id) not in settings.order_proposals_telegram_chat_allowlist:
        return Rejected("chat_not_allowed", query_id)
    parsed = rules.parse_callback_data(query.get("data"))
    if parsed is None:
        return Rejected("malformed_callback_data", query_id)
    sender = query.get("from")
    if type(sender) is not dict:
        return Rejected("invalid_telegram_identifier", query_id)
    user_id = valid_user_id(sender.get("id"))
    if user_id is None:
        return Rejected("invalid_telegram_identifier", query_id)
    if user_id not in settings.mandate_approver_telegram_user_ids:
        return Rejected(v.MandateRefusal.APPROVER_NOT_ALLOWED.value, query_id)
    message_id = message.get("message_id")
    message_text = message.get("text")
    return Authorized(
        parsed,
        chat_id,
        message_id if type(message_id) is int else None,
        query_id,
        user_id,
        message_text if type(message_text) is str and message_text else None,
    )


@dataclass(frozen=True)
class Outcome:
    handled: bool
    reason: str  # "approved" | "rejected" or a closed refusal reason
    facts: cards.CardFacts | None = None


async def _card_facts(
    session: Any, batch: Any, plan: Any, member: Any, terms: MandateTerms
) -> cards.CardFacts:
    return cards.CardFacts(
        kind=member.kind,
        market=batch.market,
        basket_key=batch.basket_key,
        symbol=plan.symbol,
        planned_amount=plan.planned_amount,
        adopted_amount=plan.adopted_amount,
        sector_key=plan.sector_key,
        thesis=plan.thesis,
        invalidation_conditions=tuple(plan.invalidation_conditions),
        terms_id=terms.id,
        terms_sha256=member.terms_sha256,
        plan_sha256=member.plan_sha256,
        target_session=batch.target_session,
        approval_deadline_at=batch.approval_deadline_at,
        expires_at=batch.expires_at,
        card_no=member.card_no,
        realm=batch.realm,
        batch_id=batch.id,
        member_id=member.id,
        open_order_count=await bt.open_order_count(session, plan.id),
    )


async def db_clock(session: Any) -> datetime:
    """The database wall clock: the one clock the answer trigger also reads."""
    value = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
    return value.astimezone(UTC)


async def answer(
    session: Any,
    auth: Authorized,
    *,
    calendar: rules.SessionCalendar,
    decision_clock: Callable[[Any], Awaitable[datetime]] | None = None,
) -> Outcome:
    """Check 4 and the write. Commits on success, rolls back on every refusal.

    The decision instant is read once, from the database clock, after the basket and
    sleeve locks are held; refusals and the effective session are judged at it. The
    answer trigger judges expiry and the approval deadline again with its own wall clock
    at the write and trusts no time from the writer. For an approval the callback also
    passes the session after the target (``mandate.next_session``, a date,
    transaction-local): a valid click decided just before the opening bell whose write
    lands just after it is then recorded for that next session instead of refused.
    """
    member = await bt.get_member(session, auth.parsed.member_id)
    if member is None:
        await session.rollback()
        return Outcome(False, "card_not_found")
    if not rules.nonce_matches(auth.parsed.nonce, member.nonce_digest):
        await session.rollback()
        return Outcome(False, v.MandateRefusal.NONCE_MISMATCH.value)
    batch = await bt.get_batch(session, member.batch_id)
    assert batch is not None
    # L2, then L3: the sleeve lock is taken before any check, so a sleeve-version
    # writer (which takes only that lock) cannot change the live clause between the
    # check and the event. Everything below sees committed state.
    plan_row = await ms.get_plan(session, member.plan_id)
    if plan_row is None:
        await session.rollback()
        return Outcome(False, v.MandateRefusal.BINDING_BROKEN.value)
    await acquire_basket_lock(session, batch.realm, batch.basket_key)
    await acquire_sleeve_lock(session, plan_row.sleeve_id)
    moment = (await (decision_clock or db_clock)(session)).astimezone(UTC)
    refusal = await _refusal(session, member, batch, moment)
    if refusal is not None:
        await session.rollback()
        return Outcome(False, refusal)
    plan = await ms.get_plan(session, member.plan_id)
    terms = await session.get(MandateTerms, member.terms_id)
    assert plan is not None and terms is not None
    approve = auth.parsed.action == "approve"
    event = v.BATCH_ANSWER_EVENTS[member.kind][0 if approve else 1]
    fields: dict[str, Any] = {
        "batch_member_id": member.id,
        "terms_id": terms.id,
        "terms_sha256": member.terms_sha256,
    }
    evidence: dict[str, Any] = {}
    next_session: date | None = None
    if approve:
        window = rules.BatchWindow(
            batch.target_session, batch.approval_deadline_at, batch.expires_at
        )
        try:
            effective = rules.effective_session(batch.market, moment, window, calendar)
        except rules.NoSession:
            await session.rollback()
            return Outcome(False, v.MandateRefusal.CALENDAR_UNKNOWN.value)
        if effective is None:  # expired: checked above, kept as a fail-closed guard
            await session.rollback()
            return Outcome(False, v.MandateRefusal.BATCH_EXPIRED.value)
        fields["effective_session"] = effective
        try:
            next_session = rules.next_session(
                batch.market, batch.target_session, calendar
            )
        except rules.NoSession:
            next_session = (
                None  # a write that straddles the bell is then refused, retried
            )
    else:
        evidence["reason_code"] = (
            "reapproval_rejected" if member.kind == "reapproval" else "batch_rejected"
        )
        if member.kind == "reapproval":
            evidence["open_order_count"] = await bt.open_order_count(session, plan.id)
    facts = await _card_facts(session, batch, plan, member, terms)
    try:
        if approve and next_session is not None:
            await session.execute(
                text("SELECT set_config('mandate.next_session', :day, true)"),
                {"day": next_session.isoformat()},
            )
        await ms.append_plan_event(
            session,
            plan_id=plan.id,
            event=event,
            actor=f"{ACTOR_PREFIX}{auth.user_id}",
            evidence=evidence,
            **fields,
        )
        await session.commit()
    except ms.MandateError as exc:
        await session.rollback()
        logger.info("mandate.batch_callback.refused", extra={"code": exc.code.value})
        return Outcome(False, _closed_reason(exc.code))
    return Outcome(True, "approved" if approve else "rejected", facts)


def _closed_reason(code: v.MandateRefusal) -> str:
    return code.value if code.value in ANSWER_TEXT else "internal_error"


async def _refusal(
    session: Any, member: Any, batch: Any, moment: datetime
) -> str | None:
    latest = await bt.latest_card(session, member.batch_id, member.plan_id)
    if latest is None or latest.id != member.id:
        return v.MandateRefusal.CARD_SUPERSEDED.value
    if await bt.answer_event(session, member.batch_id, member.plan_id) is not None:
        return v.MandateRefusal.BATCH_ANSWERED.value
    if moment >= batch.expires_at:
        return v.MandateRefusal.BATCH_EXPIRED.value
    plan = await ms.get_plan(session, member.plan_id)
    terms = await session.get(MandateTerms, member.terms_id)
    if plan is None or terms is None:
        return v.MandateRefusal.BINDING_BROKEN.value
    if bt.plan_sha256(plan) != member.plan_sha256:
        return v.MandateRefusal.BINDING_BROKEN.value
    sleeve_version = await session.get(StrategySleeveVersion, terms.sleeve_version_id)
    if sleeve_version is None:
        return v.MandateRefusal.BINDING_BROKEN.value
    recomputed = hashing.content_sha256(
        ms.terms_content_from_rows(terms, sleeve_version)
    )
    if recomputed != member.terms_sha256 or terms.content_sha256 != member.terms_sha256:
        return v.MandateRefusal.BINDING_BROKEN.value
    version, live = await ms.current_terms(session, plan.sleeve_id)
    if (
        live is None
        or live.id != terms.id
        or version is None
        or version.state != "live"
    ):
        return v.MandateRefusal.TERMS_NOT_CURRENT.value
    if terms.expires_at <= moment:
        return v.MandateRefusal.TERMS_EXPIRED.value
    return None


async def handle_mandate_callback(
    update: dict[str, Any],
    *,
    now: datetime,
    session_factory: Callable[[], Any] | None = None,
    notifier: Any = None,
    calendar: rules.SessionCalendar | None = None,
    decision_clock: Callable[[Any], Awaitable[datetime]] | None = None,
) -> dict[str, Any]:
    """Handle one ``mpb:`` update. Never raises; the HTTP layer answers 200 either way."""
    auth = authorize(update)
    if isinstance(auth, Rejected):
        await _reply(notifier, auth.callback_query_id, ANSWER_TEXT.get(auth.reason))
        return {"handled": False, "reason": auth.reason}
    try:
        if session_factory is None:
            from app.core.db import AsyncSessionLocal

            session_factory = AsyncSessionLocal
        async with session_factory() as session:
            outcome = await answer(
                session,
                auth,
                calendar=calendar or rules.ExchangeCalendar(),
                decision_clock=decision_clock,
            )
    except Exception as exc:  # noqa: BLE001 - fail closed, never leak the cause
        logger.error(
            "mandate.batch_callback.failed",
            extra={"exception_type": type(exc).__name__},
        )
        outcome = Outcome(False, "internal_error")
    await _reply(
        notifier,
        auth.callback_query_id,
        ANSWER_TEXT.get(outcome.reason, ANSWER_TEXT["internal_error"]),
    )
    if outcome.handled and outcome.facts is not None:
        await _edit(notifier, auth, ANSWER_TEXT[outcome.reason])
    return {"handled": outcome.handled, "reason": outcome.reason}


def _notifier(notifier: Any) -> Any:
    if notifier is not None:
        return notifier
    from app.monitoring.trade_notifier.notifier import get_trade_notifier

    return get_trade_notifier()


async def _reply(
    notifier: Any, callback_query_id: str | None, text: str | None
) -> None:
    if callback_query_id is None:
        return
    try:
        await _notifier(notifier).answer_callback(callback_query_id, text)
    except Exception:  # noqa: BLE001 - a lost reply never changes the result
        logger.warning("mandate.batch_callback.reply_failed")


async def _edit(notifier: Any, auth: Authorized, outcome_text: str) -> None:
    """Show the outcome on the answered card and remove its buttons.

    The decision is already durable, so nothing here may fail the webhook, and nothing
    here is a decision input. The edit extends the text that was actually published (the
    callback's own message), so the layout and the reply reference of the published card
    cannot drift with display facts read later. If that text is missing or no longer fits
    one message, only the buttons are removed.
    """
    if auth.message_id is None:
        return
    empty = {"inline_keyboard": []}
    try:
        text_ = None
        if auth.message_text is not None:
            try:
                text_ = cards.render_answered_text(auth.message_text, outcome_text)
            except ValueError:
                text_ = None
        if text_ is not None:
            await _notifier(notifier).edit_message(
                str(auth.chat_id),
                auth.message_id,
                text_,
                reply_markup=empty,
                parse_mode=None,  # the card was sent as plain text
            )
        else:
            await _notifier(notifier).edit_message_reply_markup(
                str(auth.chat_id), auth.message_id, empty
            )
    except Exception:  # noqa: BLE001
        logger.warning("mandate.batch_callback.edit_failed")
