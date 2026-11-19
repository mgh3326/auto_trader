"""hk 3795 T1: mandate store and DB guards on the test database.

Runs in CI against the run-owned test database (never a production database);
the DB round trips and the lock races are not run locally (hk host safety).
Test names carry the invariant they pin (I1, I2, I3 ...).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal, engine
from app.models.mandate import MandatePlanEvent
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate.db_support import (
    EFFECTIVE,
    PRINCIPAL,
    MandateSeed,
    digest,
    new_symbol,
)
from tests.services.strategy_sleeves.db_support import raw

validate_run_owned_database_url(engine.url)

TRANCHE_JSON = json.dumps(
    {"weights": [1], "triggers_pct_from_first": [0], "min_gap_sessions": 0}
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def refused(db, statement: str, match: str | None = None, **params) -> None:
    """The statement is refused by a trigger or constraint; the transaction rolls back."""
    with pytest.raises(DBAPIError) as caught:
        await raw(db, statement, **params)
        await db.flush()
    await db.rollback()
    if match:
        assert match in str(caught.value)


def err(code: v.MandateRefusal):
    return pytest.raises(ms.MandateError, match=code.value)


async def count(db, table: str, where: str = "true", **params) -> int:
    return (
        await db.execute(
            sa.text(f"SELECT count(*) FROM review.{table} WHERE {where}"), params
        )
    ).scalar_one()


# ------------------------------------------------------------------- I21 approvals
async def test_i21_terms_row_needs_an_approved_approval_of_the_same_content(
    db_session, seed
) -> None:
    sleeve = await seed.sleeve(state="live")  # a live version 1 to bind a clause to
    sleeve_id = sleeve.id
    version = await ms.sleeve_store.current_version(db_session, sleeve_id)
    version_id = version.id
    request_id = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_approval_requests (subject_kind, sleeve_id, "
                "base_sleeve_version, content, content_sha256, approval_ref, requested_by) "
                "VALUES ('terms', :s, 1, '{}'::jsonb, :h, 'ref', 'op') RETURNING id"
            ),
            {"s": sleeve_id, "h": "a" * 64},
        )
    ).scalar_one()
    denied = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_approvals (request_id, decision, subject_sha256, "
                "principal, nonce_digest) VALUES (:r, 'denied', :h, '1', :n) RETURNING id"
            ),
            {"r": request_id, "h": "a" * 64, "n": digest("n")},
        )
    ).scalar_one()
    await db_session.commit()
    insert = (
        "INSERT INTO review.mandate_terms (sleeve_version_id, terms_version, basket_key, "
        "account_ref, universe_rule, allowed_symbols, entry_max_premium_bps, per_order_cap, "
        "daily_cap, sector_cap_pct, basket_drawdown_freeze_pct, account_drawdown_freeze_pct, "
        "tranche_rule, loss_sell_mode, expires_at, approval_id, content_sha256) VALUES "
        "(:v, 'mandate-terms/v1', 'b-i21', 'a', 'u', ARRAY['X'], 30, 1, 1, 35, 25, 25, "
        "CAST(:tr AS jsonb), 'human_card', now() + interval '1 day', :a, :h)"
    )
    # a denied approval does not open a clause
    await refused(
        db_session,
        insert,
        "approved approval",
        v=version_id,
        a=denied,
        h="a" * 64,
        tr=TRANCHE_JSON,
    )
    # a different content hash does not either
    approved_req = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_approval_requests (subject_kind, sleeve_id, "
                "base_sleeve_version, content, content_sha256, approval_ref, requested_by) "
                "VALUES ('terms', :s, 1, '{}'::jsonb, :h, 'ref', 'op') RETURNING id"
            ),
            {"s": sleeve_id, "h": "b" * 64},
        )
    ).scalar_one()
    approved = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_approvals (request_id, decision, subject_sha256, "
                "principal, nonce_digest) VALUES (:r, 'approved', :h, '1', :n) RETURNING id"
            ),
            {"r": approved_req, "h": "b" * 64, "n": digest("n2")},
        )
    ).scalar_one()
    await db_session.commit()
    await refused(
        db_session,
        insert,
        "approved approval",
        v=version_id,
        a=approved,
        h="c" * 64,
        tr=TRANCHE_JSON,
    )


async def test_i21_an_approval_must_carry_its_request_hash(db_session, seed) -> None:
    sleeve = await seed.sleeve(state="live")
    request_id = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_approval_requests (subject_kind, sleeve_id, "
                "base_sleeve_version, content, content_sha256, approval_ref, requested_by) "
                "VALUES ('terms', :s, 1, '{}'::jsonb, :h, 'ref', 'op') RETURNING id"
            ),
            {"s": sleeve.id, "h": "a" * 64},
        )
    ).scalar_one()
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_approvals (request_id, decision, subject_sha256, "
        "principal, nonce_digest) VALUES (:r, 'approved', :h, '1', :n)",
        "content hash",
        r=request_id,
        h="d" * 64,
        n=digest("n"),
    )


async def test_i21_approval_writes_a_live_version_and_a_clause_that_rehash(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms_snap = await seed.clause_sleeve(symbols=(sym,))
    terms = await db_session.get(ms.MandateTerms, terms_snap.id)
    version = await db_session.get(ms.StrategySleeveVersion, terms.sleeve_version_id)
    assert version.state == "live" and version.version == 2
    assert version.set_by == f"operator:telegram:{PRINCIPAL}"
    assert ms.hashing.content_sha256(ms.terms_content_from_rows(terms, version)) == (
        terms.content_sha256
    )


async def test_i21_denied_request_writes_no_clause_and_a_request_decides_once(
    db_session, seed
) -> None:
    sleeve = await seed.sleeve(state="shadow")
    request = await ms.create_terms_request(
        db_session,
        sleeve_id=sleeve.id,
        data=_terms_input(new_symbol()),
        global_caps=ms.GlobalCaps(2_000_000, 5_000_000),
        approval_ref="hk:t",
        requested_by="operator:test",
    )
    await ms.record_approval(
        db_session,
        request_id=request.id,
        decision="denied",
        principal=PRINCIPAL,
        nonce_digest=digest("n"),
    )
    await db_session.commit()
    assert (await ms.current_terms(db_session, sleeve.id))[1] is None
    with err(v.MandateRefusal.REQUEST_STALE):
        await ms.record_approval(
            db_session,
            request_id=request.id,
            decision="approved",
            principal=PRINCIPAL,
            nonce_digest=digest("n2"),
        )
    await db_session.rollback()


async def test_i21_a_request_goes_stale_when_the_sleeve_moves_on(
    db_session, seed
) -> None:
    sleeve = await seed.sleeve(state="shadow")
    request = await ms.create_terms_request(
        db_session,
        sleeve_id=sleeve.id,
        data=_terms_input(new_symbol()),
        global_caps=ms.GlobalCaps(2_000_000, 5_000_000),
        approval_ref="hk:t",
        requested_by="operator:test",
    )
    await db_session.commit()
    request_id = request.id
    await ms.sleeve_store.set_sleeve_version(
        db_session, sleeve_id=sleeve.id, budget_cap="1", reason="r", actor="op"
    )
    await db_session.commit()
    with err(v.MandateRefusal.REQUEST_STALE):
        await ms.record_approval(
            db_session,
            request_id=request_id,
            decision="approved",
            principal=PRINCIPAL,
            nonce_digest=digest("n"),
        )
    await db_session.rollback()
    assert (
        await count(db_session, "mandate_approvals", "request_id = :r", r=request_id)
        == 0
    )


def _terms_input(symbol: str):
    from tests.services.mandate import db_support as d

    return ms.TermsInput(
        basket_key=f"b-{uuid.uuid4().hex[:8]}",
        account_ref="acct-x",
        universe_rule="u",
        allowed_symbols=[symbol],
        entry_max_premium_bps="30",
        per_order_cap="2000000",
        daily_cap="5000000",
        sector_cap_pct="35",
        basket_drawdown_freeze_pct="25",
        account_drawdown_freeze_pct="25",
        tranche_rule=d.TRANCHE_RULE,
        expires_at=d.FAR,
        budget_cap="1000000",
        loss_limit="100000",
        prereg=ms.PreRegistration(
            d.PREREG["metric"], ms.Decimal(d.PREREG["min_effect_pct"]), 20, 90
        ),
    )


async def test_i21_activation_profile_needs_an_approval_and_the_next_version(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    request = await ms.create_profile_request(
        db_session,
        sleeve_id=sleeve.id,
        terms_id=terms.id,
        max_tranche_index=1,
        effective_session=EFFECTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        approval_ref="hk:p",
        requested_by="operator:test",
    )
    result = await ms.record_approval(
        db_session,
        request_id=request.id,
        decision="approved",
        principal=PRINCIPAL,
        nonce_digest=digest("p"),
    )
    await db_session.commit()
    assert result.profile is not None and result.profile.profile_version == 1
    assert result.profile.max_tranche_index == 1
    latest = await ms.latest_profile(db_session, sleeve.id)
    assert latest is not None and latest.id == result.profile.id
    # a profile row without its approval, or skipping a version, is refused by the DB
    await refused(
        db_session,
        "INSERT INTO review.mandate_activation_profiles (sleeve_id, profile_version, terms_id, "
        "max_tranche_index, effective_session, expires_at, approval_id, content_sha256) "
        "VALUES (:s, 3, :t, 1, current_date, now() + interval '1 day', :a, :h)",
        "profile_version",
        s=sleeve.id,
        t=terms.id,
        a=result.approval.id,
        h=request.content_sha256,
    )


# ----------------------------------------------------------------- registration
async def test_registration_writes_plan_tranches_proposed_event_and_reservation(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    registered = await seed.register(sleeve, terms, sym)
    assert [t.amount for t in registered.tranches] == [940_000, 705_000, 705_000]
    state = await ms.plan_state(db_session, registered.plan.id)
    assert state.authority == "proposed" and state.cleanup == "none"
    assert registered.reservation.realm == "live"


async def test_i2_symbol_outside_the_clause_is_refused(db_session, seed) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    with err(v.MandateRefusal.SYMBOL_NOT_ALLOWED):
        await ms.register_plan(db_session, seed.plan_input(sleeve, terms, new_symbol()))
    await db_session.rollback()


async def test_i17_a_parking_symbol_is_never_a_plan_target(db_session, seed) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    with err(v.MandateRefusal.PARKING_SYMBOL):
        await ms.register_plan(db_session, seed.plan_input(sleeve, terms, "459580"))
    await db_session.rollback()


async def test_i15_the_plan_account_is_the_clause_account(db_session, seed) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    with err(v.MandateRefusal.ACCOUNT_MISMATCH):
        await ms.register_plan(
            db_session,
            seed.plan_input(
                sleeve,
                terms,
                sym,
                account_ref="other",
                scope=("other", terms.account_ref),
            ),
        )
    await db_session.rollback()
    # the DB refuses the same binding when the store is bypassed
    await refused(
        db_session,
        "INSERT INTO review.mandate_plans (sleeve_id, symbol, approved_terms_id, account_ref, "
        "scope_account_refs, planned_amount, adopted_amount, adopted_evidence, thesis, "
        "invalidation_conditions, sector_key, created_by) VALUES (:s, :y, :t, 'other', "
        "ARRAY['other', 'second'], 100, 0, '{}'::jsonb, 'x', ARRAY[]::text[], 'k', 'op')",
        "does not bind",
        s=sleeve.id,
        y=sym,
        t=terms.id,
    )


async def test_scope_accounts_are_fixed_at_registration_and_must_all_be_read(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    holdings = [{"account_ref": terms.account_ref, "quantity": "0", "amount": "0"}]
    with err(v.MandateRefusal.SCOPE_UNKNOWN):
        await ms.register_plan(
            db_session,
            seed.plan_input(
                sleeve,
                terms,
                sym,
                scope=(terms.account_ref, "acct-other"),
                adopted_evidence={"checked_at": "t", "holdings": holdings},
            ),
        )
    await db_session.rollback()


async def test_i3_unreleased_new_money_counts_against_the_sleeve_budget(
    db_session, seed
) -> None:
    a, b = new_symbol(), new_symbol()
    sleeve, terms = await seed.clause_sleeve(
        symbols=(a, b), budget_cap="3000000", sector_cap_pct="100"
    )
    await seed.register(sleeve, terms, a, planned="2350000")
    with err(v.MandateRefusal.BUDGET_EXCEEDED):
        await ms.register_plan(
            db_session,
            seed.plan_input(sleeve, terms, b, planned="2350000", sector="other"),
        )
    await db_session.rollback()
    # adopted holdings are not new money: 1,300,000 planned with 1,000,000 held
    # adds only 300,000 and fits (2.65M <= 3M)
    await seed.register(
        sleeve, terms, b, planned="1300000", adopted="1000000", sector="other"
    )


async def test_i4_sector_share_uses_planned_amounts_over_budget_plus_adopted(
    db_session, seed
) -> None:
    a, b = new_symbol(), new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(a, b), budget_cap="10000000")
    await seed.register(sleeve, terms, a, planned="2350000")  # 2.35M / 10M = 23.5%
    with err(v.MandateRefusal.SECTOR_CAP):  # 4.70M / 10M = 47% > 35%
        await ms.register_plan(
            db_session, seed.plan_input(sleeve, terms, b, planned="2350000")
        )
    await db_session.rollback()
    await seed.register(sleeve, terms, b, planned="2350000", sector="financial")


async def test_i1_one_unreleased_plan_per_symbol_across_both_accounts(
    db_session, seed
) -> None:
    sym = new_symbol()
    basket = f"b-{uuid.uuid4().hex[:8]}"
    kis, kis_terms = await seed.clause_sleeve(symbols=(sym,), basket_key=basket)
    toss, toss_terms = await seed.clause_sleeve(
        symbols=(sym,), basket_key=basket, account_mode="toss_live"
    )
    await seed.register(kis, kis_terms, sym)
    with err(v.MandateRefusal.SYMBOL_RESERVED):
        await ms.register_plan(db_session, seed.plan_input(toss, toss_terms, sym))
    await db_session.rollback()


async def test_i1_the_reservation_trigger_refuses_a_second_unreleased_claim(
    db_session, seed
) -> None:
    """The trigger holds even for a writer that skips the store."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    first = await seed.register(sleeve, terms, sym)
    second_id = (
        await db_session.execute(
            sa.text(
                "INSERT INTO review.mandate_plans (sleeve_id, symbol, approved_terms_id, "
                "account_ref, scope_account_refs, planned_amount, adopted_amount, "
                "adopted_evidence, thesis, invalidation_conditions, sector_key, created_by) "
                "VALUES (:s, :y, :t, :a, ARRAY[:a, 'second-acct'], 100, 0, '{}'::jsonb, 'x', "
                "ARRAY[]::text[], 'k', 'op') RETURNING id"
            ),
            {"s": sleeve.id, "y": sym, "t": terms.id, "a": terms.account_ref},
        )
    ).scalar_one()
    await db_session.commit()
    assert first.plan.id != second_id
    await refused(
        db_session,
        "INSERT INTO review.mandate_symbol_reservations (realm, market, symbol, plan_id) "
        "VALUES ('live', 'equity_kr', :y, :p)",
        "unreleased plan",
        y=sym,
        p=second_id,
    )
    # a mock reservation of the same symbol is another realm (key level): the
    # trigger takes it up to the plan check, which a live sleeve fails
    await refused(
        db_session,
        "INSERT INTO review.mandate_symbol_reservations (realm, market, symbol, plan_id) "
        "VALUES ('mock', 'equity_kr', :y, :p)",
        "does not match",
        y=sym,
        p=second_id,
    )
    await db_session.execute(sa.text("SET LOCAL session_replication_role = replica"))
    await db_session.execute(
        sa.text("DELETE FROM review.mandate_plans WHERE id = :p"), {"p": second_id}
    )
    await db_session.commit()


