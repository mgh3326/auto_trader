import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _journal(
    symbol, instrument_type, *, account="kis", account_type="live", status="active"
):
    return TradeJournal(
        symbol=symbol,
        instrument_type=instrument_type,
        side="buy",
        status=status,
        account_type=account_type,
        account=account,
        entry_price=1.0,
        quantity=1.0,
        thesis="t",
    )


def _req(market):
    return CollectorRequest(
        market=market, account_scope="kis_live", symbols=None, policy_snapshot={}
    )


@pytest_asyncio.fixture(autouse=True)
async def clean_trade_journals(db_session):
    await _delete_trade_journals()
    yield
    await _delete_trade_journals()


async def _delete_trade_journals():
    async with AsyncSessionLocal() as cleanup_session:
        await cleanup_session.execute(delete(TradeJournal))
        await cleanup_session.commit()


@pytest.mark.asyncio
async def test_us_scope_excludes_kr_live_journals(db_session):
    db_session.add_all(
        [
            _journal("AAPL", InstrumentType.equity_us),
            _journal("005930", InstrumentType.equity_kr),
        ]
    )
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    active_syms = {e["symbol"] for e in payload["active"]}
    assert active_syms == {"AAPL"}
    assert payload["active"][0]["account"] == "kis"  # provenance emitted
    assert payload["collector_status"] == "ok"


@pytest.mark.asyncio
async def test_kr_scope_excludes_us_live_journals(db_session):
    db_session.add_all(
        [
            _journal("AAPL", InstrumentType.equity_us),
            _journal("005930", InstrumentType.equity_kr),
        ]
    )
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("kr"))
    active_syms = {e["symbol"] for e in results[0].payload_json["active"]}
    assert active_syms == {"005930"}


@pytest.mark.asyncio
async def test_us_scope_includes_legacy_null_account_kis_us(db_session):
    db_session.add(_journal("MSFT", InstrumentType.equity_us, account=None))
    await db_session.flush()
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    active_syms = {e["symbol"] for e in results[0].payload_json["active"]}
    assert "MSFT" in active_syms


@pytest.mark.asyncio
async def test_empty_active_reports_ok_status(db_session):
    collector = JournalSnapshotCollector(db_session)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    assert payload["active"] == []
    assert payload["collector_status"] == "ok"


@pytest.mark.asyncio
async def test_query_failure_reports_unavailable(monkeypatch, db_session):
    collector = JournalSnapshotCollector(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "execute", _boom)
    results = await collector.collect(_req("us"))
    payload = results[0].payload_json
    assert payload["collector_status"] == "unavailable"
    assert results[0].freshness_status == "unavailable"
