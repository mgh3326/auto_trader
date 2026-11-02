# tests/test_trade_retrospective_pnl_status.py
"""ROB-1488 — realized-P&L status classification for retrospectives.

Pure unit tests: no database, no network. They pin the invariant that a NULL
``realized_pnl`` is labeled honestly and that nothing invents a value.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.review import TradeRetrospective
from app.services.trade_journal.trade_retrospective_service import (
    PNL_STATUS_BUY_LEG,
    PNL_STATUS_CLASS,
    PNL_STATUS_MISSING_REALIZED_PNL,
    PNL_STATUS_NO_FILL_EVIDENCE,
    PNL_STATUS_NO_FILL_OUTCOME,
    PNL_STATUS_POSITION_INTAKE,
    PNL_STATUS_RECORDED,
    PNL_STATUS_RECORDED_OTHER_PNL,
    PNL_STATUS_UNKNOWN_SIDE,
    _build_pnl_coverage,
    build_retrospective_aggregate,
    classify_pnl_status,
    serialize_retrospective,
)

pytestmark = pytest.mark.unit


def _classify(**overrides):
    kwargs = {
        "outcome": "filled",
        "side": "sell",
        "realized_pnl": None,
        "fill_evidence_available": True,
    }
    kwargs.update(overrides)
    return classify_pnl_status(TradeRetrospective(**kwargs))


def test_recorded_wins_over_every_bucket():
    assert _classify(realized_pnl=Decimal("100")) == PNL_STATUS_RECORDED
    # A recorded value stays recorded even for a non-fill outcome or a buy leg.
    assert (
        _classify(outcome="rejected", realized_pnl=Decimal("0")) == PNL_STATUS_RECORDED
    )
    assert _classify(side="buy", realized_pnl=Decimal("-5")) == PNL_STATUS_RECORDED


def test_zero_pnl_is_recorded_not_missing():
    assert _classify(realized_pnl=Decimal("0")) == PNL_STATUS_RECORDED


@pytest.mark.parametrize(
    "field",
    [
        "pnl_pct",
        "total_pnl_krw",
        "fx_pnl_krw",
        "security_pnl_usd",
        "security_pnl_krw",
    ],
)
@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("2.5"), Decimal("-2.5")])
def test_other_recorded_pnl_is_not_missing(field, amount):
    row = _row(outcome="filled", side="sell", realized_pnl=None)
    setattr(row, field, amount)
    coverage = _build_pnl_coverage([row])
    assert coverage["recorded"] == 1
    assert coverage["missing"] == 0
    payload = serialize_retrospective(row)
    assert payload["pnl_status"] == "recorded_other_pnl"
    assert payload["realized_pnl"] is None
    assert payload[field] == float(amount)


def test_position_intake_is_not_an_execution_evidence_gap():
    row = _row(
        outcome="filled", side="buy", realized_pnl=None, fill_evidence_available=False
    )
    row.evidence_snapshot = {"retrospective_type": "intake"}
    assert serialize_retrospective(row)["pnl_status"] == "position_intake"
    coverage = _build_pnl_coverage([row])
    assert coverage["not_applicable"] == 1
    assert coverage["unknown"] == 0


def test_no_fill_evidence_bucket():
    assert (
        _classify(outcome="unfilled", fill_evidence_available=False)
        == PNL_STATUS_NO_FILL_EVIDENCE
    )
    # Even a "filled" row with no fill evidence cannot carry a realized P&L.
    assert (
        _classify(outcome="filled", fill_evidence_available=False)
        == PNL_STATUS_NO_FILL_EVIDENCE
    )


@pytest.mark.parametrize("outcome", ["unfilled", "rejected", "cancelled"])
def test_non_fill_outcome_is_not_applicable(outcome):
    assert _classify(outcome=outcome) == PNL_STATUS_NO_FILL_OUTCOME


@pytest.mark.parametrize("outcome", ["filled", "partially_filled"])
def test_filled_buy_leg_is_not_applicable(outcome):
    assert _classify(outcome=outcome, side="buy") == PNL_STATUS_BUY_LEG


def test_unknown_side_bucket():
    assert _classify(side=None) == PNL_STATUS_UNKNOWN_SIDE


@pytest.mark.parametrize("outcome", ["filled", "partially_filled"])
def test_filled_sell_without_pnl_is_missing(outcome):
    assert _classify(outcome=outcome, side="sell") == PNL_STATUS_MISSING_REALIZED_PNL


def test_every_status_has_a_class():
    statuses = {
        PNL_STATUS_RECORDED,
        PNL_STATUS_RECORDED_OTHER_PNL,
        PNL_STATUS_POSITION_INTAKE,
        PNL_STATUS_NO_FILL_EVIDENCE,
        PNL_STATUS_NO_FILL_OUTCOME,
        PNL_STATUS_BUY_LEG,
        PNL_STATUS_UNKNOWN_SIDE,
        PNL_STATUS_MISSING_REALIZED_PNL,
    }
    assert statuses == set(PNL_STATUS_CLASS)
    assert set(PNL_STATUS_CLASS.values()) == {
        "recorded",
        "not_applicable",
        "unknown",
        "missing",
    }


def test_only_filled_sell_is_actionable_missing():
    # Every other NULL bucket is structurally non-applicable or unknown; a
    # aggregate reader must never treat them as a loss or a zero.
    assert PNL_STATUS_CLASS[PNL_STATUS_MISSING_REALIZED_PNL] == "missing"
    assert PNL_STATUS_CLASS[PNL_STATUS_UNKNOWN_SIDE] == "unknown"
    assert PNL_STATUS_CLASS[PNL_STATUS_NO_FILL_EVIDENCE] == "unknown"
    for not_applicable in (
        PNL_STATUS_NO_FILL_OUTCOME,
        PNL_STATUS_BUY_LEG,
        PNL_STATUS_POSITION_INTAKE,
    ):
        assert PNL_STATUS_CLASS[not_applicable] == "not_applicable"


def _row(*, outcome, side, realized_pnl, fill_evidence_available=True):
    return TradeRetrospective(
        outcome=outcome,
        side=side,
        realized_pnl=realized_pnl,
        fill_evidence_available=fill_evidence_available,
    )


def test_pnl_coverage_empty_is_zeroed_not_invented():
    coverage = _build_pnl_coverage([])
    assert coverage["rows"] == 0
    assert coverage["recorded"] == 0
    assert coverage["not_applicable"] == 0
    assert coverage["unknown"] == 0
    assert coverage["missing"] == 0
    assert coverage["by_status"] == {}


def test_pnl_coverage_counts_by_class():
    rows = [
        _row(outcome="filled", side="sell", realized_pnl=Decimal("100")),
        _row(outcome="filled", side="sell", realized_pnl=None),
        _row(outcome="filled", side="buy", realized_pnl=None),
        _row(outcome="rejected", side="buy", realized_pnl=None),
        _row(outcome="unfilled", side="sell", realized_pnl=None),
        _row(
            outcome="filled",
            side="sell",
            realized_pnl=None,
            fill_evidence_available=False,
        ),
        _row(outcome="filled", side=None, realized_pnl=None),
    ]
    coverage = _build_pnl_coverage(rows)
    assert coverage["rows"] == 7
    assert coverage["recorded"] == 1
    assert coverage["not_applicable"] == 3  # buy_leg + 2 no_fill
    assert coverage["unknown"] == 2
    assert coverage["missing"] == 1
    assert coverage["by_status"] == {
        PNL_STATUS_RECORDED: 1,
        PNL_STATUS_MISSING_REALIZED_PNL: 1,
        PNL_STATUS_BUY_LEG: 1,
        PNL_STATUS_NO_FILL_OUTCOME: 2,
        PNL_STATUS_NO_FILL_EVIDENCE: 1,
        PNL_STATUS_UNKNOWN_SIDE: 1,
    }


@pytest.mark.asyncio
async def test_aggregate_uses_other_pnl_and_keeps_intake_excluded():
    recorded = _row(outcome="filled", side="sell", realized_pnl=None)
    recorded.pnl_pct = Decimal("2.5")
    recorded.total_pnl_krw = Decimal("1000")
    missing = _row(outcome="filled", side="sell", realized_pnl=None)
    intake = _row(
        outcome="filled", side="buy", realized_pnl=None, fill_evidence_available=False
    )
    intake.evidence_snapshot = {"retrospective_type": "intake"}
    db = AsyncMock()
    # SQL execution alone is replaced; exercise the real aggregate and classifier.
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = [
        recorded,
        missing,
        intake,
    ]
    payload = await build_retrospective_aggregate(db)
    assert payload["excluded_intake"] == 1
    assert payload["pnl_coverage"]["rows"] == 2
    assert payload["pnl_coverage"]["recorded"] == 1
    assert payload["pnl_coverage"]["missing"] == 1
    group = payload["groups"][0]
    assert group["pnl_status_counts"] == {
        "recorded_other_pnl": 1,
        "missing_realized_pnl": 1,
    }
    assert group["wins"] == 1
    assert group["win_rate_pct"] == 100.0
    assert group["total_pnl_krw_sum"] == 1000.0
    assert group["realized_pnl_sum"] == {}
    assert recorded.realized_pnl is None
