import campaign_controls
import pit_universe

DAY = 86_400_000


def test_weekly_rebalances_inclusive_step():
    r = campaign_controls.weekly_rebalances(0, 21 * DAY, step_days=7)
    assert r == [0, 7 * DAY, 14 * DAY, 21 * DAY]


def test_buy_hold_bps_close_to_close():
    series = [(0, 100.0), (DAY, 110.0)]
    assert round(campaign_controls.buy_hold_bps(series), 6) == 1000.0
    assert campaign_controls.buy_hold_bps([]) == 0.0


def test_max_drawdown_bps_on_cumulative_pnl():
    dd = campaign_controls.max_drawdown_bps([50.0, -120.0, 10.0], notional=1000.0)
    assert dd < 0 and round(dd, 2) == round(-120.0 / 1050.0 * 1e4, 2)


def test_filter_universe_uses_membership_and_quality():
    m = pit_universe.PITManifest.from_records([
        {"symbol": "GOOD", "listed_from": 0, "delisted_at": None, "status": "live",
         "kline_coverage": 1.0, "confidence": "high"},
        {"symbol": "LOWCOV", "listed_from": 0, "delisted_at": None, "status": "live",
         "kline_coverage": 0.5, "confidence": "low"},
        {"symbol": "OUTWINDOW", "listed_from": 100 * DAY, "delisted_at": None, "status": "live",
         "kline_coverage": 1.0, "confidence": "high"},
    ])
    kept = campaign_controls.filter_universe(m, lo_ts=0, hi_ts=10 * DAY)
    assert kept == ["GOOD"]
