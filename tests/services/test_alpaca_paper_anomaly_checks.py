"""Tests for read-only Alpaca Paper execution anomaly checks (ROB-93)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.alpaca_paper_anomaly_checks import (
    STALE_PREVIEW_CLEANUP_ACTION,
    STALE_PREVIEW_CLEANUP_REQUIRED_STATE,
    PaperExecutionAnomalySeverity,
    build_paper_execution_preflight_report,
)


def _row(**kwargs):
    defaults = {
        "client_order_id": "rob93-buy-001",
        "lifecycle_correlation_id": "corr-btc",
        "side": "buy",
        "lifecycle_state": "filled",
        "order_status": "filled",
        "execution_symbol": "BTCUSD",
        "signal_symbol": "KRW-BTC",
        "filled_qty": "0.001",
        "position_snapshot": {"qty": "0"},
        "preview_payload": {},
        "validation_summary": {},
        "raw_responses": {},
        "created_at": datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _check_ids(report):
    return {a.check_id for a in report.anomalies}


def _verified_snapshot(positions=None, open_orders=None, now=None):
    """Kwargs for a freshly fetched, attested broker snapshot (ROB-1130).

    The preflight gate is fail-closed: an empty list only counts as evidence of
    an empty account when the caller also states when it was fetched.
    """
    fetched_at = now or datetime.now(UTC)
    return {
        "positions": list(positions or []),
        "open_orders": list(open_orders or []),
        "positions_fetched_at": fetched_at,
        "open_orders_fetched_at": fetched_at,
    }


@pytest.mark.unit
def test_clean_preflight_returns_info_and_does_not_block():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(client_order_id="rob93-buy-001"),
            _row(
                client_order_id="rob93-sell-001",
                side="sell",
                raw_responses={"payload": {"source_client_order_id": "rob93-buy-001"}},
            ),
        ],
        open_orders=[],
        positions=[],
        positions_fetched_at=datetime(2026, 5, 3, 12, 5, tzinfo=UTC),
        open_orders_fetched_at=datetime(2026, 5, 3, 12, 5, tzinfo=UTC),
        approval_packet={"client_order_id": "rob93-buy-002"},
        expected_signal_symbol="KRW-BTC",
        expected_execution_symbol="BTC/USD",
        now=datetime(2026, 5, 3, 12, 5, tzinfo=UTC),
    )

    assert report.status == "pass"
    assert report.should_block is False
    assert [a.check_id for a in report.anomalies] == ["preflight_clean"]
    assert report.anomalies[0].severity == PaperExecutionAnomalySeverity.info


@pytest.mark.unit
def test_open_order_blocks_new_cycle():
    report = build_paper_execution_preflight_report(
        open_orders=[
            {
                "id": "order-1",
                "client_order_id": "rob93-open-001",
                "symbol": "BTCUSD",
                "status": "accepted",
                "side": "buy",
            }
        ]
    )

    assert report.should_block is True
    assert "unexpected_open_orders" in _check_ids(report)


@pytest.mark.unit
def test_residual_position_blocks_new_cycle():
    report = build_paper_execution_preflight_report(
        positions=[{"symbol": "BTCUSD", "qty": "0.001", "asset_class": "crypto"}]
    )

    assert report.should_block is True
    assert "residual_position_exists" in _check_ids(report)


@pytest.mark.unit
def test_residual_position_can_warn_in_paper_execution_test_mode():
    report = build_paper_execution_preflight_report(
        **_verified_snapshot(
            positions=[{"symbol": "BTCUSD", "qty": "0.001", "asset_class": "crypto"}]
        ),
        legacy_cycle_blockers_as_warnings=True,
    )

    assert report.status == "pass"
    assert report.should_block is False
    anomaly = next(
        a for a in report.anomalies if a.check_id == "residual_position_exists"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.warning
    assert report.counts["warning"] == 1
    assert report.counts["block"] == 0


@pytest.mark.unit
def test_duplicate_client_order_id_blocks_against_packet_and_ledger():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(client_order_id="dup-001"),
            _row(client_order_id="dup-001", created_at=datetime(2026, 5, 3, 12, 1)),
        ],
        approval_packet={"client_order_id": "dup-001"},
    )

    assert report.should_block is True
    assert "duplicate_client_order_id" in _check_ids(report)


@pytest.mark.unit
def test_previous_buy_filled_without_linked_sell_blocks():
    report = build_paper_execution_preflight_report(
        ledger_rows=[_row(client_order_id="buy-without-sell")]
    )

    assert report.should_block is True
    assert "previous_buy_filled_sell_missing" in _check_ids(report)


# ---------------------------------------------------------------------------
# ROB-1129: open position vs missing sell leg
# ---------------------------------------------------------------------------


def _blocking_check_ids(report):
    return {
        a.check_id
        for a in report.anomalies
        if a.severity == PaperExecutionAnomalySeverity.block
    }


def _anomaly(report, check_id):
    return next(a for a in report.anomalies if a.check_id == check_id)


@pytest.mark.unit
def test_open_position_buy_without_sell_leg_is_info_not_block():
    """A filled buy whose symbol is still held is a normal open position."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="rob73-ac562d42b1d3ec16",
                side="buy",
                lifecycle_state="filled",
                execution_symbol="UBER",
                signal_symbol="UBER",
                filled_qty="4",
            )
        ],
        positions=[{"symbol": "UBER", "qty": "5", "asset_class": "us_equity"}],
        positions_fetched_at=datetime.now(UTC),
        open_orders=[],
        open_orders_fetched_at=datetime.now(UTC),
    )

    assert "previous_buy_filled_sell_missing" not in _check_ids(report)
    open_finding = _anomaly(report, "open_position_without_sell_leg")
    assert open_finding.severity == PaperExecutionAnomalySeverity.info
    assert open_finding.details["count"] == 1
    # The only remaining blocker is the flat-account residual gate, which is a
    # separate design assumption and not part of the ROB-1129 pairing defect.
    assert _blocking_check_ids(report) == {"residual_position_exists"}


