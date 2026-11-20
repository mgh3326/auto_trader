"""hk 3795 T3a: morning batches, cards and the ``mpb`` answer on the test database.

Runs in CI against the run-owned test database (never a production database); the DB
round trips and the lock races are not run locally (hk host safety). Test names
carry the invariant they pin (I13, I22, I21 ...).

Times: the DB triggers read the database wall clock (``clock_timestamp()``, never
``now()``, which is the transaction start), the store and the callback read an injected
clock. Most scenarios are built around the real clock with margins of hours, so both
agree: ``before`` = the deadline is an hour ahead, ``after`` = the deadline passed an hour
ago and the batch is still open, ``expired`` = a batch created while open and then aged
(append-only triggers off, test only) so that it closed an hour ago. The race and expiry
tests that must cross a deadline in real time use margins of seconds instead and say so.
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
from app.services.mandate.locks import acquire_basket_lock
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


async def register(seed: MandateSeed, sleeve, terms, symbol: str, **over):
    """Register a plan and return a plain snapshot of it."""
    return seed.plan_snapshot((await seed.register(sleeve, terms, symbol, **over)).plan)


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
    times=None,
) -> Scene:
    """A mock-realm basket with ``symbols`` plans, one batch and card 1 of each plan."""
    syms = tuple(new_symbol() for _ in range(symbols))
    sleeve, terms = await seed.clause_sleeve(
        symbols=syms, account_mode="kiwoom_mock", market=market
    )
    plans = []
    for i, sym in enumerate(syms):
        # one sector per plan: two plans of one sector would hit the 35% sector cap
        plan = await register(seed, sleeve, terms, sym, sector=f"sector-{i}")
        plans.append(plan)
        if approved_first:
            await seed.approve_plan(plan.id, terms)
    return await open_batch(db, seed, sleeve, terms, plans, when=when, times=times)


async def open_batch(
    db, seed, sleeve, terms, plans, *, when: str, shift: int = 0, times=None
) -> Scene:
    """``shift`` moves the calendar by whole days, to give a second batch another target."""
    # a batch cannot be CREATED after its own close (create_batch refuses it), so an
    # "expired" scene is created open and aged afterwards
    aged = when == "expired" and times is None
    created, open_at, close_at = times or timeline("before" if aged else when)
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
    if aged:
        await age_batch(db, issued.batch.id)
        snap.deadline, snap.expires = await batch_deadlines(db, issued.batch.id)
    return snap


async def age_batch(db, batch_id: int) -> None:
    """Test only: make an existing batch closed an hour ago (deadline three hours ago).

    The table is append-only for every role, so the rows are rewritten with the triggers
    off (``session_replication_role``) in their own short transaction."""
    await raw(db, "SET LOCAL session_replication_role = replica")
    await raw(
        db,
        "UPDATE review.mandate_plan_batches SET "
        "approval_deadline_at = clock_timestamp() - interval '3 hours', "
        "expires_at = clock_timestamp() - interval '1 hour' WHERE id = :b",
        b=batch_id,
    )
    await db.commit()


async def batch_deadlines(db, batch_id: int):
    row = (
        await db.execute(
            sa.text(
                "SELECT approval_deadline_at, expires_at "
                "FROM review.mandate_plan_batches WHERE id = :b"
            ),
            {"b": batch_id},
        )
    ).one()
    await db.rollback()
    return row[0], row[1]


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
        return await cb.answer(session, auth(card, action), calendar=sc.cal)


async def second_live_terms(
    db,
    seed: MandateSeed,
    sleeve,
    terms,
    symbols: tuple[str, ...],
    sector_cap: str = "36",
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
            sector_cap_pct=sector_cap,
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
        db_session,
        # both tables in one TRUNCATE: a lone TRUNCATE of the cards is refused by the
        # answer link's foreign key before any trigger runs; this one reaches the
        # members' own append-only trigger, whose message names the table
        "TRUNCATE review.mandate_plan_batch_members, review.mandate_plan_events",
        "mandate_plan_batch_members rows are append-only",
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
    times = timeline("before")  # one timeline: both attempts target the same session
    sc = await scene(
        db_session, seed, approved_first=True, times=times
    )  # approved on the live clause
    assert sc.cards == {}
    assert (
        await count(db_session, "mandate_plan_batches", "id = :b", b=sc.batch_id) == 1
    )
    with err(v.MandateRefusal.BATCH_OPEN):
        await open_batch(
            db_session, seed, sc.sleeve, sc.terms, sc.plans, when="before", times=times
        )
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
            "plan_id = :p AND answered_batch_member_id IS NOT NULL",
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
            "plan_id = :p AND answered_batch_member_id IS NOT NULL",
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


# -------------------------------------------- review round 1: I22 at card admission
CARD = (
    "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
    "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by) "
    "SELECT :b, :p, :no, 'new_plan', t.id, t.content_sha256, :h, :n, 'op' "
    "FROM review.mandate_terms t WHERE t.id = :t"
)
BATCH = (
    "INSERT INTO review.mandate_plan_batches (realm, basket_key, market, "
    "target_session, approval_deadline_at, expires_at, created_by) VALUES "
    "('mock', :k, 'equity_kr', current_date + CAST(:d AS integer), now() + interval '1 hour', "
    "now() + interval '7 hour', 'op') RETURNING id"
)


async def test_i22_two_batches_made_before_any_card_cannot_both_hold_one_plan(
    db_session, seed
) -> None:
    """Both batch rows exist (empty) before either gets members: the second batch's
    card 1 for the same plan is refused when the first batch has it open."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    first = (
        await db_session.execute(sa.text(BATCH), {"k": terms.basket_key, "d": 10})
    ).scalar_one()
    second = (
        await db_session.execute(sa.text(BATCH), {"k": terms.basket_key, "d": 11})
    ).scalar_one()
    params = {"p": plan.id, "no": 1, "h": "a" * 64, "t": terms.id}
    await raw(
        db_session, CARD, b=first, n=rules.nonce_digest(rules.new_nonce()), **params
    )
    await db_session.flush()
    await refused(
        db_session,
        CARD,
        "a plan joins only one open batch",
        b=second,
        n=rules.nonce_digest(rules.new_nonce()),
        **params,
    )


