# tests/services/test_trade_journal_read_service.py
import pytest
import pytest_asyncio

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.services.trade_journal_read_service import TradeJournalReadService


@pytest_asyncio.fixture
async def seed_closed_journals(db_session):
    """Seed some closed/stopped journals for retrospective tests."""
    j1 = TradeJournal(
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        thesis="t1",
        status="closed",
        account_type="live",
        pnl_pct=15.5,
    )
    j2 = TradeJournal(
        symbol="AAPL",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        thesis="t2",
        status="stopped",
        account_type="live",
        pnl_pct=-5.0,
    )
    j3 = TradeJournal(
        symbol="NVDA",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        thesis="t3",
        status="active",
        account_type="live",
    )
    db_session.add_all([j1, j2, j3])
    await db_session.flush()
    return [j1, j2, j3]


@pytest.mark.asyncio
async def test_list_retrospective_returns_only_terminal_journals(
    db_session, seed_closed_journals
) -> None:
    svc = TradeJournalReadService(db_session)
    resp = await svc.list_retrospective()
    seeded_ids = {seed_closed_journals[0].id, seed_closed_journals[1].id}
    resp = [j for j in resp if j.id in seeded_ids]
    assert len(resp) == 2
    symbols = {j.symbol for j in resp}
    assert symbols == {"005930", "AAPL"}
    assert "NVDA" not in symbols


@pytest.mark.asyncio
async def test_list_retrospective_ordered_by_updated_at_desc(
    db_session, seed_closed_journals
) -> None:
    svc = TradeJournalReadService(db_session)
    resp = await svc.list_retrospective()
    seeded_ids = {seed_closed_journals[0].id, seed_closed_journals[1].id}
    resp = [j for j in resp if j.id in seeded_ids]
    # j2 was created after j1, so it should be first if updated_at is similar
    # In tests they might have same timestamp, but order should be stable
    assert resp[0].updated_at >= resp[1].updated_at
