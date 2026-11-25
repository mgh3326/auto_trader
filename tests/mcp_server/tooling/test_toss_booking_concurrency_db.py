"""hk 6288 — one Toss fill booking per broker fill delta (run-owned test DB).

Every pass runs on its own real session, so the ledger row lock, the re-read
under it and the single booking commit are exercised against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.mcp_server.tooling import toss_live_ledger as mod
from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence
from app.models.execution_ledger import ExecutionLedger
from app.models.review import TossLiveOrderLedger, Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("toss_ledger_cleanup_lock"),
]

validate_run_owned_database_url(engine.url)

# A pass that waits for the row lock instead of skipping it fails here.
NO_WAIT_SECONDS = 15


class _ProcessDied(BaseException):
    """Stands for a killed process: no handler in the kernel may catch it."""


@pytest.fixture(autouse=True)
def _quiet_side_effects():
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "follow_committed_fills", new=AsyncMock()),
    ):
        yield


@pytest_asyncio.fixture
async def order(toss_ledger_cleanup_lock):
    """Factory for one accepted KR order; removes everything it booked."""
    token = uuid.uuid4().hex[:10]
    symbol = f"H6288-{token}"
    order_id = f"ord-6288-{token}"

    async def _create(side: str) -> SimpleNamespace:
        async with AsyncSessionLocal() as db:
            row = await TossLiveOrderLedgerService(db).record_send(
                operation_kind="place",
                market="kr",
                symbol=symbol,
                side=side,
                order_type="limit",
                time_in_force="DAY",
                quantity=Decimal("3"),
                price=Decimal("85000"),
                order_amount=None,
                currency="KRW",
                client_order_id=f"cid-6288-{token}",
                broker_order_id=order_id,
                original_order_id=None,
                status="accepted",
                broker_status=None,
                response_code="0",
                response_message=None,
                raw_response={},
                thesis="t" if side == "buy" else None,
                strategy="s" if side == "buy" else None,
                exit_reason="trim" if side == "sell" else None,
            )
        if side == "sell":
            # Two lots of 2: selling 3 closes the first and splits the second.
            async with AsyncSessionLocal() as db:
                for _ in range(2):
                    db.add(
                        TradeJournal(
                            symbol=symbol,
                            instrument_type=InstrumentType.equity_kr,
                            side="buy",
                            thesis="hk 6288 lot",
                            status=JournalStatus.active,
                            entry_price=Decimal("80000"),
                            quantity=Decimal("2"),
                            amount=Decimal("160000"),
                            account_type="live",
                            account="toss",
                        )
                    )
                    await db.commit()
        return SimpleNamespace(
            ledger_id=row.id, symbol=symbol, order_id=order_id, side=side
        )

    try:
        yield _create
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(ExecutionLedger).where(
                    ExecutionLedger.broker_order_id == order_id
                )
            )
            await db.execute(
                delete(TossLiveOrderLedger).where(TossLiveOrderLedger.symbol == symbol)
            )
            await db.execute(delete(TradeJournal).where(TradeJournal.symbol == symbol))
            await db.execute(delete(Trade).where(Trade.order_id == order_id))
            await db.commit()


async def _scan(order_id: str) -> TossLiveOrderLedger:
    """The detached row a reconcile pass holds after its scan."""
    async with AsyncSessionLocal() as db:
        [row] = await TossLiveOrderLedgerService(db).list_open(order_id=order_id)
    return row


async def _pass(
    row: TossLiveOrderLedger, *, cum: str = "3", local_status: str | None = None
) -> dict:
    filled = cum == "3"
    evidence = TossFillEvidence(
        verdict="filled" if filled else "partial",
        local_status=local_status or ("filled" if filled else "partial"),
        broker_status="FILLED" if filled else "PARTIAL_FILLED",
        filled_qty=Decimal(cum),
        avg_price=Decimal("85000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="test",
    )
    source = SimpleNamespace(evidence_for=AsyncMock(return_value=evidence))
    return await mod._reconcile_one_toss_row(
        row, dry_run=False, evidence_source=source, project_proposal_rungs=False
    )


async def _booked(target: SimpleNamespace) -> SimpleNamespace:
    async with AsyncSessionLocal() as db:
        ledger = await db.get(TossLiveOrderLedger, target.ledger_id)
        journals = list(
            (
                await db.execute(
                    select(TradeJournal)
                    .where(TradeJournal.symbol == target.symbol)
                    .order_by(TradeJournal.id)
                )
            )
            .scalars()
            .all()
        )
        trades = list(
            (await db.execute(select(Trade).where(Trade.order_id == target.order_id)))
            .scalars()
            .all()
        )
        fills = list(
            (
                await db.execute(
                    select(ExecutionLedger)
                    .where(ExecutionLedger.broker_order_id == target.order_id)
                    .order_by(ExecutionLedger.id)
                )
            )
            .scalars()
            .all()
        )
    closed = [j for j in journals if j.status == JournalStatus.closed]
    active = [j for j in journals if j.status == JournalStatus.active]
    return SimpleNamespace(
        ledger=ledger,
        journals=journals,
        trades=trades,
        fills=fills,
        closed_rows=len(closed),
        closed_qty=sum((j.quantity for j in closed), Decimal("0")),
        active_qty=sum((j.quantity for j in active), Decimal("0")),
    )


async def _assert_booked_exactly_once(target: SimpleNamespace) -> None:
    state = await _booked(target)
    assert state.ledger.status == "filled"
    assert state.ledger.filled_qty == Decimal("3")
    assert len(state.trades) == 1
    assert state.ledger.trade_id == state.trades[0].id
    assert [fill.filled_qty for fill in state.fills] == [Decimal("3")]
    if target.side == "buy":
        [journal] = state.journals
        assert journal.status == JournalStatus.active
        assert journal.quantity == Decimal("3")
        assert journal.trade_id == state.trades[0].id
        assert state.ledger.journal_id == journal.id
    else:
        # One FIFO close of 3: lot one whole, one split off lot two.
        assert state.closed_rows == 2
        assert state.closed_qty == Decimal("3")
        assert state.active_qty == Decimal("1")


async def _assert_nothing_booked(target: SimpleNamespace) -> None:
    state = await _booked(target)
    assert state.ledger.status == "accepted"
    assert state.ledger.filled_qty is None
    assert state.ledger.journal_id is None
    assert state.trades == []
    assert state.fills == []
    if target.side == "buy":
        assert state.journals == []
    else:
        assert state.closed_rows == 0
        assert state.active_qty == Decimal("4")


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_concurrent_passes_book_the_fill_once(order, side):
    target = await order(side)
    first_row = await _scan(target.order_id)
    second_row = await _scan(target.order_id)
    late_row = await _scan(target.order_id)
    inside, release = asyncio.Event(), asyncio.Event()
    real_upsert = mod.upsert_toss_execution_fill

    async def _held_upsert(*args, **kwargs):
        # Park the first pass mid-booking, after its journal/FIFO step.
        if not inside.is_set():
            inside.set()
            await release.wait()
        return await real_upsert(*args, **kwargs)

    with patch.object(mod, "upsert_toss_execution_fill", new=_held_upsert):
        first = asyncio.create_task(_pass(first_row))
        try:
            await asyncio.wait_for(inside.wait(), NO_WAIT_SECONDS)
            second = await asyncio.wait_for(_pass(second_row), NO_WAIT_SECONDS)
        finally:
            release.set()
        first_out = await asyncio.wait_for(first, NO_WAIT_SECONDS)

    assert first_out["action"] == "booked"
    assert second["action"] == "skipped_row_locked"
    await _assert_booked_exactly_once(target)

    # A pass still holding the pre-booking scan row books nothing: its delta
    # comes from the row re-read under the lock.
    late = await _pass(late_row)
    assert late["action"] == "noop_already_booked"
    assert late["delta_qty"] == 0.0
    await _assert_booked_exactly_once(target)


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_stale_scan_row_books_only_the_unbooked_delta(order, side):
    target = await order(side)
    first_row = await _scan(target.order_id)
    stale_row = await _scan(target.order_id)

    first = await _pass(first_row, cum="1")
    second = await _pass(stale_row)

    assert (first["action"], first["delta_qty"]) == ("booked", 1.0)
    assert (second["action"], second["delta_qty"]) == ("booked", 2.0)
    state = await _booked(target)
    assert state.ledger.filled_qty == Decimal("3")
    assert [fill.filled_qty for fill in state.fills] == [Decimal("1"), Decimal("2")]
    if side == "buy":
        assert len(state.journals) == 1
    else:
        assert state.closed_qty == Decimal("3")
        assert state.active_qty == Decimal("1")


@pytest.mark.parametrize(
    "dies_at", ["upsert_toss_execution_fill", "update_reconcile_outcome"]
)
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_pass_that_dies_after_the_journal_step_does_not_rebook(
    order, side, dies_at
):
    target = await order(side)
    owner = (
        mod
        if dies_at == "upsert_toss_execution_fill"
        else mod.TossLiveOrderLedgerService
    )
    died = AsyncMock(side_effect=_ProcessDied())

    with patch.object(owner, dies_at, new=died):
        with pytest.raises(_ProcessDied):
            await _pass(await _scan(target.order_id))

    died.assert_awaited_once()
    await _assert_nothing_booked(target)

    replay = await _pass(await _scan(target.order_id))

    assert replay["action"] == "booked"
    await _assert_booked_exactly_once(target)


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_locked_row_is_skipped_then_booked_by_the_next_pass(order, side):
    target = await order(side)
    row = await _scan(target.order_id)

    async with AsyncSessionLocal() as holder:
        await holder.execute(
            select(TossLiveOrderLedger.id)
            .where(TossLiveOrderLedger.id == target.ledger_id)
            .with_for_update()
        )
        skipped = await asyncio.wait_for(_pass(row), NO_WAIT_SECONDS)
        await holder.rollback()

    assert skipped["action"] == "skipped_row_locked"
    await _assert_nothing_booked(target)

    booked = await _pass(await _scan(target.order_id))

    assert booked["action"] == "booked"
    await _assert_booked_exactly_once(target)


async def test_older_evidence_never_moves_booked_progress_backwards(order):
    target = await order("buy")
    newer_row = await _scan(target.order_id)
    older_row = await _scan(target.order_id)
    assert (await _pass(newer_row))["action"] == "booked"

    older = await _pass(older_row, cum="1")

    assert older["action"] == "noop_stale_evidence"
    # The row moved under this pass: an ordinary race, nothing to review.
    assert "requires_manual_review" not in older
    await _assert_booked_exactly_once(target)


async def test_broker_reporting_less_than_booked_is_flagged_not_written(order):
    target = await order("buy")
    assert (await _pass(await _scan(target.order_id), cum="2"))["action"] == "booked"

    lower = await _pass(await _scan(target.order_id), cum="1")

    assert lower["action"] == "noop_stale_evidence"
    assert lower["requires_manual_review"] is True
    assert lower["reason_code"] == "evidence_contradiction"
    state = await _booked(target)
    assert state.ledger.status == "partial"
    assert state.ledger.filled_qty == Decimal("2")
    assert state.ledger.last_reconcile_error["reason_code"] == "evidence_contradiction"
    assert len(state.journals) == 1
    assert len(state.fills) == 1


async def test_row_expired_by_another_pass_is_not_reopened(order):
    target = await order("buy")
    newer_row = await _scan(target.order_id)
    older_row = await _scan(target.order_id)
    expired = await _pass(newer_row, cum="1", local_status="expired")
    assert expired["action"] == "booked"

    older = await _pass(older_row, cum="1")

    assert older["action"] == "noop_terminal"
    state = await _booked(target)
    assert state.ledger.status == "expired"
    assert state.ledger.filled_qty == Decimal("1")
    assert len(state.journals) == 1
    assert len(state.fills) == 1
