"""DB round trip for the DART symbol backfill (hk 5623).

Runs in CI against the run-owned test database. It seeds a KR DART row with
``symbol IS NULL`` the way a pre-hk 4591 collection stored it, then drives the
real ``RepositoryDartSymbolStore``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import market_events_test_lock

validate_run_owned_database_url(engine.url)

DAY = date(2026, 10, 28)
FIRST_RECEIVED = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session, _market_events_lock):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


def _dart_row(rcept_no: str, corp_name: str) -> dict[str, Any]:
    return {
        "rcept_no": rcept_no,
        "rcept_dt": "20261028",
        "corp_name": corp_name,
        "report_nm": "연결재무제표기준영업(잠정)실적",
    }


async def _seed_pre_4591_row(db_session, rcept_no: str, corp_name: str) -> dict:
    """Store a row the way a collection before hk 4591 did: symbol NULL, no reason."""
    from app.services.market_events.normalizers import normalize_dart_disclosure_row
    from app.services.market_events.repository import MarketEventsRepository

    row = _dart_row(rcept_no, corp_name)
    event, _ = normalize_dart_disclosure_row(row, corp_registry=None)
    event["symbol"] = None
    event["raw_payload_json"] = dict(row)
    event["first_received_at"] = FIRST_RECEIVED
    await MarketEventsRepository(db_session).upsert_event_with_values(event, [])
    await db_session.commit()
    return dict(row)


async def _stored(db_session, rcept_no: str):
    from app.models.market_events import MarketEvent

    return (
        await db_session.execute(
            select(MarketEvent).where(MarketEvent.source_event_id == rcept_no)
        )
    ).scalar_one()


def _fetch(listing):
    async def fetch(target: date):
        return listing.get(target, [])

    return fetch


def _load(registry):
    async def load():
        return registry

    return load


async def _run(db_session, *, listing, registry, commit=True):
    from app.services.market_events.dart_symbol_backfill import (
        RepositoryDartSymbolStore,
        backfill_dart_symbols,
    )

    return await backfill_dart_symbols(
        RepositoryDartSymbolStore(db_session),
        from_date=DAY,
        to_date=DAY,
        commit=commit,
        fetch_rows=_fetch(listing),
        load_corp_registry=_load(registry),
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_fills_symbol_and_keeps_first_received_at(db_session):
    row = await _seed_pre_4591_row(db_session, "20261028000301", "디비하이닉스")
    before = await _stored(db_session, "20261028000301")
    assert before.symbol is None
    raw_before = dict(before.raw_payload_json)
    updated_before = before.updated_at

    report = await _run(
        db_session,
        listing={DAY: [row]},
        registry={"디비하이닉스": [{"corp_code": "00164779", "stock_code": "000660"}]},
    )

    after = await _stored(db_session, "20261028000301")
    assert after.symbol == "000660"
    assert after.first_received_at == FIRST_RECEIVED
    assert after.raw_payload_json == raw_before
    # Regression (mergewake high): onupdate=func.now() must not move updated_at.
    assert after.updated_at == updated_before
    assert report.filled == 1
    assert report.reasons == {"resolved": 1}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_second_run_changes_nothing(db_session):
    row = await _seed_pre_4591_row(db_session, "20261028000302", "재실행디비")
    registry = {"재실행디비": [{"corp_code": "00000302", "stock_code": "302302"}]}

    await _run(db_session, listing={DAY: [row]}, registry=registry)
    second = await _run(db_session, listing={DAY: [row]}, registry=registry)

    assert second.candidate_rows == 0
    assert second.filled == 0
    after = await _stored(db_session, "20261028000302")
    assert after.symbol == "302302"
    assert after.first_received_at == FIRST_RECEIVED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_ambiguous_row_stays_null_with_reason(db_session):
    row = await _seed_pre_4591_row(db_session, "20261028000303", "디비동명")
    registry = {
        "디비동명": [
            {"corp_code": "00000303", "stock_code": "303303"},
            {"corp_code": "00000304", "stock_code": "304304"},
        ]
    }

    report = await _run(db_session, listing={DAY: [row]}, registry=registry)

    after = await _stored(db_session, "20261028000303")
    assert after.symbol is None
    assert after.first_received_at == FIRST_RECEIVED
    assert report.reasons == {"ambiguous_corp_name": 1}
    assert report.filled == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_preview_writes_nothing(db_session):
    row = await _seed_pre_4591_row(db_session, "20261028000305", "디비미리보기")
    registry = {"디비미리보기": [{"corp_code": "00000305", "stock_code": "305305"}]}

    report = await _run(
        db_session, listing={DAY: [row]}, registry=registry, commit=False
    )

    after = await _stored(db_session, "20261028000305")
    assert after.symbol is None
    assert report.would_fill == 1
    assert report.filled == 0
