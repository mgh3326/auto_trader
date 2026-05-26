import pytest

from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    def __init__(self):
        self.margin_calls = []
        self.holdings_calls = []

    async def inquire_overseas_margin(self, is_mock=False):
        self.margin_calls.append(is_mock)
        return [
            {"crcy_cd": "KRW", "natn_name": "한국", "frcr_dncl_amt1": 0.0},
            {"crcy_cd": "USD", "natn_name": "미국", "frcr_dncl_amt1": 500.0, "frcr_ord_psbl_amt1": 480.0},
        ]

    async def fetch_my_us_stocks(self, is_mock=False):
        self.holdings_calls.append(is_mock)
        return [{"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "TSLA"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_account_state_pins_is_mock_true():
    fake = _FakeKis()
    adapter = KisMockUsAdapter(kis_client=fake, enabled=True)
    summary = await adapter.read_account_state()
    assert fake.margin_calls == [True]
    assert fake.holdings_calls == [True]
    assert summary.cash_usd == pytest.approx(500.0)
    assert summary.buying_power_usd == pytest.approx(480.0)
    assert summary.position_count == 2


@pytest.mark.unit
def test_account_scope_is_canonical_kis_mock():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    assert adapter.account_scope == "kis_mock"  # NOT kis_mock_us
