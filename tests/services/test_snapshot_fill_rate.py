"""Fill-rate tally and dry-run summary line (hk 5752)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.snapshot_fill_rate import (
    FILL_RATE_LINE_PREFIX,
    FillRateTally,
    format_fill_rate_line,
)


@dataclass
class _Row:
    close: Decimal | None
    volume: int | None


def test_tally_counts_non_null_per_field_across_batches() -> None:
    tally = FillRateTally(("close", "volume"))
    tally.add([_Row(Decimal("1"), 10), _Row(None, 20)])
    tally.add([_Row(Decimal("3"), None)])

    summary = tally.summary()

    assert summary["rows"] == 3
    assert summary["fields"]["close"] == {"non_null": 2, "share": 0.6667}
    assert summary["fields"]["volume"] == {"non_null": 2, "share": 0.6667}


def test_missing_attribute_counts_as_null() -> None:
    tally = FillRateTally(("close", "no_such_field"))
    tally.add([_Row(Decimal("1"), 1)])

    assert tally.summary()["fields"]["no_such_field"] == {
        "non_null": 0,
        "share": 0.0,
    }


def test_empty_tally_reports_none_share_not_zero() -> None:
    summary = FillRateTally(("close",)).summary()

    assert summary == {
        "rows": 0,
        "fields": {"close": {"non_null": 0, "share": None}},
    }


def test_duplicate_field_names_are_rejected() -> None:
    with pytest.raises(ValueError):
        FillRateTally(("close", "close"))


def test_summary_line_is_one_parseable_json_object() -> None:
    tally = FillRateTally(("close",))
    tally.add([_Row(Decimal("1"), 1)])

    line = format_fill_rate_line("invest_screener_snapshots", tally.summary())

    assert "\n" not in line
    assert line.startswith(FILL_RATE_LINE_PREFIX)
    payload = json.loads(line.removeprefix(FILL_RATE_LINE_PREFIX))
    assert payload == {
        "fields": {"close": {"non_null": 1, "share": 1.0}},
        "job": "invest_screener_snapshots",
        "rows": 1,
    }


def test_consensus_cli_prints_the_fill_rate_line(capsys) -> None:
    from scripts import build_analyst_consensus_snapshots as cli

    class _Result:
        market = "kr"
        symbols_resolved = 1
        snapshots_built = 1
        committed = False
        batches = 1
        snapshot_date_distribution: dict = {}
        idempotency: dict = {}
        samples: tuple = ()
        warnings: tuple = ()
        fill_rate = FillRateTally(("target_mean",)).summary()

    cli._print_result(_Result())

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith(FILL_RATE_LINE_PREFIX)]
    assert len(lines) == 1
    assert json.loads(lines[0].removeprefix(FILL_RATE_LINE_PREFIX))["job"] == (
        "analyst_consensus_snapshots"
    )


def test_financial_cli_prints_nothing_when_no_fill_rate(capsys) -> None:
    from scripts import build_financial_fundamentals_snapshots as cli

    class _Result:
        market = "kr"
        projected_requests = None
        symbols_resolved = 0
        snapshots_built = 0
        committed = False
        idempotency: dict = {}
        samples: tuple = ()
        warnings: tuple = ()
        fill_rate: dict = {}

    cli._print_result(_Result())

    assert FILL_RATE_LINE_PREFIX not in capsys.readouterr().out
