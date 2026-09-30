"""Pacing/backoff/resume tests for scripts/backfill_dart_disclosures.py (#1097)."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import requests
import urllib3.exceptions
from sqlalchemy import delete, select

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import market_events_test_lock

validate_run_owned_database_url(engine.url)


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


DART_ROW = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "분기보고서 (2026.03)",
}

# 2026-05-04 Mon .. 2026-05-08 Fri; 05-05 Children's Day is XKRX-closed but
# these tests always return rows, so session status never gates them.
WEEK = [date(2026, 5, 4) + timedelta(days=i) for i in range(5)]


class _MockClock:
    """Records every requested sleep without waiting (mock clock)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


# ---------------------------------------------------------------------------
# transient classifier (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        ConnectionRefusedError(111, "Connection refused"),
        ConnectionAbortedError(103, "Connection aborted"),
        TimeoutError("timed out"),
        socket.gaierror("name resolution failed"),
        ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"),
        http.client.RemoteDisconnected("remote end closed"),
        http.client.IncompleteRead(b""),
        requests.exceptions.ConnectTimeout("connect timed out"),
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.RetryError("too many retries"),
        requests.exceptions.ChunkedEncodingError("connection broken"),
        urllib3.exceptions.MaxRetryError(None, "url"),
        urllib3.exceptions.ProtocolError("aborted"),
    ],
)
def test_transient_classifier_flags_network_faults(exc: BaseException):
    from scripts.backfill_dart_disclosures import _transient_dart_error

    assert _transient_dart_error(exc) == "network_error"


@pytest.mark.unit
@pytest.mark.parametrize(
    "status_code,expected",
    [(429, "http_429"), (500, "http_500"), (503, "http_503"), (599, "http_599")],
)
def test_transient_classifier_flags_http_429_and_5xx(status_code: int, expected: str):
    from scripts.backfill_dart_disclosures import _transient_dart_error

    assert _transient_dart_error(_http_error(status_code)) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,expected",
    [
        ("020", "dart_status_020"),
        ("800", "dart_status_800"),
        ("900", "dart_status_900"),
    ],
)
def test_transient_classifier_flags_dart_over_limit_statuses(
    status: str, expected: str
):
    """OpenDartReader raises ValueError({'status': ..., 'message': ...}) — the
    '020' over-limit and '800'/'900' server-side codes are retryable (#1097)."""
    from scripts.backfill_dart_disclosures import _transient_dart_error

    exc = ValueError({"status": status, "message": "err"})
    assert _transient_dart_error(exc) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        ValueError({"status": "010", "message": "unregistered key"}),
        ValueError({"status": "013", "message": "no data"}),
        ValueError({"status": "100", "message": "bad field"}),
        ValueError("plain value error"),
        ValueError(),
    ],
)
def test_transient_classifier_rejects_non_transient_dart_faults(
    exc: BaseException,
):
    from scripts.backfill_dart_disclosures import _transient_dart_error

    assert _transient_dart_error(exc) is None


@pytest.mark.unit
def test_transient_classifier_rejects_http_4xx_other_than_429():
    from scripts.backfill_dart_disclosures import _transient_dart_error

    assert _transient_dart_error(_http_error(400)) is None
    assert _transient_dart_error(_http_error(404)) is None


@pytest.mark.unit
def test_transient_classifier_rejects_schema_and_parse_faults():
    """HTML error pages / contract drift are NOT transient (#1097)."""
    from app.services.market_events.dart_helpers import DartResponseSchemaError
    from scripts.backfill_dart_disclosures import _transient_dart_error

    assert _transient_dart_error(DartResponseSchemaError("missing column")) is None
    assert _transient_dart_error(AttributeError("no attribute")) is None
    assert _transient_dart_error(KeyError("k")) is None


@pytest.mark.unit
def test_sanitize_error_text_scrubs_secret_params():
    """Transport-error URLs must not leak crtfc_key/api_key values (#1097)."""
    from scripts.backfill_dart_disclosures import _sanitize_error_text

    exc = requests.exceptions.ConnectionError(
        "Max retries exceeded with url: "
        "/corpCode.xml?crtfc_key=SECRET123&lang=ko&token=abc"
    )
    out = _sanitize_error_text(exc)
    assert "SECRET123" not in out
    assert "abc" not in out.split("token=")[1]
    assert "crtfc_key=***" in out
    assert "token=***" in out
    assert "lang=ko" in out


@pytest.mark.unit
def test_sanitize_error_text_leaves_benign_text():
    from scripts.backfill_dart_disclosures import _sanitize_error_text

    exc = ConnectionResetError(104, "Connection reset by peer")
    assert _sanitize_error_text(exc) == "[Errno 104] Connection reset by peer"


