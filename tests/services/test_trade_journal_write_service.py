# tests/services/test_trade_journal_write_service.py
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.trade_journal import TradeJournal
from app.schemas.trade_journal import JournalCreateRequest, JournalUpdateRequest
from app.services.trade_journal_write_service import (
    JournalWriteError,
    TradeJournalWriteService,
)


@pytest.mark.asyncio
async def test_create_inserts_draft_journal_with_research_metadata(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    req = JournalCreateRequest(
        symbol="005930",
        instrument_type="equity_kr",
        thesis="long-term semis play",
        target_price=80000.0,
        stop_loss=60000.0,
        min_hold_days=30,
        research_session_id=42,
        research_summary_id=7,
    )

    created = await svc.create(req)

    assert created.id is not None
    assert created.status == "draft"
    row = (
        await db_session.execute(
            select(TradeJournal).where(TradeJournal.id == created.id)
        )
    ).scalar_one()
    assert row.thesis == "long-term semis play"
    assert row.extra_metadata == {"research_session_id": 42, "research_summary_id": 7}
    assert row.hold_until is not None
    assert row.hold_until - datetime.now(UTC) > timedelta(days=29)


@pytest.mark.asyncio
async def test_create_paper_without_account_raises(db_session) -> None:
    # service is live-only by design, JournalCreateRequest doesn't have account_type
    # but the test checks if it raises when trying to pass account_type (which should fail validation)
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="t",
            account_type="paper",  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_update_rejects_terminal_status(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    created = await svc.create(
        JournalCreateRequest(
            symbol="005930", instrument_type="equity_kr", thesis="t"
        )
    )
    with pytest.raises(ValidationError):
        JournalUpdateRequest(status="closed")  # DTO blocks
    with pytest.raises(JournalWriteError):
        # service-level guard for forged payloads
        await svc._apply_update(  # noqa: SLF001
            created.id, {"status": "stopped"}
        )


@pytest.mark.asyncio
async def test_update_modifies_thesis_and_research_metadata(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    created = await svc.create(
        JournalCreateRequest(symbol="005930", instrument_type="equity_kr", thesis="t1")
    )
    updated = await svc.update(
        created.id,
        JournalUpdateRequest(thesis="t2", research_session_id=99),
    )
    assert updated.thesis == "t2"
    assert updated.research_session_id == 99


@pytest.mark.asyncio
async def test_update_missing_id_raises(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    with pytest.raises(JournalWriteError):
        await svc.update(99999, JournalUpdateRequest(thesis="x"))