@pytest.mark.unit
def test_holding_state_buy_without_broker_position_still_blocks():
    """Ledger says holding, broker says flat -> the sell leg was never recorded."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="rob73-ac4103900976a38d",
                side="buy",
                lifecycle_state="filled",
                execution_symbol="DPZ",
                signal_symbol="DPZ",
                filled_qty="1",
            )
        ],
        positions=[{"symbol": "UBER", "qty": "5", "asset_class": "us_equity"}],
        positions_fetched_at=datetime.now(UTC),
        open_orders=[],
        open_orders_fetched_at=datetime.now(UTC),
    )

    assert report.should_block is True
    finding = _anomaly(report, "previous_buy_filled_sell_missing")
    assert [r["reason"] for r in finding.details["rows"]] == [
        "holding_state_without_open_position"
    ]


@pytest.mark.unit
def test_completed_roundtrip_state_without_sell_leg_blocks_even_when_held():
    """closed/final_reconciled asserts the roundtrip ended, so a sell row is required."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="buy-closed-without-sell",
                side="buy",
                lifecycle_state="closed",
                execution_symbol="UBER",
                signal_symbol="UBER",
            )
        ],
        positions=[{"symbol": "UBER", "qty": "5", "asset_class": "us_equity"}],
        positions_fetched_at=datetime.now(UTC),
        open_orders=[],
        open_orders_fetched_at=datetime.now(UTC),
    )

    assert report.should_block is True
    finding = _anomaly(report, "previous_buy_filled_sell_missing")
    assert [r["reason"] for r in finding.details["rows"]] == [
        "completed_state_without_sell_leg"
    ]
    assert "open_position_without_sell_leg" not in _check_ids(report)


