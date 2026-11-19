"""hk 5357 — DB-backed run of the journal exit correction (test DB only).

Every row uses a random ``Q`` symbol and is deleted afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal
from app.models.execution_ledger import ExecutionLedger
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services import journal_exit_correction as jec
from tests.services.execution_ledger._quarantine_fixtures import (
    purge_test_ledger_rows,
    row_kwargs,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BOOKED = datetime(2026, 11, 19, 0, 52, tzinfo=UTC)
FILLED = datetime(2026, 11, 18, 7, 9, 45, tzinfo=UTC)
CREATED = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
REASON = "hk 5357: closed by a reconcile before hk 5332"
ACTOR = "operator"


class _Seed:
    def __init__(self, db) -> None:
        self.db = db
        self.symbols: set[str] = set()
        self.journal_ids: list[int] = []

    async def lot(self, symbol: str, *, source: str = "reconciler") -> tuple[int, int]:
        self.symbols.add(symbol)
        ledger = ExecutionLedger(
            **ExecutionLedgerUpsert(
                **row_kwargs(
                    symbol=symbol,
                    order_no=f"S{symbol[1:]}0001",
                    side="sell",
                    source=source,
                    raw_payload_json=None,
                    fill_seq=0,
                    filled_qty="2",
                    filled_price="91000",
                    filled_notional="182000",
                    filled_at=FILLED.isoformat(),
                    filled_at_basis="broker_fill_time",
                )
            ).model_dump()
        )
        journal = TradeJournal(
            symbol=symbol,
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            status="closed",
            thesis="hk5357 test lot",
            account="kis",
            account_type="live",
            entry_price=Decimal("82200"),
            quantity=Decimal("2"),
            exit_price=Decimal("91000"),
            exit_date=BOOKED,
            pnl_pct=Decimal("10.7056"),
            created_at=CREATED,
        )
        self.db.add_all([ledger, journal])
        await self.db.flush()
        ids = (int(journal.id), int(ledger.id))
        self.journal_ids.append(ids[0])
        await self.db.commit()
        return ids

    async def cleanup(self) -> None:
        await self.db.rollback()
        await self.db.execute(
            delete(TradeJournal).where(TradeJournal.id.in_(self.journal_ids))
        )
        await self.db.commit()
        await purge_test_ledger_rows(self.db, ExecutionLedger.symbol.in_(self.symbols))


@pytest_asyncio.fixture
async def seed(db_session):
    helper = _Seed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


def _symbol() -> str:
    return f"Q{uuid.uuid4().hex[:5].upper()}"


def _evidence(jid: int, symbol: str, **over) -> jec.Evidence:
    order_id = f"S{symbol[1:]}0001"
    data = {
        "journal_id": jid,
        "evidence_ref": "hk:doc/reconcile-result-test",
        "record_order_id": order_id,
        "record_closed_journal_ids": (jid,),
        "venue": "krx",
        "broker_order_id": order_id,
        "fill_seq": 0,
        "allocated_quantity": Decimal("2"),
    }
    data.update(over)
    return jec.Evidence(**data)


async def _journal(db, jid: int) -> TradeJournal:
    await db.rollback()
    db.expire_all()
    return (
        await db.execute(select(TradeJournal).where(TradeJournal.id == jid))
    ).scalar_one()


async def test_preview_commit_and_second_commit(db_session, seed) -> None:
    symbol = _symbol()
    jid, lid = await seed.lot(symbol)
    ev = {jid: _evidence(jid, symbol)}

    prev = await jec.preview(db_session, [jid], ev)
    assert prev.status == "eligible"
    [plan] = prev.plan.plans
    assert plan.ledger_id == lid
    assert plan.new_exit_date == FILLED
    assert plan.new_total_pnl_krw == Decimal("17600")
    assert (await _journal(db_session, jid)).total_pnl_krw is None  # preview: no write

    done = await jec.commit(
        db_session,
        [jid],
        ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert done.status == "committed"
    row = await _journal(db_session, jid)
    assert row.exit_date == FILLED
    assert row.total_pnl_krw == Decimal("17600")
    assert row.exit_price == Decimal("91000") and row.pnl_pct == Decimal("10.7056")
    assert f"ledger_id={lid}" in row.notes and "decision=hk 5357" in row.notes
    notes = row.notes

    # the identical retry with the original digest and no new preview
    retry = await jec.commit(
        db_session,
        [jid],
        ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert retry.status == "noop"
    assert (await _journal(db_session, jid)).notes == notes


async def test_a_provisional_only_sell_leaves_the_whole_batch_untouched(
    db_session, seed
) -> None:
    good_symbol, bad_symbol = _symbol(), _symbol()
    good_jid, _ = await seed.lot(good_symbol)
    bad_jid, _ = await seed.lot(bad_symbol, source="websocket")
    ev = {
        good_jid: _evidence(good_jid, good_symbol),
        bad_jid: _evidence(bad_jid, bad_symbol),
    }

    prev = await jec.preview(db_session, [good_jid, bad_jid], ev)
    assert prev.status == "refused"
    assert {p.journal_id: p.reason for p in prev.plan.plans}[bad_jid] == (
        "ledger_row_not_in_effect_authoritative"
    )
    out = await jec.commit(
        db_session,
        [good_jid, bad_jid],
        ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert out.status == "refused"
    assert (await _journal(db_session, good_jid)).total_pnl_krw is None


async def test_a_sell_before_the_journal_was_created_is_found_by_its_identity(
    db_session, seed
) -> None:
    """The cited fill may predate the lot's journal; identity, not time, finds it."""
    symbol = _symbol()
    jid, lid = await seed.lot(symbol)
    await db_session.execute(
        update(TradeJournal)
        .where(TradeJournal.id == jid)
        .values(created_at=FILLED + timedelta(hours=1))
    )
    await db_session.commit()

    prev = await jec.preview(db_session, [jid], {jid: _evidence(jid, symbol)})
    assert prev.status == "eligible"
    assert prev.plan.plans[0].ledger_id == lid


