"""Morning approval batches of the mandate (hk 3795 §5.1, ticket T3a).

The only writer of ``review.mandate_plan_batches`` and
``review.mandate_plan_batch_members``. It writes inside the caller's transaction and
never commits. The database triggers of ``20261120_hk5648_mandate_batches`` hold the
same rules for any other writer (I22).

* ``create_batch`` — one batch per basket and target session: it takes the basket
  lock (L2), lists the plans that need an answer (new ``proposed`` plans, and
  ``approved``/``active`` plans whose bound clause is no longer the live one), writes the
  batch and card 1 of each plan with a fresh nonce, and returns the nonces in clear
  exactly once (only their digests are stored).
* ``reissue_cards`` — a re-send is not a new batch: for each plan still unanswered it
  appends the next card (new nonce, same snapshot); the older card stops answering.

Lock order is the mandate order: basket (L2), then sleeve (L3) inside the event writer.
Nothing here reads a broker, sends a message or touches an order module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import (
    MandatePlan,
    MandatePlanBatch,
    MandatePlanBatchMember,
    MandatePlanEvent,
    MandateTerms,
)
from app.models.order_proposals import OrderProposal
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.services.mandate import batch_rules as rules
from app.services.mandate import hashing
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState
from app.services.mandate.locks import acquire_basket_lock
from app.services.strategy_sleeves.vocabulary import SYSTEM_LOSS_GATE_ACTOR

_CLOSED_PROPOSAL_STATES = ("terminal", "rejected", "expired", "voided", "superseded")
_ACTOR_PREFIX = "operator:"


@dataclass(frozen=True)
class Candidate:
    """A plan that needs an answer, with the clause it would be approved under."""

    plan: MandatePlan
    kind: str
    terms: MandateTerms


@dataclass(frozen=True)
class SkippedPlan:
    plan_id: int
    symbol: str
    reason: str


@dataclass(frozen=True)
class IssuedCard:
    """A card written to the database. ``nonce`` exists only here, in clear."""

    member: MandatePlanBatchMember
    nonce: str


@dataclass(frozen=True)
class IssuedBatch:
    batch: MandatePlanBatch
    cards: tuple[IssuedCard, ...]
    skipped: tuple[SkippedPlan, ...]


def _operator(value: str) -> str:
    who = (value or "").strip()
    if (
        not who.startswith(_ACTOR_PREFIX)
        or len(who) <= len(_ACTOR_PREFIX)
        or len(who) > 128
        or who == SYSTEM_LOSS_GATE_ACTOR
    ):
        raise ms.MandateError(
            v.MandateRefusal.INVALID_FIELDS, "actor is operator:<name>"
        )
    return who


def _now(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ms.MandateError(v.MandateRefusal.INVALID_FIELDS, "now needs a time zone")
    return now.astimezone(UTC)


def map_batch_error(exc: DBAPIError) -> ms.MandateError:
    """Name a batch trigger or constraint refusal; the transaction must roll back."""
    text = str(getattr(exc, "orig", exc))
    if "open batch" in text or "uq_mandate_plan_batches_basket_session" in text:
        return ms.MandateError(v.MandateRefusal.BATCH_OPEN, text[:200])
    if "expired" in text:
        return ms.MandateError(v.MandateRefusal.BATCH_EXPIRED, text[:200])
    if "already answered" in text:
        return ms.MandateError(v.MandateRefusal.BATCH_ANSWERED, text[:200])
    if "newer card" in text:
        return ms.MandateError(v.MandateRefusal.CARD_SUPERSEDED, text[:200])
    return ms.map_db_error(exc)


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except DBAPIError as exc:
        raise map_batch_error(exc) from exc


def plan_sha256(plan: MandatePlan) -> str:
    return hashing.content_sha256(
        hashing.plan_content(
            plan_id=plan.id,
            sleeve_id=plan.sleeve_id,
            symbol=plan.symbol,
            approved_terms_id=plan.approved_terms_id,
            account_ref=plan.account_ref,
            scope_account_refs=plan.scope_account_refs,
            planned_amount=plan.planned_amount,
            adopted_amount=plan.adopted_amount,
            thesis=plan.thesis,
            invalidation_conditions=plan.invalidation_conditions,
            sector_key=plan.sector_key,
        )
    )


# --------------------------------------------------------------------------- reads
async def candidates(
    session: AsyncSession, *, realm: str, basket_key: str, now: datetime
) -> tuple[list[Candidate], list[SkippedPlan]]:
    """Plans of the basket that need an answer now (§5.1 (a) and (b)).

    (a) ``proposed`` plans: approve under the sleeve's live clause.
    (b) ``approved``/``active`` plans whose bound clause is not the live clause.
    A plan whose account differs from the live clause's cannot be approved under it
    (I15), and a plan under an expired clause cannot be answered at all (the callback
    refuses both buttons): both are reported as skipped, never offered.
    """
    modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
    sleeves = (
        (
            await session.execute(
                select(StrategySleeve)
                .where(StrategySleeve.account_mode.in_(modes))
                .order_by(StrategySleeve.id)
            )
        )
        .scalars()
        .all()
    )
    found: list[Candidate] = []
    skipped: list[SkippedPlan] = []
    for sleeve in sleeves:
        version, terms = await ms.current_terms(session, sleeve.id)
        if (
            version is None
            or terms is None
            or version.state != "live"
            or terms.basket_key != basket_key
        ):
            continue
        plans = (
            (
                await session.execute(
                    select(MandatePlan)
                    .where(MandatePlan.sleeve_id == sleeve.id, ms._unreleased())
                    .order_by(MandatePlan.id)
                )
            )
            .scalars()
            .all()
        )
        for plan in plans:
            state: PlanState = await ms.plan_state(session, plan.id)
            if state.authority == "proposed":
                kind = "new_plan"
            elif (
                state.authority in v.SEND_AUTHORITY
                and state.current_terms_id != terms.id
            ):
                kind = "reapproval"
            else:
                continue
            if terms.expires_at <= now:
                skipped.append(SkippedPlan(plan.id, plan.symbol, "live_terms_expired"))
                continue
            if terms.account_ref != plan.account_ref:
                skipped.append(
                    SkippedPlan(
                        plan.id, plan.symbol, "account_ref_differs_from_live_terms"
                    )
                )
                continue
            found.append(Candidate(plan, kind, terms))
    return found, skipped


async def open_order_count(session: AsyncSession, plan_id: int) -> int:
    """Proposals bound to the plan that are not closed (shown on a re-approval card)."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(OrderProposal)
                .where(
                    OrderProposal.mandate_plan_id == plan_id,
                    OrderProposal.lifecycle_state.not_in(_CLOSED_PROPOSAL_STATES),
                )
            )
        ).scalar_one()
    )


