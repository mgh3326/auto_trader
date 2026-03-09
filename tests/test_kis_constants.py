import pytest

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