# ------------------------------------------ review round 1: answer vs re-send race
async def _try(session, statement: str, **params) -> str | None:
    """Run one statement; the DB error text, or None when it went through."""
    try:
        await raw(session, statement, **params)
        await session.flush()
        return None
    except DBAPIError as exc:
        await session.rollback()
        return str(exc)


async def _pid(session) -> int:
    """The backend process id of the session's connection (its transaction goes on)."""
    return (await session.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()


async def _wait_until_blocked_by(
    observer, holder_pid: int, contender_pid: int, timeout: float = 10.0
) -> None:
    """Wait until PostgreSQL reports that the contender backend is blocked BY the holder
    backend (``pg_blocking_pids``): not merely that some backend somewhere waits on a lock,
    and not that a task has not started yet."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        blocked = (
            await observer.execute(
                sa.text("SELECT :h = ANY(pg_blocking_pids(:c))"),
                {"h": holder_pid, "c": contender_pid},
            )
        ).scalar_one()
        await observer.rollback()
        if blocked:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"backend {contender_pid} was never blocked by backend {holder_pid}"
    )


ANSWER = (
    "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
    "terms_sha256, effective_session, batch_member_id, actor) "
    "SELECT :p, 2, 'approved', t.id, t.content_sha256, :e, :m, 'op' "
    "FROM review.mandate_terms t WHERE t.id = :t"
)
RESEND = CARD.replace(
    "SELECT :b, :p, :no, 'new_plan', t.id, t.content_sha256, :h,",
    "SELECT :b, :p, :no, 'new_plan', t.id, t.content_sha256, "
    "(SELECT plan_sha256 FROM review.mandate_plan_batch_members "
    " WHERE batch_id = :b AND plan_id = :p AND card_no = 1),",
)


async def test_i22_an_answer_that_commits_first_blocks_a_concurrent_resend(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        assert holder_pid != other_pid
        assert (
            await _try(holder, ANSWER, p=plan.id, t=sc.terms.id, e=sc.target, m=card.id)
            is None
        )  # uncommitted: holds the per-plan lock
        task = asyncio.create_task(
            _try(
                other,
                RESEND,
                b=sc.batch_id,
                p=plan.id,
                no=2,
                t=sc.terms.id,
                n=rules.nonce_digest(rules.new_nonce()),
            )
        )
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        assert not task.done(), "the resend did not wait for the answer"
        await holder.commit()
        message = await asyncio.wait_for(task, 15)
    assert message is not None and "already answered" in message
    assert (
        await count(
            db_session,
            "mandate_plan_batch_members",
            "batch_id = :b AND plan_id = :p AND card_no = 2",
            b=sc.batch_id,
            p=plan.id,
        )
        == 0
    )


async def test_i22_a_resend_that_commits_first_blocks_a_concurrent_answer(
    db_session, seed
) -> None:
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        assert (
            await _try(
                holder,
                RESEND,
                b=sc.batch_id,
                p=plan.id,
                no=2,
                t=sc.terms.id,
                n=rules.nonce_digest(rules.new_nonce()),
            )
            is None
        )  # uncommitted card 2
        task = asyncio.create_task(
            _try(other, ANSWER, p=plan.id, t=sc.terms.id, e=sc.target, m=card.id)
        )
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        assert not task.done(), "the answer did not wait for the resend"
        await holder.commit()
        message = await asyncio.wait_for(task, 15)
    assert message is not None and "a newer card replaced this one" in message
    assert await events_of(db_session, plan.id) == ["proposed"]


# ------------------------------------- review round 1: a rejection needs a live clause
async def test_i21_a_rejection_of_a_stale_reapproval_card_writes_no_retired_event(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    live = await second_live_terms(
        db_session, seed, sleeve, terms, (sym,), sector_cap="36"
    )
    sc = await open_batch(db_session, seed, sleeve, live, [plan], when="before")
    card = sc.cards[plan.id]
    assert card.kind == "reapproval"
    # the sleeve version changes after the card went out
    await second_live_terms(db_session, seed, sleeve, live, (sym,), sector_cap="37")
    out = await answer(sc, card, "reject")
    assert out == cb.Outcome(False, "mandate_terms_not_current")
    assert (await events_of(db_session, plan.id))[-1] == "approved"
    # and the database refuses the retirement for any writer, not only the callback
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, batch_member_id, actor) "
        "SELECT :p, 3, 'retired', t.id, t.content_sha256, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "no longer the live clause",
        p=plan.id,
        t=live.id,
        m=card.id,
    )


async def test_i21_a_rejection_of_a_stale_new_plan_card_is_refused_by_the_database(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    sc = await open_batch(db_session, seed, sleeve, terms, [plan], when="before")
    card = sc.cards[plan.id]
    await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    assert (await answer(sc, card, "reject")).reason == "mandate_terms_not_current"
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, batch_member_id, actor) "
        "SELECT :p, 2, 'rejected', t.id, t.content_sha256, :m, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t",
        "no longer the live clause",
        p=plan.id,
        t=terms.id,
        m=card.id,
    )


# ----------------------------------------------- review round 1: CLI on real sessions
async def test_status_reads_an_existing_batch_through_a_real_session(
    db_session, seed
) -> None:
    from scripts import mandate_batch as cli

    sc = await scene(db_session, seed)
    args = cli._parser().parse_args(
        ["--database-url-env", "X", "status", "--batch-id", str(sc.batch_id)]
    )
    async with AsyncSessionLocal() as session:
        payload = await cli._status(session, args)
    assert payload["batch"]["id"] == sc.batch_id
    assert payload["plans"][0]["plan_id"] == sc.plans[0].id
    assert (
        payload["plans"][0]["latest_card_no"] == 1
        and payload["plans"][0]["answer"] is None
    )


async def test_resend_of_a_summary_only_batch_sends_the_summary(
    db_session, seed
) -> None:
    from scripts import mandate_batch as cli

    sc = await scene(db_session, seed, approved_first=True)  # nothing to answer
    assert sc.cards == {}
    sent: list[str] = []

    async def sender(text, keyboard, reply_to=None):
        sent.append(text)
        return True, None, 1000 + len(sent)

    args = cli._parser().parse_args(
        [
            "--database-url-env",
            "X",
            "resend",
            "--batch-id",
            str(sc.batch_id),
            "--actor",
            ACTOR,
            "--commit",
        ]
    )
    code, body = await cli.run(
        args, session_factory=AsyncSessionLocal, sender=sender, now=datetime.now(UTC)
    )
    assert code == 0 and [r["message"] for r in body["sent"]] == ["summary"]
    assert len(sent) == 1 and "승인할 변경이 없습니다" in sent[0]


async def test_i22_an_answer_whose_transaction_began_before_the_close_is_refused_after_it(
    db_session, seed
) -> None:
    """The old Reject's transaction starts before the batch closes and resumes after the
    successor batch took an Approve: the trigger reads the wall clock, so the retired
    event cannot override the newer approval."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    live = await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    now = datetime.now(UTC)
    old = await open_batch(
        db_session,
        seed,
        sleeve,
        live,
        [plan],
        when="before",
        times=(now, now + timedelta(seconds=1), now + timedelta(seconds=8)),
    )
    old_card = old.cards[plan.id]
    assert old_card.kind == "reapproval"
    async with AsyncSessionLocal() as stale:
        started = (
            await stale.execute(sa.text("SELECT transaction_timestamp()"))
        ).scalar_one()  # its transaction (and now()) starts here
        assert started < old.expires, "the stale transaction began after the close"
        await asyncio.sleep(
            max(0.0, (old.expires - datetime.now(UTC)).total_seconds()) + 0.5
        )
        # the old batch is closed: a successor batch takes the plan and an Approve
        successor = await open_batch(
            db_session, seed, sleeve, live, [plan], when="before", shift=1
        )
        out = await answer(successor, successor.cards[plan.id])
        assert out.handled and out.reason == "approved"
        message = await _try(
            stale,
            "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
            "terms_sha256, batch_member_id, actor) "
            "SELECT :p, 4, 'retired', t.id, t.content_sha256, :m, 'op' "
            "FROM review.mandate_terms t WHERE t.id = :t",
            p=plan.id,
            t=live.id,
            m=old_card.id,
        )
    assert message is not None and "the batch has expired" in message
    assert await events_of(db_session, plan.id) == [
        "proposed",
        "approved",
        "terms_rebound",
    ]


async def test_i22_an_answer_straddling_the_opening_deadline_takes_the_next_session(
    db_session, seed, monkeypatch
) -> None:
    """Decided just before the opening bell, written just after it: the trigger judges
    the write on its own clock, so the approval is recorded for the session after the
    target (§5.1) instead of being refused or landing on the target session."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    now = datetime.now(UTC)
    sc = await open_batch(
        db_session,
        seed,
        sleeve,
        terms,
        [plan],
        when="before",
        times=(now, now + timedelta(seconds=4), now + timedelta(hours=7)),
    )
    card = sc.cards[plan.id]
    real_append = ms.append_plan_event

    async def slow_append(session, **kwargs):
        # decided before the deadline (effective = target); the write lands after it
        assert kwargs["effective_session"] == sc.target
        await asyncio.sleep(
            max(0.0, (sc.deadline - datetime.now(UTC)).total_seconds()) + 0.5
        )
        assert datetime.now(UTC) > sc.deadline
        return await real_append(session, **kwargs)

    monkeypatch.setattr(ms, "append_plan_event", slow_append)
    out = await answer(sc, card)
    assert out.handled and out.reason == "approved", out
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


async def test_a_writer_supplied_time_cannot_pull_a_late_approval_onto_the_target_session(
    db_session, seed
) -> None:
    """A fresh transaction after the deadline that supplies a recent pre-open timestamp
    (the old ``mandate.decided_at`` idea) or a next session that is not after the target
    still cannot persist a target-session approval."""
    sc = await scene(db_session, seed, when="after")  # the deadline passed an hour ago
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    pre_open = (sc.deadline - timedelta(seconds=1)).isoformat()
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa.text("SELECT set_config('mandate.decided_at', :at, true)"),
            {"at": pre_open},
        )
        message = await _try(
            session, ANSWER, p=plan.id, t=sc.terms.id, e=sc.target, m=card.id
        )
    assert message is not None and "takes effect after the target session" in message
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa.text("SELECT set_config('mandate.next_session', :d, true)"),
            {"d": sc.target.isoformat()},
        )
        message = await _try(
            session, ANSWER, p=plan.id, t=sc.terms.id, e=sc.target, m=card.id
        )
    assert message is not None and "takes effect after the target session" in message
    assert await events_of(db_session, plan.id) == ["proposed"]


# ------------------------------------------------------ review round 3 (CodeRabbit)
async def test_a_reapproval_card_for_the_clause_the_plan_is_already_bound_to_is_refused(
    db_session, seed
) -> None:
    """Reject on such a card would retire a plan that §5.1 excludes from reapproval."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)  # bound to the live clause
    batch_id = (
        await db_session.execute(sa.text(BATCH), {"k": terms.basket_key, "d": 10})
    ).scalar_one()
    await refused(
        db_session,
        CARD.replace("'new_plan'", "'reapproval'"),
        "a clause other than the bound one",
        b=batch_id,
        p=plan.id,
        no=1,
        h="a" * 64,
        t=terms.id,
        n=rules.nonce_digest(rules.new_nonce()),
    )
    # a candidate listing does not offer it either
    sc = await open_batch(
        db_session, seed, sleeve, terms, [plan], when="before", shift=3
    )
    assert sc.cards == {}


async def test_a_reapproval_after_a_rebind_is_judged_against_the_event_stream(
    db_session, seed
) -> None:
    """The plan row keeps the clause of registration; the binding is the last
    approved/terms_rebound event, so a plan rebound to the live clause gets no card."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    live = await second_live_terms(db_session, seed, sleeve, terms, (sym,))
    first = await open_batch(db_session, seed, sleeve, live, [plan], when="before")
    assert first.cards[plan.id].kind == "reapproval"
    assert (await answer(first, first.cards[plan.id])).handled  # rebound to `live`
    nxt = await open_batch(
        db_session, seed, sleeve, live, [plan], when="before", shift=1
    )
    assert nxt.cards == {}  # bound to the live clause now: nothing to reapprove


async def test_i22_a_successor_batch_waits_for_an_answer_that_is_still_open(
    db_session, seed
) -> None:
    """The answer trigger takes the basket lock (L2) before the sleeve lock, so a
    successor batch created after the close reads the plan only after the answer commits
    (it never offers a new_plan card for a plan that was just approved)."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    now = datetime.now(UTC)
    old = await open_batch(
        db_session,
        seed,
        sleeve,
        terms,
        [plan],
        when="before",
        times=(now, now + timedelta(seconds=6), now + timedelta(seconds=8)),
    )
    card = old.cards[plan.id]
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        assert (
            await _try(holder, ANSWER, p=plan.id, t=terms.id, e=old.target, m=card.id)
            is None
        )  # an approval inserted before the close, still uncommitted
        await asyncio.sleep(
            max(0.0, (old.expires - datetime.now(UTC)).total_seconds()) + 0.5
        )
        _, open_at, close_at = timeline("before")

        async def successor():
            issued = await bt.create_batch(
                other,
                realm="mock",
                basket_key=terms.basket_key,
                actor=ACTOR,
                now=datetime.now(UTC),
                calendar=calendar(
                    open_at + timedelta(days=1), close_at + timedelta(days=1)
                ),
            )
            cards_made = len(issued.cards)
            seed.batch_ids.append(issued.batch.id)
            await other.commit()
            return cards_made

        task = asyncio.create_task(successor())
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        assert not task.done(), "the successor did not wait for the open answer"
        await holder.commit()
        made = await asyncio.wait_for(task, 15)
    assert made == 0  # the plan is approved: no new_plan card for it
    assert await events_of(db_session, plan.id) == ["proposed", "approved"]


async def test_a_plan_under_an_expired_live_clause_is_skipped_not_offered(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(
        symbols=(sym,),
        account_mode="kiwoom_mock",
        expires_at=datetime.now(UTC) + timedelta(seconds=3),
    )
    plan = await register(seed, sleeve, terms, sym)
    await asyncio.sleep(3.5)
    sc = await open_batch(db_session, seed, sleeve, terms, [plan], when="before")
    assert sc.cards == {} and sc.skipped == [plan.id]


async def test_a_late_approval_cannot_be_pulled_back_by_a_same_day_suspend_and_resume(
    db_session, seed
) -> None:
    """An approval after the bell is recorded for the next session; the plan then
    suspends and resumes with the target session: refused by the fold and by the database."""
    sc = await scene(db_session, seed, when="after")  # deadline passed: effective = T+1
    plan = sc.plans[0]
    assert (await answer(sc, sc.cards[plan.id])).handled
    later = sc.target + timedelta(days=1)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    await db_session.commit()
    with err(v.MandateRefusal.INVALID_TRANSITION):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="resumed",
            actor="operator:test",
            resume_target="approved",
            effective_session=sc.target,
        )
    await db_session.rollback()
    # the database refuses it for any writer
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, resume_target, "
        "effective_session, actor) VALUES (:p, 4, 'resumed', 'approved', :e, 'op')",
        "cannot take effect before the approval",
        p=plan.id,
        e=sc.target,
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="resumed",
        actor="operator:test",
        resume_target="approved",
        effective_session=later,
    )
    await db_session.commit()
    assert (await events_of(db_session, plan.id))[-1] == "resumed"
    # a resume may move its own date and correct it back down to the approval date,
    # never below it
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="resumed",
        actor="operator:test",
        resume_target="approved",
        effective_session=later + timedelta(days=3),
    )
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="resumed",
        actor="operator:test",
        resume_target="approved",
        effective_session=later,
    )
    await db_session.commit()


