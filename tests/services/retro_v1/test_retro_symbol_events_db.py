"""hk 6410: the three design 6400 §6 samples, store → query → retro, on PostgreSQL.

CI only (run-owned test database; not run locally by host rule). Each sample is
written by its real producer path with a synthetic provider, queried back from
``market_events``, and then read through ``retro_symbol_events`` — which runs the
real SELECTs inside its own ``REPEATABLE READ READ ONLY`` transaction.

1. KR DART disclosure (§6.1, the 005930 shape).
2. US SEC filing with a later revision (§6.2, the NVDA shape).
3. Upbit caution designation and release (§6.3).

Symbols and provider ids are synthetic. ``market_event_revisions`` is
append-only, so its rows stay; every other row written here is deleted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.core.db import engine
from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventSource,
)
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.services.market_events.crypto_notices import collect_crypto_notices
from app.services.market_events.ingestion import ingest_kr_disclosures_for_date
from app.services.market_events.repository import MarketEventsRepository
from app.services.market_events.sec_edgar import collect_sec_filings
from app.services.retro_v1 import read_retro_symbol_events
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import market_events_test_lock
from tests.services.retro_v1.support import validate

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

KR, US, COIN = "Z64100", "ZZHKS", "ZZHKC"
#: A Friday that is a session on XKRX and XNYS.
DAY = date(2026, 11, 20)
NEXT = date(2026, 11, 23)
LATER = date(2026, 11, 24)
CIK = 999_006_410


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@pytest_asyncio.fixture
async def db(db_session):
    validate_run_owned_database_url(engine.url)
    async with market_events_test_lock():
        await _clean(db_session)
        try:
            yield db_session
        finally:
            await db_session.rollback()
            await _clean(db_session)


async def _clean(session) -> None:
    mine = select(MarketEvent.id).where(MarketEvent.symbol.in_([KR, US, COIN]))
    await session.execute(
        delete(MarketEventSource).where(MarketEventSource.event_id.in_(mine))
    )
    await session.execute(
        delete(MarketEvent).where(MarketEvent.symbol.in_([KR, US, COIN]))
    )
    await session.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.source.in_(["dart", "sec"]),
            MarketEventIngestionPartition.partition_date.in_([DAY, LATER]),
        )
    )
    await session.execute(
        delete(UpbitSymbolUniverse).where(UpbitSymbolUniverse.base_currency == COIN)
    )
    for table in ("public.kr_candles_1d", "public.us_candles_1d"):
        await session.execute(
            text(f"DELETE FROM {table} WHERE symbol IN (:kr, :us)"),
            {"kr": KR, "us": US},
        )
    await session.commit()


async def _stored(session, source_event_id: str) -> MarketEvent:
    """The query step of §6: the row the writer stored."""
    row = (
        await session.execute(
            select(MarketEvent)
            .where(MarketEvent.source_event_id == source_event_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _retro(session, **kw: Any) -> dict[str, Any]:
    # The reader opens its own transaction, so the session must be idle.
    await session.commit()
    response = await read_retro_symbol_events(session, **kw)
    validate(response, "RetroSymbolEventsResponse")
    return response


def _events(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in response["data"] if r["row_type"] == "event"]


def _bars(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in response["data"] if r["row_type"] == "bar"]


async def _daily_bar(
    session, table: str, symbol: str, day: date, stored_at: datetime
) -> None:
    venue = "venue" if table.endswith("kr_candles_1d") else "exchange"
    await session.execute(
        text(
            f"INSERT INTO {table} (time, symbol, {venue}, open, high, low, close, "
            "volume, value, source, ingested_at) VALUES (:t, :symbol, :venue, "
            "100, 110.5, 99, 101.25, 1000, 101250, 'kis', :stored)"
        ),
        {
            "t": datetime(day.year, day.month, day.day, tzinfo=UTC),
            "symbol": symbol,
            "venue": "KRX" if venue == "venue" else "NASD",
            "stored": stored_at,
        },
    )


async def test_sample_1_dart_disclosure_store_query_retro(db) -> None:
    receipt = f"20261120{uuid.uuid4().int % 1_000_000:06d}"

    async def fetch(_day: date) -> list[dict[str, Any]]:
        return [
            {
                "corp_code": "00000001",
                "corp_name": "Synthetic Listed Co",
                "stock_code": KR,
                "report_nm": "단일판매ㆍ공급계약체결",
                "rcept_no": receipt,
                "rcept_dt": "20261120",
            }
        ]

    result = await ingest_kr_disclosures_for_date(db, DAY, fetch_rows=fetch)
    assert result.status == "succeeded"
    await _daily_bar(db, "public.kr_candles_1d", KR, DAY, _at(21, 0))
    await db.commit()

    stored = await _stored(db, receipt)
    received = stored.first_received_at
    assert received is not None
    query = {"symbol": KR, "from_": DAY, "to": DAY}

    response = await _retro(db, known_cutoff=received, **query)
    (row,) = _events(response)
    assert row["event_ref"] == stored.event_ref
    assert row["version_state"] == "unchanged_since_cutoff"
    assert (row["published_date"], row["published_precision"]) == ("2026-11-20", "date")
    assert row["sources"][0]["source_event_id"] == receipt
    (candle,) = _bars(response)
    assert candle["close_price"] == "101.25"
    assert candle["stored_at"] == "2026-11-21T00:00:00Z"
    assert not [g for g in response["collection"]["gaps"] if g["source"] == "dart"]
    assert response["collection"]["latest_collected"]["dart"] >= "2026-11-20"

    # before the receipt: only the count
    earlier = await _retro(db, known_cutoff=received - timedelta(seconds=1), **query)
    assert _events(earlier) == []
    assert earlier["excluded_after_cutoff"]["events"] == 1

    # the next collection rewrites the row after the first cutoff
    await ingest_kr_disclosures_for_date(db, DAY, fetch_rows=fetch)
    await db.commit()
    again = await _stored(db, receipt)
    assert again.first_received_at == received
    assert again.updated_at > received
    response = await _retro(db, known_cutoff=received, **query)
    (row,) = _events(response)
    assert row["version_state"] == "rewritten_after_cutoff"
    assert row["status"] is None and row["superseded_later"] is None


async def test_sample_2_sec_filing_revision_as_of(db, monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "sec_edgar_user_agent", "auto-trader-test test@example.invalid"
    )
    accession = f"0000999006-26-{uuid.uuid4().int % 1_000_000:06d}"

    class Client:
        def __init__(self, accepted: str, items: str) -> None:
            self.accepted, self.items = accepted, items

        async def fetch_company_tickers(self):
            return {"0": {"cik_str": CIK, "ticker": US, "title": "Synthetic Inc"}}

        async def fetch_submissions(self, cik: int):
            return {
                "cik": f"{CIK:010d}",
                "name": "Synthetic Inc",
                "tickers": [US],
                "filings": {
                    "recent": {
                        "accessionNumber": [accession],
                        "filingDate": ["2026-11-20"],
                        "acceptanceDateTime": [self.accepted],
                        "form": ["8-K"],
                        "items": [self.items],
                    },
                    "files": [],
                },
            }

    for observed, client in (
        (_at(20, 22), Client("2026-11-20T21:00:00.000Z", "2.02")),
        (_at(24, 22), Client("2026-11-20T21:30:00.000Z", "2.02,9.01")),
    ):
        report = await collect_sec_filings(
            db,
            symbols=[US],
            commit=True,
            client=client,
            repo=MarketEventsRepository(db),
            clock=lambda observed=observed: observed,
        )
        assert report["totals"]["failed"] == 0, report
    await _daily_bar(db, "public.us_candles_1d", US, DAY, _at(20, 22))
    await _daily_bar(db, "public.us_candles_1d", US, NEXT, _at(25, 0))
    await db.commit()

    stored = await _stored(db, accession)
    assert stored.first_received_at == _at(20, 22)
    query = {"symbol": US, "from_": DAY, "to": NEXT}

    then = await _retro(db, known_cutoff=_at(23, 0), **query)
    (row,) = _events(then)
    assert row["event_ref"] == stored.event_ref
    assert (row["version_seq"], row["superseded_later"]) == (1, True)
    assert row["published_at"] == "2026-11-20T21:00:00Z"
    assert row["published_offset"] == "-05:00"
    assert [b["date"] for b in _bars(then)] == ["2026-11-20"]
    assert then["excluded_after_cutoff"] == {"events": 0, "bars": 1}

    after = await _retro(db, known_cutoff=_at(26, 0), **query)
    (row,) = _events(after)
    assert (row["version_seq"], row["superseded_later"]) == (2, False)
    assert row["published_at"] == "2026-11-20T21:30:00Z"
    assert len(_bars(after)) == 2

    before = await _retro(db, known_cutoff=_at(20, 21), **query)
    assert before["data"] == []
    assert before["excluded_after_cutoff"] == {"events": 1, "bars": 2}


async def test_sample_3_upbit_designation_and_release(db) -> None:
    db.add(
        UpbitSymbolUniverse(
            market=f"KRW-{COIN}",
            quote_currency="KRW",
            base_currency=COIN,
            korean_name="합성",
            english_name="Synthetic",
        )
    )
    await db.commit()
    first_id = 900_000_000 + uuid.uuid4().int % 1_000_000
    second_id = first_id + 1_000_000

    def notice(id_: int, title: str, listed: str) -> dict[str, Any]:
        return {
            "id": id_,
            "title": title,
            "category": "거래",
            "first_listed_at": listed,
            "listed_at": listed,
        }

    designated = "2026-11-20T12:00:00+09:00"
    released = "2026-11-24T12:00:00+09:00"
    designation = notice(first_id, f"거래 유의 종목 지정 ({COIN})", designated)
    edited = notice(first_id, f"거래 유의 종목 지정 정정 ({COIN})", designated)
    release = notice(second_id, f"거래 유의 종목 해제 ({COIN})", released)

    class Client:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        async def fetch_upbit_page(self, page: int):
            return self.rows, 1, []

        async def fetch_bithumb(self):
            return [], set()

    for observed, rows in (
        (_at(20, 4), [designation]),
        (_at(24, 4), [release, edited]),
    ):
        report = await collect_crypto_notices(
            db,
            commit=True,
            client=Client(rows),
            repo=MarketEventsRepository(db),
            received_at=observed,
        )
        assert report["upbit_notice"]["error"] is None, report

    first = await _stored(db, str(first_id))
    second = await _stored(db, str(second_id))
    query = {"symbol": f"KRW-{COIN}", "from_": DAY, "to": LATER}

    then = await _retro(db, known_cutoff=_at(22, 0), **query)
    (row,) = _events(then)
    assert row["event_ref"] == first.event_ref
    assert (row["version_seq"], row["superseded_later"]) == (1, True)
    # designation and release are separate events; the notice writer records
    # neither a publication precision nor an effective time
    assert row["published_at"] is None
    assert row["effective_at"] is None
    assert then["excluded_after_cutoff"]["events"] == 1

    after = await _retro(db, known_cutoff=_at(25, 0), **query)
    assert [r["event_ref"] for r in _events(after)] == [
        first.event_ref,
        second.event_ref,
    ]
    assert [r["superseded_later"] for r in _events(after)] == [False, False]
    # a source with no partitions is unknown coverage, not "no notice"
    assert after["collection"]["latest_collected"] == {
        "upbit_notice": None,
        "bithumb_notice": None,
    }
    assert {u["field"] for u in after["unknowns"] if u["reason"] == "no_partition"} == {
        "collection.latest_collected.upbit_notice",
        "collection.latest_collected.bithumb_notice",
    }