async def test_i1_concurrent_registrations_of_one_symbol_admit_one(
    db_session, seed
) -> None:
    sym = new_symbol()
    basket = f"b-{uuid.uuid4().hex[:8]}"
    kis, kis_terms = await seed.clause_sleeve(symbols=(sym,), basket_key=basket)
    toss, toss_terms = await seed.clause_sleeve(
        symbols=(sym,), basket_key=basket, account_mode="toss_live"
    )

    async def attempt(sleeve, terms) -> str:
        async with AsyncSessionLocal() as session:
            try:
                await ms.register_plan(session, seed.plan_input(sleeve, terms, sym))
                await session.commit()
                return "ok"
            except ms.MandateError as exc:
                await session.rollback()
                return exc.code.value

    results = await asyncio.gather(attempt(kis, kis_terms), attempt(toss, toss_terms))
    assert sorted(results) == ["mandate_symbol_reserved", "ok"]


async def test_i14_an_open_loss_cut_proposal_blocks_registration(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    await seed.loss_cut_proposal(
        symbol=sym, account_mode="toss_live"
    )  # any live account
    with err(v.MandateRefusal.OPEN_LOSS_CUT):
        await ms.register_plan(db_session, seed.plan_input(sleeve, terms, sym))
    await db_session.rollback()
    other = new_symbol()
    sleeve2, terms2 = await seed.clause_sleeve(symbols=(other,))
    await seed.loss_cut_proposal(
        symbol=other, lifecycle_state="terminal"
    )  # closed: fine
    await seed.register(sleeve2, terms2, other)


async def test_one_share_price_gate(db_session, seed) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    with err(v.MandateRefusal.TRANCHE_BELOW_ONE_SHARE):
        await ms.register_plan(
            db_session, seed.plan_input(sleeve, terms, sym, one_share_price="800000")
        )
    await db_session.rollback()


# ------------------------------------------------------------------------ events
async def _approved(seed, db, **kw):
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), **kw)
    plan = seed.plan_snapshot((await seed.register(sleeve, terms, sym)).plan)
    await seed.approve_plan(plan.id, terms)
    return sleeve, terms, plan