async def test_a_legacy_approval_can_still_be_resumed_to_an_earlier_date(
    db_session, seed
) -> None:
    """A non-batch approval has no floor, in the store and in the trigger: an operator
    appends a new resume to a persisted legacy history exactly as before."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)  # no batch card: effective 2026-11-23
    earlier = d.EFFECTIVE - timedelta(days=3)
    for day in (d.EFFECTIVE + timedelta(days=4), earlier):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event="suspended", actor="operator:test"
        )
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="resumed",
            actor="operator:test",
            resume_target="approved",
            effective_session=day,
        )
    await db_session.commit()
    assert (await events_of(db_session, plan.id))[-1] == "resumed"


# ------------------------------- review round 9: the verified answer link (hk 5648)
REBOUND = (
    "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
    "terms_sha256, effective_session, batch_member_id, created_at, actor) "
    "SELECT :p, :s, :ev, t.id, t.content_sha256, :e, :m, "
    "coalesce(CAST(:c AS timestamptz), now()), 'op' "
    "FROM review.mandate_terms t WHERE t.id = :t"
)
RESEND_AT = (
    "INSERT INTO review.mandate_plan_batch_members (batch_id, plan_id, card_no, "
    "kind, terms_id, terms_sha256, plan_sha256, nonce_digest, created_by, created_at) "
    "SELECT :b, :p, :no, 'new_plan', t.id, t.content_sha256, "
    "(SELECT plan_sha256 FROM review.mandate_plan_batch_members "
    " WHERE batch_id = :b AND plan_id = :p AND card_no = 1), :n, 'op', "
    "now() + CAST(:skew AS interval) FROM review.mandate_terms t WHERE t.id = :t"
)
SKEWS = [
    pytest.param(timedelta(days=-30), id="-30 days"),
    pytest.param(timedelta(0), id="0 seconds"),
    pytest.param(timedelta(days=30), id="30 days"),
]  # a card (or old row) dated before / at / after the thing it is compared with


async def next_seq(db, plan_id: int) -> int:
    return (
        await db.execute(
            sa.text(
                "SELECT coalesce(max(seq), 0) + 1 FROM review.mandate_plan_events "
                "WHERE plan_id = :p"
            ),
            {"p": plan_id},
        )
    ).scalar_one()


async def card_created_at(db, card_id: int) -> datetime:
    value = (
        await db.execute(
            sa.text(
                "SELECT created_at FROM review.mandate_plan_batch_members WHERE id = :m"
            ),
            {"m": card_id},
        )
    ).scalar_one()
    await db.rollback()
    return value


async def reapproval_scene(db, seed):
    """A plan approved under clause 1 (no batch card), a second live clause and a batch
    whose card 1 is the reapproval card."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await seed.approve_plan(plan.id, terms)
    live = await second_live_terms(db, seed, sleeve, terms, (sym,))
    sc = await open_batch(db, seed, sleeve, live, [plan], when="before")
    assert sc.cards[plan.id].kind == "reapproval"
    return sc, plan, terms, live


