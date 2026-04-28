import pytest

from app.services.brokers.kis import constants
from app.services.brokers.kis.constants import get_mock_tr_id


@pytest.mark.unit
class TestGetMockTrId:
    def test_returns_original_tr_id_when_not_mock(self) -> None:
        assert get_mock_tr_id("TTTC8434R", is_mock=False) == "TTTC8434R"

    def test_converts_domestic_ttt_prefix_once_for_mock(self) -> None:
        assert get_mock_tr_id("TTTC8434R", is_mock=True) == "VTTC8434R"

    def test_converts_only_first_ttt_prefix_for_mock(self) -> None:
        assert get_mock_tr_id("TTTT1002U", is_mock=True) == "VTTT1002U"

    def test_keeps_non_ttt_tr_id_unchanged_for_mock(self) -> None:
        assert get_mock_tr_id("FHKST03010230", is_mock=True) == "FHKST03010230"


def test_mock_unsupported_tr_set_is_documented():
    """TR IDs that have no verified mock equivalent must remain documented.

    If you add a working VTTxxx mock TR for one of these endpoints, also:
    1. Add the constant to constants.py (e.g., DOMESTIC_ORDER_INQUIRY_TR_MOCK)
    2. Remove the broker's `if is_mock: raise RuntimeError(...)` guard
    3. Update docs/kis-mock-tr-routing-matrix.md
    4. Update app/mcp_server/README.md "KIS mock unsupported endpoints"
    5. Remove the entry from this set
    """
    mock_unsupported = {
        constants.DOMESTIC_ORDER_INQUIRY_TR,  # TTTC8036R
        constants.OVERSEAS_ORDER_INQUIRY_TR,  # TTTS3018R
        constants.INTEGRATED_MARGIN_TR,  # TTTC0869R
        constants.OVERSEAS_MARGIN_TR,  # TTTC2101R (operator-confirmed unreliable on mock)
    }
    assert mock_unsupported == {
        "TTTC8036R",
        "TTTS3018R",
        "TTTC0869R",
        "TTTC2101R",
    }
