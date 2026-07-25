import pytest

from app.services import trading_policy_service as tps


@pytest.mark.unit
def test_loss_cut_max_slip_reads_policy_value():
    tps._reset_cache_for_tests()
    assert tps.loss_cut_max_slip() == pytest.approx(0.02)


@pytest.mark.unit
def test_loss_cut_max_slip_visible_in_sell_lane():
    tps._reset_cache_for_tests()
    policy = tps.get_policy_for("crypto", "sell")
    assert "sell.loss_cut_max_slip" in policy["thresholds"]
    assert policy["thresholds"]["sell.loss_cut_max_slip"]["value"] == pytest.approx(
        0.02
    )