async def _insert_event(db, plan_id: int, seq: int, event: str, **cols):
    names = ["plan_id", "seq", "event", "actor", *cols]
    values = ", ".join(f":{n}" for n in names)
    await raw(
        db,
        f"INSERT INTO review.mandate_plan_events ({', '.join(names)}) VALUES ({values})",
        plan_id=plan_id,
        seq=seq,
        event=event,
        actor="operator:test",
        **cols,
    )
    await db.flush()


async def test_the_fold_and_the_trigger_accept_a_full_first_tranche(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": pid}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="operator:test", **fields
        )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_send_fenced",
        actor="operator:test",
        tranche_index=1,
        proposal_id=pid,
        notional=940_000,
        kst_day=EFFECTIVE,
        approval_mode="human",
        account_ref=plan.account_ref,
        attempt_id=uuid.uuid4(),
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_filled",
        actor="system:mandate_projection",
        tranche_index=1,
        proposal_id=pid,
        fill_amount=940_000,
        fill_quantity=94,
        first_fill_amount=940_000,
        first_fill_quantity=94,
        evidence={"trade_date": "2026-11-23"},
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="first_tranche_settled",
        actor="system:mandate_projection",
    )
    await db_session.commit()
    state = await ms.plan_state(db_session, plan.id)
    assert state.authority == "active" and state.unclosed_fences == ()


