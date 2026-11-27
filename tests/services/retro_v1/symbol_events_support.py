"""Synthetic rows and a fake session for the ``retro_symbol_events`` tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from tests.services.retro_v1.support import FakeSession

#: 2026-11-20 is a Friday (XKRX and XNYS session), 21/22 the weekend.
DAY = date(2026, 11, 20)
CUTOFF = datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
BEFORE = CUTOFF - timedelta(days=2)
AFTER = CUTOFF + timedelta(days=2)


def event(id_: int, **kw: Any) -> SimpleNamespace:
    """One current ``market_events`` row as the reader selects it."""
    values: dict[str, Any] = {
        "id": id_,
        "event_uuid": UUID(int=id_),
        "source": "dart",
        "category": "disclosure",
        "market": "kr",
        "source_event_id": f"2026112000{id_:04d}",
        "source_url": (
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2026112000{id_:04d}"
        ),
        "symbol": "005930",
        "symbols": None,
        "event_date": DAY,
        "status": "released",
        "first_received_at": BEFORE,
        "updated_at": BEFORE,
        "published_at": None,
        "published_date": DAY,
        "published_offset": "+09:00",
        "published_precision": "date",
        "revised_at": None,
        "effective_at": None,
        "relevance": None,
        "relevance_state": None,
        "relevance_basis": None,
        "receipt_text": "20261120",
        "symbol_resolution_reason": None,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def revision(
    current: SimpleNamespace,
    seq: int,
    observed_at: datetime,
    raw: dict[str, Any] | None = None,
    provider_created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        source=current.source,
        category=current.category,
        market=current.market,
        source_event_id=current.source_event_id,
        revision_seq=seq,
        observed_at=observed_at,
        provider_created_at=provider_created_at,
        raw_payload_json=raw,
    )


def benzinga_row(**kw: Any) -> dict[str, Any]:
    """A provider earnings row (the shape ``normalize_row`` reads)."""
    row: dict[str, Any] = {
        "id": "bz-1",
        "date": "2026-11-20",
        "ticker": "NVDA",
        "date_confirmed": 1,
        "period": "Q3",
        "period_year": 2026,
        "currency": "USD",
        "eps_est": "1.20",
        "time": "amc",
    }
    row.update(kw)
    return row


def benzinga_event(id_: int, **kw: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "source": "benzinga",
        "category": "earnings",
        "market": "us",
        "source_event_id": "benzinga:earnings:bz-1",
        "source_url": None,
        "symbol": "NVDA",
        "published_date": None,
        "published_offset": None,
        "published_precision": None,
        "receipt_text": None,
    }
    values.update(kw)
    return event(id_, **values)


def notice_event(id_: int, **kw: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "source": "upbit_notice",
        "category": "crypto_exchange_notice",
        "market": "crypto",
        "source_event_id": "5001",
        "source_url": "https://upbit.com/service_center/notice?id=5001",
        "symbol": "XYZ",
        "published_date": None,
        "published_offset": None,
        "published_precision": None,
        "receipt_text": None,
    }
    values.update(kw)
    return event(id_, **values)


def bar(
    day: date = DAY,
    *,
    stored_at: datetime | None = BEFORE,
    venue: str = "KRX",
    symbol: str = "005930",
    close: str = "70500.5000",
) -> dict[str, Any]:
    return {
        "time": datetime(day.year, day.month, day.day, tzinfo=UTC),
        "symbol": symbol,
        "venue": venue,
        "open": Decimal("70000"),
        "high": Decimal("71000"),
        "low": Decimal("69000"),
        "close": Decimal(close),
        "volume": Decimal("9007199254740993"),
        "source": "kis",
        "ingested_at": stored_at,
    }


def partition(
    day: date,
    status: str,
    *,
    source: str = "dart",
    category: str = "disclosure",
    last_error: str | None = None,
    event_count: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        source=source,
        category=category,
        partition_date=day,
        status=status,
        event_count=event_count,
        last_error=last_error,
    )


def latest(day: date, *, source: str = "dart") -> SimpleNamespace:
    return SimpleNamespace(source=source, latest=day)


def session(
    *,
    events: Any = (),
    revisions: Any = (),
    sources: Any = (),
    bars: Any = (),
    partitions: Any = (),
    newest: Any = (),
    bar_table: str = "public.kr_candles_1d",
) -> FakeSession:
    """Routes the reader's SELECTs. An exception instance makes that read fail."""

    def partition_rows(sql: str) -> list[Any]:
        return list(newest if "max(" in sql.lower() else partitions)

    routes: dict[str, Any] = {
        # the events statement also mentions market_event_revisions (EXISTS),
        # so it has to be matched first
        "FROM market_events": events,
        "FROM market_event_revisions": revisions,
        "FROM market_event_sources": sources,
        "FROM market_event_ingestion_partitions": (
            partitions if isinstance(partitions, BaseException) else partition_rows
        ),
        f"FROM {bar_table}": bars,
    }
    return FakeSession(routes)
