"""Closed range, source and producer rules of a backfill request (hk 6409)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services.market_events.backfill import policy
from app.services.market_events.backfill.policy import (
    BackfillRefused,
    build_request,
    producer_argv,
)
from scripts import (
    collect_sec_edgar,
    ingest_benzinga_calendars,
    ingest_market_events,
)

pytestmark = pytest.mark.unit

#: 12:00 UTC on 2026-11-20 is 11-20 in both KST and ET.
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def make(**over: Any) -> policy.BackfillRequest:
    args: dict[str, Any] = {
        "source": "dart",
        "market": "kr",
        "category": None,
        "symbols": [],
        "from_date": date(2026, 11, 1),
        "to_date": date(2026, 11, 20),
        "reason_code": "gap_fill",
        "reason": "outage",
        "requested_by": "operator",
        "now": NOW,
    }
    args.update(over)
    return build_request(**args)


def refused(**over: Any) -> str:
    with pytest.raises(BackfillRefused) as err:
        make(**over)
    return err.value.code


def test_a_31_day_window_is_the_maximum() -> None:
    start = date(2026, 10, 21)
    assert make(from_date=start, to_date=date(2026, 11, 20))
    assert refused(from_date=start - timedelta(days=1)) == "window_too_long"
    assert refused(from_date=date(2026, 11, 2), to_date=date(2026, 11, 1)) == (
        "window_reversed"
    )


def test_future_dates_are_judged_in_the_market_zone() -> None:
    assert refused(to_date=date(2026, 11, 21)) == "future_date"
    # 16:00 UTC on 11-20 is already 11-21 in Seoul, still 11-20 in New York.
    late = datetime(2026, 11, 20, 16, tzinfo=UTC)
    assert make(to_date=date(2026, 11, 21), now=late)
    assert (
        refused(source="finnhub", market="us", to_date=date(2026, 11, 21), now=late)
        == "future_date"
    )


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"source": "naver_disclosure"}, "source_has_no_date_window"),
        ({"source": "upbit_notice", "market": "crypto"}, "source_has_no_date_window"),
        ({"source": "bithumb_notice", "market": "crypto"}, "source_has_no_date_window"),
        ({"source": "company_ir", "market": "us"}, "unsupported_source"),
        ({"market": "us"}, "unsupported_market"),
        ({"category": "earnings"}, "unsupported_category"),
        ({"symbols": ["005930"]}, "symbols_not_supported"),
        ({"source": "sec", "market": "us"}, "symbols_required"),
        (
            {"source": "sec", "market": "us", "symbols": ["--commit"]},
            "invalid_symbol",
        ),
        (
            {
                "source": "sec",
                "market": "us",
                "symbols": [f"S{i}" for i in range(21)],
            },
            "too_many_symbols",
        ),
        ({"source": "benzinga", "market": "us"}, "category_required"),
        (
            {"source": "benzinga", "market": "us", "category": "nope"},
            "unsupported_category",
        ),
        (
            {"source": "benzinga", "market": "us", "category": "short_interest"},
            "symbols_required",
        ),
        ({"reason_code": "because"}, "invalid_reason_code"),
        ({"reason": "   "}, "invalid_reason"),
        ({"reason": "x" * 501}, "invalid_reason"),
        ({"requested_by": ""}, "invalid_requested_by"),
    ],
)
def test_each_violation_has_one_closed_refusal(over: dict, code: str) -> None:
    assert refused(**over) == code


def test_a_benzinga_lookback_counts_against_the_window() -> None:
    base = {"source": "benzinga", "market": "us", "category": "congress_trade"}
    lookback = policy.CALENDAR_ENDPOINTS["congress_trade"].lookback_days
    assert lookback == 30
    day = date(2026, 11, 20)
    assert make(**base, from_date=day, to_date=day)
    assert refused(**base, from_date=day - timedelta(days=1), to_date=day) == (
        "window_too_long"
    )


def test_symbols_are_normalized_sorted_and_deduplicated() -> None:
    request = make(source="sec", market="us", symbols=["nvda", "brk-b", "NVDA"])
    assert request.symbols == ("BRK.B", "NVDA")


def test_the_hash_identifies_the_work_not_the_wording() -> None:
    base = make()
    same = make(reason="different words", requested_by="someone-else")
    assert base.request_hash == same.request_hash
    assert base.request_ref == f"bfr-{base.request_hash[:20]}"
    for over in (
        {"reason_code": "retry_after_failure"},
        {"to_date": date(2026, 11, 19)},
        {"source": "finnhub", "market": "us"},
    ):
        assert make(**over).request_hash != base.request_hash
    sec = make(source="sec", market="us", symbols=["NVDA"])
    assert (
        sec.request_hash
        != make(source="sec", market="us", symbols=["AMD"]).request_hash
    )


@pytest.mark.parametrize(
    ("over", "module", "parse"),
    [
        ({}, "scripts.ingest_market_events", ingest_market_events.parse_args),
        (
            {"source": "finnhub", "market": "us"},
            "scripts.ingest_market_events",
            ingest_market_events.parse_args,
        ),
        (
            {"source": "sec", "market": "us", "symbols": ["NVDA", "AMD"]},
            "scripts.collect_sec_edgar",
            collect_sec_edgar.parse_args,
        ),
        (
            {"source": "benzinga", "market": "us", "category": "guidance"},
            "scripts.ingest_benzinga_calendars",
            ingest_benzinga_calendars.parse_args,
        ),
        (
            {
                "source": "benzinga",
                "market": "us",
                "category": "short_interest",
                "symbols": ["NVDA", "AMD"],
            },
            "scripts.ingest_benzinga_calendars",
            ingest_benzinga_calendars.parse_args,
        ),
    ],
)
def test_only_the_existing_collectors_run_with_the_requested_window(
    over: dict, module: str, parse: Any
) -> None:
    request = make(**over)
    name, *argv = producer_argv(request)
    assert name == module
    parsed = parse(argv)
    assert (parsed.from_date, parsed.to_date) == (
        request.from_date,
        request.to_date,
    )
    if request.source in {"sec", "benzinga"}:
        assert parsed.commit is True
    else:
        assert parsed.dry_run is False and parsed.force is False
    if request.source == "sec":
        assert tuple(parsed.symbols) == request.symbols
    if request.source == "benzinga":
        assert parsed.categories == [request.category]
        assert (parsed.symbols or "") == ",".join(request.symbols)
        assert parsed.force is False


def test_the_quota_day_is_the_utc_day() -> None:
    since, until = policy.utc_day_bounds(datetime(2026, 11, 20, 23, 59, tzinfo=UTC))
    assert since == datetime(2026, 11, 20, tzinfo=UTC)
    assert until == datetime(2026, 11, 21, tzinfo=UTC)


def test_refusal_reasons_are_a_closed_vocabulary() -> None:
    with pytest.raises(AssertionError, match="unlisted refusal code"):
        BackfillRefused("because", "free text")
    root = Path(__file__).resolve().parents[3]
    sources = [
        *(root / "app/services/market_events/backfill").glob("*.py"),
        root / "scripts/request_market_event_backfill.py",
    ]
    literal = re.compile(r'(?:BackfillRefused\(\s*|"code":\s*|code=)"([a-z_]+)"')
    used = {code for path in sources for code in literal.findall(path.read_text())}
    assert "source_has_no_date_window" in used
    assert used <= policy.REFUSAL_CODES