def migration_module():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/20261120_hk5648_mandate_batches.py"
    )
    spec = importlib.util.spec_from_file_location("hk5648_db_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("skew", SKEWS)
async def test_i22_t1_rows_upgraded_in_place_are_never_read_as_answers(
    db_session, seed, skew
) -> None:
    """The real upgrade over a populated T1 table: the T1 shape (no answer link, none of
    this migration's event triggers) holds a lawful ``terms_rebound`` whose free
    ``batch_member_id`` equals the id the plan's card later received, dated before / at /
    after that card. After the T3a upgrade steps run over it, that row is no answer, sets
    no resume floor, and the card still answers (all in one rolled-back transaction)."""
    sc, plan, terms, live = await reapproval_scene(db_session, seed)
    card = sc.cards[plan.id]
    created = await card_created_at(db_session, card.id)
    offset = skew
    later = d.EFFECTIVE + timedelta(days=1)
    # the T1 shape
    await raw(
        db_session,
        "DROP TRIGGER trg_mandate_plan_events_batch_guard ON review.mandate_plan_events",
    )
    await raw(
        db_session,
        "DROP TRIGGER trg_mandate_plan_events_resume_guard ON review.mandate_plan_events",
    )
    await raw(
        db_session,
        "ALTER TABLE review.mandate_plan_events DROP COLUMN answered_batch_member_id",
    )
    await raw(
        db_session,
        REBOUND,
        p=plan.id,
        s=await next_seq(db_session, plan.id),
        ev="terms_rebound",
        e=later,
        m=card.id,
        c=created + offset,
        t=terms.id,
    )
    # the T3a upgrade steps, over that populated table
    module = migration_module()
    for statement in (*module.COLUMN_DDL, *module.GUARD_DDL):
        await raw(db_session, statement)
    await db_session.flush()
    # not an answer: the batch is still open and the plan is offered a re-send
    assert await bt.answer_event(db_session, sc.batch_id, plan.id) is None
    result = await bt.reissue(
        db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
    )
    assert [c.member.plan_id for c in result.cards] == [plan.id] and not result.skipped
    # no floor: the T1 row is the latest binding, so suspend + resume to any earlier day
    # works through the store (the fold) ...
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="resumed",
        actor="operator:test",
        resume_target="approved",
        effective_session=d.EFFECTIVE,
    )
    # ... and a genuine answer on the newest card is one answer, with the link set by the
    # database, whatever date the old row carries
    newest = result.cards[0].member
    await raw(
        db_session,
        REBOUND,
        p=plan.id,
        s=await next_seq(db_session, plan.id),
        ev="terms_rebound",
        e=sc.target,
        m=newest.id,
        c=None,
        t=live.id,
    )
    await db_session.flush()
    link = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT answered_batch_member_id FROM review.mandate_plan_events "
                    "WHERE plan_id = :p AND batch_member_id = :m"
                ),
                {"p": plan.id, "m": newest.id},
            )
        )
        .scalars()
        .all()
    )
    assert link == [newest.id]  # the genuine answer only ...
    t1_link = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT answered_batch_member_id FROM review.mandate_plan_events "
                    "WHERE plan_id = :p AND batch_member_id = :m"
                ),
                {"p": plan.id, "m": card.id},
            )
        )
        .scalars()
        .all()
    )
    assert t1_link == [None]  # ... the T1 row keeps its NULL link
    found = await bt.answer_event(db_session, sc.batch_id, plan.id)
    assert found is not None and found.answered_batch_member_id == newest.id
    # the genuine approval now floors a new resume (store, then the database)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    with err(v.MandateRefusal.INVALID_TRANSITION):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="resumed",
            actor="operator:test",
            resume_target="approved",
            effective_session=sc.target - timedelta(days=1),
        )
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, resume_target, "
        "effective_session, actor) VALUES (:p, :s, 'resumed', 'approved', :e, 'op')",
        "cannot take effect before the approval",
        p=plan.id,
        s=await next_seq(db_session, plan.id),
        e=sc.target - timedelta(days=1),
    )


