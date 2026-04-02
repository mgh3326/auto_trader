"""Tests for backtest report helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare  # pyright: ignore[reportMissingImports]
import report  # pyright: ignore[reportMissingImports]


def _make_result() -> prepare.BacktestResult:
    return prepare.BacktestResult(
        total_return_pct=12.0,
        sharpe=1.8,
        max_drawdown_pct=8.5,
        num_trades=6,
        win_rate_pct=2 / 3,
        profit_factor=1.9,
        avg_holding_days=8.0,
        backtest_seconds=0.5,
        trade_log=[
            {
                "date": "2025-07-01",
                "symbol": "BTC",
                "action": "buy",
                "quantity": 10.0,
                "price": 100.0,
                "fee": 10.0,
                "reason": "entry_a",
            },
            {
                "date": "2025-07-05",
                "symbol": "BTC",
                "action": "sell",
                "quantity": 4.0,
                "price": 110.0,
                "fee": 4.0,
                "realized_pnl": 36.0,
                "reason": "trim_a",
            },
            {
                "date": "2025-07-10",
                "symbol": "BTC",
                "action": "sell",
                "quantity": 6.0,
                "price": 120.0,
                "fee": 6.0,
                "realized_pnl": 114.0,
                "reason": "exit_a",
            },
            {
                "date": "2025-08-01",
                "symbol": "ETH",
                "action": "buy",
                "quantity": 5.0,
                "price": 200.0,
                "fee": 5.0,
                "reason": "entry_b",
            },
            {
                "date": "2025-08-04",
                "symbol": "ETH",
                "action": "sell",
                "quantity": 5.0,
                "price": 190.0,
                "fee": 5.0,
                "realized_pnl": -55.0,
                "reason": "exit_b",
            },
            {
                "date": "2025-08-10",
                "symbol": "BTC",
                "action": "buy",
                "quantity": 5.0,
                "price": 130.0,
                "fee": 5.0,
                "reason": "entry_c",
            },
            {
                "date": "2025-08-15",
                "symbol": "BTC",
                "action": "sell",
                "quantity": 5.0,
                "price": 140.0,
                "fee": 5.0,
                "realized_pnl": 45.0,
                "reason": "exit_c",
            },
        ],
        equity_curve=[
            10_000_000.0,
            10_000_000.0,
            10_200_000.0,
            10_150_000.0,
            10_400_000.0,
            10_350_000.0,
        ],
        equity_dates=[
            "2025-07-01",
            "2025-07-01",
            "2025-07-31",
            "2025-08-01",
            "2025-08-15",
            "2025-08-31",
        ],
    )


def _make_cv_result() -> prepare.CVResult:
    fold_result = prepare.BacktestResult(
        total_return_pct=5.0,
        sharpe=1.2,
        max_drawdown_pct=4.0,
        num_trades=12,
        win_rate_pct=0.5,
        profit_factor=1.4,
        avg_holding_days=6.0,
        trade_log=[
            {"date": "2025-04-01", "symbol": "BTC", "action": "buy", "quantity": 1.0},
            {
                "date": "2025-04-02",
                "symbol": "BTC",
                "action": "sell",
                "quantity": 1.0,
                "realized_pnl": 10.0,
            },
        ],
        equity_curve=[10_000_000.0, 10_500_000.0],
        equity_dates=["2025-04-01", "2025-04-02"],
    )
    return prepare.CVResult(
        fold_scores=[1.0],
        fold_results=[fold_result],
        fold_indices=[0],
        mean_score=1.0,
        std_score=0.0,
        min_score=1.0,
        cv_score=1.0,
    )


def test_build_round_trips_aggregates_partial_sells_until_flat() -> None:
    round_trips = report.build_round_trips(_make_result().trade_log)

    assert len(round_trips) == 3
    assert round_trips[0]["symbol"] == "BTC"
    assert round_trips[0]["entry_date"] == "2025-07-01"
    assert round_trips[0]["exit_date"] == "2025-07-10"
    assert round_trips[0]["pnl"] == pytest.approx(150.0)
    assert round_trips[0]["entry_reason"] == "entry_a"
    assert round_trips[0]["exit_reason"] == "exit_a"


def test_generate_monthly_table_returns_monthly_rows() -> None:
    rows = report.generate_monthly_table(
        _make_result().equity_curve,
        _make_result().equity_dates,
        _make_result().trade_log,
    )

    assert [row["month"] for row in rows] == ["2025-07", "2025-08"]
    assert rows[0]["trades"] == 3
    assert rows[1]["trades"] == 4
    assert rows[0]["return_pct"] == pytest.approx(2.0)


def test_generate_risk_metrics_returns_required_keys() -> None:
    metrics = report.generate_risk_metrics(
        _make_result().equity_curve,
        _make_result().equity_dates,
        _make_result().trade_log,
    )

    assert set(metrics) == {
        "calmar_ratio",
        "avg_win_avg_loss",
        "max_consecutive_losses",
        "max_consecutive_wins",
        "longest_drawdown_period_days",
        "recovery_time_from_max_dd_days",
        "time_in_market_pct",
    }
    assert metrics["max_consecutive_wins"] >= 1
    assert metrics["max_consecutive_losses"] >= 1


def test_build_report_payload_returns_approved_top_level_sections() -> None:
    payload = report.build_report_payload(
        _make_result(),
        data={"BTC": object(), "ETH": object()},
        split_info={"name": "val", "start": "2025-07-01", "end": "2026-01-31"},
        cv_result=_make_cv_result(),
    )

    assert set(payload) == {
        "summary",
        "monthly_returns",
        "per_symbol",
        "top_trades",
        "bottom_trades",
        "cv",
        "risk_metrics",
    }
    assert payload["summary"]["split"] == "val"
    assert payload["top_trades"][0]["symbol"] == "BTC"
    assert payload["bottom_trades"][0]["symbol"] == "ETH"


def test_build_report_payload_uses_official_time_in_market_pct() -> None:
    """Test that report uses BacktestResult.time_in_market_pct in both summary and risk_metrics."""
    result = _make_result()
    result.time_in_market_pct = 37.5

    payload = report.build_report_payload(
        result,
        data={"BTC": object()},
        split_info={"name": "val", "start": "2025-07-01", "end": "2026-01-31"},
        cv_result=_make_cv_result(),
    )

    assert payload["summary"]["time_in_market_pct"] == pytest.approx(37.5)
    assert payload["risk_metrics"]["time_in_market_pct"] == pytest.approx(37.5)
