"""hk 3795 T3a: morning batches, cards and the ``mpb`` answer on the test database.

Runs in CI against the run-owned test database (never a production database); the DB
round trips and the lock races are not run locally (hk host safety). Test names
carry the invariant they pin (I13, I22, I21 ...).

Times: the DB triggers read the database clock (``now()``), the store and the
callback read an injected clock. Every scenario is built around the real clock with
margins of hours, so both agree: ``before`` = the deadline is an hour ahead, ``after``
= the deadline passed an hour ago and the batch is still open, ``expired`` = the batch
closed an hour ago.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.db import AsyncSessionLocal, engine
from app.services.mandate import batch_callback as cb
from app.services.mandate import batch_rules as rules
from app.services.mandate import batches as bt
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate import db_support as d
from tests.services.mandate.db_support import MandateSeed, new_symbol
from tests.services.strategy_sleeves.db_support import raw

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

KST = ZoneInfo("Asia/Seoul")
OPERATOR = 424242
ACTOR = "operator:test"


class SessionMap:
    """A calendar of explicit sessions: ``{day: (open, close)}``."""

    def __init__(self, sessions: dict[date, tuple[datetime, datetime]]) -> None:
        self.sessions = sessions

    def bounds(self, market: str, day: date):
        return self.sessions.get(day)


def calendar(open_at: datetime, close_at: datetime, days: int = 3) -> SessionMap:
    first = open_at.astimezone(KST).date()
    return SessionMap(
        {
            first + timedelta(days=i): (
                open_at + timedelta(days=i),
                close_at + timedelta(days=i),
            )
            for i in range(days)
        }
    )


def timeline(when: str):
    """``(clock at batch creation, session open, session close)`` around the real now."""
    now = datetime.now(UTC)
    if when == "before":
        return now, now + timedelta(hours=1), now + timedelta(hours=7)
    if when == "after":
        return (
            now - timedelta(hours=2),
            now - timedelta(hours=1),
            now + timedelta(hours=6),
        )
    if when == "expired":
        return (
            now - timedelta(hours=4),
            now - timedelta(hours=3),
            now - timedelta(hours=1),
        )
    raise AssertionError(when)


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


def err(code: v.MandateRefusal):
    return pytest.raises(ms.MandateError, match=code.value)


async def refused(db, statement: str, match: str, **params) -> None:
    """The statement is refused by a trigger or constraint; the transaction rolls back."""
    with pytest.raises(DBAPIError) as caught:
        await raw(db, statement, **params)
        await db.flush()
    await db.rollback()
    assert match in str(caught.value), str(caught.value)[:300]


async def count(db, table: str, where: str = "true", **params) -> int:
    return (
        await db.execute(
            sa.text(f"SELECT count(*) FROM review.{table} WHERE {where}"), params
        )
    ).scalar_one()


async def events_of(db, plan_id: int) -> list[str]:
    rows = await db.execute(
        sa.text(
            "SELECT event FROM review.mandate_plan_events WHERE plan_id = :p ORDER BY seq"
        ),
        {"p": plan_id},
    )
    return [r[0] for r in rows.all()]


async def register(seed: MandateSeed, sleeve, terms, symbol: str):
    """Register a plan and return a plain snapshot of it."""
    return seed.plan_snapshot((await seed.register(sleeve, terms, symbol)).plan)


class Scene(SimpleNamespace):
    """Plain snapshot of one batch scenario (an ORM object cannot lazy-load later)."""


async def scene(
    db,
    seed: MandateSeed,
    *,
    when: str = "before",
    approved_first: bool = False,
    symbols: int = 1,
    market: str = "equity_kr",
) -> Scene:
    """A mock-realm basket with ``symbols`` plans, one batch and card 1 of each plan."""
    syms = tuple(new_symbol() for _ in range(symbols))
    sleeve, terms = await seed.clause_sleeve(
        symbols=syms, account_mode="kiwoom_mock", market=market
    )
    plans = []
    for sym in syms:
        plan = await register(seed, sleeve, terms, sym)
        plans.append(plan)
        if approved_first:
            await seed.approve_plan(plan.id, terms)
    return await open_batch(db, seed, sleeve, terms, plans, when=when)


async def open_batch(
    db, seed, sleeve, terms, plans, *, when: str, shift: int = 0
) -> Scene:
    """``shift`` moves the calendar by whole days, to give a second batch another target."""
    created, open_at, close_at = timeline(when)
    cal = calendar(
        open_at + timedelta(days=shift), close_at + timedelta(days=shift), days=3
    )
    issued = await bt.create_batch(
        db,
        realm="mock",
        basket_key=terms.basket_key,
        actor=ACTOR,
        now=created,
        calendar=cal,
    )
    snap = Scene(
        sleeve=sleeve,
        terms=terms,
        plans=plans,
        cal=cal,
        batch_id=issued.batch.id,
        target=issued.batch.target_session,
        deadline=issued.batch.approval_deadline_at,
        expires=issued.batch.expires_at,
        basket_key=terms.basket_key,
        cards={
            c.member.plan_id: SimpleNamespace(
                id=c.member.id,
                plan_id=c.member.plan_id,
                nonce=c.nonce,
                kind=c.member.kind,
            )
            for c in issued.cards
        },
        skipped=[s.plan_id for s in issued.skipped],
    )
    seed.batch_ids.append(issued.batch.id)
    await db.commit()
    return snap


def auth(card, action: str = "approve", user: int = OPERATOR) -> cb.Authorized:
    return cb.Authorized(
        parsed=rules.ParsedCallback(action, card.id, card.nonce),
        chat_id=-1,
        message_id=None,
        callback_query_id=None,
        user_id=user,
    )


async def answer(sc: Scene, card, action: str = "approve") -> cb.Outcome:
    async with AsyncSessionLocal() as session:
        return await cb.answer(
            session, auth(card, action), now=datetime.now(UTC), calendar=sc.cal
        )


async def second_live_terms(
    db, seed: MandateSeed, sleeve, terms, symbols: tuple[str, ...]
):
    """A new live clause for the same sleeve (changed sector cap, same account)."""
    request = await ms.create_terms_request(
        db,
        sleeve_id=sleeve.id,
        data=ms.TermsInput(
            basket_key=terms.basket_key,
            account_ref=terms.account_ref,
            universe_rule="test universe",
            allowed_symbols=list(symbols),
            entry_max_premium_bps="30",
            per_order_cap="2000000",
            daily_cap="5000000",
            sector_cap_pct="36",
            basket_drawdown_freeze_pct="25",
            account_drawdown_freeze_pct="25",
            tranche_rule=d.TRANCHE_RULE,
            expires_at=d.FAR,
            budget_cap="10000000",
            loss_limit="500000",
            prereg=ms.PreRegistration(
                d.PREREG["metric"],
                ms.Decimal(d.PREREG["min_effect_pct"]),
                int(d.PREREG["horizon_sessions"]),
                int(d.PREREG["max_duration_days"]),
            ),
        ),
        global_caps=d.GLOBAL_CAPS,
        approval_ref="hk:test2",
        requested_by="operator:test",
    )
    result = await ms.record_approval(
        db,
        request_id=request.id,
        decision="approved",
        principal=d.PRINCIPAL,
        nonce_digest=d.digest("second"),
    )
    await db.commit()
    assert result.terms is not None
    return SimpleNamespace(
        id=result.terms.id,
        account_ref=result.terms.account_ref,
        basket_key=result.terms.basket_key,
        content_sha256=result.terms.content_sha256,
    )


# ---------------------------------------------------------------------- I13 / I22
async def test_i13_batch_and_card_rows_are_append_only(db_session, seed) -> None:
    sc = await scene(db_session, seed)
    card = sc.cards[sc.plans[0].id]
    await refused(
        db_session,
        "UPDATE review.mandate_plan_batches SET created_by = 'x' WHERE id = :b",
        "append-only",
        b=sc.batch_id,
    )
    await refused(
        db_session,
        "DELETE FROM review.mandate_plan_batch_members WHERE id = :m",
        "append-only",
        m=card.id,
    )
    await refused(
        db_session,
        "UPDATE review.mandate_plan_batch_members SET nonce_digest = :n WHERE id = :m",
        "append-only",
        m=card.id,
        n="f" * 64,
    )
    await refused(
        db_session, "TRUNCATE review.mandate_plan_batch_members", "append-only"
    )


async def test_i22_a_new_plan_gets_card_one_with_the_snapshot_and_only_a_digest(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    assert card.kind == "new_plan" and sc.skipped == []
    row = (
        await db_session.execute(
            sa.text(
                "SELECT card_no, terms_id, terms_sha256, plan_sha256, nonce_digest "
                "FROM review.mandate_plan_batch_members WHERE id = :m"
            ),
            {"m": card.id},
        )
    ).one()
    assert row.card_no == 1 and row.terms_id == sc.terms.id
    assert row.nonce_digest == rules.nonce_digest(card.nonce) != card.nonce
    assert (
        row.terms_sha256
        == (
            await db_session.execute(
                sa.text(
                    "SELECT content_sha256 FROM review.mandate_terms WHERE id = :t"
                ),
                {"t": sc.terms.id},
            )
        ).scalar_one()
    )
    assert len(row.plan_sha256) == 64
    assert sc.deadline < sc.expires
    assert sc.target == sc.deadline.astimezone(KST).date()


async def test_i22_one_batch_per_basket_and_target_session(db_session, seed) -> None:
    sc = await scene(db_session, seed)
    with err(v.MandateRefusal.BATCH_OPEN):
        await open_batch(db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before")
    await db_session.rollback()
    assert (
        await count(db_session, "mandate_plan_batches", "id = :b", b=sc.batch_id) == 1
    )


async def test_i22_a_batch_with_nothing_to_answer_still_holds_the_session(
    db_session, seed
) -> None:
    sc = await scene(
        db_session, seed, approved_first=True
    )  # approved on the live clause
    assert sc.cards == {}
    assert (
        await count(db_session, "mandate_plan_batches", "id = :b", b=sc.batch_id) == 1
    )
    with err(v.MandateRefusal.BATCH_OPEN):
        await open_batch(db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before")
    await db_session.rollback()


async def test_i22_a_new_batch_waits_for_the_earlier_batch_to_close(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    # another target session while the first batch has an unanswered plan: refused
    with err(v.MandateRefusal.BATCH_OPEN):
        await open_batch(
            db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before", shift=1
        )
    await db_session.rollback()
    # the plan is answered: the earlier batch is closed and the next one may be made
    out = await answer(sc, sc.cards[plan.id])
    assert out.handled
    second = await open_batch(
        db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before", shift=1
    )
    assert second.batch_id != sc.batch_id and second.target != sc.target


async def test_i22_an_expired_batch_is_closed_even_with_an_unanswered_plan(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed, when="expired")
    # the earlier batch expired an hour ago: a batch for today may be made
    second = await open_batch(
        db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before", shift=2
    )
    assert second.batch_id != sc.batch_id
    # the plan is still unanswered and is offered again (a new card 1 in the new batch)
    assert set(second.cards) == {sc.plans[0].id}


async def test_i22_the_database_trigger_holds_without_the_store(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batches (realm, basket_key, market, "
        "target_session, approval_deadline_at, expires_at, created_by) VALUES "
        "('mock', :k, 'equity_kr', :t, now() + interval '1 day', "
        "now() + interval '2 day', 'op')",
        "open batch",
        k=sc.basket_key,
        t=sc.target + timedelta(days=5),
    )


async def test_i22_a_realm_other_than_the_baskets_does_not_block(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    # the same basket key in the other realm is a different basket
    await raw(
        db_session,
        "INSERT INTO review.mandate_plan_batches (realm, basket_key, market, "
        "target_session, approval_deadline_at, expires_at, created_by) VALUES "
        "('live', :k, 'equity_kr', :t, now() + interval '1 day', "
        "now() + interval '2 day', 'op')",
        k=sc.basket_key,
        t=sc.target,
    )
    await db_session.flush()
    row = (
        await db_session.execute(
            sa.text(
                "SELECT id FROM review.mandate_plan_batches "
                "WHERE realm = 'live' AND basket_key = :k"
            ),
            {"k": sc.basket_key},
        )
    ).scalar_one()
    seed.batch_ids.append(row)
    await db_session.commit()


async def test_a_card_joins_a_batch_only_in_the_batch_own_transaction(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed, approved_first=True)  # no card made
    plan = sc.plans[0]
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
        "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
        "SELECT :b, :p, 1, 'reapproval', t.id, t.content_sha256, :h, :n, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "own transaction",
        b=sc.batch_id,
        p=plan.id,
        t=sc.terms.id,
        h="a" * 64,
        n=rules.nonce_digest(rules.new_nonce()),
    )


async def test_the_card_kind_must_fit_the_plan_authority(db_session, seed) -> None:
    """A ``reapproval`` card for a plan that is only ``proposed`` is refused."""
    syms = (new_symbol(),)
    sleeve, terms = await seed.clause_sleeve(symbols=syms, account_mode="kiwoom_mock")
    plan_id = (await register(seed, sleeve, terms, syms[0])).id
    batch_id = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_plan_batches (realm, basket_key, market, "
                "target_session, approval_deadline_at, expires_at, created_by) VALUES "
                "('mock', :k, 'equity_kr', current_date, now() + interval '1 hour', "
                "now() + interval '7 hour', 'op') RETURNING id"
            ),
            {"k": terms.basket_key},
        )
    ).scalar_one()
    seed.batch_ids.append(batch_id)
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
        "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
        "SELECT :b, :p, 1, 'reapproval', t.id, t.content_sha256, :h, :n, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "does not fit the card kind",
        b=batch_id,
        p=plan_id,
        t=terms.id,
        h="a" * 64,
        n=rules.nonce_digest(rules.new_nonce()),
    )


async def test_a_card_names_the_clause_of_the_plan_sleeve_and_basket(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    other_syms = (new_symbol(),)
    _, other_terms = await seed.clause_sleeve(
        symbols=other_syms, account_mode="kiwoom_mock"
    )
    plan = sc.plans[0]
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
        "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
        "SELECT :b, :p, 2, 'new_plan', t.id, t.content_sha256, :h, :n, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "not the plan sleeve clause",
        b=sc.batch_id,
        p=plan.id,
        t=other_terms.id,
        h="a" * 64,
        n=rules.nonce_digest(rules.new_nonce()),
    )


# --------------------------------------------------------------------- re-sending
async def test_resend_appends_the_next_card_and_the_old_card_stops_answering(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    first = sc.cards[plan.id]
    again = await bt.reissue_cards(
        db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
    )
    snap = [
        SimpleNamespace(id=c.member.id, nonce=c.nonce, no=c.member.card_no)
        for c in again
    ]
    await db_session.commit()
    assert [c.no for c in snap] == [2] and snap[0].nonce != first.nonce
    assert (
        await count(db_session, "mandate_plan_batches", "id = :b", b=sc.batch_id) == 1
    )
    out = await answer(sc, first)
    assert out == cb.Outcome(False, "mandate_card_superseded")
    assert await events_of(db_session, plan.id) == ["proposed"]
    out = await answer(sc, snap[0])
    assert out.handled and out.reason == "approved"


async def test_resend_keeps_the_snapshot_and_numbers_cards_in_sequence(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    await bt.reissue_cards(
        db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
    )
    await db_session.commit()
    # a changed snapshot (hash cannot be extended: build it from another value)
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
        "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
        "SELECT :b, :p, 3, 'new_plan', t.id, t.content_sha256, :h, :n, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "keeps the snapshot of card 1",
        b=sc.batch_id,
        p=plan.id,
        t=sc.terms.id,
        h="c" * 64,
        n=rules.nonce_digest(rules.new_nonce()),
    )
    # a skipped number
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
        "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
        "SELECT :b, :p, 5, 'new_plan', t.id, t.content_sha256, "
        "(SELECT plan_sha256 FROM review.mandate_plan_batch_members "
        " WHERE batch_id = :b AND plan_id = :p AND card_no = 1), :n, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "numbered in sequence",
        b=sc.batch_id,
        p=plan.id,
        t=sc.terms.id,
        n=rules.nonce_digest(rules.new_nonce()),
    )


async def test_resend_skips_a_plan_that_is_already_answered(db_session, seed) -> None:
    sc = await scene(db_session, seed, symbols=2)
    first, second = sc.plans
    assert (await answer(sc, sc.cards[first.id])).handled
    again = await bt.reissue_cards(
        db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
    )
    assert [c.member.plan_id for c in again] == [second.id]
    await db_session.commit()


async def test_resend_after_the_expiry_is_refused(db_session, seed) -> None:
    sc = await scene(db_session, seed, when="expired")
    with err(v.MandateRefusal.BATCH_EXPIRED):
        await bt.reissue_cards(
            db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
        )
    await db_session.rollback()


# ----------------------------------------------------------- the answer (I21, §5.1)
async def test_approve_before_the_deadline_is_effective_on_the_target_session(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed, when="before")
    plan = sc.plans[0]
    out = await answer(sc, sc.cards[plan.id])
    assert out.handled and out.reason == "approved"
    row = (
        await db_session.execute(
            sa.text(
                "SELECT event, effective_session, terms_id, actor, batch_member_id "
                "FROM review.mandate_plan_events WHERE plan_id = :p AND seq = 2"
            ),
            {"p": plan.id},
        )
    ).one()
    assert row.event == "approved" and row.effective_session == sc.target
    assert row.terms_id == sc.terms.id
    assert row.actor == f"operator:telegram:{OPERATOR}"
    assert row.batch_member_id == sc.cards[plan.id].id


async def test_approve_after_the_deadline_is_effective_on_the_next_session(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed, when="after")
    plan = sc.plans[0]
    out = await answer(sc, sc.cards[plan.id])
    assert out.handled
    effective = (
        await db_session.execute(
            sa.text(
                "SELECT effective_session FROM review.mandate_plan_events "
                "WHERE plan_id = :p AND event = 'approved'"
            ),
            {"p": plan.id},
        )
    ).scalar_one()
    assert effective == sc.target + timedelta(days=1)


async def test_reject_writes_rejected_with_the_card_clause(db_session, seed) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    out = await answer(sc, sc.cards[plan.id], "reject")
    assert out.handled and out.reason == "rejected"
    row = (
        await db_session.execute(
            sa.text(
                "SELECT event, terms_id, effective_session FROM review.mandate_plan_events "
                "WHERE plan_id = :p AND seq = 2"
            ),
            {"p": plan.id},
        )
    ).one()
    assert (row.event, row.terms_id, row.effective_session) == (
        "rejected",
        sc.terms.id,
        None,
    )


async def test_i21_a_reapproval_card_rebinds_the_plan_to_the_new_clause(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    new_terms = await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    sc = await open_batch(db_session, seed, sleeve, new_terms, [plan], when="before")
    card = sc.cards[plan.id]
    assert card.kind == "reapproval"
    out = await answer(sc, card)
    assert out.handled
    assert await events_of(db_session, plan.id) == [
        "proposed",
        "approved",
        "terms_rebound",
    ]
    bound = (
        await db_session.execute(
            sa.text(
                "SELECT terms_id FROM review.mandate_plan_events "
                "WHERE plan_id = :p AND event = 'terms_rebound'"
            ),
            {"p": plan.id},
        )
    ).scalar_one()
    assert bound == new_terms.id


async def test_a_rejected_reapproval_retires_the_plan_and_keeps_the_reservation(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    new_terms = await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    sc = await open_batch(db_session, seed, sleeve, new_terms, [plan], when="before")
    out = await answer(sc, sc.cards[plan.id], "reject")
    assert out.handled and out.facts is not None and out.facts.open_order_count == 0
    assert (await events_of(db_session, plan.id))[-1] == "retired"
    # retired is not released: the symbol stays reserved (I19)
    assert (
        await count(
            db_session,
            "mandate_symbol_reservations r",
            "r.plan_id = :p AND NOT EXISTS (SELECT 1 FROM "
            "review.mandate_symbol_reservation_releases x WHERE x.reservation_id = r.id)",
            p=plan.id,
        )
        == 1
    )


async def test_a_nonce_is_spent_by_the_answer(db_session, seed) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    assert (await answer(sc, card)).handled
    again = await answer(sc, card)
    assert again == cb.Outcome(False, "mandate_batch_answered")
    assert (await answer(sc, card, "reject")).reason == "mandate_batch_answered"
    assert await events_of(db_session, plan.id) == ["proposed", "approved"]
    assert (
        await count(
            db_session,
            "mandate_plan_events",
            "plan_id = :p AND batch_member_id IS NOT NULL",
            p=plan.id,
        )
        == 1
    )


async def test_a_wrong_nonce_writes_nothing_and_the_card_stays_answerable(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    wrong = SimpleNamespace(id=card.id, nonce=rules.new_nonce())
    assert (await answer(sc, wrong)).reason == "mandate_nonce_mismatch"
    assert await events_of(db_session, plan.id) == ["proposed"]
    assert (await answer(sc, card)).handled


async def test_i21_an_unlisted_sender_writes_nothing_and_spends_no_nonce(
    db_session, seed, monkeypatch
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "-1")
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", str(OPERATOR))

    def update(user: int) -> dict:
        return {
            "callback_query": {
                "id": "q",
                "from": {"id": user},
                "message": {"message_id": 1, "chat": {"id": -1}},
                "data": rules.build_callback_data("approve", card.id, card.nonce),
            }
        }

    class Silent:
        async def answer_callback(self, *a, **k):
            return True

        async def edit_message(self, *a, **k):
            return None

    result = await cb.handle_mandate_callback(
        update(7), now=datetime.now(UTC), notifier=Silent(), calendar=sc.cal
    )
    assert result == {"handled": False, "reason": "mandate_approver_not_allowed"}
    assert await events_of(db_session, plan.id) == ["proposed"]
    # the operator can still answer with the same card
    result = await cb.handle_mandate_callback(
        update(OPERATOR), now=datetime.now(UTC), notifier=Silent(), calendar=sc.cal
    )
    assert result == {"handled": True, "reason": "approved"}


async def test_an_empty_approver_list_records_nobody_s_approval(
    db_session, seed, monkeypatch
) -> None:
    sc = await scene(db_session, seed)
    card = sc.cards[sc.plans[0].id]
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "-1")
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", "")
    update = {
        "callback_query": {
            "id": "q",
            "from": {"id": OPERATOR},
            "message": {"message_id": 1, "chat": {"id": -1}},
            "data": rules.build_callback_data("approve", card.id, card.nonce),
        }
    }

    class Silent:
        async def answer_callback(self, *a, **k):
            return True

        async def edit_message(self, *a, **k):
            return None

    result = await cb.handle_mandate_callback(
        update, now=datetime.now(UTC), notifier=Silent(), calendar=sc.cal
    )
    assert result["reason"] == "mandate_approver_not_allowed"
    assert await events_of(db_session, sc.plans[0].id) == ["proposed"]


async def test_an_expired_batch_takes_no_answer(db_session, seed) -> None:
    sc = await scene(db_session, seed, when="expired")
    plan = sc.plans[0]
    out = await answer(sc, sc.cards[plan.id])
    assert out == cb.Outcome(False, "mandate_batch_expired")
    assert await events_of(db_session, plan.id) == ["proposed"]
    # the database refuses it too, for any writer
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, actor) "
        "SELECT :p, 2, 'approved', t.id, t.content_sha256, :e, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "has expired",
        p=plan.id,
        t=sc.terms.id,
        e=sc.target,
        m=sc.cards[plan.id].id,
    )


async def test_a_stale_clause_takes_no_answer_and_stays_for_the_next_batch(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    sc = await open_batch(db_session, seed, sleeve, terms, [plan], when="before")
    new_terms = await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    out = await answer(sc, sc.cards[plan.id])
    assert out == cb.Outcome(False, "mandate_terms_not_current")
    assert await events_of(db_session, plan.id) == ["proposed"]
    # the database refuses an approval under the old clause for any writer
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, actor) "
        "SELECT :p, 2, 'approved', t.id, t.content_sha256, :e, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "no longer the live clause",
        p=plan.id,
        t=terms.id,
        e=sc.target,
        m=sc.cards[plan.id].id,
    )
    assert new_terms.id != terms.id


async def test_the_trigger_refuses_an_answer_that_does_not_fit_the_card_kind(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, actor) "
        "SELECT :p, 2, 'terms_rebound', t.id, t.content_sha256, :e, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "does not answer this kind of card",
        p=plan.id,
        t=sc.terms.id,
        e=sc.target,
        m=sc.cards[plan.id].id,
    )


async def test_the_trigger_refuses_a_wrong_effective_session_for_any_writer(
    db_session, seed
) -> None:
    before = await scene(db_session, seed, when="before")
    plan = before.plans[0]
    insert = (
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, actor) "
        "SELECT :p, 2, 'approved', t.id, t.content_sha256, :e, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t"
    )
    await refused(
        db_session,
        insert,
        "takes effect on the target session",
        p=plan.id,
        t=before.terms.id,
        e=before.target + timedelta(days=1),
        m=before.cards[plan.id].id,
    )
    after = await scene(db_session, seed, when="after")
    plan2 = after.plans[0]
    await refused(
        db_session,
        insert,
        "takes effect after the target session",
        p=plan2.id,
        t=after.terms.id,
        e=after.target,
        m=after.cards[plan2.id].id,
    )


async def test_the_trigger_refuses_a_second_answer_and_a_card_of_another_plan(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed, symbols=2)
    first, second = sc.plans
    assert (await answer(sc, sc.cards[first.id])).handled
    insert = (
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, actor) "
        "SELECT :p, :s, :ev, t.id, t.content_sha256, :e, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t"
    )
    await refused(
        db_session,
        insert,
        "already answered",
        p=first.id,
        s=3,
        ev="approved",
        t=sc.terms.id,
        e=sc.target,
        m=sc.cards[first.id].id,
    )
    await refused(
        db_session,
        insert,
        "names no card of this plan",
        p=second.id,
        s=2,
        ev="approved",
        t=sc.terms.id,
        e=sc.target,
        m=sc.cards[first.id].id,
    )


async def test_i22_two_presses_at_once_write_one_answer(db_session, seed) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    results = await asyncio.gather(
        answer(sc, card), answer(sc, card), answer(sc, card, "reject")
    )
    handled = [r for r in results if r.handled]
    assert len(handled) == 1, results
    assert all(
        r.reason in ("mandate_batch_answered", "approved", "rejected") for r in results
    )
    assert (
        await count(
            db_session,
            "mandate_plan_events",
            "plan_id = :p AND batch_member_id IS NOT NULL",
            p=plan.id,
        )
        == 1
    )


async def test_the_callback_writes_plan_events_and_nothing_on_the_order_side(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    before = {
        "order_proposals": await count(db_session, "order_proposals"),
        "execution_ledger": await count(db_session, "execution_ledger"),
    }
    assert (await answer(sc, sc.cards[plan.id])).handled
    after = {
        "order_proposals": await count(db_session, "order_proposals"),
        "execution_ledger": await count(db_session, "execution_ledger"),
    }
    assert after == before


async def test_candidates_skip_a_plan_whose_account_differs_from_the_live_clause(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    # a second live clause with another account reference cannot approve this plan
    request = await ms.create_terms_request(
        db_session,
        sleeve_id=sleeve.id,
        data=ms.TermsInput(
            basket_key=terms.basket_key,
            account_ref="another-account",
            universe_rule="u",
            allowed_symbols=[sym],
            entry_max_premium_bps="30",
            per_order_cap="2000000",
            daily_cap="5000000",
            sector_cap_pct="35",
            basket_drawdown_freeze_pct="25",
            account_drawdown_freeze_pct="25",
            tranche_rule=d.TRANCHE_RULE,
            expires_at=d.FAR,
            budget_cap="10000000",
            loss_limit="500000",
            prereg=ms.PreRegistration(
                d.PREREG["metric"],
                ms.Decimal(d.PREREG["min_effect_pct"]),
                int(d.PREREG["horizon_sessions"]),
                int(d.PREREG["max_duration_days"]),
            ),
        ),
        global_caps=d.GLOBAL_CAPS,
        approval_ref="hk:acct",
        requested_by="operator:test",
    )
    await ms.record_approval(
        db_session,
        request_id=request.id,
        decision="approved",
        principal=d.PRINCIPAL,
        nonce_digest=d.digest("acct"),
    )
    await db_session.commit()
    sc = await open_batch(db_session, seed, sleeve, terms, [plan], when="before")
    assert sc.cards == {} and sc.skipped == [plan.id]