@pytest.mark.parametrize("skew", SKEWS)
async def test_i22_a_genuine_answer_counts_whatever_date_its_card_carries(
    db_session, seed, skew
) -> None:
    """A card dated in the past, now or the future: the answer written through the
    callback is one answer; the batch closes; a re-send, a second answer and the next
    batch see it; a late approval's date floors a same-day resume."""
    sc = await scene(db_session, seed, when="after")  # deadline passed: effective T+1
    plan = sc.plans[0]
    nonce = rules.new_nonce()
    await raw(
        db_session,
        RESEND_AT,
        b=sc.batch_id,
        p=plan.id,
        no=2,
        t=sc.terms.id,
        n=rules.nonce_digest(nonce),
        skew=skew,
    )
    card2 = (
        await db_session.execute(
            sa.text(
                "SELECT id FROM review.mandate_plan_batch_members "
                "WHERE batch_id = :b AND plan_id = :p AND card_no = 2"
            ),
            {"b": sc.batch_id, "p": plan.id},
        )
    ).scalar_one()
    await db_session.commit()
    out = await answer(sc, SimpleNamespace(id=card2, nonce=nonce))
    assert out.handled and out.reason == "approved", out
    assert await events_of(db_session, plan.id) == ["proposed", "approved"]
    found = await bt.answer_event(db_session, sc.batch_id, plan.id)
    assert found is not None and found.answered_batch_member_id == card2
    await db_session.rollback()
    # the batch is closed for the plan: no re-send, the older card stays refused
    result = await bt.reissue(
        db_session, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC)
    )
    assert result.cards == () and not result.skipped
    await db_session.rollback()
    assert (await answer(sc, sc.cards[plan.id])).reason == "mandate_card_superseded"
    # a late approval floors a same-day resume: store and database
    later = sc.target + timedelta(days=1)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="suspended", actor="operator:test"
    )
    await db_session.commit()
    with err(v.MandateRefusal.INVALID_TRANSITION):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="resumed",
            actor="operator:test",
            resume_target="approved",
            effective_session=sc.target,
        )
    await db_session.rollback()
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="resumed",
        actor="operator:test",
        resume_target="approved",
        effective_session=later,
    )
    await db_session.commit()