# ---------------------------------------------------------------------------
# CLI arg validation (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_args_pacing_and_resume_defaults():
    from scripts.backfill_dart_disclosures import parse_args

    ns = parse_args(["--from-date", "2026-01-01", "--to-date", "2026-01-31"])
    assert ns.resume_from is None
    assert ns.max_days is None
    assert ns.pace_seconds == 1.0  # AC: default pacing at least 1 s
    assert ns.max_calls == 1000
    assert ns.retry_max_attempts == 5
    assert ns.retry_base_seconds == 5.0
    assert ns.retry_max_seconds == 60.0
    assert ns.max_consecutive_failures == 3  # AC: default stop threshold


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        ["--resume-from", "2026-02-01"],  # after --to-date
        ["--max-days", "0"],
        ["--pace-seconds", "-1"],
        ["--max-calls", "0"],
        ["--retry-max-attempts", "0"],
        ["--retry-base-seconds", "-0.5"],
        ["--retry-max-seconds", "0"],
        ["--max-consecutive-failures", "0"],
    ],
)
def test_parse_args_rejects_invalid_pacing_values(extra: list[str]):
    from scripts.backfill_dart_disclosures import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--from-date", "2026-01-01", "--to-date", "2026-01-31", *extra])


@pytest.mark.unit
def test_parse_args_accepts_resume_and_bounds():
    from scripts.backfill_dart_disclosures import parse_args

    ns = parse_args(
        [
            "--from-date",
            "2026-01-01",
            "--to-date",
            "2026-07-21",
            "--resume-from",
            "2026-01-19",
            "--max-days",
            "30",
            "--pace-seconds",
            "2.5",
            "--max-calls",
            "500",
            "--commit",
        ]
    )
    assert ns.resume_from == date(2026, 1, 19)
    assert ns.max_days == 30
    assert ns.pace_seconds == 2.5
    assert ns.max_calls == 500


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"resume_from": date(2026, 2, 1)}, "resume_from is after to_date"),
        ({"max_days": 0}, "max_days"),
        ({"pace_seconds": -1.0}, "pace_seconds"),
        ({"max_calls": 0}, "max_calls"),
        ({"retry_max_attempts": 0}, "retry_max_attempts"),
        ({"retry_base_seconds": -1.0}, "retry backoff"),
        ({"retry_max_seconds": 0.0}, "retry backoff"),
        ({"max_consecutive_failures": 0}, "max_consecutive_failures"),
    ],
)
async def test_run_backfill_validates_pacing_args(kwargs, match: str):
    from scripts.backfill_dart_disclosures import run_backfill

    with pytest.raises(ValueError, match=match):
        await run_backfill(
            db=None,  # type: ignore[arg-type] - validation precedes any db use
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            fetch_rows=AsyncMock(return_value=[]),
            **kwargs,
        )


