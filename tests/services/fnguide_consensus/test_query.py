"""hk 6508: read model — revision math and the latest-consensus shape (pure)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.fnguide_consensus.query import (
    PreviousDay,
    SnapshotMeta,
    ValueMeta,
    build_consensus_view,
)
from tests.services.fnguide_consensus._helpers import FETCHED, OBSERVED, parse_fixture

pytestmark = pytest.mark.unit


def _meta(
    symbol: str, status: str = "parsed", reason: str | None = None
) -> SnapshotMeta:
    return SnapshotMeta(
        symbol=symbol,
        observed_date=OBSERVED,
        fetched_at=FETCHED,
        source_url=f"https://wcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd={symbol}",
        company_name=None,
        status=status,
        reason=reason,
    )


def _consensus_values(symbol: str) -> list[ValueMeta]:
    parsed = parse_fixture(f"{symbol}_consensus.html", symbol=symbol)
    return [
        ValueMeta(
            item_name=v.item_name,
            unit=v.unit,
            digit=v.digit,
            column_key=v.column_key,
            column_label=v.column_label,
            value_text=v.value_text,
            value_reason=v.value_reason,
        )
        for v in parsed.values
        if v.section == "consensus"
    ]


def _target(view: dict, item: str) -> dict:
    return view["revision"][item]


def test_target_price_revision_against_each_earlier_column() -> None:
    view = build_consensus_view(_meta("005930"), _consensus_values("005930"), None)

    target = _target(view, "목표주가")
    assert target["current"]["value"] == "458181.82"
    by_key = {entry["column_key"]: entry for entry in target["vs_earlier"]}
    assert set(by_key) == {"VAL2", "VAL3", "VAL4", "VAL5"}
    assert by_key["VAL2"]["earlier_value"] == "495227.27"
    assert by_key["VAL2"]["delta"] == "-37045.45"
    assert by_key["VAL2"]["column_label"] == "1개월전"
    assert by_key["VAL2"]["delta_pct"] is not None
    assert by_key["VAL2"]["delta_pct"].startswith("-7.4")
    assert target["vs_previous_day"] is None


def test_previous_stored_day_is_compared_and_its_date_is_shown() -> None:
    previous = PreviousDay(
        observed_date=date(2026, 11, 24),
        values=[
            ValueMeta("목표주가", "(원)", 0, "VAL1", "당기순이익", "400000.00", None),
            ValueMeta("EPS", "(원)", 0, "VAL1", "당기순이익", None, "missing"),
        ],
    )
    view = build_consensus_view(_meta("005930"), _consensus_values("005930"), previous)

    prev = _target(view, "목표주가")["vs_previous_day"]
    assert prev["observed_date"] == "2026-11-24"
    assert prev["value"] == "400000.00"
    assert prev["delta"] == "58181.82"

    eps_prev = _target(view, "EPS")["vs_previous_day"]
    assert eps_prev["delta"] is None
    assert eps_prev["reason"] == "missing"


def test_zero_base_gives_no_percentage_with_a_reason() -> None:
    values = [
        ValueMeta("목표주가", None, 0, "VAL1", "now", "100.00", None),
        ValueMeta("목표주가", None, 0, "VAL2", "1개월전", "0", None),
    ]
    view = build_consensus_view(_meta("000001"), values, None)
    entry = _target(view, "목표주가")["vs_earlier"][0]
    assert entry["delta"] == "100.00"
    assert entry["delta_pct"] is None


def test_missing_current_value_is_not_treated_as_zero() -> None:
    values = [
        ValueMeta("목표주가", None, 0, "VAL1", "now", None, "missing"),
        ValueMeta("목표주가", None, 0, "VAL2", "1개월전", "495227.27", None),
    ]
    view = build_consensus_view(_meta("000001"), values, None)
    target = _target(view, "목표주가")
    assert target["current"]["value"] is None
    assert target["vs_earlier"][0]["reason"] == "current_missing"
    assert target["vs_earlier"][0]["delta"] is None


def test_no_consensus_values_yields_a_reason_and_no_revision() -> None:
    view = build_consensus_view(
        _meta("028300", status="parsed", reason="all_consensus_values_null"),
        _consensus_values("028300"),
        None,
    )
    assert view["reason"] == "all_consensus_values_null"
    assert view["items"] == []
    assert view["revision"] == {}
