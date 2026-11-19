"""hk 5357 — DB-backed run of the journal exit correction (test DB only).

Every row uses a random ``Q`` symbol and is deleted afterwards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

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


async def _journal(db, jid: int) -> TradeJournal:
    await db.rollback()
    db.expire_all()
    return (
        await db.execute(select(TradeJournal).where(TradeJournal.id == jid))
    ).scalar_one()


async def test_preview_commit_and_second_commit(db_session, seed) -> None:
    jid, lid = await seed.lot(_symbol())

    prev = await jec.preview(db_session, [jid])
    assert prev.status == "eligible"
    [plan] = prev.plan.plans
    assert plan.ledger_id == lid
    assert plan.new_exit_date == FILLED
    assert plan.new_total_pnl_krw == Decimal("17600")
    assert (await _journal(db_session, jid)).total_pnl_krw is None  # preview: no write

    done = await jec.commit(
        db_session, [jid], expected_digest=prev.plan.digest, reason=REASON, actor=ACTOR
    )
    assert done.status == "committed"
    row = await _journal(db_session, jid)
    assert row.exit_date == FILLED
    assert row.total_pnl_krw == Decimal("17600")
    assert row.exit_price == Decimal("91000") and row.pnl_pct == Decimal("10.7056")
    assert f"ledger_id={lid}" in row.notes and "decision=hk 5357" in row.notes
    notes = row.notes

    again = await jec.preview(db_session, [jid])
    assert again.status == "noop"
    second = await jec.commit(
        db_session, [jid], expected_digest=again.plan.digest, reason=REASON, actor=ACTOR
    )
    assert second.status == "noop"
    assert (await _journal(db_session, jid)).notes == notes


async def test_a_provisional_only_sell_leaves_the_whole_batch_untouched(
    db_session, seed
) -> None:
    good_jid, _ = await seed.lot(_symbol())
    bad_jid, _ = await seed.lot(_symbol(), source="websocket")

    prev = await jec.preview(db_session, [good_jid, bad_jid])
    assert prev.status == "refused"
    assert {p.journal_id: p.reason for p in prev.plan.plans}[bad_jid] == (
        "no_matching_ledger_row"
    )
    out = await jec.commit(
        db_session,
        [good_jid, bad_jid],
        expected_digest=prev.plan.digest,
        reason=REASON,
        actor=ACTOR,
    )
    assert out.status == "refused"
    assert (await _journal(db_session, good_jid)).total_pnl_krw is None
