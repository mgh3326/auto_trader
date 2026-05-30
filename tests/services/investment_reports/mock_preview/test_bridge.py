import pytest

from app.services.investment_reports.mock_preview.bridge import (
    MockPreviewBridge,
    OrderParams,
    extract_order_params,
)


@pytest.mark.unit
def test_extract_order_params_happy_path() -> None:
    params = extract_order_params(
        symbol="AAPL",
        evidence_snapshot={"reference_price_usd": 200.0},
        max_action={"notional_usd": 50.0},
    )
    assert params == OrderParams(
        symbol="AAPL",
        quantity=pytest.approx(0.25),
        limit_price_usd=200.0,
        notional_cap_usd=50.0,
        reference_price_usd=200.0,
    )


@pytest.mark.unit
def test_extract_order_params_skips_when_no_price() -> None:
    assert extract_order_params(
        symbol="AAPL", evidence_snapshot={}, max_action={}
    ) is None


@pytest.mark.unit
def test_extract_order_params_skips_when_no_symbol() -> None:
    assert extract_order_params(
        symbol=None, evidence_snapshot={"reference_price_usd": 10.0}, max_action={}
    ) is None


@pytest.mark.asyncio
async def test_bridge_fail_closed_when_adapter_disabled() -> None:
    """No KIS_MOCK_* env -> adapter disabled -> 'unsupported', names only, no submit."""
    from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

    bridge = MockPreviewBridge(adapter=KisMockUsAdapter(enabled=False))
    out = await bridge.preview(
        OrderParams(
            symbol="AAPL", quantity=0.25, limit_price_usd=200.0,
            notional_cap_usd=50.0, reference_price_usd=200.0,
        )
    )
    assert out["status"] == "unsupported"
    assert out["submit_enabled"] is False
    # names only — never values
    assert "KIS_MOCK_APP_KEY" in out.get("missing_env_keys", []) or out.get(
        "missing_env_keys"
    ) is not None


@pytest.mark.asyncio
async def test_bridge_previews_kis_mock_only_no_alpaca() -> None:
    """When enabled with a stub client, only kis_mock broker appears; submit off."""
    from app.schemas.us_dual_paper import (
        AccountStateSummary,
        BrokerPreviewRequest,
        BrokerPreviewResult,
        DualPaperBrokerStatus,
    )
    from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

    class _StubAdapter(KisMockUsAdapter):
        def is_enabled(self) -> bool:  # bypass env gate
            return True

        async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
            return BrokerPreviewResult(
                account_scope="kis_mock",
                status=DualPaperBrokerStatus.PREVIEWED,
                quantity=req.quantity,
                limit_price_usd=req.limit_price_usd,
                notional_usd=req.quantity * req.limit_price_usd,
                account_state=AccountStateSummary(buying_power_usd=1000.0),
            )

    bridge = MockPreviewBridge(adapter=_StubAdapter(enabled=True))
    out = await bridge.preview(
        OrderParams(
            symbol="AAPL", quantity=0.25, limit_price_usd=200.0,
            notional_cap_usd=50.0, reference_price_usd=200.0,
        )
    )
    assert out["status"] == "previewed"
    assert out["account_scope"] == "kis_mock"
    assert out["submit_enabled"] is False
    assert "alpaca_paper" not in out  # no Alpaca evidence mixed in
