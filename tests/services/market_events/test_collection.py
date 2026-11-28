"""The shared collection projection: expected days, closed reasons, no zero."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.market_events import collection as mc
from app.services.market_events.expected_sources import expected_sources_for_date
from app.services.market_events.repository import CollectionFailure

pytestmark = pytest.mark.unit

#: A Friday session on XKRX; the next day is a Saturday.
FRIDAY = date(2026, 11, 20)
SATURDAY = date(2026, 11, 21)
#: US Thanksgiving: XNYS closed, XKRX open.
THANKSGIVING = date(2026, 11, 26)


def partition(day: date, status: str, **over: Any) -> SimpleNamespace:
    row = {
        "source": "dart",
        "category": "disclosure",
        "partition_date": day,
        "status": status,
        "event_count": 0,
        "last_error": None,
        "finished_at": None,
    }
    row.update(over)
    return SimpleNamespace(**row)


def _missing(names: tuple[str, ...], start: date, end: date) -> list[tuple[str, str]]:
    gaps = mc.collection_gaps([], names, start, end, end)
    return [(g["source"], g["date"]) for g in gaps]


def test_expected_days_follow_the_expected_sources_calendar() -> None:
    """A day without any partition is a gap exactly where a source is expected."""
    names = ("dart", "finnhub", "sec", "benzinga", "naver_disclosure")
    start, end = date(2026, 11, 20), date(2026, 11, 30)
    want = []
    day = start
    while day <= end:
        want += [
            (source, day.isoformat())
            for source, _, _ in sorted(expected_sources_for_date(day))
        ]
        day += timedelta(days=1)
    assert _missing(names, start, end) == sorted(want, key=lambda i: (i[1], i[0]))


def test_weekends_and_holidays_are_not_gaps() -> None:
    assert _missing(("dart", "finnhub"), SATURDAY, SATURDAY) == []
    assert _missing(("finnhub",), THANKSGIVING, THANKSGIVING) == []
    assert _missing(("dart",), THANKSGIVING, THANKSGIVING) == [("dart", "2026-11-26")]


def test_reasons_are_closed_and_never_the_stored_message() -> None:
    rows = [
        partition(date(2026, 11, 16), "failed", last_error="quota"),
        partition(date(2026, 11, 17), "failed", last_error="secret-token boom"),
        partition(date(2026, 11, 18), "pending"),
        partition(date(2026, 11, 19), "succeeded"),
        partition(FRIDAY, "succeeded", event_count=3),
    ]
    gaps = mc.collection_gaps(rows, ("dart",), date(2026, 11, 16), FRIDAY, FRIDAY)
    assert [g["reason"] for g in gaps] == [
        "quota",
        "failed",
        "pending",
        "unproven_zero",
    ]
    assert "secret-token" not in json.dumps(gaps)


def test_an_unclassifiable_day_is_calendar_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mc, "trading_session_status", lambda *_: "unknown")
    gaps = mc.collection_gaps([], ("dart",), FRIDAY, FRIDAY, FRIDAY)
    assert [g["reason"] for g in gaps] == ["calendar_unknown"]


def test_the_wrong_category_never_covers_an_expected_day() -> None:
    rows = [partition(FRIDAY, "succeeded", category="earnings", event_count=1)]
    gaps = mc.collection_gaps(rows, ("dart",), FRIDAY, FRIDAY, FRIDAY)
    assert [g["reason"] for g in gaps] == ["not_yet_run"]


def test_failure_codes_match_the_writer() -> None:
    assert mc.FAILURE_CODES == {code.value for code in CollectionFailure}
    assert dict(mc.PROVEN_ZERO_CUTOVERS) == {}
    assert mc.SCOPED_SOURCES <= mc.PARTITIONED_SOURCES


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class _Session:
    def __init__(self, window: list[Any], newest: list[Any]) -> None:
        self.answers = [window, newest]
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(self.answers.pop(0))


@pytest.mark.asyncio
async def test_the_operator_read_covers_every_collector_and_marks_unknowns() -> None:
    session = _Session(
        [partition(FRIDAY, "succeeded", source="finnhub", category="earnings")],
        [SimpleNamespace(source="finnhub", latest=date(2026, 11, 19))],
    )
    block = await mc.read_collection(
        session, market="us", start=FRIDAY, end=FRIDAY, today=FRIDAY
    )
    assert block["basis"] == "current_partitions"
    assert block["latest_collected"] == {
        "finnhub": "2026-11-19",
        "sec": None,
        "benzinga": None,
    }
    assert [(g["source"], g["reason"]) for g in block["gaps"]] == [
        ("finnhub", "unproven_zero")
    ]
    assert {(u["field"], u["reason"]) for u in block["unknowns"]} == {
        ("collection.latest_collected.sec", "scoped_collection"),
        ("collection.latest_collected.benzinga", "missing_data"),
    }
    assert all(s.lstrip().startswith("SELECT") for s in session.statements)


@pytest.mark.asyncio
async def test_a_source_without_partitions_says_no_partition() -> None:
    block = await mc.read_collection(
        _Session([], []), market="crypto", start=FRIDAY, end=FRIDAY, today=FRIDAY
    )
    assert block["gaps"] == []
    assert block["unknowns"] == [
        {
            "field": f"collection.latest_collected.{name}",
            "reason": "no_partition",
        }
        for name in ("upbit_notice", "bithumb_notice")
    ]


@pytest.mark.asyncio
async def test_a_truncated_read_is_unknown_not_empty() -> None:
    rows = [partition(FRIDAY, "pending")] * 2
    block = await mc.read_collection(
        _Session(rows, []),
        market="kr",
        start=FRIDAY,
        end=FRIDAY,
        today=FRIDAY,
        row_limit=2,
    )
    assert block["gaps"] is None
    assert block["latest_collected"]["dart"] is None
    assert {"field": "collection", "reason": "incomplete_evidence"} in block["unknowns"]


@pytest.mark.asyncio
async def test_a_long_gap_list_keeps_the_newest_and_says_so() -> None:
    block = await mc.read_collection(
        _Session([], []),
        market="kr",
        start=date(2025, 11, 20),
        end=FRIDAY,
        today=FRIDAY,
    )
    assert block["gaps_truncated"] is True
    assert len(block["gaps"]) == mc.MAX_GAPS
    assert block["gaps"][-1]["date"] == "2026-11-20"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"market": "jp", "start": FRIDAY, "end": FRIDAY}, "unsupported market"),
        ({"market": "kr", "start": SATURDAY, "end": FRIDAY}, "start must be"),
    ],
)
@pytest.mark.asyncio
async def test_bad_requests_run_no_statement(kwargs: dict, message: str) -> None:
    session = _Session([], [])
    with pytest.raises(ValueError, match=message):
        await mc.read_collection(session, today=FRIDAY, **kwargs)
    assert session.statements == []


@pytest.mark.asyncio
async def test_the_operator_read_applies_the_scoped_and_cutover_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutover = datetime(2026, 11, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(mc, "PROVEN_ZERO_CUTOVERS", {"benzinga": cutover})
    rows = [
        partition(FRIDAY, "pending", source="sec"),
        partition(
            FRIDAY,
            "succeeded",
            source="benzinga",
            category="split",
            finished_at=cutover,
        ),
        partition(
            FRIDAY,
            "succeeded",
            source="benzinga",
            category="guidance",
            finished_at=cutover - timedelta(seconds=1),
        ),
    ]
    session = _Session(rows, [])
    block = await mc.read_collection(
        session, market="us", start=FRIDAY, end=FRIDAY, today=FRIDAY
    )
    assert [(g["source"], g["category"], g["reason"]) for g in block["gaps"]] == [
        ("benzinga", "guidance", "unproven_zero"),
        ("finnhub", "earnings", "not_yet_run"),
    ]
    assert "finished_at >=" in session.statements[1]
