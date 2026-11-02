"""The real store and DB constraints (ROB-1460). Runs in CI against the test DB."""

from __future__ import annotations

import importlib.util
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import settings
from app.core.db import AsyncSessionLocal, engine
from app.models.base import Base
from app.models.execution_ledger import ExecutionLedger
from app.models.mechanical_loss_cut import LotExitPlan, MechanicalLossCutDecision
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.models.trading import InstrumentType
from app.services.mechanical_loss_cut import positions
from app.services.mechanical_loss_cut.job import DatabasePorts, evaluate_all_lots
from app.services.mechanical_loss_cut.plans import record_buy_exit_plan
from app.services.mechanical_loss_cut.rules import (
    AddLevel,
    CutDecision,
    LotKey,
    PlanDraft,
)
from app.services.mechanical_loss_cut.store import (
    LIVE_ARMED_DETAIL,
    MechanicalLossCutStore,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_TABLES = [LotExitPlan.__table__, MechanicalLossCutDecision.__table__]
_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/20261102_rob1460_lot_exit_plans.py"
)
_NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("rob1460_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> Any:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES, checkfirst=True)
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("TRUNCATE review.lot_exit_plans, review.mechanical_loss_cut_decisions")
        )
        await session.commit()


def _key() -> LotKey:
    return LotKey("kis_live", "equity_kr", f"T{uuid.uuid4().hex[:5].upper()}")


def _draft(key: LotKey, **overrides: Any) -> PlanDraft:
    values: dict[str, Any] = {
        "key": key,
        "event": "entry",
        "stop_price": Decimal("85000"),
        "stop_basis": "support",
        "stop_reason": "60-session low",
        "reference_price": Decimal("100000"),
        "average_cost": Decimal("100000"),
        "quantity": Decimal("10"),
        "add_ladder": (AddLevel(Decimal("95000"), Decimal("2")),),
        "max_adds": 1,
        "adds_used": 0,
        "add_verdict": None,
        "backfilled": False,
    }
    values.update(overrides)
    return PlanDraft(**values)


def _decision(key: LotKey, *, trigger: str = "v1:85000:r1") -> CutDecision:
    return CutDecision(
        key=key,
        plan_id=1,
        plan_version=1,
        backfilled=False,
        trigger_family="plan_stop_close",
        trigger_key=trigger,
        session=date(2026, 10, 30),
        close=Decimal("84000"),
        stop_price=Decimal("85000"),
        average_cost=Decimal("100000"),
        unrealized_pct=Decimal("-16"),
        held_quantity=Decimal("10"),
        open_sell_quantity=Decimal("0"),
        protected_quantity=Decimal("0"),
        intended_quantity=Decimal("10"),
        intended_limit_price=Decimal("83200"),
        intended_notional=Decimal("832000"),
        currency="KRW",
        episode_start=date(2026, 10, 30),
        details={"close": "84000"},
    )


async def test_buy_then_add_writes_increasing_versions() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        await store.lock_lot(key)
        first = await store.append_plan(_draft(key))
        second = await store.append_plan(
            _draft(key, event="add", average_cost=Decimal("95000"), adds_used=1)
        )
        await session.commit()

    assert (first.version, second.version) == (1, 2)
    async with AsyncSessionLocal() as session:
        latest = await MechanicalLossCutStore(session).latest_plan(key)
    assert latest is not None
    assert latest.version == 2
    assert latest.average_cost == Decimal("95000")
    assert latest.add_ladder == (AddLevel(Decimal("95000"), Decimal("2")),)
    assert latest.created_at is not None


async def test_one_decision_per_trigger() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        inserted = await store.insert_decision(
            _decision(key),
            mode="shadow",
            per_order_cap=Decimal("2000000"),
            evaluated_at=_NOW,
        )
        repeated = await store.insert_decision(
            _decision(key),
            mode="shadow",
            per_order_cap=Decimal("2000000"),
            evaluated_at=_NOW,
        )
        other_trigger = await store.insert_decision(
            _decision(key, trigger="v2:85000:r1"),
            mode="shadow",
            per_order_cap=Decimal("2000000"),
            evaluated_at=_NOW,
        )
        await session.commit()

    assert inserted is not None
    assert repeated is None
    assert other_trigger is not None


async def test_prior_decision_is_the_latest_and_counts_its_plan_and_stop() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        for trigger in ("v1:85000:r1", "v1:85000:r2"):
            await store.insert_decision(
                _decision(key, trigger=trigger),
                mode="shadow",
                per_order_cap=Decimal("2000000"),
                evaluated_at=_NOW,
            )
        await session.commit()
        prior = await store.prior_decisions([key], mode="shadow")
        other_mode = await store.prior_decisions([key], mode="live")

    assert prior[key].cuts_on_this_stop == 2
    assert prior[key].stop_price == Decimal("85000")
    assert other_mode == {}