async def test_commit_waits_for_a_ledger_writer_and_notices_the_changed_fill(
    db_session, seed
) -> None:
    """The cited fill moving after the preview rolls the correction back."""
    symbol = _symbol()
    jid, lid = await seed.lot(symbol)
    ev = {jid: _evidence(jid, symbol)}
    prev = await jec.preview(db_session, [jid], ev)

    async with AsyncSessionLocal() as writer:
        await writer.execute(
            update(ExecutionLedger)
            .where(ExecutionLedger.id == lid)
            .values(filled_at=FILLED + timedelta(minutes=1))
        )
        task = asyncio.create_task(
            jec.commit(
                db_session,
                [jid],
                ev,
                expected_digest=prev.plan.digest,
                reason=REASON,
                actor=ACTOR,
            )
        )
        await asyncio.sleep(0.5)
        assert not task.done(), "commit must wait for the in-flight ledger writer"
        await writer.commit()
        result = await asyncio.wait_for(task, timeout=15)

    assert result.status == "plan_changed"
    assert (await _journal(db_session, jid)).total_pnl_krw is None


async def test_a_writer_that_starts_after_the_revalidation_waits_for_the_commit(
    db_session, seed, monkeypatch
) -> None:
    """The lock is held from revalidation to commit, not just while reading."""
    symbol = _symbol()
    jid, lid = await seed.lot(symbol)
    ev = {jid: _evidence(jid, symbol)}
    prev = await jec.preview(db_session, [jid], ev)

    real_apply = jec.apply_plan
    writer_state: dict = {}

    async def late_writer() -> None:
        async with AsyncSessionLocal() as writer:
            await writer.execute(
                update(ExecutionLedger)
                .where(ExecutionLedger.id == lid)
                .values(filled_at=FILLED - timedelta(minutes=5))
            )
            await writer.commit()

    def apply_then_start_writer(*args, **kwargs):
        # runs after the plan was revalidated and before the commit
        writer_state["task"] = asyncio.get_running_loop().create_task(late_writer())
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(jec, "apply_plan", apply_then_start_writer)
    result = await jec.commit(
        db_session,
        [jid],
        ev,
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert result.status == "committed"
    task = writer_state["task"]
    await asyncio.wait_for(
        task, timeout=15
    )  # it ran only once the commit released the lock
    row = await _journal(db_session, jid)
    assert row.exit_date == FILLED  # the correction used the revalidated fill


async def test_a_stuck_ledger_writer_makes_commit_fail_closed(
    db_session, seed, monkeypatch
) -> None:
    symbol = _symbol()
    jid, lid = await seed.lot(symbol)
    ev = {jid: _evidence(jid, symbol)}
    prev = await jec.preview(db_session, [jid], ev)
    monkeypatch.setattr(jec, "LOCK_TIMEOUT_MS", 200)

    async with AsyncSessionLocal() as writer:
        await writer.execute(
            update(ExecutionLedger)
            .where(ExecutionLedger.id == lid)
            .values(filled_at=FILLED + timedelta(minutes=1))
        )
        with pytest.raises(DBAPIError):
            await jec.commit(
                db_session,
                [jid],
                ev,
                expected_digest=prev.plan.digest,
                reason=REASON,
                actor=ACTOR,
            )
        await writer.rollback()
    assert (await _journal(db_session, jid)).total_pnl_krw is None


async def test_an_order_with_two_authoritative_fills_is_refused_in_the_database(
    db_session, seed
) -> None:
    """The cited fill_seq does not make an ambiguous order unique."""
    symbol = _symbol()
    jid, _ = await seed.lot(symbol)
    db_session.add(
        ExecutionLedger(
            **ExecutionLedgerUpsert(
                **row_kwargs(
                    symbol=symbol,
                    order_no=f"S{symbol[1:]}0001",
                    side="sell",
                    source="reconciler",
                    raw_payload_json=None,
                    fill_seq=1,
                    filled_qty="2",
                    filled_price="91000",
                    filled_notional="182000",
                    filled_at=(FILLED + timedelta(minutes=1)).isoformat(),
                    filled_at_basis="broker_fill_time",
                )
            ).model_dump()
        )
    )
    await db_session.commit()

    prev = await jec.preview(db_session, [jid], {jid: _evidence(jid, symbol)})
    assert prev.status == "refused"
    assert prev.plan.plans[0].reason == "ambiguous_order_fills"


async def test_an_order_time_row_needs_a_verified_fill_time(db_session, seed) -> None:
    symbol = _symbol()
    jid, _ = await seed.lot(symbol)
    await db_session.execute(
        update(ExecutionLedger)
        .where(ExecutionLedger.symbol == symbol)
        .values(filled_at_basis="broker_order_time")
    )
    await db_session.commit()

    prev = await jec.preview(db_session, [jid], {jid: _evidence(jid, symbol)})
    assert prev.plan.plans[0].reason == "fill_time_not_a_broker_fill_time"
    verified = _evidence(
        jid,
        symbol,
        verified_fill_at=FILLED + timedelta(hours=2),
        verified_fill_ref="hk:doc/fill-time",
    )
    again = await jec.preview(db_session, [jid], {jid: verified})
    assert again.status == "eligible"
    assert again.plan.plans[0].new_exit_date == FILLED + timedelta(hours=2)
