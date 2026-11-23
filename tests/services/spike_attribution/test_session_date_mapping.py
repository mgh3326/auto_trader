"""Daily rows are labelled with their session date, whatever hour they are stored.

The bug this pins: US daily rows are stored at 00:00 UTC of the session date,
and reading them in New York time moved every one back a session. A US date D
then reported ``no_bar_for_session_date`` even after D's bar existed, and the
cache kept that negative. The fakes below return the stored ``time`` only, so
the session date has to come from the timestamp, not from the SQL.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.spike_attribution.cache import (
    STATE_FRESH,
    STATE_MISSING,
    CacheEntry,
    lookup,
    write_entry,
)
from app.services.spike_attribution.detect import detect_spikes
from app.services.spike_attribution.materials import (
    daily_session_date,
    load_daily_bars,
)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
UTC = dt.UTC
NOW = dt.datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "golden" / "kr_daily_bars_detect.json"

# (day, open, high, low, close, volume) — stored at a fixed UTC hour per test.
KR_BARS = [
    (dt.date(2026, 8, 13), "99000", "101000", "98000", "100000", "1000000"),
    (dt.date(2026, 8, 14), "100000", "102000", "99500", "101000", "900000"),
    (dt.date(2026, 8, 17), "101000", "103000", "100500", "102000", "950000"),
    (dt.date(2026, 8, 18), "102000", "104000", "101500", "103000", "800000"),
    (dt.date(2026, 8, 19), "103000", "105000", "102500", "104000", "700000"),
    (dt.date(2026, 8, 20), "104000", "116000", "103500", "112000", "3000000"),
]
US_BARS = [
    (dt.date(2026, 11, 16), "180", "182", "178", "181", "50000000"),
    (dt.date(2026, 11, 17), "181", "183", "179", "180", "48000000"),
    (dt.date(2026, 11, 18), "180", "184", "179", "183", "51000000"),
    (dt.date(2026, 11, 19), "183", "185", "181", "184", "47000000"),
    (dt.date(2026, 11, 20), "184", "186", "182", "185", "52000000"),
    (dt.date(2026, 11, 23), "186", "205", "185", "198", "90000000"),
]


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeDB:
    """Returns the rows it was built with. Only ``time`` and prices exist."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def execute(self, stmt, params=None) -> _Result:
        return _Result(self._rows)


def _rows(bars, stored_hour_utc: int) -> list[SimpleNamespace]:
    out = []
    for day, o, h, lo, c, v in bars:
        stored = dt.datetime(day.year, day.month, day.day, stored_hour_utc, tzinfo=UTC)
        out.append(
            SimpleNamespace(
                time=stored,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(lo),
                close=Decimal(c),
                volume=Decimal(v),
            )
        )
    return out


async def _load(market: str, bars, stored_hour_utc: int, session: dt.date):
    return await load_daily_bars(
        _FakeDB(_rows(bars, stored_hour_utc)),
        market=market,
        symbol="NVDA" if market == "us" else "035420",
        start=session - dt.timedelta(days=20),
        end=session,
    )


def _serialize(
    market: str, symbol: str, stored_hour_utc: int, session: dt.date, loaded
):
    """Same shape as the committed golden; deliberately plain string output."""

    event, diagnostics = detect_spikes(
        market=market, symbol=symbol, bars=loaded, session_date=session
    )
    return {
        "market": market,
        "symbol": symbol,
        "stored_hour_utc": stored_hour_utc,
        "session_date": session.isoformat(),
        "bars": [
            {
                k: (v.isoformat() if isinstance(v, dt.date) else str(v))
                for k, v in dataclasses.asdict(b).items()
            }
            for b in loaded
        ],
        "event": (
            {
                k: (v.isoformat() if isinstance(v, dt.date | dt.datetime) else str(v))
                for k, v in dataclasses.asdict(event).items()
            }
            if event
            else None
        ),
        "diagnostics": json.loads(json.dumps(diagnostics, default=str, sort_keys=True)),
    }


@pytest.mark.parametrize(
    ("stored_at", "expected"),
    [
        (dt.datetime(2026, 11, 20, 0, tzinfo=UTC), dt.date(2026, 11, 20)),
        (dt.datetime(2026, 11, 23, 0, tzinfo=UTC), dt.date(2026, 11, 23)),
        (dt.datetime(2026, 11, 20, 5, tzinfo=UTC), dt.date(2026, 11, 20)),
        (dt.datetime(2026, 11, 23, 5, tzinfo=UTC), dt.date(2026, 11, 23)),
    ],
)
def test_us_stored_hour_does_not_move_the_session_date(
    stored_at: dt.datetime, expected: dt.date
) -> None:
    assert daily_session_date("us", stored_at) == expected


@pytest.mark.asyncio
async def test_us_rows_at_00z_and_05z_load_with_the_session_date_they_are_labelled_with() -> (
    None
):
    midnight = await _load("us", US_BARS, 0, dt.date(2026, 11, 23))
    early = await _load("us", US_BARS, 5, dt.date(2026, 11, 23))

    assert [b.session_date for b in midnight][-2:] == [
        dt.date(2026, 11, 20),
        dt.date(2026, 11, 23),
    ]
    assert [b.session_date for b in early] == [b.session_date for b in midnight]