async def test_i16_i24_a_fence_after_retirement_is_refused_by_the_trigger(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="tranche_armed", actor="o", tranche_index=1
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_proposed",
        actor="o",
        tranche_index=1,
        proposal_id=pid,
    )
    await ms.append_plan_event(db_session, plan_id=plan.id, event="retired", actor="o")
    await db_session.commit()
    fence = {
        "tranche_index": 1,
        "proposal_id": pid,
        "notional": 1,
        "kst_day": EFFECTIVE,
        "approval_mode": "human",
        "account_ref": plan.account_ref,
        "attempt_id": uuid.uuid4(),
    }
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id) VALUES "
        "(:p, 6, 'tranche_send_fenced', 'o', :tranche_index, :proposal_id, :notional, "
        ":kst_day, :approval_mode, :account_ref, :attempt_id)",
        "sending authority",
        p=plan.id,
        **fence,
    )
    # a writer that read the stream before the retirement lands on a stale seq
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id) VALUES "
        "(:p, 5, 'tranche_send_fenced', 'o', :tranche_index, :proposal_id, :notional, "
        ":kst_day, :approval_mode, :account_ref, :attempt_id)",
        None,
        p=plan.id,
        **fence,
    )


async def test_i16_a_fence_before_the_effective_session_is_refused(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="tranche_armed", actor="o", tranche_index=1
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_proposed",
        actor="o",
        tranche_index=1,
        proposal_id=pid,
    )
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id) VALUES "
        "(:p, 5, 'tranche_send_fenced', 'o', 1, :pid, 1, :day, 'human', :a, :att)",
        "effective session",
        p=plan.id,
        pid=pid,
        day=EFFECTIVE - timedelta(days=3),
        a=plan.account_ref,
        att=uuid.uuid4(),
    )


