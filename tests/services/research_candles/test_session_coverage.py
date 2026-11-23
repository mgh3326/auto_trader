"""hk 6145: coverage math + read-only guarantees of the diagnostic."""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.services.research_candles import session_coverage as cov

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "diagnose_session_coverage.py"
KR_DATE = date(2026, 11, 24)


def _utc_kst(hour: int, minute: int) -> datetime:
    # KST = UTC+9 and the fixture day is not DST sensitive.
    return datetime(2026, 11, 24, hour, minute, tzinfo=UTC) - timedelta(hours=9)


def _row(
    minute: datetime,
    extra_s: int,
    *,
    symbol="005930",
    venue="KRX",
    segment="KRX_REGULAR",
):
    return cov.StoredMinute(
        symbol=symbol,
        venue=venue,
        session_segment=segment,
        time_utc=minute,
        retrieved_at=minute + timedelta(minutes=1, seconds=extra_s),
    )


def _regular_minutes():
    start = _utc_kst(9, 0)
    return [start + timedelta(minutes=i) for i in range(390)]


def test_expected_minutes_per_segment_match_the_windows() -> None:
    krx = cov.expected_minutes_by_segment("kr", "KRX", KR_DATE)
    ntx = cov.expected_minutes_by_segment("kr", "NTX", KR_DATE)
    assert {k: len(v) for k, v in krx.items()} == {"KRX_REGULAR": 390, "KRX_AFTER": 240}
    assert {k: len(v) for k, v in ntx.items()} == {
        "NXT_PRE": 50,
        "NXT_OVERLAP": 390,
        "NXT_POST": 270,
    }
    us = cov.expected_minutes_by_segment("us", "NASD", date(2026, 12, 15))
    assert {k: len(v) for k, v in us.items()} == {
        "US_PRE": 330,
        "US_REGULAR": 390,
        "US_POST": 240,
    }


def test_missing_minutes_and_latency_percentiles_on_a_fixture() -> None:
    minutes = _regular_minutes()
    skipped = {minutes[60], minutes[61], minutes[62]}  # 10:00, 10:01, 10:02
    rows = []
    stored = [m for m in minutes if m not in skipped]
    for index, minute in enumerate(stored):
        # last 30 stored rows are 200 s late, the rest 20 s
        rows.append(_row(minute, 200 if index >= len(stored) - 30 else 20))
    report = cov.coverage_report(
        rows,
        market="kr",
        session_date=KR_DATE,
        as_of=_utc_kst(21, 0),
        requested_symbols=["005930", "000660"],
    )
    regular = next(
        e for e in report["entries"] if e["session_segment"] == "KRX_REGULAR"
    )
    assert regular["rows"] == 387
    assert regular["expected_minutes"] == 390
    assert regular["missing_minutes"] == 3
    assert regular["latency_p50_s"] == 20.0
    assert regular["latency_p95_s"] == 200.0
    after = next(e for e in report["entries"] if e["session_segment"] == "KRX_AFTER")
    assert (after["rows"], after["expected_minutes"], after["missing_minutes"]) == (
        0,
        240,
        240,
    )
    assert after["latency_p50_s"] is None
    assert report["symbols_without_rows"] == ["000660"]


def test_only_closed_minutes_are_expected_at_as_of() -> None:
    rows = [_row(m, 5) for m in _regular_minutes()[:59]]
    report = cov.coverage_report(
        rows, market="kr", session_date=KR_DATE, as_of=_utc_kst(10, 0)
    )
    regular = next(
        e for e in report["entries"] if e["session_segment"] == "KRX_REGULAR"
    )
    # 09:58 ends 09:59:00 (+10 s grace) <= 10:00:00; 09:59 ends 10:00:00 (+10 s) > as_of.
    assert regular["expected_minutes"] == 59 and regular["missing_minutes"] == 0


def test_rows_in_unknown_segment_are_reported_with_zero_expected() -> None:
    odd = _utc_kst(8, 55)
    report = cov.coverage_report(
        [_row(odd, 0, segment="UNKNOWN", venue="NTX")],
        market="kr",
        session_date=KR_DATE,
        as_of=_utc_kst(21, 0),
    )
    unknown = next(e for e in report["entries"] if e["session_segment"] == "UNKNOWN")
    assert (
        unknown["rows"],
        unknown["expected_minutes"],
        unknown["missing_minutes"],
    ) == (1, 0, 0)


def test_percentile_interpolates() -> None:
    assert cov.percentile([], 0.5) is None
    assert cov.percentile([4.0], 0.95) == 4.0
    assert cov.percentile([0.0, 10.0], 0.5) == 5.0


def test_backfill_batches_are_recognised() -> None:
    assert cov.is_backfill_batch("promote-20261124T010101-KIS-KRX")
    assert cov.is_backfill_batch("session-minutes-backfill-kr-1")
    assert not cov.is_backfill_batch("session-minutes-kr-20261124T010101Z-ab12cd34")
    assert cov.BACKFILL_BATCH_LIKE == ("%backfill%", "promote-%")


# ------------------------------------------------------------ static guard ---


def _string_literals(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_diagnostic_sets_read_only_timeout_and_issues_select_only() -> None:
    source = SCRIPT.read_text()
    literals = _string_literals(ast.parse(source))
    assert "SET TRANSACTION READ ONLY" in literals
    assert any("statement_timeout = '8s'" in s for s in literals)
    # Every SQL-looking literal is a SELECT or one of the two SET statements.
    sql = [
        s
        for s in literals
        if re.match(
            r"\s*(SELECT|SET|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|GRANT)\b",
            s,
            re.I,
        )
    ]
    assert sql
    for statement in sql:
        assert re.match(
            r"\s*(SELECT|SET TRANSACTION READ ONLY|SET LOCAL statement_timeout)",
            statement,
            re.I,
        ), statement
    joined = " ".join(literals).upper()
    for verb in (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "TRUNCATE ",
        "CREATE ",
        "DROP ",
        "ALTER ",
    ):
        assert verb not in joined
    # READ ONLY is set before the first data read inside the transaction.
    assert source.index("SET TRANSACTION READ ONLY") < source.index(
        "conn.execute(\n                statement"
    )


def test_diagnostic_reads_only_research_minute_tables() -> None:
    literals = " ".join(_string_literals(ast.parse(SCRIPT.read_text())))
    assert "research.kr_candles_1m" in literals and "research.us_candles_1m" in literals
    assert "public." not in literals


def test_value_null_count_and_ratio_per_segment() -> None:
    minutes = _regular_minutes()[:10]
    rows = [
        cov.StoredMinute(
            symbol="005930",
            venue="KRX",
            session_segment="KRX_REGULAR",
            time_utc=m,
            retrieved_at=m + timedelta(minutes=1, seconds=5),
            value_is_null=index < 3,
        )
        for index, m in enumerate(minutes)
    ]
    report = cov.coverage_report(
        rows, market="kr", session_date=KR_DATE, as_of=_utc_kst(21, 0)
    )
    regular = next(
        e for e in report["entries"] if e["session_segment"] == "KRX_REGULAR"
    )
    assert (regular["rows"], regular["value_null"], regular["value_null_ratio"]) == (
        10,
        3,
        0.3,
    )
    after = next(e for e in report["entries"] if e["session_segment"] == "KRX_AFTER")
    assert (after["value_null"], after["value_null_ratio"]) == (0, None)


def test_diagnostic_selects_the_value_null_flag() -> None:
    literals = " ".join(_string_literals(ast.parse(SCRIPT.read_text())))
    assert literals.count("(value IS NULL) AS value_is_null") == 2
