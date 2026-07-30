"""ROB-1130: the preflight broker snapshot must be fail-closed.

These tests live in their own module (not in
``tests/services/test_alpaca_paper_anomaly_checks.py``) because ROB-1129 is
editing that file in parallel; the assertions here only cover snapshot-level
evidence, which is ROB-1130's scope.

Covered holes:

* the snapshot is absent / empty-but-unattested / stale / future / unparseable,
* the snapshot container is the response envelope instead of the row list,
* the snapshot is a fresh, well-formed read of the *other* paper account.

In every case a new-cycle gate must block instead of reporting ``positions=0``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.alpaca_paper_anomaly_checks import (
    build_paper_execution_preflight_report,
)

NOW = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
UBER_POSITION = {
    "asset_id": "uber-asset",
    "symbol": "UBER",
    "qty": "1",
    "avg_entry_price": "69.65",
    "side": "long",
}


def _check_ids(report):
    return {a.check_id for a in report.anomalies}


def _finding(report, check_id):
    return next(a for a in report.anomalies if a.check_id == check_id)


def _report(**overrides):
    """Gate-shaped call: an order-authorizing preflight declares its account."""
    kwargs = {
        "ledger_rows": [],
        "expected_account_mode": "alpaca_paper",
        "now": NOW,
    }
    kwargs.update(overrides)
    return build_paper_execution_preflight_report(**kwargs)


def _attested(**overrides):
    """A freshly fetched, fully attested snapshot of the gated account."""
    kwargs = {
        "positions": [],
        "open_orders": [],
        "positions_fetched_at": NOW,
        "open_orders_fetched_at": NOW,
        "snapshot_account_mode": "alpaca_paper",
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# (A) the snapshot itself is absent or unattested
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_absent_snapshot_blocks_and_reports_no_position_count():
    report = _report()

    assert report.should_block is True
    assert report.status == "blocked"
    assert "broker_snapshot_unverified" in _check_ids(report)
    # The false-assurance signal ROB-1130 was filed for: "positions: 0".
    assert "positions" not in report.counts
    assert report.broker_snapshot["positions"] == {
        "provided": False,
        "count": None,
        "fetched_at": None,
        "verified": False,
        "reason": "snapshot_missing",
    }


@pytest.mark.unit
def test_empty_snapshot_without_attestation_blocks():
    report = _report(positions=[], open_orders=[])

    assert report.should_block is True
    assert _finding(report, "broker_snapshot_unverified").details["reasons"] == {
        "positions": "snapshot_not_attested",
        "open_orders": "snapshot_not_attested",
    }
    assert "positions" not in report.counts


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fetched_at", "expected_reason"),
    [
        (NOW - timedelta(minutes=30), "snapshot_stale"),
        (NOW + timedelta(minutes=30), "snapshot_attestation_in_future"),
        ("not-a-timestamp", "snapshot_attestation_unparseable"),
        ("", "snapshot_not_attested"),
    ],
)
def test_untrustworthy_attestation_blocks(fetched_at, expected_reason):
    report = _report(
        **_attested(positions_fetched_at=fetched_at, open_orders_fetched_at=fetched_at)
    )

    assert report.should_block is True
    assert _finding(report, "broker_snapshot_unverified").details["reasons"] == {
        "positions": expected_reason,
        "open_orders": expected_reason,
    }
    assert "positions" not in report.counts


# ---------------------------------------------------------------------------
# (A') "no snapshot" vs "genuinely zero positions" stay distinguishable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_genuinely_queried_flat_account_passes():
    report = _report(**_attested())

    assert report.should_block is False
    assert report.status == "pass"
    assert report.counts["positions"] == 0
    assert report.counts["open_orders"] == 0
    assert report.broker_snapshot["positions"]["verified"] is True
    assert report.broker_snapshot["account"]["verified"] is True
    assert _check_ids(report) == {"preflight_clean"}


@pytest.mark.unit
def test_absent_and_zero_position_snapshots_are_not_the_same_state():
    absent = _report()
    queried_zero = _report(**_attested())

    assert absent.counts.get("positions") is None
    assert queried_zero.counts["positions"] == 0
    assert absent.broker_snapshot["positions"]["provided"] is False
    assert queried_zero.broker_snapshot["positions"]["provided"] is True
    assert absent.should_block is not queried_zero.should_block


@pytest.mark.unit
def test_held_position_in_a_correct_snapshot_blocks_the_new_cycle():
    report = _report(**_attested(positions=[UBER_POSITION]))

    assert report.should_block is True
    assert report.counts["positions"] == 1
    assert "residual_position_exists" in _check_ids(report)
    # The holding must be reported as a holding, not as an unverified snapshot.
    assert "broker_snapshot_unverified" not in _check_ids(report)


# ---------------------------------------------------------------------------
# (A'') the snapshot container is not a row list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_response_envelope_instead_of_row_list_blocks():
    """``positions=<whole MCP response>`` must not normalize to a flat account."""
    envelope = {
        "success": True,
        "account_mode": "alpaca_paper",
        "count": 1,
        "positions": [UBER_POSITION],
    }

    report = _report(**_attested(positions=envelope))

    assert report.should_block is True
    reasons = _finding(report, "broker_snapshot_unverified").details["reasons"]
    assert reasons["positions"] == "snapshot_container_not_a_row_list"
    assert "positions" not in report.counts
    assert report.broker_snapshot["positions"]["count"] is None


@pytest.mark.unit
def test_empty_mapping_snapshot_blocks():
    report = _report(**_attested(positions={}, open_orders={}))

    assert report.should_block is True
    assert _finding(report, "broker_snapshot_unverified").details["reasons"] == {
        "positions": "snapshot_container_not_a_row_list",
        "open_orders": "snapshot_container_not_a_row_list",
    }


# ---------------------------------------------------------------------------
# (A''') the snapshot belongs to another paper account
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unattested_account_identity_blocks():
    report = _report(**_attested(snapshot_account_mode=None))

    assert report.should_block is True
    finding = _finding(report, "broker_snapshot_unverified")
    assert finding.details["account"] == {
        "expected": "alpaca_paper",
        "attested": None,
        "verified": False,
        "reason": "snapshot_account_unattested",
    }
    assert finding.details["reasons"] == {
        "positions": "snapshot_account_unattested",
        "open_orders": "snapshot_account_unattested",
    }
    assert "positions" not in report.counts
    assert (
        "account identity (account_mode echoed by those reads)"
        in (finding.details["required_reads"])
    )


@pytest.mark.unit
def test_other_paper_accounts_flat_snapshot_does_not_clear_this_account():
    """The lab account being flat says nothing about the default account."""
    report = _report(**_attested(snapshot_account_mode="alpaca_paper_lab"))

    assert report.should_block is True
    assert _finding(report, "broker_snapshot_unverified").details["account"] == {
        "expected": "alpaca_paper",
        "attested": "alpaca_paper_lab",
        "verified": False,
        "reason": "snapshot_account_mismatch",
    }
    assert "positions" not in report.counts


@pytest.mark.unit
def test_account_attestation_is_case_and_whitespace_insensitive():
    report = _report(**_attested(snapshot_account_mode="  Alpaca_Paper "))

    assert report.should_block is False
    assert report.broker_snapshot["account"]["verified"] is True


@pytest.mark.unit
def test_lab_gate_accepts_only_the_lab_snapshot():
    blocked = build_paper_execution_preflight_report(
        ledger_rows=[],
        expected_account_mode="alpaca_paper_lab",
        now=NOW,
        **_attested(snapshot_account_mode="alpaca_paper"),
    )
    passed = build_paper_execution_preflight_report(
        ledger_rows=[],
        expected_account_mode="alpaca_paper_lab",
        now=NOW,
        **_attested(snapshot_account_mode="alpaca_paper_lab"),
    )

    assert blocked.should_block is True
    assert blocked.broker_snapshot["account"]["reason"] == "snapshot_account_mismatch"
    assert passed.should_block is False


# ---------------------------------------------------------------------------
# The snapshot blocker cannot be downgraded, and audit callers are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"positions": {}, "open_orders": {}},
        {"snapshot_account_mode": "alpaca_paper_lab"},
    ],
)
def test_legacy_flag_never_downgrades_a_snapshot_blocker(overrides):
    kwargs = _attested(**overrides) if overrides else {}
    report = _report(legacy_cycle_blockers_as_warnings=True, **kwargs)

    assert report.should_block is True
    assert _finding(report, "broker_snapshot_unverified").severity == "block"


@pytest.mark.unit
def test_audit_callers_without_a_declared_account_are_unchanged():
    """``snapshot_evidence_required=False`` is the audit path (roundtrip report)."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[],
        open_orders=[],
        positions=[],
        snapshot_evidence_required=False,
        now=NOW,
    )

    assert report.should_block is False
    assert report.broker_snapshot["account"]["reason"] == "account_scope_not_declared"
    assert report.counts["positions"] == 0


@pytest.mark.unit
def test_unverified_account_snapshot_is_not_used_as_open_position_evidence():
    """A wrong-account snapshot must not settle a buy leg's missing sell either.

    ROB-1129 uses the position snapshot to tell a still-held buy apart from a
    buy whose sell was never recorded. That evidence path must not accept a
    snapshot this gate could not attribute to the gated account.
    """
    buy = SimpleNamespace(
        client_order_id="uber-buy-1",
        lifecycle_correlation_id="corr-uber",
        side="buy",
        lifecycle_state="filled",
        order_status="filled",
        execution_symbol="UBER",
        signal_symbol="UBER",
        filled_qty="1",
        position_snapshot={"qty": "1"},
        preview_payload={},
        validation_summary={},
        raw_responses={},
        created_at=NOW - timedelta(minutes=10),
    )

    report = _report(
        ledger_rows=[buy],
        **_attested(snapshot_account_mode="alpaca_paper_lab"),
    )

    assert report.should_block is True
    assert report.broker_snapshot["positions"]["verified"] is False