async def test_i7_the_trigger_refuses_tranche_two_before_tranche_one_completes(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index) "
        "VALUES (:p, 3, 'tranche_armed', 'o', 2)",
        "cannot be armed",
        p=plan.id,
    )


async def test_i9_the_trigger_allows_one_open_proposal_per_tranche(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="tranche_armed", actor="o", tranche_index=1
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_proposed",
        actor="o",
        tranche_index=1,
        proposal_id=pid,
    )
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index) "
        "VALUES (:p, 5, 'tranche_armed', 'o', 1)",
        "cannot be armed",
        p=plan.id,
    )
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id) VALUES (:p, 5, 'tranche_proposed', 'o', 1, :pid)",
        "armed tranche",
        p=plan.id,
        pid=uuid.uuid4(),
    )


async def test_i9_an_unclosed_fence_blocks_a_second_fence_and_a_release_closes_it(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": pid}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="o", **fields
        )
    fence = {
        "tranche_index": 1,
        "proposal_id": pid,
        "notional": 1,
        "kst_day": EFFECTIVE,
        "approval_mode": "human",
        "account_ref": plan.account_ref,
    }
    row = await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_send_fenced",
        actor="o",
        attempt_id=uuid.uuid4(),
        **fence,
    )
    await db_session.commit()
    fence_seq = row.seq
    with err(v.MandateRefusal.TRANCHE_UNCERTAIN):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="tranche_send_fenced",
            actor="o",
            attempt_id=uuid.uuid4(),
            **fence,
        )
    await db_session.rollback()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id) VALUES "
        "(:p, 6, 'tranche_send_fenced', 'o', 1, :pid, 1, :day, 'human', :a, :att)",
        "not closed",
        p=plan.id,
        pid=pid,
        day=EFFECTIVE,
        a=plan.account_ref,
        att=uuid.uuid4(),
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="tranche_send_released",
        actor="o",
        tranche_index=1,
        fenced_seq=fence_seq,
    )
    await db_session.commit()
    assert (await ms.plan_state(db_session, plan.id)).unclosed_fences == ()


