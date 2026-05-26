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
    assert entry["account_cash_read"] is True
    assert "no_kis_mock_us_alias" not in entry  # canonical scope token only