@pytest.mark.asyncio
async def test_a_us_date_whose_bar_exists_is_detected_against_the_previous_close() -> (
    None
):
    session = dt.date(2026, 11, 23)
    loaded = await _load("us", US_BARS, 0, session)
    event, diagnostics = detect_spikes(
        market="us", symbol="NVDA", bars=loaded, session_date=session
    )

    # Before the fix this came back as skipped: no_bar_for_session_date.
    assert diagnostics.get("skipped") != "no_bar_for_session_date"
    assert event is not None
    assert event.session_date == session
    assert event.prev_close == Decimal("185")  # the 2026-11-20 close
    assert event.window_start_exclusive == dt.datetime(2026, 11, 20, 16, tzinfo=NY)


@pytest.mark.asyncio
async def test_kr_daily_bars_are_byte_identical_to_the_pre_change_golden() -> None:
    session = dt.date(2026, 8, 20)
    loaded = await _load("kr", KR_BARS, 0, session)
    produced = _serialize("kr", "035420", 0, session, loaded)

    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert json.dumps(produced, ensure_ascii=False, sort_keys=True) == json.dumps(
        expected, ensure_ascii=False, sort_keys=True
    )
    assert produced["event"]["session_date"] == "2026-08-20"


def test_a_cache_entry_written_under_the_old_us_layout_is_not_served(
    tmp_path: Path,
) -> None:
    session = dt.date(2026, 11, 23)
    old_path = tmp_path / "us" / session.isoformat() / "NVDA.json"
    old_path.parent.mkdir(parents=True)
    old_entry = CacheEntry(
        market="us",
        session_date=session,
        symbol="NVDA",
        mode="intraday",
        computed_at=NOW,
        spec_sha256="x",
        payload={
            "symbol": "NVDA",
            "spike": False,
            "skipped": "no_bar_for_session_date",
        },
    )
    old_path.write_text(json.dumps(old_entry.as_dict()), encoding="utf-8")

    read = lookup(
        market="us", session_date=session, symbol="NVDA", now=NOW, root=tmp_path
    )

    assert read.state == STATE_MISSING
    assert read.entry is None


def test_a_new_us_entry_is_served_and_kr_keeps_its_directory(tmp_path: Path) -> None:
    session = dt.date(2026, 11, 23)
    us_entry = CacheEntry(
        market="us",
        session_date=session,
        symbol="NVDA",
        mode="intraday",
        computed_at=NOW,
        spec_sha256="x",
        payload={"symbol": "NVDA", "spike": True},
    )
    kr_entry = CacheEntry(
        market="kr",
        session_date=dt.date(2026, 8, 20),
        symbol="035420",
        mode="intraday",
        computed_at=NOW,
        spec_sha256="x",
        payload={"symbol": "035420", "spike": False},
    )
    us_path = write_entry(us_entry, root=tmp_path)
    kr_path = write_entry(kr_entry, root=tmp_path)

    assert us_path.relative_to(tmp_path).parts[0] == "us-utc-session-v2"
    assert kr_path.relative_to(tmp_path).parts[0] == "kr"
    read = lookup(
        market="us", session_date=session, symbol="NVDA", now=NOW, root=tmp_path
    )
    assert read.state == STATE_FRESH


@pytest.mark.asyncio
async def test_a_symbol_with_00z_and_05z_rows_for_one_session_keeps_the_00z_bar() -> (
    None
):
    # Two stored rows for the same US session: KIS/Yahoo/Toss at 00:00Z and an
    # Alpaca IEX row at 05:00Z. Both map to 2026-11-20. Keep the earlier 00:00Z.
    rows = _rows(US_BARS[:5], 0) + [
        SimpleNamespace(
            time=dt.datetime(2026, 11, 20, 5, tzinfo=UTC),
            open=Decimal("184"),
            high=Decimal("190"),
            low=Decimal("181"),
            close=Decimal("188"),
            volume=Decimal("1"),
        )
    ]
    loaded = await load_daily_bars(
        _FakeDB(rows),
        market="us",
        symbol="NVDA",
        start=dt.date(2026, 11, 16),
        end=dt.date(2026, 11, 23),
    )
    nov20 = [b for b in loaded if b.session_date == dt.date(2026, 11, 20)]
    assert len(nov20) == 1
    assert nov20[0].close == Decimal("185")
    session_dates = [b.session_date for b in loaded]
    assert session_dates == sorted(set(session_dates))


@pytest.mark.asyncio
async def test_two_rows_with_the_same_timestamp_fail_closed() -> None:
    rows = _rows(US_BARS[:2], 0)
    rows.append(rows[0])
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        await load_daily_bars(
            _FakeDB(rows),
            market="us",
            symbol="NVDA",
            start=dt.date(2026, 11, 16),
            end=dt.date(2026, 11, 17),
        )
