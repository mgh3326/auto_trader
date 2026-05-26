import pytest

from app.schemas.us_dual_paper import BrokerPreviewRequest, DualPaperBrokerStatus
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    async def inquire_overseas_margin(self, is_mock=False):
        return [{"crcy_cd": "USD", "natn_name": "미국", "frcr_dncl_amt1": 500.0, "frcr_ord_psbl_amt1": 40.0}]

    async def fetch_my_us_stocks(self, is_mock=False):
        return []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_cap_and_buying_power_is_previewed():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.PREVIEWED
    assert res.notional_usd == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_over_cap_is_blocked():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=10, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "notional_exceeds_cap" in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insufficient_buying_power_is_blocked():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)  # buying power = 40
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=45.0, notional_cap_usd=50.0)
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "insufficient_buying_power" in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_reference_price_warns_not_blocks():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0)
    )
    assert "reference_price_missing_for_limit_sanity" in res.warnings
    assert res.status is DualPaperBrokerStatus.PREVIEWED
