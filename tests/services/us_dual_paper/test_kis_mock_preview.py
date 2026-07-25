import pytest

from app.schemas.us_dual_paper import BrokerPreviewRequest, DualPaperBrokerStatus
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


class _FakeKis:
    """VTTS3007R-only mock client with $40 of USD buying power."""

    def __init__(self, orderable: float | None = 40.0):
        self._orderable = orderable

    async def inquire_mock_overseas_buyable_amount(self):
        return {
            "ovrs_ord_psbl_amt": self._orderable,
            "sll_ruse_psbl_amt": 13.95,
            "exrt": 1488.88,
            "raw": {},
        }

    async def fetch_my_us_stocks(self, is_mock=False):
        return []


class _FakeKisUnreadable(_FakeKis):
    async def inquire_mock_overseas_buyable_amount(self):
        raise RuntimeError("VTTS3007R timeout")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_cap_and_buying_power_is_previewed():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0
        )
    )
    assert res.status is DualPaperBrokerStatus.PREVIEWED
    assert res.notional_usd == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_over_cap_is_blocked():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=10, limit_price_usd=10.0, notional_cap_usd=50.0
        )
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "notional_exceeds_cap" in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insufficient_buying_power_is_blocked():
    """ROB-951 fail-open regression.

    With the reader on the deprecated OPSQ0002 path, buying power read as None
    and a $45 preview against $40 of real buying power passed as PREVIEWED
    with only a warning. It must block.
    """
    adapter = KisMockUsAdapter(kis_client=_FakeKis(orderable=40.0), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=1, limit_price_usd=45.0, notional_cap_usd=50.0
        )
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "insufficient_buying_power" in res.blocked_reasons
    assert "buying_power_unavailable" not in res.warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreadable_buying_power_warns_as_unverified():
    """Fail-closed: an unreadable balance is surfaced, not assumed sufficient."""
    adapter = KisMockUsAdapter(kis_client=_FakeKisUnreadable(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=1, limit_price_usd=45.0, notional_cap_usd=50.0
        )
    )
    assert "buying_power_unavailable" in res.warnings
    assert "insufficient_buying_power" not in res.blocked_reasons


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_reference_price_warns_not_blocks():
    adapter = KisMockUsAdapter(kis_client=_FakeKis(), enabled=True)
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0
        )
    )
    assert "reference_price_missing_for_limit_sanity" in res.warnings
    assert res.status is DualPaperBrokerStatus.PREVIEWED
