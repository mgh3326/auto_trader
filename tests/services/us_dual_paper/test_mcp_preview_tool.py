import pytest

from app.mcp_server.tooling import us_dual_paper as tool_mod
from app.mcp_server.tooling.us_dual_paper import (
    US_DUAL_PAPER_TOOL_NAMES,
    us_dual_paper_preview,
)


@pytest.mark.unit
def test_preview_tool_name_registered():
    assert "us_dual_paper_preview" in US_DUAL_PAPER_TOOL_NAMES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_tool_returns_per_broker_packet(monkeypatch):
    from app.schemas.us_dual_paper import (
        BrokerPreviewResult,
        DualBrokerPreviewPacket,
        DualPaperBrokerStatus,
    )

    async def _fake_build(**kwargs):
        return DualBrokerPreviewPacket(
            symbol=kwargs["symbol"],
            limit_price_source=kwargs["limit_price_source"],
            notional_cap_usd=kwargs["notional_cap_usd"],
            brokers={
                "alpaca_paper": BrokerPreviewResult(
                    account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED
                ),
                "kis_mock": BrokerPreviewResult(
                    account_scope="kis_mock", status=DualPaperBrokerStatus.BLOCKED
                ),
            },
        )

    monkeypatch.setattr(tool_mod, "build_packet", _fake_build)
    out = await us_dual_paper_preview(
        symbol="NVDA", quantity=1, limit_price_usd=10.0, notional_cap_usd=50.0
    )
    assert out["submit_enabled"] is False
    assert out["brokers"]["alpaca_paper"]["status"] == "previewed"
    assert out["brokers"]["kis_mock"]["status"] == "blocked"
