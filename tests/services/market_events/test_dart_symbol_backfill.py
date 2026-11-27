"""DART symbol backfill for stored rows without a symbol (hk 5623).

Pure unit tests. An in-memory store stands in for the database, DART listings
are fixtures, and no network call is made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.market_events.dart_helpers import normalize_dart_corp_name
from app.services.market_events.dart_symbol_backfill import backfill_dart_symbols

pytestmark = pytest.mark.unit

DAY = date(2026, 10, 28)
FIRST_RECEIVED = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


@dataclass
class StoredRow:
    rcept_no: str
    event_date: date
    symbol: str | None
    first_received_at: datetime
    raw_payload_json: dict[str, Any] = field(default_factory=dict)


class FakeStore:
    """Mirrors the repository contract: fills only rows whose symbol is NULL."""

    def __init__(self, rows: list[StoredRow]) -> None:
        self.rows = {row.rcept_no: row for row in rows}
        self.writes: list[tuple[str, str]] = []
        self.commits = 0

    async def list_null_symbol_rows(self, *, from_date, to_date):
        return [
            (row.rcept_no, row.event_date)
            for row in self.rows.values()
            if row.symbol is None and from_date <= row.event_date <= to_date
        ]

    async def fill_symbol_if_null(self, *, rcept_no, symbol):
        row = self.rows.get(rcept_no)
        if row is None or row.symbol is not None:
            return 0
        row.symbol = symbol
        self.writes.append((rcept_no, symbol))
        return 1

    async def commit(self):
        self.commits += 1


def _dart_row(rcept_no: str, corp_name: str, report_nm: str = "잠정실적") -> dict:
    return {
        "rcept_no": rcept_no,
        "rcept_dt": "20261028",
        "corp_name": corp_name,
        "report_nm": report_nm,
    }


def _registry(corp_name: str, *entries: dict[str, str]) -> dict:
    return {normalize_dart_corp_name(corp_name): list(entries)}


def _fetch_from(listing: dict[date, list[dict]]):
    async def fetch(target: date) -> list[dict]:
        return listing.get(target, [])

    return fetch


def _load(registry):
    async def load():
        return registry

    return load


def _stored(rcept_no: str, *, symbol: str | None = None) -> StoredRow:
    return StoredRow(
        rcept_no=rcept_no,
        event_date=DAY,
        symbol=symbol,
        first_received_at=FIRST_RECEIVED,
        raw_payload_json={"rcept_no": rcept_no, "corp_name": "stored"},
    )


async def _run(store, *, listing, registry, commit=True):
    return await backfill_dart_symbols(
        store,
        from_date=DAY,
        to_date=DAY,
        commit=commit,
        fetch_rows=_fetch_from(listing),
        load_corp_registry=_load(registry),
    )


@pytest.mark.asyncio
async def test_registry_match_fills_symbol_and_keeps_first_received_at():
    store = FakeStore([_stored("20261028000001")])
    listing = {DAY: [_dart_row("20261028000001", "테스트하이닉스")]}
    registry = _registry(
        "테스트하이닉스", {"corp_code": "00164779", "stock_code": "000660"}
    )
    stored_before = dict(store.rows["20261028000001"].__dict__)

    report = await _run(store, listing=listing, registry=registry)

    row = store.rows["20261028000001"]
    assert row.symbol == "000660"
    assert row.first_received_at == FIRST_RECEIVED
    assert row.raw_payload_json == stored_before["raw_payload_json"]
    assert store.writes == [("20261028000001", "000660")]
    assert report.reasons == {"resolved": 1}
    assert report.filled == 1
    assert report.ok


@pytest.mark.asyncio
async def test_ambiguous_name_stays_null_with_reason():
    store = FakeStore([_stored("20261028000002")])
    listing = {DAY: [_dart_row("20261028000002", "동명회사")]}
    registry = _registry(
        "동명회사",
        {"corp_code": "00000001", "stock_code": "111111"},
        {"corp_code": "00000002", "stock_code": "222222"},
    )

    report = await _run(store, listing=listing, registry=registry)

    assert store.rows["20261028000002"].symbol is None
    assert store.writes == []
    assert report.reasons == {"ambiguous_corp_name": 1}
    assert report.filled == 0


@pytest.mark.asyncio
async def test_unlisted_name_stays_null_with_reason():
    store = FakeStore([_stored("20261028000003")])
    listing = {DAY: [_dart_row("20261028000003", "비상장회사")]}
    registry = _registry("비상장회사", {"corp_code": "00000003", "stock_code": ""})

    report = await _run(store, listing=listing, registry=registry)

    assert store.rows["20261028000003"].symbol is None
    assert report.reasons == {"unlisted": 1}


@pytest.mark.asyncio
async def test_missing_registry_entry_reports_no_match():
    store = FakeStore([_stored("20261028000004")])
    listing = {DAY: [_dart_row("20261028000004", "없는회사")]}

    report = await _run(store, listing=listing, registry={})

    assert store.rows["20261028000004"].symbol is None
    assert report.reasons == {"no_registry_match": 1}


@pytest.mark.asyncio
async def test_unavailable_registry_reports_registry_unavailable():
    store = FakeStore([_stored("20261028000005")])
    listing = {DAY: [_dart_row("20261028000005", "레지스트리없음")]}

    report = await _run(store, listing=listing, registry=None)

    assert store.rows["20261028000005"].symbol is None
    assert report.reasons == {"registry_unavailable": 1}


@pytest.mark.asyncio
async def test_registry_load_failure_is_treated_as_unavailable():
    store = FakeStore([_stored("20261028000006")])
    listing = {DAY: [_dart_row("20261028000006", "로드실패")]}

    async def failing_load():
        raise RuntimeError("registry down")

    report = await backfill_dart_symbols(
        store,
        from_date=DAY,
        to_date=DAY,
        commit=True,
        fetch_rows=_fetch_from(listing),
        load_corp_registry=failing_load,
    )

    assert store.rows["20261028000006"].symbol is None
    assert report.reasons == {"registry_unavailable": 1}


@pytest.mark.asyncio
async def test_second_run_changes_nothing():
    store = FakeStore([_stored("20261028000007")])
    listing = {DAY: [_dart_row("20261028000007", "재실행회사")]}
    registry = _registry(
        "재실행회사", {"corp_code": "00000007", "stock_code": "123456"}
    )

    first = await _run(store, listing=listing, registry=registry)
    second = await _run(store, listing=listing, registry=registry)

    assert first.filled == 1
    assert second.candidate_rows == 0
    assert second.filled == 0
    assert second.reasons == {}
    assert store.writes == [("20261028000007", "123456")]
    assert store.rows["20261028000007"].symbol == "123456"


@pytest.mark.asyncio
async def test_preview_writes_nothing_and_counts_would_fill():
    store = FakeStore([_stored("20261028000008")])
    listing = {DAY: [_dart_row("20261028000008", "미리보기회사")]}
    registry = _registry(
        "미리보기회사", {"corp_code": "00000008", "stock_code": "654321"}
    )

    report = await _run(store, listing=listing, registry=registry, commit=False)

    assert store.writes == []
    assert store.commits == 0
    assert store.rows["20261028000008"].symbol is None
    assert report.mode == "preview"
    assert report.would_fill == 1
    assert report.filled == 0


@pytest.mark.asyncio
async def test_row_absent_from_listing_is_counted_not_written():
    store = FakeStore([_stored("20261028000009")])

    report = await _run(store, listing={DAY: []}, registry={})

    assert store.rows["20261028000009"].symbol is None
    assert report.not_in_listing == 1
    assert report.reasons == {}


@pytest.mark.asyncio
async def test_fetch_failure_is_reported_and_other_days_still_run():
    other_day = date(2026, 10, 29)
    store = FakeStore(
        [
            _stored("20261028000010"),
            StoredRow(
                rcept_no="20261029000011",
                event_date=other_day,
                symbol=None,
                first_received_at=FIRST_RECEIVED,
            ),
        ]
    )
    listing = {
        other_day: [_dart_row("20261029000011", "다음날회사")],
    }
    registry = _registry(
        "다음날회사", {"corp_code": "00000011", "stock_code": "777777"}
    )

    async def fetch(target: date):
        if target == DAY:
            raise ConnectionError("dart unreachable")
        return listing.get(target, [])

    report = await backfill_dart_symbols(
        store,
        from_date=DAY,
        to_date=other_day,
        commit=True,
        fetch_rows=fetch,
        load_corp_registry=_load(registry),
    )

    assert report.ok is False
    assert report.date_errors == [
        {"date": DAY.isoformat(), "error_class": "ConnectionError"}
    ]
    assert store.rows["20261029000011"].symbol == "777777"
    assert store.rows["20261028000010"].symbol is None


@pytest.mark.asyncio
async def test_non_null_symbol_is_never_overwritten():
    store = FakeStore([_stored("20261028000012", symbol="999999")])
    listing = {DAY: [_dart_row("20261028000012", "이미있음")]}
    registry = _registry("이미있음", {"corp_code": "00000012", "stock_code": "000001"})

    report = await _run(store, listing=listing, registry=registry)

    assert store.rows["20261028000012"].symbol == "999999"
    assert report.candidate_rows == 0
    assert store.writes == []
