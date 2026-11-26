"""hk 6154: a newly seen intraday mover gets one earlier KIS page (hour cursor).

KIS returns ~30 bars per call regardless of ``n``; the second page is read with
``end_time`` = the earliest bar's time of the first page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.services.research_candles import session_minutes_collector as sc

pytestmark = pytest.mark.unit

# 2026-11-24 11:05 KST
NOW = datetime(2026, 11, 24, 2, 5, tzinfo=UTC)


def _labels(start: str, count: int) -> list[str]:
    first = datetime.strptime(f"2026-11-24 {start}", "%Y-%m-%d %H:%M")
    return [
        (first + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M") for i in range(count)
    ]


def _frame(labels: list[str]) -> pd.DataFrame:
    n = len(labels)
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(labels),
            "open": [100 + i for i in range(n)],
            "high": [101 + i for i in range(n)],
            "low": [99 + i for i in range(n)],
            "close": [100 + i for i in range(n)],
            "volume": [10] * n,
            "value": [1000 + 10 * i for i in range(n)],
        }
    )


class PagedKis:
    def __init__(self, pages: dict, fail_cursor: str | None = None):
        self.pages = pages  # (code, market, end_time | None) -> frame
        self.fail_cursor = fail_cursor
        self.calls: list[tuple[str, str, str | None]] = []

    async def inquire_minute_chart(
        self, code, market="J", time_unit=1, n=200, end_time=None
    ):
        self.calls.append((code, market, end_time))
        if end_time is not None and end_time == self.fail_cursor:
            raise RuntimeError("detail that must not leak")
        return self.pages.get((code, market, end_time), _frame([]))


class Writer:
    def __init__(self):
        self.bars: list[sc.MinuteBar] = []

    async def insert(
        self, bars, *, batch_id, retrieved_at, auction_observed_before=None
    ):
        self.bars.extend(bars)
        return len(bars), 0


def _pages_for(code: str):
    newest = _frame(_labels("10:30", 30))  # 10:30 .. 10:59
    older = _frame(_labels("10:00", 31))  # 10:00 .. 10:30 (10:30 overlaps)
    return {(code, "J", None): newest, (code, "J", "103000"): older}


@pytest.mark.asyncio
async def test_new_mover_reads_two_pages_and_covers_sixty_minutes() -> None:
    kis = PagedKis(_pages_for("000700"))
    writer = Writer()
    report = await sc.collect(
        market="kr",
        symbols=["000700"],
        kis=kis,
        writer=writer,
        clock=lambda: NOW,
        backfill_symbols=["000700"],
    )
    assert kis.calls == [("000700", "J", None), ("000700", "J", "103000")]
    assert report["backfill_symbols"] == 1
    assert report["backfill_pages"] == 1
    assert report["reads"] == 2
    assert report["errors"] == 0
    starts = sorted(b.time_utc for b in writer.bars)
    assert len(starts) == 60  # 10:00..10:59, the overlapping 10:30 bar counted once
    assert len(set(starts)) == 60
    assert starts[0] == datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # 10:00 KST
    assert starts[-1] == datetime(2026, 11, 24, 1, 59, tzinfo=UTC)  # 10:59 KST


@pytest.mark.asyncio
async def test_other_symbols_stay_on_one_page() -> None:
    pages = {**_pages_for("000700"), **_pages_for("005930")}
    kis = PagedKis(pages)
    await sc.collect(
        market="kr",
        symbols=["000700", "005930"],
        kis=kis,
        writer=Writer(),
        clock=lambda: NOW,
        backfill_symbols=["000700"],
    )
    assert [c for c in kis.calls if c[0] == "005930"] == [("005930", "J", None)]


@pytest.mark.asyncio
async def test_no_earlier_page_when_the_first_page_already_starts_at_the_open() -> None:
    kis = PagedKis({("000700", "J", None): _frame(_labels("09:00", 20))})
    report = await sc.collect(
        market="kr",
        symbols=["000700"],
        kis=kis,
        writer=Writer(),
        clock=lambda: NOW,
        backfill_symbols=["000700"],
    )
    assert kis.calls == [("000700", "J", None)]
    assert report["backfill_pages"] == 0


@pytest.mark.asyncio
async def test_a_failing_earlier_page_keeps_the_first_page_and_is_reported() -> None:
    kis = PagedKis(_pages_for("000700"), fail_cursor="103000")
    writer = Writer()
    report = await sc.collect(
        market="kr",
        symbols=["000700"],
        kis=kis,
        writer=writer,
        clock=lambda: NOW,
        backfill_symbols=["000700"],
    )
    assert len(writer.bars) == 30  # the newest page is not lost
    assert report["backfill_pages"] == 0
    assert report["error_details"] == [
        {
            "symbol": "000700",
            "venue": "KRX",
            "origin": "intraday_movers",
            "error_class": "RuntimeError",
        }
    ]
    assert "must not leak" not in str(report)


# ------------------------------------------------------------------- US (6510)


US_NOW = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)  # 10:30 ET


def _us_frame(labels: list[str]) -> pd.DataFrame:
    return _frame(labels)


def _us_labels(start: str, count: int) -> list[str]:
    first = datetime.strptime(f"2026-11-24 {start}", "%Y-%m-%d %H:%M")
    return [
        (first + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M") for i in range(count)
    ]


class UsPagedKis:
    """``keyb`` -> page; the first page is ``None``. Records every call."""

    def __init__(self, pages: dict, fail_keyb: str | None = None):
        self.pages = pages
        self.fail_keyb = fail_keyb
        self.calls: list[str | None] = []

    async def inquire_overseas_minute_chart(
        self, symbol, exchange_code="NASD", n=120, keyb=""
    ):
        from types import SimpleNamespace

        self.calls.append(keyb or None)
        if keyb and keyb == self.fail_keyb:
            raise RuntimeError("detail that must not leak")
        frame, has_more, next_keyb = self.pages.get(
            keyb or None, (_frame([]), False, None)
        )
        return SimpleNamespace(frame=frame, has_more=has_more, next_keyb=next_keyb)


def _us_pages():
    return {
        None: (_us_frame(_us_labels("09:30", 30)), True, "093000"),
        "093000": (_us_frame(_us_labels("09:00", 30)), True, "090000"),
        # reaches 04:00 ET, the session start: the loop stops there
        "090000": (_us_frame(_us_labels("04:00", 30)), True, "040000"),
    }


@pytest.mark.asyncio
async def test_us_backfill_pages_back_to_the_session_start() -> None:
    kis = UsPagedKis(_us_pages())
    writer = Writer()
    report = await sc.collect(
        market="us",
        symbols=["NVDA"],
        kis=kis,  # type: ignore[arg-type]
        writer=writer,
        us_exchanges={"NVDA": "NASD"},
        clock=lambda: US_NOW,
        backfill_symbols=["NVDA"],
    )
    assert kis.calls == [None, "093000", "090000"]
    assert report["backfill_pages"] == 2
    assert report["backfill_symbols"] == 1
    assert report["reads"] == 3
    assert report["errors"] == 0
    assert len(writer.bars) == 90  # 04:00..09:59 ET, in minute order
    assert writer.bars[0].time_utc == datetime(
        2026, 11, 24, 9, 0, tzinfo=UTC
    )  # 04:00 ET
    assert writer.bars[-1].time_utc == datetime(2026, 11, 24, 14, 59, tzinfo=UTC)


@pytest.mark.asyncio
async def test_us_backfill_is_bounded_by_the_page_cap() -> None:
    pages = {None: (_us_frame(_us_labels("09:30", 5)), True, "k1")}
    for index in range(sc.US_BACKFILL_MAX_PAGES + 5):
        pages[f"k{index + 1}"] = (
            _us_frame(_us_labels("08:00", 5)),
            True,
            f"k{index + 2}",
        )
    kis = UsPagedKis(pages)
    report = await sc.collect(
        market="us",
        symbols=["NVDA"],
        kis=kis,  # type: ignore[arg-type]
        writer=Writer(),
        us_exchanges={"NVDA": "NASD"},
        clock=lambda: US_NOW,
        backfill_symbols=["NVDA"],
    )
    assert len(kis.calls) == 1 + sc.US_BACKFILL_MAX_PAGES
    assert report["backfill_pages"] == sc.US_BACKFILL_MAX_PAGES


@pytest.mark.asyncio
async def test_us_earlier_page_failure_keeps_the_first_page() -> None:
    kis = UsPagedKis(_us_pages(), fail_keyb="093000")
    writer = Writer()
    report = await sc.collect(
        market="us",
        symbols=["NVDA"],
        kis=kis,  # type: ignore[arg-type]
        writer=writer,
        us_exchanges={"NVDA": "NASD"},
        clock=lambda: US_NOW,
        backfill_symbols=["NVDA"],
    )
    assert report["errors"] == 1
    assert report["error_details"][0]["origin"] == "intraday_movers"
    assert report["error_details"][0]["error_class"] == "RuntimeError"
    assert "detail that must not leak" not in str(report["error_details"])
    assert len(writer.bars) == 30  # only the newest page survived


@pytest.mark.asyncio
async def test_us_symbols_outside_the_backfill_stay_on_one_page() -> None:
    kis = UsPagedKis(_us_pages())
    await sc.collect(
        market="us",
        symbols=["AMD"],
        kis=kis,  # type: ignore[arg-type]
        writer=Writer(),
        us_exchanges={"AMD": "NASD"},
        clock=lambda: US_NOW,
        backfill_symbols=["NVDA"],
    )
    assert kis.calls == [None]