async def test_fence_account_must_be_the_plan_account(db_session, seed) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    pid = uuid.uuid4()
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": pid}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="o", **fields
        )
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id) VALUES "
        "(:p, 5, 'tranche_send_fenced', 'o', 1, :pid, 1, :day, 'human', 'someone-else', :att)",
        "plan account",
        p=plan.id,
        pid=pid,
        day=EFFECTIVE,
        att=uuid.uuid4(),
    )


async def test_i15_terms_rebound_cannot_move_the_plan_to_another_account(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    other = await seed.clause_sleeve(symbols=(plan.symbol,), account_ref="elsewhere")
    with err(v.MandateRefusal.BINDING_MISMATCH):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="terms_rebound",
            actor="o",
            terms_id=other[1].id,
            effective_session=EFFECTIVE,
        )
    await db_session.rollback()


# ----------------------------------------------------------------------- release
async def _retire_and_dispose(
    db, plan_id: int, scope: list[str], *, kind="zero_holdings"
):
    await ms.append_plan_event(
        db, plan_id=plan_id, event="retired", actor="operator:test"
    )
    await ms.append_plan_event(
        db, plan_id=plan_id, event="orders_settled", actor="system:mandate_projection"
    )
    evidence = {"holdings_disposition": kind}
    if kind == "zero_holdings":
        evidence |= {"holdings_account_refs": scope, "holdings_quantity": "0"}
    await ms.append_plan_event(
        db,
        plan_id=plan_id,
        event="holdings_disposed",
        actor="operator:test"
        if kind == "kept_outside_basket"
        else "system:mandate_projection",
        evidence=evidence,
    )


async def test_i19_release_needs_settled_orders_disposed_holdings_and_frees_the_symbol(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    # retired but not released: the symbol, budget and sector stay claimed
    await ms.append_plan_event(db_session, plan_id=plan.id, event="retired", actor="o")
    await db_session.commit()
    with err(v.MandateRefusal.SYMBOL_RESERVED):
        await ms.register_plan(db_session, seed.plan_input(sleeve, terms, plan.symbol))
    await db_session.rollback()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor) "
        "VALUES (:p, 4, 'released', 'o')",
        "released needs",
        p=plan.id,
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="orders_settled",
        actor="system:mandate_projection",
    )
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor) "
        "VALUES (:p, 5, 'released', 'o')",
        "released needs",
        p=plan.id,
    )
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="holdings_disposed",
        actor="operator:test",
        evidence={"holdings_disposition": "kept_outside_basket"},
    )
    await ms.append_plan_event(db_session, plan_id=plan.id, event="released", actor="o")
    await db_session.commit()
    assert await count(db_session, "mandate_symbol_reservation_releases") >= 1
    # the symbol is free again, in either account
    await seed.register(sleeve, terms, plan.symbol, planned="2350000")


async def test_i19_zero_holdings_must_read_the_scope_accounts_fixed_at_registration(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    await ms.append_plan_event(db_session, plan_id=plan.id, event="retired", actor="o")
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="orders_settled",
        actor="system:mandate_projection",
    )
    await db_session.commit()
    with err(v.MandateRefusal.SCOPE_UNKNOWN):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="holdings_disposed",
            actor="system:mandate_projection",
            evidence={
                "holdings_disposition": "zero_holdings",
                "holdings_account_refs": ["a-new-account"],
                "holdings_quantity": "0",
            },
        )
    await db_session.rollback()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, evidence) VALUES "
        "(:p, 5, 'holdings_disposed', 'system:mandate_projection', "
        '\'{"holdings_disposition":"zero_holdings","holdings_account_refs":["a-new"],'
        '"holdings_quantity":"0"}\'::jsonb)',
        "zero_holdings",
        p=plan.id,
    )
    await _noop()