async def test_a_live_row_requires_a_proposal_and_a_shadow_row_refuses_one() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await MechanicalLossCutStore(session).insert_decision(
                _decision(key),
                mode="live",
                per_order_cap=Decimal("2000000"),
                evaluated_at=_NOW,
            )
        await session.rollback()
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await MechanicalLossCutStore(session).insert_decision(
                _decision(key),
                mode="shadow",
                per_order_cap=Decimal("2000000"),
                evaluated_at=_NOW,
                proposal_id=uuid.uuid4(),
            )
        await session.rollback()


async def test_unemitted_trigger_is_the_armed_latest_shadow_row_with_no_live_row() -> (
    None
):
    armed = _key()
    unarmed = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)

        async def shadow(key: LotKey, trigger: str, *, live_armed: bool) -> int | None:
            decision = _decision(key, trigger=trigger)
            if live_armed:
                decision = replace(
                    decision,
                    details={**decision.details, LIVE_ARMED_DETAIL: "true"},
                )
            return await store.insert_decision(
                decision,
                mode="shadow",
                per_order_cap=Decimal("2000000"),
                evaluated_at=_NOW,
            )

        first = await shadow(armed, "v1:85000:r1", live_armed=True)
        await shadow(unarmed, "v1:85000:r1", live_armed=False)
        await session.commit()

        keys = [armed, unarmed]
        assert await store.unemitted_live_triggers(keys) == {
            armed: ("v1:85000:r1", first)
        }
        assert await store.unemitted_live_triggers([unarmed]) == {}

        await store.insert_decision(
            _decision(armed, trigger="v1:85000:r1"),
            mode="live",
            per_order_cap=Decimal("2000000"),
            evaluated_at=_NOW,
            proposal_id=uuid.uuid4(),
        )
        await session.commit()
        assert await store.unemitted_live_triggers(keys) == {}

        # A later run on the lot is owed on its own; the old one is not revived.
        second = await shadow(armed, "v1:85000:r2", live_armed=True)
        await session.commit()
        assert await store.unemitted_live_triggers(keys) == {
            armed: ("v1:85000:r2", second)
        }


async def test_a_backfill_never_overwrites_a_plan_a_buy_just_wrote() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        await store.lock_lot(key)
        entry = await store.append_plan(_draft(key))
        await session.commit()

        result = await DatabasePorts(session).append_backfill(
            _draft(key, event="backfill", backfilled=True)
        )
        await session.commit()

        assert result.id == entry.id
        latest = await store.latest_plan(key)
        assert latest is not None
        assert (latest.version, latest.event) == (entry.version, "entry")


async def test_latest_sell_is_read_per_lot_from_authoritative_fills() -> None:
    symbol = f"{uuid.uuid4().int % 1000000:06d}"
    key = LotKey("kis_live", "equity_kr", symbol)
    async with AsyncSessionLocal() as session:
        await _seed_fill(session, symbol)
        await session.commit()
        assert await positions.latest_sell_at(session, key) is None

        sold_at = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
        session.add(
            ExecutionLedger(
                broker="kis",
                account_mode="live",
                venue="krx",
                instrument_type=InstrumentType.equity_kr,
                symbol=symbol,
                raw_symbol=symbol,
                side="sell",
                filled_qty=Decimal("10"),
                filled_price=Decimal("90000"),
                filled_notional=Decimal("900000"),
                filled_at=sold_at,
                currency="KRW",
                broker_order_id=f"{symbol}S1",
                fill_seq=0,
                source="reconciler",
            )
        )
        await session.commit()
        assert await positions.latest_sell_at(session, key) == sold_at


async def test_live_decision_is_found_by_its_proposal_only() -> None:
    key = _key()
    proposal_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        await store.insert_decision(
            _decision(key),
            mode="live",
            per_order_cap=Decimal("2000000"),
            evaluated_at=_NOW,
            proposal_id=proposal_id,
        )
        await session.commit()
        assert (await store.live_decision_for_proposal(proposal_id)) is not None
        assert (await store.live_decision_for_proposal(uuid.uuid4())) is None


async def test_rows_are_append_only_under_the_migration_guards() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        store = MechanicalLossCutStore(session)
        await store.lock_lot(key)
        await store.append_plan(_draft(key))
        await session.commit()

    async with engine.begin() as conn:
        for statement in _load_migration().GUARD_DDL:
            await conn.execute(text(statement))
    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(
                    text("UPDATE review.lot_exit_plans SET stop_price = 1")
                )
            await session.rollback()
        async with AsyncSessionLocal() as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text("DELETE FROM review.lot_exit_plans"))
            await session.rollback()
        for table in ("lot_exit_plans", "mechanical_loss_cut_decisions"):
            async with AsyncSessionLocal() as session:
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(text(f"TRUNCATE review.{table}"))
                await session.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS trg_lot_exit_plans_append_only "
                    "ON review.lot_exit_plans"
                )
            )
            await conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_mechanical_loss_cut_decisions_append_only "
                    "ON review.mechanical_loss_cut_decisions"
                )
            )
            await conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS trg_lot_exit_plans_no_truncate "
                    "ON review.lot_exit_plans"
                )
            )
            await conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_mechanical_loss_cut_decisions_no_truncate "
                    "ON review.mechanical_loss_cut_decisions"
                )
            )


