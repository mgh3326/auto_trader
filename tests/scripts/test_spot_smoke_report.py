"""ROB-299 — spot_demo_smoke_report builder shape."""

from __future__ import annotations

from scripts.binance_spot_demo_smoke import build_spot_smoke_report


def test_report_shape_clean_reconcile():
    report = {
        "deployed_sha": "abc1234",
        "env_enabled": True,
        "env_credentials_present": True,
        "buy_qty": "6.0",
        "buy_status": "FILLED",
        "close_qty": "5.9",
        "close_status": "FILLED",
        "open_orders_count": 0,
        "reconciliation_status": "reconciled",
        "blockers": [],
    }
    out = build_spot_smoke_report(report)
    assert out["event"] == "spot_demo_smoke_report"
    assert out["deployed_sha"] == "abc1234"
    assert out["reconciliation_status"] == "reconciled"
    assert out["residual_dust"] is None
    assert out["blockers"] == []


def test_report_shape_dust_includes_residual():
    report = {
        "deployed_sha": "abc1234",
        "env_enabled": True,
        "env_credentials_present": True,
        "close_qty": "0",
        "residual_dust_amount": "0.0925",
        "residual_dust_notional": "0.18",
        "reconciliation_status": "dust",
        "blockers": [],
    }
    out = build_spot_smoke_report(report)
    assert out["residual_dust"]["amount"] == "0.0925"
    assert out["residual_dust"]["notional_usdt"] == "0.18"
