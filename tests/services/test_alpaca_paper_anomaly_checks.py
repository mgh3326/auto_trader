"""Tests for read-only Alpaca Paper execution anomaly checks (ROB-93)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.alpaca_paper_anomaly_checks import (
    PaperExecutionAnomalySeverity,
    build_paper_execution_preflight_report,
)


def _row(**kwargs):
    defaults = {
        "client_order_id": "rob93-buy-001",
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
        open_orders=[],
        positions=[],
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
        open_orders=[{"id": "order-1", "status": "new", "symbol": "BTCUSD"}],
        now=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )
    data = report.to_dict()

    assert data["status"] == "blocked"
    assert data["should_block"] is True
    assert data["counts"]["block"] == 1
    assert data["anomalies"][0]["check_id"] == "unexpected_open_orders"
