"""ROB-1488 — tests for the read-only investment_report writer diagnostic.

Pure classification + a static guard on the module source. No DB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.diagnose_investment_reports import (
    STALL_THRESHOLD_DAYS,
    watch_linkage_verdict,
    writer_health,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SOURCE = Path("scripts/diagnose_investment_reports.py").read_text(encoding="utf-8")


def test_no_report_ever_written_is_never() -> None:
    assert writer_health(None, now=NOW) == {"status": "never", "stale_days": None}


def test_writer_within_threshold_is_ok() -> None:
    last = NOW - timedelta(days=STALL_THRESHOLD_DAYS)
    assert writer_health(last, now=NOW) == {
        "status": "ok",
        "stale_days": STALL_THRESHOLD_DAYS,
    }


def test_writer_one_second_past_threshold_is_stale() -> None:
    last = NOW - timedelta(days=STALL_THRESHOLD_DAYS, seconds=1)
    result = writer_health(last, now=NOW)
    assert result["status"] == "stale"
    assert result["stale_days"] == STALL_THRESHOLD_DAYS


def test_long_stall_reports_measured_age() -> None:
    # Regression shape: the real 2026-07-28 stall measured on 2026-11-02.
    last = datetime(2026, 7, 28, 0, 32, 3, tzinfo=UTC)
    result = writer_health(last, now=NOW)
    assert result["status"] == "stale"
    assert result["stale_days"] == 97


def test_linkage_verdict_separates_watch_use_from_report_writes() -> None:
    result = watch_linkage_verdict(
        lookback_null=29,
        lookback_resolved=0,
        lookback_dangling=0,
        active_dangling=0,
    )
    assert result["verdict"] == "no_report_linked_events"
    assert result["recent_report_linked_events_observed"] is False


def test_linkage_verdict_reports_dependency_when_events_are_linked() -> None:
    result = watch_linkage_verdict(
        lookback_null=3,
        lookback_resolved=2,
        lookback_dangling=0,
        active_dangling=0,
    )
    assert result["verdict"] == "report_linked_events"
    assert result["recent_report_linked_events_observed"] is True


def test_linkage_verdict_without_events() -> None:
    result = watch_linkage_verdict(
        lookback_null=0,
        lookback_resolved=0,
        lookback_dangling=0,
        active_dangling=4,
    )
    assert result["verdict"] == "no_recent_events"
    assert result["active_dangling_alerts"] == 4


def test_module_is_read_only_and_never_touches_orders() -> None:
    assert "SET TRANSACTION READ ONLY" in SOURCE
    for forbidden in (
        "INSERT INTO",
        "DELETE FROM",
        "UPDATE review.",
        ".commit(",
        ".flush(",
        "app.services.brokers",
        "order_proposal",
        "fastmcp",
    ):
        assert forbidden not in SOURCE, f"diagnostic must stay read-only: {forbidden}"


def test_stale_render_distinguishes_entrypoints_from_scheduled_producers() -> None:
    from scripts.diagnose_investment_reports import render_text

    table = {
        "status": "stale",
        "stale_days": 97,
        "rows": 68,
        "last_created": None,
        "rows_30d": 0,
    }
    links = {name: {"rows": 0} for name in ("null", "resolved", "dangling")}
    payload = {
        "as_of": NOW.isoformat(),
        "stall_threshold_days": 14,
        "watch_lookback_days": 30,
        "report_writer": table,
        "report_items": table,
        "report_item_decisions": table,
        "report_status_counts": {},
        "report_profiles": [],
        "writer_ok": False,
        "watch_alerts": {**table, "active": links},
        "watch_events": {**table, "last_delivered": None, "lookback_by_link": links},
        "watch_linkage": {
            "verdict": "no_report_linked_events",
            "active_dangling_alerts": 0,
        },
    }
    output = render_text(payload)
    assert "no in-repo scheduled producer" in output
    assert "in-repo writer entrypoints" in output
    assert "Writer entrypoints are out-of-process callers" not in output
