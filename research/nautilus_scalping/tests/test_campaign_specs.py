import campaign_specs
import families
from validated_gate import PortfolioPeriod

DAY = 86_400_000


def test_summary_from_trades_gross_net_and_oos_split():
    split = 10 * DAY
    trades = [
        families.make_taker_trade(0.01 * 1000.0, 5 * DAY, 1000.0),
        families.make_taker_trade(0.005 * 1000.0, 6 * DAY, 1000.0),
        families.make_taker_trade(-0.002 * 1000.0, 20 * DAY, 1000.0),
    ]
    s = campaign_specs._summary_from_trades("f1", trades, split)
    assert s.sample_count == 3
    assert round(s.gross_expectancy_bps, 6) == round((100 + 50 - 20) / 3, 6)
    assert round(s.fee_adjusted_bps, 6) == round(((100 - 20) + (50 - 20) + (-20 - 20)) / 3, 6)
    assert round(s.oos_gross_bps, 6) == -20.0
    assert round(s.oos_fee_adjusted_bps, 6) == -40.0


def test_summary_from_periods_uses_notional_bps():
    split = 10 * DAY
    periods = [
        PortfolioPeriod(ts=5 * DAY, gross_ref_pnl=8.0, commission_ref=2.0),
        PortfolioPeriod(ts=20 * DAY, gross_ref_pnl=-4.0, commission_ref=1.0),
    ]
    s = campaign_specs._summary_from_periods("f2", periods, split, notional=1000.0)
    assert s.sample_count == 2
    assert round(s.gross_expectancy_bps, 6) == round((100 + (-30)) / 2, 6)
    assert round(s.fee_adjusted_bps, 6) == round((80 + (-40)) / 2, 6)
    assert round(s.oos_gross_bps, 6) == -30.0
    assert round(s.oos_fee_adjusted_bps, 6) == -40.0


def test_summary_empty_is_safe():
    s = campaign_specs._summary_from_trades("f1", [], 0)
    assert s.sample_count == 0
    assert s.gross_expectancy_bps == 0.0 and s.oos_gross_bps is None
