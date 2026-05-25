"""ROB-315 Phase 1 — daily rollup of scalp_trade_analytics (pure, no DB)."""

from __future__ import annotations

from decimal import Decimal

from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.scalping_reviews.rollup import build_rollup


def _row(**kw) -> ScalpTradeAnalytics:
    base = {
        "open_client_order_id": "o",
        "instrument_id": 1,
        "product": "usdm_futures",
        "symbol": "XRPUSDT",
        "side": "BUY",
        "qty": Decimal("1"),
    }
    base.update(kw)
    return ScalpTradeAnalytics(**base)


def test_empty_rows_are_all_zero_and_na() -> None:
    r = build_rollup([])
    assert r.trade_count == 0
    assert r.win_count == 0
    assert r.loss_count == 0
    assert r.anomaly_count == 0
    assert r.net_pnl_usdt is None  # n/a, not 0
    assert r.avg_slippage_bps is None
    assert r.exit_reason_counts == {}


def test_fill_proven_rows_count_wins_losses_and_pnl() -> None:
    rows = [
        _row(
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            entry_notional_usdt=Decimal("100"),
            net_pnl_usdt=Decimal("0.9"),
            gross_pnl_usdt=Decimal("1.0"),
            entry_slippage_bps=Decimal("2"),
            exit_reason="take_profit",
        ),
        _row(
            entry_price=Decimal("100"),
            exit_price=Decimal("99"),
            entry_notional_usdt=Decimal("100"),
            net_pnl_usdt=Decimal("-1.1"),
            gross_pnl_usdt=Decimal("-1.0"),
            entry_slippage_bps=Decimal("4"),
            exit_reason="stop_loss",
        ),
    ]
    r = build_rollup(rows)
    assert r.trade_count == 2
    assert r.win_count == 1
    assert r.loss_count == 1
    assert r.anomaly_count == 0
    assert r.net_pnl_usdt == Decimal("-0.2")  # 0.9 + (-1.1)
    assert r.gross_pnl_usdt == Decimal("0.0")
    # capital-weighted: -0.2 / 200 * 10_000 = -10 bps
    assert r.net_return_bps == Decimal("-10")
    assert r.avg_slippage_bps == Decimal("3")  # mean(2, 4)
    assert r.exit_reason_counts == {"take_profit": 1, "stop_loss": 1}


def test_no_fill_price_rows_count_as_anomaly_only() -> None:
    rows = [
        _row(
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            entry_notional_usdt=Decimal("100"),
            net_pnl_usdt=Decimal("0.9"),
            gross_pnl_usdt=Decimal("1.0"),
            exit_reason="take_profit",
        ),
        # partial row: no derivable entry price → anomaly, not a trade.
        _row(entry_price=None, exit_reason="timeout"),
    ]
    r = build_rollup(rows)
    assert r.trade_count == 1  # only the fill-proven one
    assert r.anomaly_count == 1
    assert r.win_count == 1
    assert r.net_pnl_usdt == Decimal("0.9")  # anomaly row contributes nothing
    # exit_reason_counts still records every row's reason (audit trail).
    assert r.exit_reason_counts == {"take_profit": 1, "timeout": 1}


def test_telemetry_averages_skip_nulls_and_report_na_when_absent() -> None:
    rows = [
        _row(
            entry_price=Decimal("100"),
            net_pnl_usdt=Decimal("0.1"),
            entry_notional_usdt=Decimal("100"),
            mae_bps=Decimal("-10"),
            mfe_bps=Decimal("40"),
            holding_seconds=10,
            # entry_spread_bps absent → excluded from avg_spread_bps
        ),
        _row(
            entry_price=Decimal("100"),
            net_pnl_usdt=Decimal("0.3"),
            entry_notional_usdt=Decimal("100"),
            mae_bps=Decimal("-20"),
            mfe_bps=Decimal("60"),
            holding_seconds=20,
        ),
    ]
    r = build_rollup(rows)
    assert r.avg_mae_bps == Decimal("-15")  # mean(-10, -20)
    assert r.avg_mfe_bps == Decimal("50")  # mean(40, 60)
    assert r.avg_holding_seconds == 15
    assert r.avg_spread_bps is None  # no row carried it → n/a, not 0
