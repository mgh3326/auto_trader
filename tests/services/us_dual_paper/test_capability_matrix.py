import pytest

from app.services.us_dual_paper.capability_matrix import (
    SUPPORTED_ACCOUNT_SCOPES,
    get_capability_matrix,
)


@pytest.mark.unit
def test_matrix_covers_both_brokers():
    matrix = get_capability_matrix()
    assert set(matrix) == {"kis_mock", "alpaca_paper"}
    assert SUPPORTED_ACCOUNT_SCOPES == ("kis_mock", "alpaca_paper")


@pytest.mark.unit
@pytest.mark.parametrize("scope", ["kis_mock", "alpaca_paper"])
def test_matrix_entry_is_long_limit_paper_only(scope):
    entry = get_capability_matrix()[scope]
    assert entry["market"] == "us"
    assert entry["asset_class"] == "us_equity"
    assert entry["supported_sides"] == ["buy"]
    assert entry["supported_order_types"] == ["limit"]
    assert entry["preview_supported"] is True
    assert entry["submit_gate"] == "confirm_only_default_disabled"
    assert entry["positions_read"] is True  # holdings read works for both
    assert "no_kis_mock_us_alias" not in entry  # canonical scope token only


@pytest.mark.unit
def test_kis_mock_overseas_cash_read_supported_but_open_orders_unsupported():
    # ROB-951 verified VTTS3007R mock-US buying power. Pending-order inquiry
    # (TTTS3018R) remains unsupported, so only cash-read capability flips.
    entry = get_capability_matrix()["kis_mock"]
    assert entry["account_cash_read"] is True
    assert entry["open_orders_read"] is False
    assert entry["positions_read"] is True  # holdings read works on mock host


@pytest.mark.unit
def test_alpaca_paper_reads_supported():
    entry = get_capability_matrix()["alpaca_paper"]
    assert entry["account_cash_read"] is True
    assert entry["positions_read"] is True
    assert entry["open_orders_read"] is True
