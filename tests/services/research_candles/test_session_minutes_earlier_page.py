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

    async def insert(self, bars, *, batch_id, retrieved_at):
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


@pytest.mark.asyncio
async def test_backfill_is_ignored_for_us() -> None:
    class UsKis:
        calls = 0

        async def inquire_overseas_minute_chart(
            self, symbol, exchange_code="NASD", n=120, keyb=""
        ):
            UsKis.calls += 1
            from types import SimpleNamespace

            return SimpleNamespace(frame=_frame([]))

    report = await sc.collect(
        market="us",
        symbols=["NVDA"],
        kis=UsKis(),  # type: ignore[arg-type]
        writer=Writer(),
        us_exchanges={"NVDA": "NASD"},
        clock=lambda: NOW,
        backfill_symbols=["NVDA"],
    )
    assert UsKis.calls == 1 and report["backfill_symbols"] == 0
