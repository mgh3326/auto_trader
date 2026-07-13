"""ROB-842 atomic submit-claim on the existing Alpaca paper ledger (DB-backed).

Exercises the real PostgreSQL partial-unique execution slot: a single winner,
losing duplicates, the in-flight discriminator, and a genuine two-session /
barrier concurrency race proving exactly one caller claims submit ownership.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import (
    AlpacaPaperLedgerService,
    is_inflight_execution,
)

pytestmark = [pytest.mark.asyncio]

_COIDS = (
    "rob842-claim-winner",
    "rob842-claim-dup-seq",
    "rob842-claim-inflight",
    "rob842-claim-barrier",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(db_session):
    await db_session.execute(
        delete(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.client_order_id.in_(_COIDS)
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        delete(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.client_order_id.in_(_COIDS)
        )
    )
    await db_session.commit()


def _claim_kwargs(coid: str) -> dict:
    return {
        "client_order_id": coid,
        "lifecycle_correlation_id": coid,
        "execution_symbol": "BTC/USD",
        "execution_venue": "alpaca_paper",
        "instrument_type": InstrumentType.crypto,
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "gtc",
        "requested_notional": Decimal("10"),
        "requested_price": Decimal("50000"),
        "preview_payload": {"symbol": "BTC/USD", "side": "buy"},
    }


async def test_claim_submit_winner_inserts_inflight_execution_row(db_session):
    svc = AlpacaPaperLedgerService(db_session)
    claim = await svc.claim_submit(**_claim_kwargs("rob842-claim-winner"))

    assert claim.won is True
    assert claim.row is not None
    assert claim.row.record_kind == "execution"
    assert claim.row.lifecycle_state == "submitted"
    assert is_inflight_execution(claim.row) is True


async def test_claim_submit_sequential_duplicate_loses(db_session):
    svc = AlpacaPaperLedgerService(db_session)
    first = await svc.claim_submit(**_claim_kwargs("rob842-claim-dup-seq"))
    second = await svc.claim_submit(**_claim_kwargs("rob842-claim-dup-seq"))

    assert first.won is True
    assert second.won is False
    # Both resolve to the same single execution row.
    assert second.row is not None
    assert second.row.id == first.row.id


async def test_record_submit_clears_inflight_marker(db_session):
    svc = AlpacaPaperLedgerService(db_session)
    claim = await svc.claim_submit(**_claim_kwargs("rob842-claim-inflight"))
    assert is_inflight_execution(claim.row) is True

    await svc.record_submit(
        "rob842-claim-inflight",
        {"id": "paper-xyz", "status": "accepted", "filled_qty": "0"},
        raw_response={"id": "paper-xyz", "status": "accepted"},
    )
    # Fresh snapshot (session is expire_on_commit=False).
    db_session.expire_all()
    row = await svc.find_executed_by_client_order_id("rob842-claim-inflight")
    assert row.broker_order_id == "paper-xyz"
    assert row.submitted_at is not None
    assert is_inflight_execution(row) is False


async def test_two_session_barrier_exactly_one_winner(db_session):
    """Two independent DB sessions race the same claim; exactly one wins."""
    from app.core.db import AsyncSessionLocal

    coid = "rob842-claim-barrier"
    barrier = asyncio.Barrier(2)

    async def _attempt() -> bool:
        async with AsyncSessionLocal() as session:
            svc = AlpacaPaperLedgerService(session)
            # Align both inserts as closely as possible at the DB.
            await barrier.wait()
            claim = await svc.claim_submit(**_claim_kwargs(coid))
            return claim.won

    results = await asyncio.gather(_attempt(), _attempt())

    assert sum(1 for won in results if won) == 1, results
    # Exactly one execution row exists for the intent.
    rows = (
        await db_session.execute(
            delete(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.client_order_id == coid)
            .returning(AlpacaPaperOrderLedger.id)
        )
    ).all()
    await db_session.commit()
    assert len(rows) == 1
