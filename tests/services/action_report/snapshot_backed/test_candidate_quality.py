from app.services.action_report.snapshot_backed.candidate_quality import (
    compute_priority_score,
    compute_quality_flags,
    confidence_cap_for,
)


def test_penny_and_illiquid_flags():
    qf = compute_quality_flags(
        latest_close=3.5,
        daily_volume=1_000_000,
        change_rate=2.0,
        week_change_rate=1.0,
        is_common_stock=True,
        screener_stale=False,
    )
    assert "penny" in qf  # 3.5 < 5.0
    assert "illiquid" in qf  # 3.5 * 1e6 = 3.5e6 < 5e6


def test_liquid_large_price_clean():
    qf = compute_quality_flags(
        latest_close=150.0,
        daily_volume=10_000_000,
        change_rate=2.0,
        week_change_rate=1.0,
        is_common_stock=True,
        screener_stale=False,
    )
    assert qf == frozenset()


def test_abnormal_spike_percent_units():
    assert "abnormal_spike" in compute_quality_flags(
        latest_close=50.0,
        daily_volume=10_000_000,
        change_rate=16.0,
        week_change_rate=1.0,
        is_common_stock=True,
        screener_stale=False,
    )
    assert "abnormal_spike" in compute_quality_flags(
        latest_close=50.0,
        daily_volume=10_000_000,
        change_rate=1.0,
        week_change_rate=51.0,
        is_common_stock=True,
        screener_stale=False,
    )


def test_common_stock_flag_tri_state():
    assert "non_common_stock" in compute_quality_flags(
        latest_close=50.0,
        daily_volume=10_000_000,
        change_rate=1.0,
        week_change_rate=1.0,
        is_common_stock=False,
        screener_stale=False,
    )
    assert "common_stock_unknown" in compute_quality_flags(
        latest_close=50.0,
        daily_volume=10_000_000,
        change_rate=1.0,
        week_change_rate=1.0,
        is_common_stock=None,
        screener_stale=False,
    )


def test_priority_score_orders_liquid_over_illiquid():
    big = compute_priority_score(
        latest_close=100.0,
        daily_volume=50_000_000,
        change_rate=5.0,
        quality_flags=frozenset(),
    )
    small = compute_priority_score(
        latest_close=4.0,
        daily_volume=100_000,
        change_rate=5.0,
        quality_flags=frozenset({"illiquid", "penny"}),
    )
    assert big > small


def test_confidence_cap_for_stale():
    assert confidence_cap_for(frozenset({"screener_stale"})) == 40
    assert confidence_cap_for(frozenset()) is None
