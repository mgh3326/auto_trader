import pytest

from app.schemas.us_dual_paper import BrokerPreviewRequest, DualPaperBrokerStatus
from app.services.us_dual_paper.adapters import alpaca as alpaca_mod
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter


@pytest.fixture
def _stub_preview(monkeypatch):
    async def _fake(
        symbol, side, type, qty=None, notional=None, limit_price=None, **kw
    ):  # noqa: A002
        cost = float(qty) * float(limit_price)
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "preview": True,
            "submitted": False,
            "estimated_cost": str(cost),
            "account_context": {"cash": "1000", "buying_power": "1000"},
            "would_exceed_buying_power": cost > 1000,
            "warnings": [],
        }

    monkeypatch.setattr(alpaca_mod, "alpaca_paper_preview_order", _fake)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_within_cap_is_previewed(_stub_preview):
    adapter = AlpacaPaperAdapter()
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0
        )
    )
    assert res.account_scope == "alpaca_paper"
    assert res.status is DualPaperBrokerStatus.PREVIEWED
    assert res.notional_usd == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_over_cap_is_blocked(_stub_preview):
    adapter = AlpacaPaperAdapter()
    res = await adapter.preview(
        BrokerPreviewRequest(
            symbol="NVDA", quantity=10, limit_price_usd=10.0, notional_cap_usd=50.0
        )
    )
    assert res.status is DualPaperBrokerStatus.BLOCKED
    assert "notional_exceeds_cap" in res.blocked_reasons