async def get_member(
    session: AsyncSession, member_id: int
) -> MandatePlanBatchMember | None:
    return await session.get(MandatePlanBatchMember, member_id)


async def get_batch(session: AsyncSession, batch_id: int) -> MandatePlanBatch | None:
    return await session.get(MandatePlanBatch, batch_id)


async def latest_card(
    session: AsyncSession, batch_id: int, plan_id: int
) -> MandatePlanBatchMember | None:
    return (
        await session.execute(
            select(MandatePlanBatchMember)
            .where(
                MandatePlanBatchMember.batch_id == batch_id,
                MandatePlanBatchMember.plan_id == plan_id,
            )
            .order_by(MandatePlanBatchMember.card_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def answer_event(
    session: AsyncSession, batch_id: int, plan_id: int
) -> MandatePlanEvent | None:
    """The one plan event that answered this plan of this batch, if any."""
    return (
        await session.execute(
            select(MandatePlanEvent)
            .join(
                MandatePlanBatchMember,
                MandatePlanBatchMember.id == MandatePlanEvent.batch_member_id,
            )
            .where(
                MandatePlanBatchMember.batch_id == batch_id,
                MandatePlanBatchMember.plan_id == plan_id,
                # a card id on an event counts only for the plan the card belongs to
                MandatePlanEvent.plan_id == MandatePlanBatchMember.plan_id,
                # written after the card: an id persisted by the earlier schema predates
                # every card, even a card of the same plan that later takes that id
                MandatePlanEvent.created_at >= MandatePlanBatchMember.created_at,
                MandatePlanEvent.event.in_(
                    [e for ev in v.BATCH_ANSWER_EVENTS.values() for e in ev]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def members_of_batch(
    session: AsyncSession, batch_id: int
) -> list[MandatePlanBatchMember]:
    """Card 1 of every plan of the batch (the snapshot rows), in plan order."""
    return list(
        (
            await session.execute(
                select(MandatePlanBatchMember)
                .where(
                    MandatePlanBatchMember.batch_id == batch_id,
                    MandatePlanBatchMember.card_no == 1,
                )
                .order_by(MandatePlanBatchMember.plan_id)
            )
        )
        .scalars()
        .all()
    )


# -------------------------------------------------------------------------- writes
def _card(
    batch: MandatePlanBatch,
    plan: MandatePlan,
    kind: str,
    terms: MandateTerms,
    *,
    card_no: int,
    actor: str,
) -> IssuedCard:
    nonce = rules.new_nonce()
    row = MandatePlanBatchMember(
        batch_id=batch.id,
        plan_id=plan.id,
        card_no=card_no,
        kind=kind,
        terms_id=terms.id,
        terms_sha256=terms.content_sha256,
        plan_sha256=plan_sha256(plan),
        nonce_digest=rules.nonce_digest(nonce),
        created_by=actor,
    )
    return IssuedCard(row, nonce)


async def create_batch(
    session: AsyncSession,
    *,
    realm: str,
    basket_key: str,
    actor: str,
    now: datetime,
    calendar: rules.SessionCalendar | None = None,
) -> IssuedBatch:
    """Create the basket's batch for its next session, with card 1 of each plan.

    Refuses (``mandate_batch_open``) while an earlier batch of the basket is open or
    when this target session already has its batch. A batch with no plan to answer
    is still written: it is the "today's basis" summary and it holds the target
    session (I22).
    """
    who = _operator(actor)
    moment = _now(now)
    if realm not in v.REALMS:
        raise ms.MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"unknown realm {realm!r}"
        )
    await acquire_basket_lock(session, realm, basket_key)
    found, skipped = await candidates(
        session, realm=realm, basket_key=basket_key, now=moment
    )
    market = await _basket_market(session, realm, basket_key)
    calendar = calendar or rules.ExchangeCalendar()
    try:
        window = rules.batch_window(market, moment, calendar)
    except rules.NoSession as exc:
        raise ms.MandateError(v.MandateRefusal.CALENDAR_UNKNOWN, str(exc)) from exc
    batch = MandatePlanBatch(
        realm=realm,
        basket_key=basket_key,
        market=market,
        target_session=window.target_session,
        approval_deadline_at=window.approval_deadline_at,
        expires_at=window.expires_at,
        created_by=who,
    )
    session.add(batch)
    await _flush(session)
    cards = [_card(batch, c.plan, c.kind, c.terms, card_no=1, actor=who) for c in found]
    for card in cards:
        session.add(card.member)
    await _flush(session)
    return IssuedBatch(batch, tuple(cards), tuple(skipped))


async def _basket_market(session: AsyncSession, realm: str, basket_key: str) -> str:
    """The one market of a basket: its clauses' sleeves all share it (§3.1)."""
    modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
    markets = {
        row[0]
        for row in (
            await session.execute(
                select(StrategySleeve.market)
                .join(
                    StrategySleeveVersion,
                    StrategySleeveVersion.sleeve_id == StrategySleeve.id,
                )
                .join(
                    MandateTerms,
                    MandateTerms.sleeve_version_id == StrategySleeveVersion.id,
                )
                .where(
                    MandateTerms.basket_key == basket_key,
                    StrategySleeve.account_mode.in_(modes),
                )
            )
        ).all()
    }
    if len(markets) != 1:
        raise ms.MandateError(
            v.MandateRefusal.UNSUPPORTED,
            f"basket {basket_key!r} must hold exactly one market (found {sorted(markets)})",
        )
    return next(iter(markets))


async def reissue_cards(
    session: AsyncSession,
    *,
    batch_id: int,
    actor: str,
    now: datetime,
) -> list[IssuedCard]:
    """Re-send: the next card, with a new nonce, of every plan still unanswered.

    The snapshot is the card 1 snapshot. A plan whose clause is no longer the
    live one keeps its old snapshot and its card stays answerable only until the
    answer is refused for it (the answer path re-checks the live clause); that plan
    is covered again by the next day's batch.
    """
    who = _operator(actor)
    moment = _now(now)
    batch = await get_batch(session, batch_id)
    if batch is None:
        raise ms.MandateError(v.MandateRefusal.UNSUPPORTED, f"batch {batch_id} missing")
    await acquire_basket_lock(session, batch.realm, batch.basket_key)
    if moment >= batch.expires_at:
        raise ms.MandateError(v.MandateRefusal.BATCH_EXPIRED, "the batch has expired")
    issued: list[IssuedCard] = []
    for first in await members_of_batch(session, batch_id):
        if await answer_event(session, batch_id, first.plan_id) is not None:
            continue
        last = await latest_card(session, batch_id, first.plan_id)
        assert last is not None
        plan = await ms.get_plan(session, first.plan_id)
        terms = await session.get(MandateTerms, first.terms_id)
        assert plan is not None and terms is not None
        if terms.expires_at <= moment:
            continue  # an expired clause cannot be answered; the next batch re-offers it
        card = _card(
            batch, plan, first.kind, terms, card_no=last.card_no + 1, actor=who
        )
        session.add(card.member)
        issued.append(card)
    await _flush(session)
    return issued