# ---------------------------------------------------------------------------
# run-level behavior (integration, throwaway DB + injected fetch/clock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reset_on_day_n_then_success_after_backoff(db_session):
    """ConnectionResetError on day 2 retries with exponential backoff and the
    run still completes (#1097 AC: reset on day N then success after backoff)."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(
        side_effect=[
            [dict(DART_ROW)],  # 05-06 succeeds first try
            ConnectionResetError(104, "Connection reset by peer"),
            ConnectionResetError(104, "Connection reset by peer"),
            [dict(DART_ROW)],  # 05-07 succeeds on attempt 3
            [dict(DART_ROW)],  # 05-08 succeeds first try
        ]
    )
    clock = _MockClock()
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[2],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_base_seconds=5.0,
        retry_max_seconds=60.0,
        rng=lambda: 0.5,  # jitter multiplier = 0.5 + 0.5 = 1.0 -> deterministic
        sleep=clock,
    )

    assert rc == 0
    assert fetch.await_count == 5
    # two backoff sleeps: 5.0 * 2^0 and 5.0 * 2^1 (jitter multiplier 1.0)
    assert [s for s in clock.sleeps if s > 0] == [5.0, 10.0]
    retry_lines = [line for line in lines if "fetch_retry" in line]
    assert len(retry_lines) == 2
    assert all("reason=network_error" in line for line in retry_lines)
    summary = json.loads(lines[-1])
    assert summary["succeeded"] == 3
    assert summary["failed"] == 0
    assert summary["dart_calls"] == 5
    assert summary["dart_retries"] == 2
    assert summary["stop_reason"] == "completed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_three_consecutive_failures_stop_cleanly(db_session):
    """3 consecutive failed days stop the run cleanly — summary, no traceback
    (#1097 AC)."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
    clock = _MockClock()
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_max_attempts=2,
        retry_base_seconds=1.0,
        rng=lambda: 0.5,
        sleep=clock,
    )

    assert rc == 2
    # 3 days * 2 attempts; days 4 and 5 never reached
    assert fetch.await_count == 6
    per_day = [line for line in lines if line.startswith("2026-05-0")]
    assert len([line for line in per_day if "fetch_error=" in line]) == 3
    assert not any(line.startswith(WEEK[3].isoformat()) for line in per_day)
    assert not any(line.startswith(WEEK[4].isoformat()) for line in per_day)
    assert any("stop:" in line and "consecutive failed days" in line for line in lines)
    summary = json.loads(lines[-1])
    assert summary["stop_reason"] == "consecutive_failures"
    assert summary["consecutive_failures"] == 3
    assert summary["failed"] == 3
    assert summary["failed_dates"] == [
        WEEK[0].isoformat(),
        WEEK[1].isoformat(),
        WEEK[2].isoformat(),
    ]
    assert summary["processed_through"] == WEEK[2].isoformat()
    assert summary["next_from_date"] == WEEK[3].isoformat()
    assert summary["days_remaining"] == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resume_from_skips_days_before_it(db_session):
    """--resume-from restarts exactly where the prior run stopped (#1097 AC)."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        resume_from=WEEK[2],
        pace_seconds=0.0,
        sleep=_MockClock(),
    )

    assert rc == 0
    assert [c.args[0] for c in fetch.await_args_list] == WEEK[2:]
    per_day = [line for line in lines if line.startswith("2026-05-0")]
    assert not any(line.startswith(WEEK[0].isoformat()) for line in per_day)
    assert not any(line.startswith(WEEK[1].isoformat()) for line in per_day)
    summary = json.loads(lines[-1])
    assert summary["resume_from"] == WEEK[2].isoformat()
    assert summary["days_processed"] == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resume_from_before_from_date_clamps(db_session):
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[3],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lambda _line: None,
        resume_from=WEEK[0],  # before --from-date: clamps, not an error
        pace_seconds=0.0,
        sleep=_MockClock(),
    )
    assert rc == 0
    assert fetch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resume_from_equals_to_date_processes_last_day_only(db_session):
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lambda _line: None,
        resume_from=WEEK[4],
        pace_seconds=0.0,
        sleep=_MockClock(),
    )
    assert rc == 0
    assert [c.args[0] for c in fetch.await_args_list] == [WEEK[4]]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pacing_honored_with_mock_clock(db_session):
    """Every DART call after the first waits pace_seconds (mock clock, #1097)."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    clock = _MockClock()

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[2],
        commit=False,
        fetch_rows=fetch,
        emit=lambda _line: None,
        pace_seconds=1.5,
        sleep=clock,
    )

    assert rc == 0
    assert fetch.await_count == 3
    assert clock.sleeps == [1.5, 1.5]  # between calls only — no wait before call 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pacing_applies_to_retry_calls_too(db_session):
    """Backoff AND inter-call pacing both show on the mock clock."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(
        side_effect=[
            ConnectionResetError(104, "reset"),
            [dict(DART_ROW)],
        ]
    )
    clock = _MockClock()

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[0],
        commit=False,
        fetch_rows=fetch,
        emit=lambda _line: None,
        pace_seconds=2.0,
        retry_base_seconds=5.0,
        rng=lambda: 0.5,
        sleep=clock,
    )

    assert rc == 0
    assert fetch.await_count == 2
    # retry attempt: 5.0 backoff, then 2.0 pacing before the next call
    assert clock.sleeps == [5.0, 2.0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_writes_nothing_on_fetch_failures(db_session):
    """Dry-run records failed days in the summary but writes nothing, even
    after exhausted retries (#1097 AC)."""
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[3],
        to_date=WEEK[3],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_max_attempts=3,
        retry_base_seconds=1.0,
        rng=lambda: 0.5,
        sleep=_MockClock(),
    )

    assert rc == 2
    assert fetch.await_count == 3
    assert any("fetch_error=ConnectionResetError" in line for line in lines)
    summary = json.loads(lines[-1])
    assert summary["failed"] == 1
    assert summary["dart_retries"] == 2

    for model in (MarketEvent, MarketEventValue, MarketEventIngestionPartition):
        rows = (await db_session.execute(select(model))).scalars().all()
        assert rows == [], f"dry-run wrote {model.__tablename__} rows"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_call_budget_cap_stops_run(db_session):
    """The global --max-calls cap bounds the run and reports next_from_date."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        max_calls=3,
        sleep=_MockClock(),
    )

    assert rc == 2
    assert fetch.await_count == 3
    assert any("call_budget_exhausted" in line for line in lines)
    summary = json.loads(lines[-1])
    assert summary["stop_reason"] == "call_budget_exhausted"
    assert summary["dart_calls"] == 3
    assert summary["days_processed"] == 3
    assert summary["failed"] == 0
    assert summary["next_from_date"] == WEEK[3].isoformat()
    assert summary["days_remaining"] == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_max_days_limits_run_and_reports_resume_point(db_session):
    """--max-days bounds one run; the JSON tells the desk where to resume."""
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[4],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        max_days=2,
        sleep=_MockClock(),
    )

    assert rc == 0
    assert fetch.await_count == 2
    summary = json.loads(lines[-1])
    assert summary["stop_reason"] == "max_days"
    assert summary["days_processed"] == 2
    assert summary["next_from_date"] == WEEK[2].isoformat()
    assert summary["days_remaining"] == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_non_transient_error_fails_day_without_retry(db_session):
    """Schema/parse faults are not retried: 1 call, failed day, run continues."""
    from app.services.market_events.dart_helpers import DartResponseSchemaError
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(
        side_effect=[
            DartResponseSchemaError("missing required columns: rcept_no"),
            [dict(DART_ROW)],
        ]
    )
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[1],
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_max_attempts=5,  # would retry 4x if this were transient — it is not
        sleep=_MockClock(),
    )

    assert rc == 2
    assert fetch.await_count == 2  # no retries on the schema error
    summary = json.loads(lines[-1])
    assert summary["failed"] == 1
    assert summary["succeeded"] == 1
    assert summary["dart_retries"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_retries_transient_then_succeeds(db_session):
    """--commit routes the paced/retried fetch through the ingestion path."""
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(
        side_effect=[
            ConnectionResetError(104, "Connection reset by peer"),
            [dict(DART_ROW)],
        ]
    )
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[3],
        to_date=WEEK[3],
        commit=True,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_base_seconds=1.0,
        rng=lambda: 0.5,
        sleep=_MockClock(),
    )

    assert rc == 0
    assert fetch.await_count == 2
    summary = json.loads(lines[-1])
    assert summary["succeeded"] == 1
    assert summary["dart_retries"] == 1

    partition = (
        await db_session.execute(
            select(MarketEventIngestionPartition).where(
                MarketEventIngestionPartition.source == "dart"
            )
        )
    ).scalar_one()
    assert partition.status == "succeeded"
    assert partition.event_count == 1

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "dart")
            )
        )
        .scalars()
        .all()
    )
    assert [e.source_event_id for e in events] == [DART_ROW["rcept_no"]]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_exhausted_retries_record_failed_partition_and_continue(
    db_session,
):
    """After retries are exhausted the day is recorded failed with the reason
    and the run continues to the next day (#1097 AC)."""
    from app.models.market_events import MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    async def fake_fetch(d: date):
        if d == WEEK[0]:
            raise ConnectionResetError(104, "Connection reset by peer")
        return [dict(DART_ROW)]

    fetch = AsyncMock(side_effect=fake_fetch)
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=WEEK[0],
        to_date=WEEK[1],
        commit=True,
        fetch_rows=fetch,
        emit=lines.append,
        pace_seconds=0.0,
        retry_max_attempts=2,
        retry_base_seconds=1.0,
        rng=lambda: 0.5,
        sleep=_MockClock(),
    )

    assert rc == 2
    assert fetch.await_count == 3  # 2 attempts on day 1 + 1 on day 2
    assert any(
        "status=failed" in line and "reset" in line.lower()
        for line in lines
        if WEEK[0].isoformat() in line
    )
    assert any(
        "status=succeeded" in line for line in lines if WEEK[1].isoformat() in line
    )
    summary = json.loads(lines[-1])
    assert summary["failed_dates"] == [WEEK[0].isoformat()]
    assert summary["stop_reason"] == "completed"  # 1 consecutive failure < 3

    partitions = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition)
                .where(MarketEventIngestionPartition.source == "dart")
                .order_by(MarketEventIngestionPartition.partition_date)
            )
        )
        .scalars()
        .all()
    )
    assert len(partitions) == 2
    assert partitions[0].status == "failed"
    assert "reset" in (partitions[0].last_error or "").lower()
    assert partitions[1].status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_main_passes_pacing_flags_to_run_backfill(db_session, monkeypatch):
    from scripts import backfill_dart_disclosures as cli

    captured: dict[str, object] = {}

    async def fake_run_backfill(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_backfill", fake_run_backfill)
    rc = await cli.main(
        [
            "--from-date",
            "2026-01-01",
            "--to-date",
            "2026-07-21",
            "--resume-from",
            "2026-01-19",
            "--max-days",
            "30",
            "--pace-seconds",
            "2.0",
            "--max-calls",
            "500",
            "--retry-max-attempts",
            "3",
            "--max-consecutive-failures",
            "5",
        ]
    )

    assert rc == 0
    assert captured["resume_from"] == date(2026, 1, 19)
    assert captured["max_days"] == 30
    assert captured["pace_seconds"] == 2.0
    assert captured["max_calls"] == 500
    assert captured["retry_max_attempts"] == 3
    assert captured["max_consecutive_failures"] == 5
