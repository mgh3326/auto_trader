"""hk 3795 T4b: the fill projection on the run-owned test database (CI only).

Locally this module is collected but not run (hk host safety: no local database).
Each test checks what the projection left in the plan's event stream, not what it
printed. The ledger and the order rows are seeded with the sleeve helpers (a KIS
live sleeve: the same attribution the sleeve book uses, T1m-a).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.models.mandate import (
    MandatePlanEvent,
    MandateSymbolReservation,
    MandateSymbolReservationRelease,
)
from app.models.order_proposals import OrderProposalRung
from app.services.mandate import projection as proj
from app.services.mandate import store as ms
from app.services.mandate.projection_rules import HoldingsReading
from scripts import mandate_projection as cli
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate.db_support import MandateSeed, new_symbol
from tests.services.strategy_sleeves.db_support import unique_order_id

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def _events(db, plan_id: int) -> list[str]:
    rows = await db.execute(
        sa.select(MandatePlanEvent.event)
        .where(MandatePlanEvent.plan_id == plan_id)
        .order_by(MandatePlanEvent.seq)
    )
    return list(rows.scalars().all())


async def _state(db, plan_id: int):
    state = await ms.plan_state(db, plan_id)
    await db.rollback()
    return state


async def _prepare_plan(
    seed: MandateSeed, sleeve, terms, symbol: str
) -> SimpleNamespace:
    registered = await seed.register(sleeve, terms, symbol)
    plan = seed.plan_snapshot(registered.plan)
    await seed.approve_plan(plan.id, terms)
    return plan


async def _propose_tranche(
    seed: MandateSeed,
    plan,
    sleeve,
    *,
    state: str,
    filled_qty: str | None,
    quantity: str = "9",
    limit: str = "100000",
) -> SimpleNamespace:
    """Arm tranche 1, bind a proposal with one sent buy rung and record the event."""
    db = seed.db
    await ms.append_plan_event(
        db,
        plan_id=plan.id,
        event="tranche_armed",
        actor="operator:test",
        tranche_index=1,
    )
    row = await seed.bound_proposal(plan=plan, sleeve=sleeve)
    proposal_id, proposal_pk = row.proposal_id, row.id
    db.add(
        OrderProposalRung(
            proposal_pk=proposal_pk,
            rung_index=0,
            side="buy",
            quantity=Decimal(quantity),
            limit_price=Decimal(limit),
            state="pending_approval",
        )
    )
    await db.commit()
    await ms.append_plan_event(
        db,
        plan_id=plan.id,
        event="tranche_proposed",
        actor="operator:test",
        tranche_index=1,
        proposal_id=proposal_id,
    )
    order_id = unique_order_id()
    await seed.mark_acked(SimpleNamespace(id=proposal_pk, symbol=plan.symbol), order_id)
    await db.execute(
        sa.update(OrderProposalRung)
        .where(OrderProposalRung.proposal_pk == proposal_pk)
        .values(
            state=state, filled_qty=None if filled_qty is None else Decimal(filled_qty)
        )
    )
    await db.commit()
    return SimpleNamespace(
        proposal_id=proposal_id, proposal_pk=proposal_pk, order_id=order_id
    )


def _later() -> datetime:
    """A projection clock safely after every seeded fill (fills are stamped "now + 1s")."""
    return datetime.now(UTC) + timedelta(minutes=2)


def _zero(plan, refs=None, *, now: datetime) -> list[HoldingsReading]:
    return [
        HoldingsReading(
            account_ref=ref,
            symbol=plan.symbol,
            quantity=Decimal("0"),
            checked_at=now - timedelta(minutes=1),
        )
        for ref in (refs if refs is not None else plan.scope_account_refs)
    ]


async def _project(plan_id: int, *, write: bool, now: datetime, holdings=()):
    async with AsyncSessionLocal() as session:
        report = await proj.project_plan(
            session, plan_id, holdings=holdings, now=now, write=write
        )
        if write and report.planned and report.error is None:
            await session.commit()
        else:
            await session.rollback()
    return report


# ------------------------------------------------------------------ I10
async def test_i10_a_websocket_only_fill_is_not_projected(seed, db_session) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    sent = await _propose_tranche(seed, plan, sleeve, state="filled", filled_qty="9")
    await seed.fill(
        order_id=sent.order_id,
        symbol=symbol,
        quantity="9",
        price="100000",
        source="websocket",
    )
    before = await _events(db_session, plan.id)

    report = await _project(plan.id, write=True, now=_later())

    assert report.planned == ()
    assert [h.code.value for h in report.holds] == ["fill_not_booked"]
    assert await _events(db_session, plan.id) == before


async def test_i10_a_reconciler_fill_is_projected_once_and_a_replay_writes_nothing(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    sent = await _propose_tranche(seed, plan, sleeve, state="filled", filled_qty="9")
    await seed.fill(order_id=sent.order_id, symbol=symbol, quantity="9", price="100000")
    now = _later()

    preview = await _project(plan.id, write=False, now=now)
    assert [e.event for e in preview.planned] == [
        "tranche_filled",
        "first_tranche_settled",
    ]
    assert await _events(db_session, plan.id) == [
        "proposed",
        "approved",
        "tranche_armed",
        "tranche_proposed",
    ]

    written = await _project(plan.id, write=True, now=now)
    assert written.error is None
    assert await _events(db_session, plan.id) == [
        "proposed",
        "approved",
        "tranche_armed",
        "tranche_proposed",
        "tranche_filled",
        "first_tranche_settled",
    ]
    state = await _state(db_session, plan.id)
    assert state.authority == "active"
    assert state.first_fill_amount == Decimal("900000")
    assert state.first_fill_quantity == Decimal("9")

    again = await _project(plan.id, write=True, now=now)
    assert again.planned == ()
    assert await _events(db_session, plan.id) == [
        "proposed",
        "approved",
        "tranche_armed",
        "tranche_proposed",
        "tranche_filled",
        "first_tranche_settled",
    ]


async def test_i10_the_cli_preview_leaves_the_stream_alone_and_the_commit_writes_it(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    sent = await _propose_tranche(seed, plan, sleeve, state="expired", filled_qty="3")
    await seed.fill(order_id=sent.order_id, symbol=symbol, quantity="3", price="100000")
    argv = ["--database-url-env", "MANDATE_TEST_DB", "--plan-id", str(plan.id)]

    code, payload = await cli.run(
        cli._parser().parse_args(argv),
        session_factory=AsyncSessionLocal,
        now=_later(),
    )
    assert code == cli.EXIT_OK and payload["mode"] == "preview"
    assert [e["event"] for e in payload["plans"][0]["events"]] == [
        "tranche_partially_filled"
    ]
    assert (await _events(db_session, plan.id))[-1] == "tranche_proposed"

    code, payload = await cli.run(
        cli._parser().parse_args([*argv, "--commit"]),
        session_factory=AsyncSessionLocal,
        now=_later(),
    )
    assert code == cli.EXIT_OK and payload["plans"][0]["written"] is True
    assert (await _events(db_session, plan.id))[-1] == "tranche_partially_filled"
    state = await _state(db_session, plan.id)
    assert state.authority == "approved"  # a partial fill does not complete tranche 1
    assert state.tranche(1).filled_amount == Decimal("300000")


async def test_i10_a_proposal_that_ended_without_a_fill_is_written_as_unfilled(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    await _propose_tranche(seed, plan, sleeve, state="expired", filled_qty=None)

    report = await _project(plan.id, write=True, now=_later())

    assert [e.event for e in report.planned] == ["tranche_unfilled"]
    assert (await _events(db_session, plan.id))[-1] == "tranche_unfilled"


# ------------------------------------------------------------------ closed
async def test_closed_needs_the_plan_accounts_zero_read_after_thesis_broken(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    sent = await _propose_tranche(seed, plan, sleeve, state="filled", filled_qty="9")
    await seed.fill(order_id=sent.order_id, symbol=symbol, quantity="9", price="100000")
    now = _later()
    await _project(plan.id, write=True, now=now)
    # hk 5651: the event may only cite a retrospective that already exists
    from app.models.review import TradeRetrospective

    retro = TradeRetrospective(
        symbol=symbol,
        instrument_type="equity_kr",
        account_mode="kis_live",
        market="kr",
        outcome="filled",
        side="sell",
        correlation_id=f"live:{uuid.uuid4()}",
        realized_pnl=Decimal("-100"),
        realized_pnl_currency="KRW",
        pnl_pct=Decimal("-1.0"),
        trigger_type="stop_loss",
    )
    db_session.add(retro)
    await db_session.flush()
    retro_id = retro.id
    await ms.append_plan_event(
        db_session,
        plan_id=plan.id,
        event="thesis_broken",
        actor="operator:test",
        evidence={
            "reason_code": "test",
            "retrospective_id": retro_id,
            "approval_issue_id": "hk:1",
        },
    )
    await db_session.commit()

    held = await _project(
        plan.id,
        write=True,
        now=now,
        holdings=[
            HoldingsReading(
                plan.account_ref, symbol, Decimal("9"), now - timedelta(minutes=1)
            )
        ],
    )
    assert held.planned == ()
    assert [h.code.value for h in held.holds] == ["holdings_remain"]

    flat = await _project(
        plan.id, write=True, now=now, holdings=_zero(plan, [plan.account_ref], now=now)
    )
    assert [e.event for e in flat.planned] == ["closed", "orders_settled"]
    state = await _state(db_session, plan.id)
    assert state.authority == "closed"
    assert state.cleanup == "orders_clear"


# ------------------------------------------------------------------ I19
async def test_i19_release_needs_every_scope_account_read_at_zero(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    now = _later()

    only_plan_account = await _project(
        plan.id, write=True, now=now, holdings=_zero(plan, [plan.account_ref], now=now)
    )
    assert [e.event for e in only_plan_account.planned] == ["orders_settled"]
    assert [h.code.value for h in only_plan_account.holds] == ["scope_account_unread"]

    swapped = await _project(
        plan.id,
        write=True,
        now=now,
        holdings=_zero(plan, [plan.account_ref, "acct-replacement"], now=now),
    )
    assert swapped.planned == ()  # the registered second account was never read
    assert (await _events(db_session, plan.id))[-1] == "orders_settled"

    reserved = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(MandateSymbolReservation)
        .where(MandateSymbolReservation.plan_id == plan.id)
    )
    assert reserved == 1
    with pytest.raises(ms.MandateError):  # still reserved: a second plan is refused
        await seed.register(sleeve, terms, symbol)
    await db_session.rollback()

    done = await _project(plan.id, write=True, now=now, holdings=_zero(plan, now=now))
    assert [e.event for e in done.planned] == ["holdings_disposed", "released"]
    assert done.error is None
    released = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(MandateSymbolReservationRelease)
        .join(
            MandateSymbolReservation,
            MandateSymbolReservation.id
            == MandateSymbolReservationRelease.reservation_id,
        )
        .where(MandateSymbolReservation.plan_id == plan.id)
    )
    assert released == 1
    # the symbol is free again (I1): the same symbol registers anew
    again = await seed.register(sleeve, terms, symbol)
    assert again.plan.id != plan.id


async def test_i19_an_unsettled_bound_order_blocks_orders_settled(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    await _propose_tranche(seed, plan, sleeve, state="resting", filled_qty=None)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    before = await _events(db_session, plan.id)

    report = await _project(
        plan.id, write=True, now=_later(), holdings=_zero(plan, now=_later())
    )

    assert report.planned == ()
    assert "rung_open" in [h.code.value for h in report.holds]
    assert await _events(db_session, plan.id) == before


async def test_i19_a_plan_that_was_never_approved_is_released_in_one_run(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    registered = await seed.register(sleeve, terms, symbol)
    plan = seed.plan_snapshot(registered.plan)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()

    report = await _project(plan.id, write=True, now=_later())

    assert [e.event for e in report.planned] == [
        "orders_settled",
        "holdings_disposed",
        "released",
    ]
    assert (await _state(db_session, plan.id)).released


async def test_the_cli_reads_a_holdings_file_and_commits_the_release(
    seed, db_session, tmp_path
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await _prepare_plan(seed, sleeve, terms, symbol)
    await ms.append_plan_event(
        db_session, plan_id=plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    now = _later()
    path = tmp_path / "holdings.json"
    path.write_text(
        json.dumps(
            {
                "readings": [
                    {
                        "account_ref": ref,
                        "symbol": symbol,
                        "quantity": "0",
                        "checked_at": (now - timedelta(minutes=1)).isoformat(),
                    }
                    for ref in plan.scope_account_refs
                ]
            }
        )
    )
    args = cli._parser().parse_args(
        [
            "--database-url-env",
            "MANDATE_TEST_DB",
            "--plan-id",
            str(plan.id),
            "--holdings",
            str(path),
            "--commit",
        ]
    )
    code, payload = await cli.run(args, session_factory=AsyncSessionLocal, now=now)
    assert code == cli.EXIT_OK
    assert [e["event"] for e in payload["plans"][0]["events"]] == [
        "orders_settled",
        "holdings_disposed",
        "released",
    ]
    assert (await _state(db_session, plan.id)).released
