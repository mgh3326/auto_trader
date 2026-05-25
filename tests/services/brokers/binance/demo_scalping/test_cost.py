"""ROB-313 PR1 — cost computation unit tests (pure, no DB / no network).

Slippage is measured exactly from the actual fill vs the intended/reference
price; fees are estimated from a config fee-rate (Demo "exact" commission is
not real-VIP/BNB-accurate, so a consistent model shared with the backtester
is more useful — see ROB-313 D3).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.cost import (
    build_round_trip_economics,
    fee_estimate_usdt,
    mae_mfe_bps,
    net_pnl_usdt,
    net_return_bps,
    slippage_bps,
    spot_avg_fill_price,
)


class TestMaeMfeBps:
    """Max adverse / max favorable excursion vs entry, direction-aware.
    MFE >= MAE always; favorable is the up-move for a long, the down-move for
    a short. No observed path → (None, None)."""

    def test_long_excursions(self) -> None:
        mae, mfe = mae_mfe_bps(
            side="BUY",
            entry_price=Decimal("100"),
            path_min=Decimal("99"),
            path_max=Decimal("103"),
        )
        assert mfe == Decimal("300")  # (103-100)/100 * 10_000, favorable up
        assert mae == Decimal("-100")  # (99-100)/100 * 10_000, adverse down

    def test_short_excursions(self) -> None:
        mae, mfe = mae_mfe_bps(
            side="SELL",
            entry_price=Decimal("100"),
            path_min=Decimal("97"),
            path_max=Decimal("101"),
        )
        assert mfe == Decimal("300")  # (100-97)/100 * 10_000, favorable down
        assert mae == Decimal("-100")  # (100-101)/100 * 10_000, adverse up

    def test_no_path_returns_none(self) -> None:
        assert mae_mfe_bps(
            side="BUY", entry_price=Decimal("100"), path_min=None, path_max=None
        ) == (None, None)


class TestSlippageBps:
    """Adverse slippage is POSITIVE; favourable is negative. Direction-aware:
    a BUY pays up (fill >= ref is adverse); a SELL receives less
    (fill <= ref is adverse)."""

    def test_buy_adverse_fill_above_reference_is_positive(self) -> None:
        # Bought at 100.10 vs intended 100.00 → +10 bps adverse.
        assert slippage_bps(
            fill_price=Decimal("100.10"),
            reference_price=Decimal("100.00"),
            side="BUY",
        ) == Decimal("10")

    def test_sell_adverse_fill_below_reference_is_positive(self) -> None:
        # Sold at 99.90 vs intended 100.00 → +10 bps adverse.
        assert slippage_bps(
            fill_price=Decimal("99.90"),
            reference_price=Decimal("100.00"),
            side="SELL",
        ) == Decimal("10")

    def test_buy_favourable_fill_below_reference_is_negative(self) -> None:
        assert slippage_bps(
            fill_price=Decimal("99.95"),
            reference_price=Decimal("100.00"),
            side="BUY",
        ) == Decimal("-5")

    def test_zero_when_fill_equals_reference(self) -> None:
        assert slippage_bps(
            fill_price=Decimal("100.00"),
            reference_price=Decimal("100.00"),
            side="SELL",
        ) == Decimal("0")

    def test_zero_reference_raises(self) -> None:
        with pytest.raises(ValueError):
            slippage_bps(
                fill_price=Decimal("100"),
                reference_price=Decimal("0"),
                side="BUY",
            )


class TestFeeEstimateUsdt:
    """fees = notional * fee_rate_bps / 10_000. No broker call."""

    def test_taker_5bps_on_2000_notional(self) -> None:
        assert fee_estimate_usdt(
            notional_usdt=Decimal("2000"), fee_rate_bps=Decimal("5")
        ) == Decimal("1.0")

    def test_zero_rate_is_zero(self) -> None:
        assert fee_estimate_usdt(
            notional_usdt=Decimal("2000"), fee_rate_bps=Decimal("0")
        ) == Decimal("0")

    def test_negative_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            fee_estimate_usdt(notional_usdt=Decimal("2000"), fee_rate_bps=Decimal("-1"))


class TestNetPnlUsdt:
    """Round-trip net PnL in USDT. funding=0 in the MVP."""

    def test_long_winner_minus_fees(self) -> None:
        # LONG (entry BUY) qty 1 @100 → exit @101, fees 0.5 each side.
        # gross = (101-100)*1 = 1.0 ; net = 1.0 - 0.5 - 0.5 = 0.0
        assert net_pnl_usdt(
            side="BUY",
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            qty=Decimal("1"),
            entry_fee_usdt=Decimal("0.5"),
            exit_fee_usdt=Decimal("0.5"),
        ) == Decimal("0.0")

    def test_short_winner_minus_fees(self) -> None:
        # SHORT (entry SELL) qty 1 @100 → exit (BUY) @99, fees 0.2 each side.
        # gross = (100-99)*1 = 1.0 ; net = 1.0 - 0.4 = 0.6
        assert net_pnl_usdt(
            side="SELL",
            entry_price=Decimal("100"),
            exit_price=Decimal("99"),
            qty=Decimal("1"),
            entry_fee_usdt=Decimal("0.2"),
            exit_fee_usdt=Decimal("0.2"),
        ) == Decimal("0.6")

    def test_long_loser(self) -> None:
        # LONG @100 → exit @99, no fees → gross -1.0
        assert net_pnl_usdt(
            side="BUY",
            entry_price=Decimal("100"),
            exit_price=Decimal("99"),
            qty=Decimal("1"),
            entry_fee_usdt=Decimal("0"),
            exit_fee_usdt=Decimal("0"),
        ) == Decimal("-1")


class TestNetReturnBps:
    """net_return_bps = net_pnl / entry_notional * 10_000."""

    def test_basic(self) -> None:
        # net 1.0 on 2000 notional → 5 bps
        assert net_return_bps(
            net_pnl_usdt=Decimal("1.0"), entry_notional_usdt=Decimal("2000")
        ) == Decimal("5")

    def test_zero_notional_raises(self) -> None:
        with pytest.raises(ValueError):
            net_return_bps(
                net_pnl_usdt=Decimal("1.0"), entry_notional_usdt=Decimal("0")
            )


class TestSpotAvgFillPrice:
    """Spot has no avgPrice field — derive from cumQuote / executedQty."""

    def test_basic(self) -> None:
        assert spot_avg_fill_price(
            cummulative_quote_qty=Decimal("200"), executed_qty=Decimal("2")
        ) == Decimal("100")

    def test_zero_executed_qty_is_none(self) -> None:
        # Unfilled order → no derivable fill price (caller treats as anomaly).
        assert (
            spot_avg_fill_price(
                cummulative_quote_qty=Decimal("0"), executed_qty=Decimal("0")
            )
            is None
        )


class TestBuildRoundTripEconomics:
    def test_complete_long_round_trip(self) -> None:
        econ = build_round_trip_economics(
            side="BUY",
            qty=Decimal("2"),
            entry_reference_price=Decimal("100"),
            entry_fill_price=Decimal("100"),
            fee_rate_bps=Decimal("0"),
            exit_fill_price=Decimal("101"),
            exit_reference_price=Decimal("101"),
        )
        assert econ.entry_notional_usdt == Decimal("200")
        assert econ.entry_slippage_bps == Decimal("0")
        assert econ.exit_slippage_bps == Decimal("0")
        assert econ.gross_pnl_usdt == Decimal("2")
        assert econ.net_pnl_usdt == Decimal("2")
        assert econ.net_return_bps == Decimal("100")

    def test_fees_reduce_net(self) -> None:
        # entry BUY fill 100 qty 2 → entry_notional 200; exit 101 qty 2.
        # fee 5bps: entry_fee 200*5/1e4=0.1 ; exit_fee 202*5/1e4=0.101
        econ = build_round_trip_economics(
            side="BUY",
            qty=Decimal("2"),
            entry_reference_price=Decimal("100"),
            entry_fill_price=Decimal("100"),
            fee_rate_bps=Decimal("5"),
            exit_fill_price=Decimal("101"),
            exit_reference_price=Decimal("101"),
        )
        assert econ.entry_fee_usdt == Decimal("0.1")
        assert econ.exit_fee_usdt == Decimal("0.101")
        # gross 2.0 - 0.1 - 0.101 = 1.799
        assert econ.net_pnl_usdt == Decimal("1.799")

    def test_short_round_trip_slippage_uses_opposite_side(self) -> None:
        # entry SELL ref 100 fill 99.90 (adverse +10bps); exit BUY ref 99 fill 99.05
        # exit is a BUY → adverse when fill > ref: (99.05-99)/99*1e4 ≈ 5.05 bps
        econ = build_round_trip_economics(
            side="SELL",
            qty=Decimal("1"),
            entry_reference_price=Decimal("100"),
            entry_fill_price=Decimal("99.90"),
            fee_rate_bps=Decimal("0"),
            exit_fill_price=Decimal("99.05"),
            exit_reference_price=Decimal("99"),
        )
        assert econ.entry_slippage_bps == Decimal("10")
        assert econ.exit_slippage_bps > Decimal("5")
        # SHORT gross = (entry_fill - exit_fill) * qty = (99.90 - 99.05) = 0.85
        assert econ.gross_pnl_usdt == Decimal("0.85")

    def test_anomaly_no_exit_leaves_exit_fields_none(self) -> None:
        econ = build_round_trip_economics(
            side="BUY",
            qty=Decimal("2"),
            entry_reference_price=Decimal("100"),
            entry_fill_price=Decimal("100.10"),
            fee_rate_bps=Decimal("5"),
            exit_fill_price=None,
        )
        # Entry economics still computed.
        assert econ.entry_notional_usdt == Decimal("200.20")
        assert econ.entry_slippage_bps == Decimal("10")
        assert econ.entry_fee_usdt is not None
        # No exit → no fabricated PnL.
        assert econ.exit_fee_usdt is None
        assert econ.exit_slippage_bps is None
        assert econ.gross_pnl_usdt is None
        assert econ.net_pnl_usdt is None
        assert econ.net_return_bps is None