async def _seed_fill(session: Any, symbol: str, order: str = "A1") -> None:
    session.add(
        ExecutionLedger(
            broker="kis",
            account_mode="live",
            venue="krx",
            instrument_type=InstrumentType.equity_kr,
            symbol=symbol,
            raw_symbol=symbol,
            side="buy",
            broker_order_id=f"{symbol}{order}",
            fill_seq=0,
            filled_qty=Decimal("10"),
            filled_price=Decimal("100000"),
            filled_notional=Decimal("1000000"),
            filled_at=_NOW,
            currency="KRW",
            source="reconciler",
        )
    )
    await session.flush()


async def test_held_lots_and_open_sells_come_from_the_database() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        await _seed_fill(session, key.symbol)
        session.add(
            KISLiveOrderLedger(
                trade_date=_NOW,
                symbol=key.symbol,
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("4"),
                price=Decimal("110000"),
                amount=Decimal("440000"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"{key.symbol}S",
                account_mode="kis_live",
                broker="kis",
                status="accepted",
                lifecycle_state="accepted",
                filled_qty=Decimal("1"),
            )
        )
        await session.flush()

        lots = await positions.load_held_lots(session, markets=("equity_kr",))
        held = next(lot for lot in lots if lot.key == key)
        sells = await positions.load_open_sell_quantities(session, [key])
        protected = await positions.load_protected_quantities(session, [key])
        await session.rollback()

    assert held.quantity == Decimal("10")
    assert held.average_cost == Decimal("100000")
    assert sells[key] == Decimal("3")
    assert protected[key] == Decimal("0")


async def test_an_active_sell_proposal_counts_as_committed_quantity() -> None:
    key = _key()
    async with AsyncSessionLocal() as session:
        group = OrderProposal(
            proposal_id=uuid.uuid4(),
            root_proposal_id=uuid.uuid4(),
            revision=1,
            no_resubmit=False,
            payload_hash="x" * 64,
            symbol=key.symbol,
            market=key.market,
            account_mode=key.account_mode,
            side="sell",
            order_type="limit",
            proposer="test",
            lifecycle_state="proposed",
            action="place",
        )
        session.add(group)
        await session.flush()
        session.add(
            OrderProposalRung(
                proposal_pk=group.id,
                rung_index=0,
                side="sell",
                quantity=Decimal("6"),
                limit_price=Decimal("110000"),
                state="pending_approval",
            )
        )
        await session.flush()

        sells = await positions.load_open_sell_quantities(session, [key])
        await session.rollback()

    assert sells[key] == Decimal("6")


async def test_buy_on_an_unplanned_held_lot_keeps_the_whole_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LOT_EXIT_PLAN_WRITE_ENABLED", True)
    key = _key()
    async with AsyncSessionLocal() as session:
        await _seed_fill(session, key.symbol)
        await session.commit()
    buy = {
        "account_mode": key.account_mode,
        "market": key.market,
        "symbol": key.symbol,
        "price": Decimal("90000"),
        "quantity": Decimal("10"),
        "now": _NOW,
    }

    async def rows() -> list[Any]:
        async with AsyncSessionLocal() as session:
            return list(
                (
                    await session.execute(
                        select(LotExitPlan)
                        .where(LotExitPlan.symbol == key.symbol)
                        .order_by(LotExitPlan.version)
                    )
                )
                .scalars()
                .all()
            )

    try:
        assert await record_buy_exit_plan(**buy) == "left_to_backfill"
        assert await rows() == []

        stated = {"stop_price": "80000", "stop_basis": "support", "stop_reason": "low"}
        assert await record_buy_exit_plan(**buy, stated=stated) == "add"

        backfill, add = await rows()
        assert (backfill.version, backfill.event, backfill.backfilled) == (
            1,
            "backfill",
            True,
        )
        assert (backfill.quantity, backfill.average_cost) == (
            Decimal("10"),
            Decimal("100000"),
        )
        assert (add.version, add.event, add.backfilled) == (2, "add", False)
        assert (add.quantity, add.average_cost, add.stop_price) == (
            Decimal("20"),
            Decimal("95000"),
            Decimal("80000"),
        )
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("DELETE FROM review.execution_ledger WHERE symbol = :symbol"),
                {"symbol": key.symbol},
            )
            await session.commit()


async def test_job_backfills_and_stays_quiet_without_candles() -> None:
    """End to end on the real ports: no bars means no decision, and a plan."""
    key = _key()
    async with AsyncSessionLocal() as session:
        await _seed_fill(session, key.symbol)
        await session.commit()
    try:
        async with AsyncSessionLocal() as session:
            result = await evaluate_all_lots(
                DatabasePorts(session), now=_NOW, markets=("equity_kr",)
            )
        assert result["decisions_written"] == 0
        assert result["skipped"]["price_missing"] >= 1
        async with AsyncSessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(LotExitPlan).where(LotExitPlan.symbol == key.symbol)
                    )
                )
                .scalars()
                .all()
            )
        assert [(row.event, row.backfilled) for row in rows] == [("backfill", True)]
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("DELETE FROM review.execution_ledger WHERE symbol = :symbol"),
                {"symbol": key.symbol},
            )
            await session.commit()
