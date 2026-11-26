"""kr/us 1m ``get_bars`` and ``data_gaps`` read the hk 6145 research tables (hk 6244)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.services.research_candles.session_coverage import is_backfill_batch
from app.services.retro_v1 import read_data_gaps, read_get_bars
from app.services.retro_v1.daily_bars import is_first_receipt_batch
from tests.services.retro_v1.support import FakeSession, bar_row, validate
from tests.services.retro_v1.test_data_gaps_sources import by_dataset
from tests.services.retro_v1.test_other_readers import gap_session

pytestmark = pytest.mark.unit

DAY = date(2026, 11, 25)
# 2026-11-25 09:00 KST = 2026-11-25 00:00Z (KRX regular open).
OPEN_AT = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)
EARLIEST = datetime(2026, 11, 24, 2, 51, tzinfo=UTC)
RECEIVED = OPEN_AT + timedelta(minutes=1, seconds=3)


def minute(symbol: str, at: datetime, close: str, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source": "KIS",
        "session_segment": "KRX_REGULAR",
        "value": None,
        "retrieved_at": RECEIVED,
        "batch_id": "live-20261125-0901",
    }
    values.update(kw)
    return bar_row(symbol, at, close, **values)


def research_session(
    table: str, rows: list[Any], earliest: Any = EARLIEST
) -> FakeSession:
    def route(sql: str) -> list[Any]:
        if "MIN(time_utc)" in sql:
            return [{"earliest": earliest}]
        return rows

    return FakeSession({f"FROM research.{table}": route})


@pytest.mark.asyncio
async def test_kr_krx_and_ntx_bars_at_one_minute_are_two_rows_with_their_own_fields() -> (
    None
):
    rows = [
        minute(
            "005930",
            OPEN_AT,
            "71000",
            venue="KRX",
            session_segment="KRX_REGULAR",
            source="KIS",
            value="71000000",
        ),
        minute(
            "005930",
            OPEN_AT,
            "71010",
            venue="NTX",
            session_segment="NXT_OVERLAP",
            source="TOSS",
            value=None,  # not proven per bar: stored NULL
            batch_id="session-minutes-backfill-1",
        ),
    ]
    session = research_session("kr_candles_1m", rows)
    response = await read_get_bars(session, symbol="005930", tf="1m", from_=DAY, to=DAY)
    validate(response, "GetBarsResponse")
    krx, ntx = response["data"]
    assert (krx["venue"], krx["session_segment"], krx["source"]) == (
        "KRX",
        "KRX_REGULAR",
        "KIS",
    )
    assert (ntx["venue"], ntx["session_segment"], ntx["source"]) == (
        "NTX",
        "NXT_OVERLAP",
        "TOSS",
    )
    assert krx["close_price"] == "71000" and ntx["close_price"] == "71010"
    # value: exact string when stored; absent (never 0) + closed reason when NULL
    assert krx["value"] == "71000000" and "value_reason" not in krx
    assert "value" not in ntx and ntx["value_reason"] == "unavailable"
    # first receipt: only for a non-backfill batch
    assert krx["first_received_at"] == "2026-11-25T00:01:03Z"
    assert "first_received_reason" not in krx
    assert "first_received_at" not in ntx
    assert ntx["first_received_reason"] == "unavailable"
    assert response["total"] == 2
    assert not [u for u in response["unknowns"] if u["field"] == "data"]
    issued = [s for s in session.statements if "candles_1m" in s]
    assert all("research.kr_candles_1m" in s for s in issued)
    assert not any("public.kr_candles_1m" in s for s in session.statements)
    assert "ORDER BY b.symbol, b.time_utc, b.venue" in issued[0]


@pytest.mark.asyncio
async def test_us_minute_reads_the_research_table_with_stored_source() -> None:
    at = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    session = research_session(
        "us_candles_1m",
        [
            minute(
                "BRK.B",
                at,
                "480.12",
                venue="NYSE",
                source="YAHOO",
                session_segment="US_REGULAR",
                value="48012.5",
            )
        ],
        earliest=datetime(2026, 11, 17, 9, tzinfo=UTC),
    )
    response = await read_get_bars(
        session, symbol="BRK.B", market="us", tf="1m", from_=DAY, to=DAY
    )
    validate(response, "GetBarsResponse")
    row = response["data"][0]
    assert (row["source"], row["venue"], row["session_segment"], row["value"]) == (
        "YAHOO",
        "NYSE",
        "US_REGULAR",
        "48012.5",
    )
    assert not [u for u in response["unknowns"] if u["field"] == "data"]
    assert not any("public.us_candles_1m" in s for s in session.statements)


@pytest.mark.asyncio
async def test_window_before_research_coverage_is_an_unknown_not_an_empty_success() -> (
    None
):
    # The window starts 2026-11-20 KST; the research table's first bar is 11-24.
    session = research_session("kr_candles_1m", [])
    response = await read_get_bars(
        session,
        symbol="005930",
        tf="1m",
        from_=date(2026, 11, 20),
        to=date(2026, 11, 21),
    )
    validate(response, "GetBarsResponse")
    assert response["data"] == []
    assert {"field": "data", "reason": "missing_data"} in response["unknowns"]
    assert response["total"] == 0

    # Rows exist but the window began before the first stored bar: still flagged.
    partial = research_session(
        "kr_candles_1m", [minute("005930", EARLIEST, "70000", venue="KRX")]
    )
    response = await read_get_bars(
        partial,
        symbol="005930",
        tf="1m",
        from_=date(2026, 11, 24),
        to=date(2026, 11, 24),
    )
    validate(response, "GetBarsResponse")
    assert len(response["data"]) == 1
    assert {"field": "data", "reason": "missing_data"} in response["unknowns"]


@pytest.mark.asyncio
async def test_symbol_absent_from_the_research_table_is_missing_data() -> None:
    session = research_session("us_candles_1m", [], earliest=None)
    response = await read_get_bars(
        session, symbol="AAPL", market="us", tf="1m", from_=DAY, to=DAY
    )
    validate(response, "GetBarsResponse")
    assert response["data"] == [] and response["total"] == 0
    assert {"field": "data", "reason": "missing_data"} in response["unknowns"]


@pytest.mark.asyncio
async def test_crypto_minute_and_kr_daily_rows_have_no_research_fields() -> None:
    crypto = FakeSession(
        {
            "FROM public.crypto_candles_1m": [
                bar_row(
                    "KRW-BTC", OPEN_AT, "95000000", venue="upbit", source="upbit_ws"
                )
            ]
        }
    )
    response = await read_get_bars(
        crypto, symbol="KRW-BTC", market="crypto", tf="1m", from_=DAY, to=DAY
    )
    validate(response, "GetBarsResponse")
    row = response["data"][0]
    assert not {
        "session_segment",
        "value",
        "value_reason",
        "first_received_at",
        "first_received_reason",
    } & set(row)
    assert not [u for u in response["unknowns"] if u["field"] == "data"]
    daily = FakeSession(
        {"FROM public.kr_candles_1d": [bar_row("005930", OPEN_AT, "1")]}
    )
    response = await read_get_bars(daily, symbol="005930", tf="1d", from_=DAY, to=DAY)
    assert "session_segment" not in response["data"][0]
    # no coverage probe for daily / crypto reads
    assert not any("MIN(time_utc)" in s for s in crypto.statements + daily.statements)


@pytest.mark.asyncio
async def test_coverage_read_failure_is_an_unknown() -> None:
    from tests.services.retro_v1.support import operational_error

    def route(sql: str) -> list[Any]:
        if "MIN(time_utc)" in sql:
            raise operational_error()
        return [minute("005930", OPEN_AT, "1", venue="KRX")]

    session = FakeSession({"FROM research.kr_candles_1m": route})
    response = await read_get_bars(session, symbol="005930", tf="1m", from_=DAY, to=DAY)
    validate(response, "GetBarsResponse")
    assert {"field": "data", "reason": "upstream_error"} in response["unknowns"]


def test_contract_session_segment_enum_covers_the_stored_vocabulary() -> None:
    from app.services.research_candles import session_segment as seg
    from tests.services.retro_v1.support import SPEC

    enum = set(
        SPEC["components"]["schemas"]["BarRow"]["properties"]["session_segment"]["enum"]
    )
    stored = {
        getattr(seg, name)
        for name in ("KRX_REGULAR", "KRX_AFTER", "NXT_PRE", "NXT_OVERLAP", "NXT_POST")
        + ("US_PRE", "US_REGULAR", "US_POST", "UNKNOWN")
    }
    assert stored <= enum and "US_DAY" in enum


def test_first_receipt_rule_matches_the_coverage_diagnostic() -> None:
    for batch in ("live-1", "backfill-2", "promote-x", "PROMOTE-y", "xBackfillx", "a"):
        assert is_first_receipt_batch(batch) == (not is_backfill_batch(batch))
    assert is_first_receipt_batch(None) is False


@pytest.mark.asyncio
async def test_data_gaps_minute_freshness_reads_the_research_tables() -> None:
    session = gap_session(**{"FROM research.kr_candles_1m": [_Latest(None)]})
    response = await read_data_gaps(session)
    rows = by_dataset(response)
    assert rows["candles_1m.kr"]["missing_reason"] == "missing_data"
    issued = [s for s in session.statements if "candles_1m" in s]
    assert any("FROM research.kr_candles_1m" in s for s in issued)
    assert any("FROM research.us_candles_1m" in s for s in issued)
    assert not any(
        "public.kr_candles_1m" in s or "public.us_candles_1m" in s for s in issued
    )


class _Latest:
    def __init__(self, latest: Any) -> None:
        self.latest = latest


@pytest.mark.asyncio
async def test_get_bars_prefers_kis_closing_auction_revision() -> None:
    at = datetime(2026, 11, 26, 6, 30, tzinfo=UTC)
    first = at + timedelta(minutes=13, seconds=23)
    revised = at + timedelta(minutes=15)

    def route(sql: str) -> list[Any]:
        if "MIN(time_utc)" in sql:
            return [{"earliest": EARLIEST}]
        assert "LEFT JOIN research.kr_auction_minute_revisions" in sql
        assert "USING (time_utc, symbol, venue)" in sql
        assert "COALESCE(r.close, b.close)" in sql
        assert "COALESCE(r.volume, b.volume)" in sql
        assert "CASE WHEN r.revised_at IS NULL THEN b.value ELSE r.value END" in sql
        return [
            minute(
                "005930",
                at,
                "171300",
                volume="1137019",
                venue="KRX",
                session_segment="UNKNOWN",
                retrieved_at=first,
                revised_at=revised,
                krx_official_close="171300",
            )
        ]

    session = FakeSession({"FROM research.kr_candles_1m": route})
    response = await read_get_bars(
        session, symbol="005930", tf="1m", from_=at.date(), to=at.date()
    )
    validate(response, "GetBarsResponse")
    (row,) = response["data"]
    assert row["close_price"] == row["krx_official_close"] == "171300"
    assert row["quantity"] == "1137019"
    assert datetime.fromisoformat(row["first_received_at"]) == first
    assert datetime.fromisoformat(row["revised_at"]) == revised