@pytest.mark.unit
def test_missing_position_snapshot_keeps_unlinked_buy_blocked():
    """Without a snapshot the two cases are indistinguishable -> stay blocked."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="rob73-ac562d42b1d3ec16",
                side="buy",
                lifecycle_state="filled",
                execution_symbol="UBER",
                signal_symbol="UBER",
            )
        ],
        positions=[],
    )

    assert report.should_block is True
    finding = _anomaly(report, "previous_buy_filled_sell_missing")
    assert [r["reason"] for r in finding.details["rows"]] == [
        "position_snapshot_unverified"
    ]


def _rob1129_measured_buy_legs():
    """The nine buy rows measured as `previous_buy_filled_sell_missing` on 07-28.

    Source: ~/work/herdr-inbox/alpaca-paper-block-proposal-2026-07-28.md §2-A.
    Every row carries its own lifecycle_correlation_id equal to its
    client_order_id, which is why payload-based buy/sell pairing never matches.
    """
    measured = [
        ("rob73-93bc5d4b6ba0ac6e", "ISRG", "1"),
        ("rob73-ac562d42b1d3ec16", "UBER", "4"),
        ("rob73-3ccf09a984c7758e", "WDC", "1"),
        ("rob73-47a669297ff57cb3", "F", "1"),
        ("rob73-a8691ab4f782af06", "ROST", "1"),
        ("rob73-1862fd0d16137ff6", "OLED", "0.0614"),
        ("rob73-ac4103900976a38d", "DPZ", "1"),
        ("rob73-c9726ec2359bd763", "AMC", "1"),
        ("rob73-08ebbf8c64e2dd93", "ISRG", "0.014"),
    ]
    return [
        _row(
            client_order_id=client_order_id,
            lifecycle_correlation_id=client_order_id,
            side="buy",
            lifecycle_state="filled",
            order_status="filled",
            execution_symbol=symbol,
            signal_symbol=symbol,
            filled_qty=filled_qty,
            position_snapshot=None,
        )
        for client_order_id, symbol, filled_qty in measured
    ]


@pytest.mark.unit
def test_rob1129_measured_ledger_drops_five_false_positive_open_positions():
    """Regression fixture for the 07-28 measurement: 9 findings -> 4 real ones.

    Positions are the live broker holdings measured the same day. ISRG, UBER,
    WDC and OLED are still held, so those five buy legs are open positions.
    F, ROST, DPZ and AMC are flat at the broker, so their missing sell legs are
    real ledger defects and must keep blocking.
    """
    report = build_paper_execution_preflight_report(
        ledger_rows=_rob1129_measured_buy_legs(),
        positions=[
            {"symbol": "ISRG", "qty": "1.014", "asset_class": "us_equity"},
            {"symbol": "UBER", "qty": "5", "asset_class": "us_equity"},
            {"symbol": "WDC", "qty": "1", "asset_class": "us_equity"},
            {"symbol": "OLED", "qty": "0.0614", "asset_class": "us_equity"},
            {"symbol": "BSX", "qty": "1", "asset_class": "us_equity"},
            {"symbol": "HCA", "qty": "1", "asset_class": "us_equity"},
            {"symbol": "CPNG", "qty": "1", "asset_class": "us_equity"},
        ],
        positions_fetched_at=datetime(2026, 7, 28, 14, 0, tzinfo=UTC),
        open_orders=[],
        open_orders_fetched_at=datetime(2026, 7, 28, 14, 0, tzinfo=UTC),
        now=datetime(2026, 7, 28, 14, 0, tzinfo=UTC),
    )

    open_finding = _anomaly(report, "open_position_without_sell_leg")
    assert open_finding.severity == PaperExecutionAnomalySeverity.info
    assert open_finding.details["count"] == 5
    assert {r["client_order_id"] for r in open_finding.details["rows"]} == {
        "rob73-93bc5d4b6ba0ac6e",  # ISRG 07-24
        "rob73-08ebbf8c64e2dd93",  # ISRG 07-17 tranche
        "rob73-ac562d42b1d3ec16",  # UBER
        "rob73-3ccf09a984c7758e",  # WDC
        "rob73-1862fd0d16137ff6",  # OLED
    }

    missing_finding = _anomaly(report, "previous_buy_filled_sell_missing")
    assert missing_finding.severity == PaperExecutionAnomalySeverity.block
    assert missing_finding.details["count"] == 4
    assert {r["client_order_id"] for r in missing_finding.details["rows"]} == {
        "rob73-47a669297ff57cb3",  # F 07-23
        "rob73-a8691ab4f782af06",  # ROST 07-21
        "rob73-ac4103900976a38d",  # DPZ 07-20
        "rob73-c9726ec2359bd763",  # AMC 07-20
    }
    assert {r["reason"] for r in missing_finding.details["rows"]} == {
        "holding_state_without_open_position"
    }


@pytest.mark.unit
def test_linked_sell_prevents_missing_sell_anomaly():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(client_order_id="buy-closed"),
            _row(
                client_order_id="sell-closed",
                side="sell",
                raw_responses={"payload": {"source_client_order_id": "buy-closed"}},
            ),
        ]
    )

    assert "previous_buy_filled_sell_missing" not in _check_ids(report)


@pytest.mark.unit
def test_canonical_completed_roundtrip_states_do_not_block():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="buy-reconciled",
                lifecycle_state="position_reconciled",
                order_status="filled",
            ),
            _row(
                client_order_id="sell-final-reconciled",
                side="sell",
                lifecycle_state="final_reconciled",
                order_status="filled",
                raw_responses={"payload": {"source_client_order_id": "buy-reconciled"}},
            ),
        ],
        **_verified_snapshot(),
    )

    assert report.status == "pass"
    assert report.should_block is False
    assert [a.check_id for a in report.anomalies] == ["preflight_clean"]


@pytest.mark.unit
def test_reconciled_buy_without_linked_sell_blocks_as_missing_sell():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="buy-reconciled-without-sell",
                lifecycle_state="position_reconciled",
                order_status="filled",
            )
        ]
    )

    assert report.should_block is True
    assert "previous_buy_filled_sell_missing" in _check_ids(report)
    assert "ledger_order_fill_mismatch" not in _check_ids(report)


@pytest.mark.unit
def test_filled_sell_with_nonzero_final_position_blocks():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="sell-not-closed",
                side="sell",
                lifecycle_state="filled",
                position_snapshot={"qty": "0.001"},
            )
        ]
    )

    assert report.should_block is True
    assert "sell_filled_position_not_closed" in _check_ids(report)


@pytest.mark.unit
def test_ledger_order_fill_mismatch_blocks():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="bad-fill",
                lifecycle_state="filled",
                order_status="filled",
                filled_qty="0",
            )
        ]
    )

    assert report.should_block is True
    assert "ledger_order_fill_mismatch" in _check_ids(report)


@pytest.mark.unit
def test_stale_preview_blocks():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="stale-preview",
                side="buy",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=45),
            )
        ],
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is True
    assert "stale_preview_or_approval_packet" in _check_ids(report)
    anomaly = next(
        a for a in report.anomalies if a.check_id == "stale_preview_or_approval_packet"
    )
    assert anomaly.details["lifecycle_state"] == STALE_PREVIEW_CLEANUP_REQUIRED_STATE
    assert anomaly.details["recommended_action"] == STALE_PREVIEW_CLEANUP_ACTION
    assert anomaly.details["cleanup_plan"] == {
        "mode": "dry_run",
        "mutates_broker": False,
        "mutates_db": False,
        "description": (
            "Mark same-scope stale preview rows cleanup-required only "
            "through a separately approved cleanup operation."
        ),
    }
    assert anomaly.details["rows"][0]["recommended_lifecycle_state"] == (
        STALE_PREVIEW_CLEANUP_REQUIRED_STATE
    )


@pytest.mark.unit
def test_spent_stale_preview_with_final_reconciled_sibling_warns_not_blocks():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="cleanup-sol-rob85-20260505-preview",
                lifecycle_correlation_id="cleanup-sol-rob85-20260505",
                side="sell",
                lifecycle_state="previewed",
                order_status=None,
                execution_symbol="SOLUSD",
                signal_symbol="SOL/USD",
                filled_qty=None,
                created_at=datetime(2026, 5, 4, 16, 8, 35, tzinfo=UTC),
            ),
            _row(
                client_order_id="cleanup-sol-rob85-20260505",
                lifecycle_correlation_id="cleanup-sol-rob85-20260505",
                side="sell",
                lifecycle_state="final_reconciled",
                order_status="filled",
                execution_symbol="SOLUSD",
                signal_symbol="SOL/USD",
            ),
        ],
        **_verified_snapshot(now=now),
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is False
    anomaly = next(
        a for a in report.anomalies if a.check_id == "spent_preview_without_cleanup"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.warning
    assert anomaly.details["rows"][0]["client_order_id"] == (
        "cleanup-sol-rob85-20260505-preview"
    )
    assert anomaly.details["rows"][0]["terminal_sibling_lifecycle_state"] == (
        "final_reconciled"
    )


@pytest.mark.unit
def test_scoped_stale_preview_uses_unscoped_terminal_sibling_evidence():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="stale-preview-current-candidate",
                lifecycle_correlation_id="corr-spent",
                candidate_uuid="candidate-current",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=45),
            ),
            _row(
                client_order_id="final-reconcile-prior-candidate",
                lifecycle_correlation_id="corr-spent",
                candidate_uuid="candidate-prior",
                lifecycle_state="final_reconciled",
                order_status="filled",
            ),
        ],
        approval_packet={"candidate_uuid": "candidate-current"},
        **_verified_snapshot(now=now),
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is False
    assert _check_ids(report) == {"spent_preview_without_cleanup"}
    assert report.counts["ledger_rows"] == 1
    assert report.counts["unscoped_ledger_rows"] == 2


@pytest.mark.unit
def test_spent_stale_preview_fixture_for_rob_950_two_terminal_pairs_does_not_block():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="cleanup-sol-rob85-20260505-preview",
                lifecycle_correlation_id="cleanup-sol-rob85-20260505",
                side="sell",
                lifecycle_state="previewed",
                order_status=None,
                execution_symbol="SOLUSD",
                signal_symbol="SOL/USD",
                filled_qty=None,
                created_at=datetime(2026, 5, 4, 16, 8, 35, tzinfo=UTC),
            ),
            _row(
                client_order_id="cleanup-sol-rob85-20260505",
                lifecycle_correlation_id="cleanup-sol-rob85-20260505",
                side="sell",
                lifecycle_state="final_reconciled",
                order_status="filled",
                execution_symbol="SOLUSD",
                signal_symbol="SOL/USD",
            ),
            _row(
                client_order_id="cleanup-btc-rob85-20260505-preview",
                lifecycle_correlation_id="cleanup-btc-rob85-20260505",
                side="sell",
                lifecycle_state="previewed",
                order_status=None,
                execution_symbol="BTCUSD",
                signal_symbol="BTC/USD",
                filled_qty=None,
                created_at=datetime(2026, 5, 4, 16, 8, 34, tzinfo=UTC),
            ),
            _row(
                client_order_id="cleanup-btc-rob85-20260505",
                lifecycle_correlation_id="cleanup-btc-rob85-20260505",
                side="sell",
                lifecycle_state="final_reconciled",
                order_status="filled",
                execution_symbol="BTCUSD",
                signal_symbol="BTC/USD",
            ),
        ],
        **_verified_snapshot(now=now),
        now=now,
        stale_after_minutes=30,
    )

    assert report.status == "pass"
    assert report.should_block is False
    anomaly = next(
        a for a in report.anomalies if a.check_id == "spent_preview_without_cleanup"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.warning
    assert anomaly.details["count"] == 2


@pytest.mark.unit
def test_stale_preview_without_terminal_sibling_still_blocks():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="stale-preview-unspent",
                lifecycle_correlation_id="corr-unspent",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=45),
            )
        ],
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is True
    anomaly = next(
        a for a in report.anomalies if a.check_id == "stale_preview_or_approval_packet"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.block


@pytest.mark.unit
def test_stale_preview_with_nonterminal_sibling_still_blocks():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="stale-preview-open-sibling",
                lifecycle_correlation_id="corr-open-sibling",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=45),
            ),
            _row(
                client_order_id="submitted-sibling",
                lifecycle_correlation_id="corr-open-sibling",
                lifecycle_state="submitted",
                order_status="accepted",
                filled_qty=None,
            ),
        ],
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is True
    assert "stale_preview_or_approval_packet" in _check_ids(report)
    assert "spent_preview_without_cleanup" not in _check_ids(report)


@pytest.mark.unit
def test_stale_approval_packet_still_blocks_without_ledger_sibling_matching():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        approval_packet={
            "client_order_id": "stale-approval-packet",
            "generated_at": (now - timedelta(minutes=45)).isoformat(),
        },
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is True
    anomaly = next(
        a for a in report.anomalies if a.check_id == "stale_preview_or_approval_packet"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.block


@pytest.mark.unit
def test_stale_preview_can_warn_in_paper_execution_test_mode():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="stale-preview",
                side="buy",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=45),
            )
        ],
        **_verified_snapshot(now=now),
        now=now,
        stale_after_minutes=30,
        legacy_cycle_blockers_as_warnings=True,
    )

    assert report.status == "pass"
    assert report.should_block is False
    anomaly = next(
        a for a in report.anomalies if a.check_id == "stale_preview_or_approval_packet"
    )
    assert anomaly.severity == PaperExecutionAnomalySeverity.warning
    assert anomaly.details["recommended_action"] == STALE_PREVIEW_CLEANUP_ACTION
    assert report.counts["warning"] == 1
    assert report.counts["block"] == 0


@pytest.mark.unit
def test_test_mode_still_blocks_open_order_conflicts():
    report = build_paper_execution_preflight_report(
        open_orders=[
            {
                "id": "order-1",
                "client_order_id": "rob93-open-001",
                "symbol": "BTCUSD",
                "status": "accepted",
                "side": "buy",
            }
        ],
        positions=[{"symbol": "BTCUSD", "qty": "0.001", "asset_class": "crypto"}],
        legacy_cycle_blockers_as_warnings=True,
    )

    assert report.status == "blocked"
    assert report.should_block is True
    severities = {a.check_id: a.severity for a in report.anomalies}
    assert severities["unexpected_open_orders"] == PaperExecutionAnomalySeverity.block
    assert (
        severities["residual_position_exists"] == PaperExecutionAnomalySeverity.warning
    )


@pytest.mark.unit
def test_signal_execution_symbol_mismatch_blocks():
    report = build_paper_execution_preflight_report(
        ledger_rows=[_row(signal_symbol="KRW-ETH", execution_symbol="ETHUSD")],
        expected_signal_symbol="KRW-BTC",
        expected_execution_symbol="BTC/USD",
    )

    assert report.should_block is True
    assert "signal_execution_symbol_mismatch" in _check_ids(report)


@pytest.mark.unit
def test_report_to_dict_is_operator_readable():
    report = build_paper_execution_preflight_report(
        **_verified_snapshot(
            open_orders=[{"id": "order-1", "status": "new", "symbol": "BTCUSD"}],
            now=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
        ),
        now=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )
    data = report.to_dict()

    assert data["status"] == "blocked"
    assert data["should_block"] is True
    assert data["counts"]["block"] == 1
    assert data["anomalies"][0]["check_id"] == "unexpected_open_orders"
    assert data["broker_snapshot"]["positions"]["verified"] is True
    assert data["broker_snapshot"]["open_orders"]["verified"] is True


@pytest.mark.unit
def test_scoped_preflight_ignores_unrelated_stale_and_symbol_rows():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="eth-preview-old",
                lifecycle_correlation_id="corr-eth",
                lifecycle_state="previewed",
                order_status=None,
                execution_symbol="ETHUSD",
                signal_symbol="KRW-ETH",
                filled_qty=None,
                created_at=now - timedelta(minutes=90),
            ),
            _row(
                client_order_id="btc-preview-fresh",
                lifecycle_correlation_id="corr-btc",
                lifecycle_state="previewed",
                order_status=None,
                execution_symbol="BTCUSD",
                signal_symbol="KRW-BTC",
                filled_qty=None,
                created_at=now - timedelta(minutes=5),
            ),
        ],
        approval_packet={
            "client_order_id": "btc-submit",
            "lifecycle_correlation_id": "corr-btc",
            "signal_symbol": "KRW-BTC",
            "execution_symbol": "BTC/USD",
        },
        **_verified_snapshot(now=now),
        now=now,
        stale_after_minutes=30,
    )

    assert report.status == "pass"
    assert report.should_block is False
    assert _check_ids(report) == {"preflight_clean"}
    assert report.counts["ledger_rows"] == 1
    assert report.counts["unscoped_ledger_rows"] == 2


@pytest.mark.unit
def test_scoped_preflight_still_blocks_stale_row_inside_same_correlation():
    now = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="btc-preview-old",
                lifecycle_correlation_id="corr-btc",
                lifecycle_state="previewed",
                order_status=None,
                filled_qty=None,
                created_at=now - timedelta(minutes=90),
            )
        ],
        approval_packet={
            "client_order_id": "btc-submit",
            "lifecycle_correlation_id": "corr-btc",
        },
        now=now,
        stale_after_minutes=30,
    )

    assert report.should_block is True
    assert "stale_preview_or_approval_packet" in _check_ids(report)
    anomaly = next(
        a for a in report.anomalies if a.check_id == "stale_preview_or_approval_packet"
    )
    assert anomaly.details["lifecycle_state"] == STALE_PREVIEW_CLEANUP_REQUIRED_STATE
    assert anomaly.details["recommended_action"] == STALE_PREVIEW_CLEANUP_ACTION
    assert anomaly.details["cleanup_plan"]["mutates_broker"] is False
    assert anomaly.details["cleanup_plan"]["mutates_db"] is False
    assert anomaly.details["rows"][0]["lifecycle_correlation_id"] == "corr-btc"
    assert anomaly.details["rows"][0]["recommended_action"] == (
        STALE_PREVIEW_CLEANUP_ACTION
    )


@pytest.mark.unit
def test_scoped_preflight_still_blocks_symbol_mismatch_inside_same_correlation():
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="btc-preview-wrong-symbol",
                lifecycle_correlation_id="corr-btc",
                lifecycle_state="previewed",
                order_status=None,
                signal_symbol="KRW-SOL",
                execution_symbol="SOLUSD",
                filled_qty=None,
            )
        ],
        approval_packet={
            "client_order_id": "btc-submit",
            "lifecycle_correlation_id": "corr-btc",
            "signal_symbol": "KRW-BTC",
            "execution_symbol": "BTC/USD",
        },
    )

    assert report.should_block is True
    assert "signal_execution_symbol_mismatch" in _check_ids(report)


@pytest.mark.unit
def test_preflight_does_not_flag_canceled_state_as_anomaly():
    # Check that a canceled row does not trigger unexpected anomalies
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="buy-reconciled",
                lifecycle_state="position_reconciled",
                order_status="filled",
            ),
            _row(
                client_order_id="sell-canceled",
                side="sell",
                lifecycle_state="canceled",
                order_status="canceled",
                cancel_status="canceled",
                raw_responses={"payload": {"source_client_order_id": "buy-reconciled"}},
            ),
        ],
        **_verified_snapshot(),
    )

    assert report.status == "pass"
    assert report.should_block is False
    assert "ledger_anomaly_row" not in _check_ids(report)


# ---------------------------------------------------------------------------
# ROB-1130: broker snapshot is fail-closed
# ---------------------------------------------------------------------------

_ROB1130_NOW = datetime(2026, 7, 28, 14, 0, tzinfo=UTC)


@pytest.mark.unit
def test_rob1130_empty_snapshot_without_attestation_blocks():
    """Regression 1/4: an empty snapshot is not evidence of an empty account."""
    report = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[],
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    assert report.status == "blocked"
    finding = _anomaly(report, "broker_snapshot_unverified")
    assert finding.severity == PaperExecutionAnomalySeverity.block
    assert finding.details["unverified"] == ["positions", "open_orders"]
    assert finding.details["reasons"] == {
        "positions": "snapshot_not_attested",
        "open_orders": "snapshot_not_attested",
    }
    # The misleading "positions: 0" signal from ROB-1130 must not be reported as
    # a verified count.
    assert report.broker_snapshot["positions"]["verified"] is False
    assert "positions" not in report.counts


@pytest.mark.unit
def test_rob1130_missing_snapshot_fields_block():
    """Regression 2/4: omitting the snapshot entirely blocks."""
    report = build_paper_execution_preflight_report(now=_ROB1130_NOW)

    assert report.should_block is True
    finding = _anomaly(report, "broker_snapshot_unverified")
    assert finding.details["reasons"] == {
        "positions": "snapshot_missing",
        "open_orders": "snapshot_missing",
    }
    assert report.broker_snapshot["positions"]["provided"] is False
    assert report.broker_snapshot["positions"]["count"] is None
    assert "positions" not in report.counts
    assert "open_orders" not in report.counts


@pytest.mark.unit
def test_rob1130_partial_snapshot_blocks_on_the_missing_half():
    """Attesting positions only still leaves the open-order view unqueried."""
    report = build_paper_execution_preflight_report(
        positions=[],
        positions_fetched_at=_ROB1130_NOW,
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    finding = _anomaly(report, "broker_snapshot_unverified")
    assert finding.details["unverified"] == ["open_orders"]
    assert report.broker_snapshot["positions"]["verified"] is True


@pytest.mark.unit
def test_rob1130_genuinely_queried_empty_account_passes():
    """Regression 3/4: a real positions=0 read keeps the normal pass path."""
    report = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[],
        open_orders_fetched_at=_ROB1130_NOW,
        positions_fetched_at=_ROB1130_NOW,
        now=_ROB1130_NOW,
    )

    assert report.status == "pass"
    assert report.should_block is False
    assert _check_ids(report) == {"preflight_clean"}
    assert report.counts["positions"] == 0
    assert report.counts["open_orders"] == 0
    assert report.broker_snapshot["positions"] == {
        "provided": True,
        "count": 0,
        "fetched_at": _ROB1130_NOW.isoformat(),
        "verified": True,
        "reason": None,
    }


@pytest.mark.unit
def test_rob1130_held_position_in_real_snapshot_is_reflected():
    """Regression 4/4: the UBER holding measured on 07-28 must be seen."""
    report = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[{"symbol": "UBER", "qty": "1", "asset_class": "us_equity"}],
        open_orders_fetched_at=_ROB1130_NOW,
        positions_fetched_at=_ROB1130_NOW,
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    finding = _anomaly(report, "residual_position_exists")
    assert finding.severity == PaperExecutionAnomalySeverity.block
    assert finding.details["positions"] == [
        {"symbol": "UBER", "qty": "1", "asset_class": "us_equity"}
    ]
    assert report.counts["positions"] == 1
    assert "broker_snapshot_unverified" not in _check_ids(report)


@pytest.mark.unit
def test_rob1130_stale_snapshot_blocks():
    report = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[],
        open_orders_fetched_at=_ROB1130_NOW - timedelta(minutes=30),
        positions_fetched_at=_ROB1130_NOW - timedelta(minutes=30),
        snapshot_max_age_minutes=5,
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    finding = _anomaly(report, "broker_snapshot_unverified")
    assert finding.details["reasons"] == {
        "positions": "snapshot_stale",
        "open_orders": "snapshot_stale",
    }


@pytest.mark.unit
def test_rob1130_unparseable_and_future_attestations_block():
    unparseable = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[],
        open_orders_fetched_at="not-a-timestamp",
        positions_fetched_at="not-a-timestamp",
        now=_ROB1130_NOW,
    )
    assert unparseable.should_block is True
    assert _anomaly(unparseable, "broker_snapshot_unverified").details["reasons"] == {
        "positions": "snapshot_attestation_unparseable",
        "open_orders": "snapshot_attestation_unparseable",
    }

    future = build_paper_execution_preflight_report(
        open_orders=[],
        positions=[],
        open_orders_fetched_at=_ROB1130_NOW + timedelta(minutes=10),
        positions_fetched_at=_ROB1130_NOW + timedelta(minutes=10),
        now=_ROB1130_NOW,
    )
    assert future.should_block is True
    assert _anomaly(future, "broker_snapshot_unverified").details["reasons"] == {
        "positions": "snapshot_attestation_in_future",
        "open_orders": "snapshot_attestation_in_future",
    }


@pytest.mark.unit
def test_rob1130_snapshot_blocker_is_not_downgradeable_by_legacy_flag():
    """legacy_cycle_blockers_as_warnings must not reopen the fail-open hole."""
    report = build_paper_execution_preflight_report(
        legacy_cycle_blockers_as_warnings=True,
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    assert (
        _anomaly(report, "broker_snapshot_unverified").severity
        == PaperExecutionAnomalySeverity.block
    )


@pytest.mark.unit
def test_rob1130_unverified_snapshot_keeps_open_position_classification_closed():
    """Without snapshot evidence a filled buy stays blocked, never assumed open."""
    report = build_paper_execution_preflight_report(
        ledger_rows=[
            _row(
                client_order_id="rob73-ac562d42b1d3ec16",
                side="buy",
                lifecycle_state="filled",
                execution_symbol="UBER",
                signal_symbol="UBER",
            )
        ],
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    assert "open_position_without_sell_leg" not in _check_ids(report)
    finding = _anomaly(report, "previous_buy_filled_sell_missing")
    assert [r["reason"] for r in finding.details["rows"]] == [
        "position_snapshot_unverified"
    ]


@pytest.mark.unit
def test_rob1130_audit_callers_may_opt_out_without_downgrading_other_blockers():
    """Historical audit views are not order gates, but keep every real blocker."""
    report = build_paper_execution_preflight_report(
        snapshot_evidence_required=False,
        now=_ROB1130_NOW,
    )
    assert report.should_block is False
    assert _check_ids(report) == {"preflight_clean"}

    still_blocking = build_paper_execution_preflight_report(
        open_orders=[{"id": "order-1", "status": "new", "symbol": "BTCUSD"}],
        positions=[{"symbol": "UBER", "qty": "1"}],
        snapshot_evidence_required=False,
        now=_ROB1130_NOW,
    )
    assert still_blocking.should_block is True
    assert "broker_snapshot_unverified" not in _check_ids(still_blocking)
    assert {"unexpected_open_orders", "residual_position_exists"} <= _check_ids(
        still_blocking
    )


@pytest.mark.unit
def test_rob1130_invalid_snapshot_max_age_raises():
    with pytest.raises(ValueError, match="snapshot_max_age_minutes must be >= 1"):
        build_paper_execution_preflight_report(snapshot_max_age_minutes=0)


# ---------------------------------------------------------------------------
# ROB-1129 + ROB-1130 combined: what the 07-28 account looks like after both fixes
# ---------------------------------------------------------------------------


def _rob1129_measured_sell_legs():
    """The seven sell rows measured as `sell_filled_position_not_closed` on 07-28.

    Source: ~/work/herdr-inbox/alpaca-paper-block-proposal-2026-07-28.md 2-B.
    Their stored `position_snapshot` is the pre-fill sell_claim_baseline rather
    than a post-fill re-read, which is a separate defect from the buy/sell
    pairing one and is deliberately left untouched by the ROB-1129 fix.
    """
    measured = [
        ("rob73-ce5159550b11e626", "F"),
        ("rob73-abc306c7599f4b63", "F"),
        ("rob73-cce8e0d3264fb86d", "ROST"),
        ("rob73-9dd2e832f0e9dd05", "REGN"),
        ("rob73-2b58a4ff9b6645ff", "F"),
        ("rob73-e37deae38a423090", "DPZ"),
        ("rob73-6623cf6a1009e34c", "AMC"),
    ]
    return [
        _row(
            client_order_id=client_order_id,
            lifecycle_correlation_id=client_order_id,
            side="sell",
            lifecycle_state="filled",
            order_status="filled",
            execution_symbol=symbol,
            signal_symbol=symbol,
            filled_qty="1",
            position_snapshot={"qty": "1"},
        )
        for client_order_id, symbol in measured
    ]


@pytest.mark.unit
def test_rob1129_stage_one_does_not_release_the_account_block():
    """Both fixes together shrink the findings but the account still blocks.

    Fixing the checker removes the five false positives and fixing the snapshot
    input makes the holdings visible. What remains is real: four buy legs the
    broker already closed, seven sell legs whose ledger rows never advanced, and
    the flat-account residual gate. Advancing those rows needs the stage 2/3
    tooling, not a checker change.
    """
    report = build_paper_execution_preflight_report(
        ledger_rows=_rob1129_measured_buy_legs() + _rob1129_measured_sell_legs(),
        positions=[
            {"symbol": "ISRG", "qty": "1.014", "asset_class": "us_equity"},
            {"symbol": "UBER", "qty": "5", "asset_class": "us_equity"},
            {"symbol": "WDC", "qty": "1", "asset_class": "us_equity"},
            {"symbol": "OLED", "qty": "0.0614", "asset_class": "us_equity"},
        ],
        positions_fetched_at=_ROB1130_NOW,
        open_orders=[],
        open_orders_fetched_at=_ROB1130_NOW,
        now=_ROB1130_NOW,
    )

    assert report.should_block is True
    assert _blocking_check_ids(report) == {
        "residual_position_exists",
        "previous_buy_filled_sell_missing",
        "sell_filled_position_not_closed",
    }
    assert _anomaly(report, "open_position_without_sell_leg").details["count"] == 5
    assert _anomaly(report, "previous_buy_filled_sell_missing").details["count"] == 4
    assert len(_anomaly(report, "sell_filled_position_not_closed").details["rows"]) == 7
    # No bypass: the test-mode downgrade flag stays off and untouched.
    assert report.broker_snapshot["evidence_required"] is True