async def _noop() -> None:
    return None


async def test_i19_a_release_row_exists_only_with_the_released_event(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    reservation_id = (
        await db_session.execute(
            sa.text(
                "SELECT id FROM review.mandate_symbol_reservations WHERE plan_id = :p"
            ),
            {"p": plan.id},
        )
    ).scalar_one()
    await refused(
        db_session,
        "INSERT INTO review.mandate_symbol_reservation_releases (reservation_id) VALUES (:r)",
        "released event",
        r=reservation_id,
    )


# ---------------------------------------------------------------- I13 append-only
async def test_i13_the_nine_tables_refuse_update_delete_and_truncate(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    request = await ms.create_profile_request(
        db_session,
        sleeve_id=sleeve.id,
        terms_id=terms.id,
        max_tranche_index=1,
        effective_session=EFFECTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=9),
        approval_ref="hk:p",
        requested_by="operator:test",
    )
    await ms.record_approval(
        db_session,
        request_id=request.id,
        decision="approved",
        principal=PRINCIPAL,
        nonce_digest=digest("p"),
    )
    await _retire_and_dispose(
        db_session, plan.id, list(plan.scope_account_refs), kind="kept_outside_basket"
    )
    await ms.append_plan_event(db_session, plan_id=plan.id, event="released", actor="o")
    await db_session.commit()
    updates = {
        "mandate_approval_requests": "approval_ref = 'x'",
        "mandate_approvals": "principal = '2'",
        "mandate_terms": "universe_rule = 'x'",
        "mandate_activation_profiles": "max_tranche_index = 2",
        "mandate_plans": "thesis = 'x'",
        "mandate_plan_tranches": "amount = 1",
        "mandate_plan_events": "actor = 'x'",
        "mandate_symbol_reservations": "symbol = 'x'",
        "mandate_symbol_reservation_releases": "reservation_id = reservation_id + 0",
    }
    for table, assignment in updates.items():
        assert await count(db_session, table, "true") >= 1, table
        await refused(
            db_session, f"UPDATE review.{table} SET {assignment}", "append-only"
        )
        await refused(db_session, f"DELETE FROM review.{table}", "append-only")
        # Postgres checks foreign-key references before BEFORE TRUNCATE triggers:
        # a referenced table fails on the FK, the others reach the trigger.
        unreferenced = table in (
            "mandate_plan_events",
            "mandate_plan_tranches",
            "mandate_symbol_reservation_releases",
        )
        await refused(
            db_session,
            f"TRUNCATE review.{table}",
            "append-only" if unreferenced else ("append-only", "cannot truncate"),
        )


# ------------------------------------------------------------------- proposals
async def test_i15_a_bound_proposal_must_match_the_plan_and_be_a_limit_buy(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    row = await seed.bound_proposal(plan=plan, sleeve=sleeve)
    await db_session.commit()
    assert row.mandate_plan_id == plan.id
    for kwargs in (
        {"symbol": new_symbol()},
        {"account_mode": "toss_live"},
        {"side": "sell"},
        {"order_type": "market"},
        {"tranche_index": None},
        {"tranche_index": 4},
        {"plan_id": None},
    ):
        with pytest.raises(DBAPIError):
            await seed.bound_proposal(plan=plan, sleeve=sleeve, **kwargs)
        await db_session.rollback()


async def test_i15_the_binding_is_set_once(db_session, seed) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    row = await seed.bound_proposal(plan=plan, sleeve=sleeve)
    await db_session.commit()
    row_id = row.id
    await refused(
        db_session,
        "UPDATE review.order_proposals SET mandate_tranche_index = 2 WHERE id = :i",
        "cannot change",
        i=row_id,
    )
    # an update that leaves the binding alone is not touched by the trigger
    await raw(
        db_session,
        "UPDATE review.order_proposals SET thesis = 't' WHERE id = :i",
        i=row_id,
    )
    await db_session.commit()


async def test_untagged_proposals_behave_as_before(db_session, seed) -> None:
    pid = uuid.uuid4()
    await raw(
        db_session,
        "INSERT INTO review.order_proposals (proposal_id, root_proposal_id, symbol, market, "
        "account_mode, side, order_type, proposer) VALUES (:p, :p, 'UNTAGGED1', 'equity_kr', "
        "'kis_live', 'sell', 'market', 'test')",
        p=pid,
    )
    await db_session.commit()
    row = (
        await db_session.execute(
            sa.text(
                "SELECT mandate_plan_id, mandate_tranche_index FROM review.order_proposals "
                "WHERE proposal_id = :p"
            ),
            {"p": pid},
        )
    ).one()
    assert tuple(row) == (None, None)
    await raw(
        db_session, "DELETE FROM review.order_proposals WHERE proposal_id = :p", p=pid
    )
    await db_session.commit()


async def test_events_orm_rows_round_trip_through_the_fold(db_session, seed) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    rows = await ms.list_plan_events(db_session, plan.id)
    assert [r.event for r in rows] == ["proposed", "approved"]
    assert isinstance(rows[0], MandatePlanEvent)
    assert ms.event_to_fold(rows[1]).effective_session == EFFECTIVE


_PLAN_INSERT = (
    "INSERT INTO review.mandate_plans (sleeve_id, symbol, approved_terms_id, account_ref, "
    "scope_account_refs, planned_amount, adopted_amount, adopted_evidence, thesis, "
    "invalidation_conditions, sector_key, created_by) VALUES (:s, :y, :t, :a, "
    "CAST(:scope AS text[]), 100, 0, '{}'::jsonb, 'x', ARRAY[]::text[], 'k', 'op')"
)


async def test_i15_a_live_plan_scopes_both_real_accounts(db_session, seed) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    with err(v.MandateRefusal.ACCOUNT_MISMATCH):
        await ms.register_plan(
            db_session,
            seed.plan_input(sleeve, terms, sym, scope=(terms.account_ref,)),
        )
    await db_session.rollback()
    params = {"s": sleeve.id, "y": sym, "t": terms.id, "a": terms.account_ref}
    await refused(
        db_session,
        _PLAN_INSERT,
        "scopes both real accounts",
        scope="{" + terms.account_ref + "}",
        **params,
    )
    await refused(
        db_session,
        _PLAN_INSERT,
        "scopes both real accounts",
        scope="{" + terms.account_ref + "," + terms.account_ref + "}",
        **params,
    )


async def test_i19_an_open_tranche_proposal_blocks_settlement(db_session, seed) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": uuid.uuid4()}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="o", **fields
        )
    await ms.append_plan_event(db_session, plan_id=plan.id, event="retired", actor="o")
    await db_session.commit()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor) "
        "VALUES (:p, 6, 'orders_settled', 'o')",
        "proposal is still open",
        p=plan.id,
    )


