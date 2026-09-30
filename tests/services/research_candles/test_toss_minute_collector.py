"""#1086 collector: rows, session labels, maturity, gaps, idempotent writes."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.services.brokers.toss.dto import TossMinuteCandle
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossErrorEnvelope,
    TossPaginationCapExceeded,
    TossResponseContractError,
)
from app.services.research_candles import toss_minute_collector as mod
from app.services.research_candles.toss_minute_collector import (
    CollectionRequest,
    JsonlGapSink,
    MemoryGapSink,
    SqlCandleWriter,
    TossMinuteCollector,
    WriteResult,
    bar_start,
    candle_to_row,
    classify_session_segment,
)

KST = timezone(timedelta(hours=9))
REPO = Path(__file__).resolve().parents[3]
D0 = date(2026, 9, 24)
D1 = date(2026, 9, 25)
AFTER_D1 = datetime(2026, 9, 25, 20, 10, tzinfo=KST)


def _candle(day: date, hh: int, mm: int, *, price="1000", volume="10", currency="KRW"):
    return TossMinuteCandle(
        timestamp=datetime(day.year, day.month, day.day, hh, mm, tzinfo=KST),
        open_price=Decimal(price),
        high_price=Decimal(price),
        low_price=Decimal(price),
        close_price=Decimal(price),
        volume=Decimal(volume),
        currency=currency,
    )


class _FakeClient:
    def __init__(self, result=None, exc=None):
        self.result = result or []
        self.exc = exc
        self.calls: list[dict] = []

    async def collect_minute_candles(self, symbol, **kwargs):
        self.calls.append({"symbol": symbol, **kwargs})
        if self.exc is not None:
            raise self.exc
        return list(self.result)


class _FakeWriter:
    def __init__(self):
        self.batches = []

    async def write(self, rows):
        self.batches.append(list(rows))
        return WriteResult(len(rows), len(rows), 0, 0)


def _collector(client, writer=None, sink=None, now=AFTER_D1):
    return TossMinuteCollector(
        client=client,
        writer=writer,
        gap_sink=sink if sink is not None else MemoryGapSink(),
        run_id="test-run",
        now=lambda: now,
    )


def _full_day(day: date) -> list[TossMinuteCandle]:
    return [
        _candle(day, 8, 1),
        _candle(day, 9, 1),
        _candle(day, 15, 30),
        _candle(day, 18, 11),
    ]


REQUEST = CollectionRequest(symbol="005930", d0=D0, d1=D1, rcept_nos=("r1",))


# --- labels -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hh", "mm", "segment"),
    [
        (8, 0, "NXT_PRE"),
        (8, 59, "NXT_PRE"),
        (9, 0, "KRX_REGULAR"),
        (15, 30, "KRX_REGULAR"),
        (15, 31, "NXT_POST"),
        (18, 11, "NXT_POST"),
        (20, 0, "NXT_POST"),
    ],
)
def test_session_segment_labels(hh, mm, segment):
    assert (
        classify_session_segment(datetime(2026, 9, 24, hh, mm, tzinfo=KST)) == segment
    )


@pytest.mark.parametrize(("hh", "mm"), [(7, 59), (20, 1), (0, 0)])
def test_unclassifiable_segment(hh, mm):
    with pytest.raises(mod.UnclassifiableSessionSegment):
        classify_session_segment(datetime(2026, 9, 24, hh, mm, tzinfo=KST))


def test_segment_rule_matches_phase2_corpus():
    """The shared table carries one labelling convention."""
    phase2 = importlib.import_module("research.toss_phase2.collect")
    start = datetime(2026, 9, 24, 0, 0, tzinfo=KST)
    for minute in range(0, 24 * 60):
        ts = start + timedelta(minutes=minute)
        try:
            expected = phase2.classify_session_segment(ts)
        except phase2.UnclassifiableSessionSegment:
            with pytest.raises(mod.UnclassifiableSessionSegment):
                classify_session_segment(ts)
            continue
        assert classify_session_segment(ts) == expected


def test_row_keeps_raw_timestamp_and_exposes_bar_start():
    candle = _candle(D0, 9, 6, price="72000", volume="3")
    row = candle_to_row(candle, symbol="005930", retrieved_at=AFTER_D1, batch_id="b")
    assert row.time_utc == datetime(2026, 9, 24, 0, 6, tzinfo=UTC)
    assert row.bar_start == datetime(2026, 9, 24, 0, 5, tzinfo=UTC)
    assert bar_start(row.time_utc) == row.bar_start
    assert row.session_date_kst == D0
    assert row.value == Decimal("216000")
    assert row.is_padding is False
    padding = candle_to_row(
        _candle(D0, 9, 7, volume="0"),
        symbol="005930",
        retrieved_at=AFTER_D1,
        batch_id="b",
    )
    assert padding.is_padding is True
    assert mod.BAR_START_SQL == "time_utc - interval '1 minute'"


def test_non_krw_bar_is_a_contract_error():
    with pytest.raises(TossResponseContractError):
        candle_to_row(
            _candle(D0, 9, 1, currency="USD"),
            symbol="005930",
            retrieved_at=AFTER_D1,
            batch_id="b",
        )


# --- collect ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_fetches_d0_d1_window_unadjusted_and_writes_after_hours():
    client = _FakeClient(result=_full_day(D0) + _full_day(D1))
    writer = _FakeWriter()
    sink = MemoryGapSink()
    result = await _collector(client, writer, sink).collect(REQUEST)

    call = client.calls[0]
    assert call["adjusted"] is False
    assert call["before"] == "2026-09-25T20:00:00+09:00"
    assert call["not_before"] == datetime(2026, 9, 24, 0, 0, tzinfo=KST)
    assert result.status == "written"
    assert sink.gaps == []
    rows = writer.batches[0]
    assert len(rows) == 8
    assert {r.session_segment for r in rows} == {"NXT_PRE", "KRX_REGULAR", "NXT_POST"}
    assert result.bars_by_session["2026-09-24"] == {
        "NXT_PRE": 1,
        "KRX_REGULAR": 2,
        "NXT_POST": 1,
    }
    assert all(r.batch_id == "t1086-candles-1m:test-run" for r in rows)


@pytest.mark.asyncio
async def test_bars_outside_d0_d1_are_not_written():
    client = _FakeClient(
        result=_full_day(D0) + _full_day(D1) + [_candle(date(2026, 9, 26), 9, 1)]
    )
    writer = _FakeWriter()
    await _collector(client, writer).collect(REQUEST)
    assert {r.session_date_kst for r in writer.batches[0]} == {D0, D1}


@pytest.mark.asyncio
async def test_immature_request_makes_no_call_and_no_gap():
    client = _FakeClient(result=_full_day(D0))
    sink = MemoryGapSink()
    result = await _collector(
        client, _FakeWriter(), sink, now=datetime(2026, 9, 25, 20, 5, tzinfo=KST)
    ).collect(REQUEST)
    assert result.status == "immature"
    assert client.calls == [] and sink.gaps == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            TossApiResponseError(
                TossErrorEnvelope(
                    request_id="x", code="internal-error", message="m", data=None
                ),
                status_code=500,
            ),
            "toss_api_error",
        ),
        (httpx.ConnectTimeout("t"), "toss_transport_error"),
        (TossResponseContractError("bad"), "toss_contract_error"),
        (TossPaginationCapExceeded("cap"), "pagination_cap"),
    ],
)
async def test_toss_failure_records_gap_for_both_sessions(exc, reason):
    sink = MemoryGapSink()
    writer = _FakeWriter()
    result = await _collector(_FakeClient(exc=exc), writer, sink).collect(REQUEST)
    assert result.status == "gap"
    assert [(g.session_date_kst, g.reason) for g in sink.gaps] == [
        (D0, reason),
        (D1, reason),
    ]
    assert all(g.rcept_nos == ("r1",) for g in sink.gaps)
    assert writer.batches == []


@pytest.mark.asyncio
async def test_programming_errors_are_not_laundered_into_gaps():
    with pytest.raises(ValueError):
        await _collector(_FakeClient(exc=ValueError("bug"))).collect(REQUEST)


@pytest.mark.asyncio
async def test_missing_session_day_is_a_gap_and_other_day_still_written():
    sink = MemoryGapSink()
    writer = _FakeWriter()
    result = await _collector(_FakeClient(result=_full_day(D0)), writer, sink).collect(
        REQUEST
    )
    assert result.status == "partial_gap"
    assert [(g.session_date_kst, g.reason) for g in sink.gaps] == [(D1, "no_bars")]
    assert {r.session_date_kst for r in writer.batches[0]} == {D0}


@pytest.mark.asyncio
async def test_no_bars_at_all_is_a_gap_without_write():
    sink = MemoryGapSink()
    writer = _FakeWriter()
    result = await _collector(_FakeClient(result=[]), writer, sink).collect(REQUEST)
    assert result.status == "gap"
    assert [g.reason for g in sink.gaps] == ["no_bars", "no_bars"]
    assert writer.batches == []


@pytest.mark.asyncio
async def test_after_hours_only_day_is_stored_but_flagged():
    sink = MemoryGapSink()
    writer = _FakeWriter()
    client = _FakeClient(result=_full_day(D0) + [_candle(D1, 18, 11)])
    result = await _collector(client, writer, sink).collect(REQUEST)
    assert result.status == "partial_gap"
    assert [(g.session_date_kst, g.reason) for g in sink.gaps] == [
        (D1, "no_regular_bars")
    ]
    assert any(r.session_date_kst == D1 for r in writer.batches[0])


@pytest.mark.asyncio
async def test_unclassifiable_bar_drops_its_whole_day():
    sink = MemoryGapSink()
    writer = _FakeWriter()
    client = _FakeClient(result=_full_day(D0) + [_candle(D1, 9, 1), _candle(D1, 20, 1)])
    result = await _collector(client, writer, sink).collect(REQUEST)
    assert result.status == "partial_gap"
    assert [(g.session_date_kst, g.reason) for g in sink.gaps] == [
        (D1, "unclassifiable_session_segment")
    ]
    assert {r.session_date_kst for r in writer.batches[0]} == {D0}


@pytest.mark.asyncio
async def test_dry_run_collector_never_writes():
    result = await _collector(
        _FakeClient(result=_full_day(D0) + _full_day(D1)), None
    ).collect(REQUEST)
    assert result.status == "dry_run" and result.write is None


def test_jsonl_gap_sink_appends(tmp_path):
    sink = JsonlGapSink(tmp_path / "g" / "gaps.jsonl")
    gap = mod.GapRecord(
        symbol="005930",
        session_date_kst=D1,
        reason="no_bars",
        detail="0 bars",
        d0=D0,
        rcept_nos=("r1",),
        run_id="x",
        recorded_at=AFTER_D1,
    )
    sink.record(gap)
    sink.record(gap)
    lines = (tmp_path / "g" / "gaps.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert '"target_table": "research.kr_candles_1m_toss"' in lines[0]


def test_module_registers_nothing():
    for name in ("toss_minute_collector.py", "dart_minute_trigger.py"):
        source = (REPO / "app/services/research_candles" / name).read_text().lower()
        for forbidden in ("taskiq", "prefect", "@broker", "crontab", "schedule="):
            assert forbidden not in source, (name, forbidden)


# --- SqlCandleWriter against a throwaway test DB -------------------------------


def _migration_create_table_sql() -> str:
    """The real CREATE TABLE from the phase2 migration, captured via a fake op."""
    path = REPO / "alembic" / "versions" / "20260804_toss_phase2_corpus.py"
    spec = importlib.util.spec_from_file_location("toss_phase2_migration_1086", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    captured: list[str] = []
    migration.op = SimpleNamespace(
        execute=lambda sql: captured.append(str(sql)),
        create_index=lambda *a, **k: None,
    )
    migration.upgrade()
    (ddl,) = [s for s in captured if "CREATE TABLE research.kr_candles_1m_toss" in s]
    return ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)


def _rows(price="1000"):
    return [
        candle_to_row(c, symbol="005930", retrieved_at=AFTER_D1, batch_id="b1")
        for c in [
            _candle(D0, 9, 1, price=price),
            _candle(D0, 18, 11, price=price, volume="0"),
        ]
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sql_writer_is_idempotent_and_never_overwrites(db_session):
    await db_session.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
    await db_session.execute(text(_migration_create_table_sql()))
    try:
        await mod.assert_insert_privilege(db_session)
        writer = SqlCandleWriter(db_session)

        first = await writer.write(_rows())
        assert (first.inserted, first.existing_same, first.existing_conflict) == (
            2,
            0,
            0,
        )

        again = await writer.write(_rows())
        assert (again.inserted, again.existing_same, again.existing_conflict) == (
            0,
            2,
            0,
        )

        changed = await writer.write(_rows(price="999"))
        assert (changed.inserted, changed.existing_conflict) == (0, 2)

        stored = (
            (
                await db_session.execute(
                    text(
                        "SELECT time_utc, session_segment, source, value_semantics, "
                        "is_padding, pre_nxt, close, "
                        "time_utc - interval '1 minute' AS bar_start "
                        "FROM research.kr_candles_1m_toss WHERE symbol = '005930' "
                        "ORDER BY time_utc"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [r["close"] for r in stored] == [Decimal("1000"), Decimal("1000")]
        assert [r["session_segment"] for r in stored] == ["KRX_REGULAR", "NXT_POST"]
        assert {r["source"] for r in stored} == {"TOSS"}
        assert {r["value_semantics"] for r in stored} == {"CLOSE_X_VOLUME_SYNTHETIC"}
        assert [r["is_padding"] for r in stored] == [False, True]
        assert {r["pre_nxt"] for r in stored} == {None}
        assert stored[0]["bar_start"] == datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
        assert await mod.regular_bar_counts(db_session, "005930", [D0, D1]) == {D0: 1}
    finally:
        await db_session.rollback()
