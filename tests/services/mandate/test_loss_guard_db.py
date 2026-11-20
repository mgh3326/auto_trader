"""hk 3795 T4c: I14 — a loss sale of a planned instrument needs ``thesis_broken``.

CI database only (the concurrent case opens two sessions). Test names carry the case
letter of the §5.2 test contract: (a) (c) (d) (e) (f) (g) (h) (i).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.telegram_callback import _mandate_pre_nonce_block
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate.db_support import MandateSeed

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

EVIDENCE = {"retrospective_id": 42, "approval_issue_id": "ROB-800"}


def new_kr_symbol() -> str:
    return str(uuid.uuid4().int % 900_000 + 100_000)


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


@pytest.fixture
def retro(monkeypatch):
    """The stored retrospective every loss_cut of these tests points at."""
    state = {"symbol": None}

    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol=state["symbol"],
            trigger_type="stop_loss",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    return state


def _loss_cut(sym: str, *, account_mode="kis_live", retro_id=42, issue="ROB-800"):
    return {
        "symbol": sym,
        "market": "equity_kr",
        "account_mode": account_mode,
        "side": "sell",
        "order_type": "limit",
        "proposer": "p",
        "rungs": [RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": retro_id,
        "approval_issue_id": issue,
        "now": datetime.now(UTC),
    }


async def _planned(seed, db, *, account_mode="kis_live"):
    sym = new_kr_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), account_mode=account_mode)
    plan = seed.plan_snapshot((await seed.register(sleeve, terms, sym)).plan)
    await seed.approve_plan(plan.id, terms)
    return sym, sleeve, terms, plan


async def _count_loss_cuts(db, sym: str) -> int:
    await db.rollback()
    return (
        await db.execute(
            sa.text(
                "SELECT count(*) FROM review.order_proposals "
                "WHERE symbol = :s AND exit_intent = 'loss_cut'"
            ),
            {"s": sym},
        )
    ).scalar_one()


async def test_i14_a_loss_cut_of_a_planned_symbol_without_thesis_broken_is_not_created(
    db_session, seed, retro
) -> None:
    sym, _s, _t, _plan = await _planned(seed, db_session)
    retro["symbol"] = sym
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await service.create_proposal(**_loss_cut(sym))
    assert await _count_loss_cuts(db_session, sym) == 0


async def test_i14_d_a_toss_plan_guards_the_kis_loss_cut_of_the_same_symbol(
    db_session, seed, retro
) -> None:
    """The reservation is keyed by market and symbol, never by account."""
    sym, _s, _t, _plan = await _planned(seed, db_session, account_mode="toss_live")
    retro["symbol"] = sym
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await OrderProposalsService(db_session).create_proposal(
            **_loss_cut(sym, account_mode="kis_live")
        )


async def test_i14_g_a_symbol_without_a_plan_is_not_affected(
    db_session, seed, retro
) -> None:
    sym = new_kr_symbol()
    retro["symbol"] = sym
    group = await OrderProposalsService(db_session).create_proposal(**_loss_cut(sym))
    await db_session.commit()
    seed.proposal_pks.append(group.id)
    assert group.exit_intent == "loss_cut"


async def test_i14_f_the_ids_of_the_thesis_broken_event_open_the_loss_cut(
    db_session, seed, retro
) -> None:
    sym, _s, _t, plan = await _planned(seed, db_session)
    retro["symbol"] = sym
    broken = await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_broken",
        actor="operator:test",
        evidence=EVIDENCE,
    )
    await db_session.commit()
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await service.create_proposal(**_loss_cut(sym, retro_id=43))
    await db_session.rollback()
    group = await service.create_proposal(**_loss_cut(sym))
    await db_session.commit()
    seed.proposal_pks.append(group.id)
    assert group.retrospective_id == 42

    # 72 hours later: the new retrospective needs the refresh event, not a new proposal
    await db_session.rollback()
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await service.create_proposal(**_loss_cut(sym, retro_id=77, issue="ROB-900"))
    await db_session.rollback()
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_evidence_refreshed",
        actor="operator:test",
        thesis_seq=broken.seq,
        evidence={"retrospective_id": 77, "approval_issue_id": "ROB-900"},
    )
    await db_session.commit()
    again = await service.create_proposal(
        **_loss_cut(sym, retro_id=77, issue="ROB-900")
    )
    await db_session.commit()
    seed.proposal_pks.append(again.id)


async def test_i14_h_a_retired_plan_still_guards_and_a_refresh_still_opens_it(
    db_session, seed, retro
) -> None:
    sym, _s, _t, plan = await _planned(seed, db_session)
    retro["symbol"] = sym
    broken = await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_broken",
        actor="operator:test",
        evidence=EVIDENCE,
    )
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await service.create_proposal(**_loss_cut(sym, retro_id=88, issue="ROB-901"))
    await db_session.rollback()
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_evidence_refreshed",
        actor="operator:test",
        thesis_seq=broken.seq,
        evidence={"retrospective_id": 88, "approval_issue_id": "ROB-901"},
    )
    await db_session.commit()
    group = await service.create_proposal(
        **_loss_cut(sym, retro_id=88, issue="ROB-901")
    )
    await db_session.commit()
    seed.proposal_pks.append(group.id)
    state = await ms.plan_state(db_session, plan.id)
    assert state.authority == "retired" and state.cleanup != "released"


async def test_i14_i_an_event_written_before_its_retrospective_exists_refuses(
    db_session, seed, retro
) -> None:
    sym, _s, _t, plan = await _planned(seed, db_session)
    retro["symbol"] = sym
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_broken",
        actor="operator:test",
        evidence={"retrospective_id": 1, "approval_issue_id": "ROB-1"},
    )
    await db_session.commit()
    # the retrospective that was made later has another id than the one in the event
    with pytest.raises(OrderProposalError, match="mandate_thesis_not_broken"):
        await OrderProposalsService(db_session).create_proposal(
            **_loss_cut(sym, retro_id=2, issue="ROB-1")
        )


async def test_i14_c_a_card_from_before_the_guard_is_refused_before_its_nonce(
    db_session, seed
) -> None:
    """A loss_cut card without evidence, next to a plan that has none either: the
    callback's check refuses and nothing about the nonce changes."""
    sym, _s, _t, _plan = await _planned(seed, db_session)
    card = await seed.loss_cut_proposal(symbol=sym)
    used_before = card.approval_nonce_used_at
    service = OrderProposalsService(db_session)

    block = await _mandate_pre_nonce_block(service, card, [], now=datetime.now(UTC))

    assert block == "mandate_thesis_not_broken"
    assert card.approval_nonce_used_at == used_before
    assert await service.mandate_loss_cut_refusal(card) == "mandate_thesis_not_broken"


async def test_i14_e_creating_a_loss_cut_and_registering_a_plan_race_to_one_winner(
    db_session, seed, retro
) -> None:
    sym = new_kr_symbol()
    retro["symbol"] = sym
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))
    plan_input = seed.plan_input(sleeve, terms, sym)

    async def create():
        async with AsyncSessionLocal() as s:
            group = await OrderProposalsService(s).create_proposal(**_loss_cut(sym))
            await s.commit()
            return group.id

    async def register():
        async with AsyncSessionLocal() as s:
            await ms.register_plan(s, plan_input)
            await s.commit()

    results = await asyncio.gather(create(), register(), return_exceptions=True)
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1, results
    loser = next(r for r in results if isinstance(r, BaseException))
    assert isinstance(loser, OrderProposalError | ms.MandateError)
    if isinstance(loser, ms.MandateError):
        assert loser.code is v.MandateRefusal.OPEN_LOSS_CUT
    else:
        assert "mandate_thesis_not_broken" in str(loser)
    created = [r for r in results if isinstance(r, int)]
    seed.proposal_pks.extend(created)