async def test_i16_an_auto_fence_cannot_cite_another_sleeves_profile(
    db_session, seed
) -> None:
    sleeve, terms, plan = await _approved(seed, db_session)
    other_sleeve, other_terms = await seed.clause_sleeve(symbols=(new_symbol(),))
    request = await ms.create_profile_request(
        db_session,
        sleeve_id=other_sleeve.id,
        terms_id=other_terms.id,
        max_tranche_index=1,
        effective_session=EFFECTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        approval_ref="hk:p",
        requested_by="operator:test",
    )
    approved = await ms.record_approval(
        db_session,
        request_id=request.id,
        decision="approved",
        principal=PRINCIPAL,
        nonce_digest=digest("p"),
    )
    profile_id = approved.profile.id
    pid = uuid.uuid4()
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": pid}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="o", **fields
        )
    await db_session.commit()
    with err(v.MandateRefusal.BINDING_MISMATCH):
        await ms.append_plan_event(
            db_session,
            plan_id=plan.id,
            event="tranche_send_fenced",
            actor="o",
            tranche_index=1,
            proposal_id=pid,
            notional=1,
            kst_day=EFFECTIVE,
            approval_mode="auto",
            account_ref=plan.account_ref,
            attempt_id=uuid.uuid4(),
            activation_profile_id=profile_id,
        )
    await db_session.rollback()
    await refused(
        db_session,
        "INSERT INTO review.mandate_plan_events (plan_id, seq, event, actor, tranche_index, "
        "proposal_id, notional, kst_day, approval_mode, account_ref, attempt_id, "
        "activation_profile_id) VALUES (:p, 5, 'tranche_send_fenced', 'o', 1, :pid, 1, :day, "
        "'auto', :a, :att, :prof)",
        "another sleeve",
        p=plan.id,
        pid=pid,
        day=EFFECTIVE,
        a=plan.account_ref,
        att=uuid.uuid4(),
        prof=profile_id,
    )