async def test_i22_no_writer_can_name_the_answer_link(db_session, seed) -> None:
    """The link is the answer trigger's output: naming it (with or without a card id, on
    an answer or on any other event) is refused, and a plain answer gets it from the DB."""
    sc = await scene(db_session, seed)
    plan = sc.plans[0]
    card = sc.cards[plan.id]
    approved = (
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, terms_id, "
        "terms_sha256, effective_session, batch_member_id, answered_batch_member_id, "
        "actor) SELECT :p, 2, 'approved', t.id, t.content_sha256, :e, :m, :l, 'op' "
        "FROM review.mandate_terms t WHERE t.id = :t"
    )
    for member in (card.id, None):  # the card id and the link, then the link alone
        await refused(
            db_session,
            approved,
            "answer link",
            p=plan.id,
            t=sc.terms.id,
            e=sc.target,
            m=member,
            l=card.id,
        )
    await refused(  # an event that answers no card
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, "
        "answered_batch_member_id, actor) VALUES (:p, 2, 'suspended', :l, 'op')",
        "answer link",
        p=plan.id,
        l=card.id,
    )
    assert await events_of(db_session, plan.id) == ["proposed"]
    # the same answer without naming the link is accepted and the DB fills it in
    await raw(db_session, ANSWER, p=plan.id, t=sc.terms.id, e=sc.target, m=card.id)
    await db_session.flush()
    stored = (
        await db_session.execute(
            sa.text(
                "SELECT batch_member_id, answered_batch_member_id "
                "FROM review.mandate_plan_events WHERE plan_id = :p AND seq = 2"
            ),
            {"p": plan.id},
        )
    ).one()
    assert stored.batch_member_id == stored.answered_batch_member_id == card.id
    await db_session.rollback()


