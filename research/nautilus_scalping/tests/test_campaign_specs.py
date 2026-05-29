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


def _ramp(start_ts, n, base=100.0, step=1.0):
    return [(start_ts + i * DAY, base + i * step) for i in range(n)]


def test_breakout_spec_pools_trades_across_symbols():
    panel = {"AUSDT": _ramp(0, 40), "BUSDT": _ramp(0, 40, base=50.0, step=0.5)}
    spec = campaign_specs.breakout_spec(panel, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family1_breakout_continuation"
    assert spec["kind"] == "trade"
    assert all(hasattr(t, "net_ref_pnl") for t in spec["data"])
    assert spec["summary"].sample_count == len(spec["data"])
    assert spec["maker_conservative_net"] is None


def test_ts_trend_spec_is_portfolio():
    panel = {"AUSDT": _ramp(0, 40), "BUSDT": _ramp(0, 40, base=50.0, step=-0.3)}
    spec = campaign_specs.ts_trend_spec(panel, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family2_ts_trend_basket"
    assert spec["kind"] == "portfolio"
    assert all(isinstance(p, PortfolioPeriod) for p in spec["data"])


def test_xs_momentum_spec_is_portfolio_pit_aware():
    panel = {s: _ramp(0, 40, base=b) for s, b in [("AUSDT", 100), ("BUSDT", 50), ("CUSDT", 75)]}
    import pit_universe
    m = pit_universe.PITManifest.from_records([{"symbol": s, "listed_from": 0} for s in panel])
    rebals = [10 * DAY, 17 * DAY, 24 * DAY, 31 * DAY]
    spec = campaign_specs.xs_momentum_spec(panel, rebals, m, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family3_xs_momentum"
    assert spec["kind"] == "portfolio"