async def test_i22_the_batch_identity_is_the_creating_transaction_not_a_writer_time(
    db_session, seed
) -> None:
    """Card 1 joins a batch only from the transaction that inserted it. A batch row whose
    writer-chosen ``created_at`` equals another transaction's start does not let that
    transaction add card 1, and a writer-chosen transaction id is overwritten."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    plan = await register(seed, sleeve, terms, sym)
    await db_session.commit()
    async with AsyncSessionLocal() as late, AsyncSessionLocal() as maker:
        started = (
            await late.execute(sa.text("SELECT transaction_timestamp()"))
        ).scalar_one()  # the card writer's transaction (and its now()) starts here
        batch_id = (
            await maker.execute(
                sa.text(
                    "INSERT INTO review.mandate_plan_batches (realm, basket_key, market, "
                    "target_session, approval_deadline_at, expires_at, created_by, "
                    "created_at, created_xact_id) VALUES ('mock', :k, 'equity_kr', "
                    "current_date + 20, now() + interval '1 hour', now() + interval '7 hour', "
                    "'op', :ts, 1) RETURNING id"
                ),
                {"k": terms.basket_key, "ts": started},
            )
        ).scalar_one()
        stored = (
            await maker.execute(
                sa.text(
                    "SELECT created_xact_id = pg_current_xact_id()::text::bigint "
                    "FROM review.mandate_plan_batches WHERE id = :b"
                ),
                {"b": batch_id},
            )
        ).scalar_one()
        assert stored is True  # the writer's value 1 was overwritten by the trigger
        await maker.commit()
        seed.batch_ids.append(batch_id)
        message = await _try(
            late,
            CARD,
            b=batch_id,
            p=plan.id,
            no=1,
            t=terms.id,
            h="a" * 64,
            n=rules.nonce_digest(rules.new_nonce()),
        )
    assert message is not None and "own transaction" in message


async def test_i22_a_clause_that_expires_while_create_waits_is_skipped_not_offered(
    db_session, seed
) -> None:
    sym = new_symbol()
    expires = datetime.now(UTC) + timedelta(seconds=8)
    sleeve, terms = await seed.clause_sleeve(
        symbols=(sym,), account_mode="kiwoom_mock", expires_at=expires
    )
    plan = await register(seed, sleeve, terms, sym)
    await db_session.commit()
    _, open_at, close_at = timeline("before")
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        await acquire_basket_lock(holder, "mock", terms.basket_key)
        task = asyncio.create_task(
            bt.create_batch(
                other,
                realm="mock",
                basket_key=terms.basket_key,
                actor=ACTOR,
                now=datetime.now(UTC),  # the clause is still valid at this instant
                calendar=calendar(open_at, close_at),
            )
        )
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        # the clause expires while the writer waits
        await asyncio.sleep(max(0.0, (expires - datetime.now(UTC)).total_seconds()) + 1)
        await holder.rollback()
        issued = await asyncio.wait_for(task, 15)
        seed.batch_ids.append(issued.batch.id)
        await other.commit()
    assert issued.cards == ()
    assert [(s.plan_id, s.reason) for s in issued.skipped] == [
        (plan.id, "live_terms_expired")
    ]


async def test_i22_a_batch_that_closes_while_create_waits_is_refused(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode="kiwoom_mock")
    await register(seed, sleeve, terms, sym)
    await db_session.commit()
    now = datetime.now(UTC)
    # the session opens in 1 s and closes in 3 s: it is over when the lock is released
    cal = calendar(now + timedelta(seconds=1), now + timedelta(seconds=3))
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        await acquire_basket_lock(holder, "mock", terms.basket_key)
        task = asyncio.create_task(
            bt.create_batch(
                other,
                realm="mock",
                basket_key=terms.basket_key,
                actor=ACTOR,
                now=now,
                calendar=cal,
            )
        )
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        await asyncio.sleep(4)
        await holder.rollback()
        with err(v.MandateRefusal.BATCH_EXPIRED):
            await asyncio.wait_for(task, 15)
        await other.rollback()
    assert (
        await count(
            db_session, "mandate_plan_batches", "basket_key = :k", k=terms.basket_key
        )
        == 0
    )


async def test_i22_a_clause_that_expires_while_resend_waits_is_reported_skipped(
    db_session, seed
) -> None:
    sym_a = new_symbol()
    expires = datetime.now(UTC) + timedelta(seconds=9)
    sleeve, terms = await seed.clause_sleeve(
        symbols=(sym_a,), account_mode="kiwoom_mock", expires_at=expires
    )
    plan = await register(seed, sleeve, terms, sym_a)
    sc = await open_batch(db_session, seed, sleeve, terms, [plan], when="before")
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as other:
        holder_pid, other_pid = await _pid(holder), await _pid(other)
        await acquire_basket_lock(holder, "mock", terms.basket_key)
        task = asyncio.create_task(
            bt.reissue(other, batch_id=sc.batch_id, actor=ACTOR, now=datetime.now(UTC))
        )
        await _wait_until_blocked_by(db_session, holder_pid, other_pid)
        await asyncio.sleep(max(0.0, (expires - datetime.now(UTC)).total_seconds()) + 1)
        await holder.rollback()
        result = await asyncio.wait_for(task, 15)
        await other.commit()
    assert result.cards == ()
    assert [(s.plan_id, s.reason) for s in result.skipped] == [
        (plan.id, "terms_expired")
    ]
    # the database refuses a card of an expired clause for any writer
    await refused(
        db_session,
        RESEND,
        "the card clause has expired",
        b=sc.batch_id,
        p=plan.id,
        no=2,
        t=terms.id,
        n=rules.nonce_digest(rules.new_nonce()),
    )
