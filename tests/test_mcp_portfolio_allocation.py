import pytest

from app.mcp_server.tooling import portfolio_allocation
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
async def test_get_portfolio_allocation_handler_rolls_up_positions_cash_and_errors(
    monkeypatch,
) -> None:
    async def fake_collect_positions(**kwargs):
        assert kwargs["account"] is None
        assert kwargs["market"] is None
        assert kwargs["include_current_price"] is True
        return (
            [
                {
                    "account": "kis",
                    "account_name": "기본 계좌",
                    "broker": "kis",
                    "instrument_type": "equity_us",
                    "market": "us",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "evaluation_amount": 1000.0,
                }
            ],
            [{"source": "holdings", "error": "partial"}],
            None,
            None,
        )

    async def fake_cash_balance(**kwargs):
        assert kwargs["account"] is None
        return {
            "accounts": [
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "currency": "KRW",
                    "balance": 200000.0,
                }
            ],
            "errors": [{"source": "cash", "error": "partial"}],
        }

    monkeypatch.setattr(
        portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions
    )
    monkeypatch.setattr(
        portfolio_allocation, "get_cash_balance_impl", fake_cash_balance
    )
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", lambda: [])

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        account=None,
        market=None,
        include_cash=True,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(1600000.0)
    assert result["errors"] == [
        {"source": "holdings", "error": "partial"},
        {"source": "cash", "error": "partial"},
    ]


@pytest.mark.asyncio
async def test_get_portfolio_allocation_tool_is_registered(monkeypatch) -> None:
    async def fake_impl(**kwargs):
        assert kwargs["include_cash"] is True
        return {"ok": True}

    monkeypatch.setattr(
        portfolio_allocation, "get_portfolio_allocation_impl", fake_impl
    )
    mcp = DummyMCP()
    portfolio_allocation.register_portfolio_allocation_tool(mcp)

    result = await mcp.tools["get_portfolio_allocation"]()

    assert result == {"ok": True, "account_mode": "kis_live"}


@pytest.mark.asyncio
async def test_get_portfolio_allocation_tool_passes_kis_mock(monkeypatch) -> None:
    calls = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"summary": {"total_value_krw": 0.0}}

    monkeypatch.setattr(
        portfolio_allocation, "get_portfolio_allocation_impl", fake_impl
    )
    monkeypatch.setattr(portfolio_allocation, "validate_kis_mock_config", lambda: [])
    mcp = DummyMCP()
    portfolio_allocation.register_portfolio_allocation_tool(mcp)

    result = await mcp.tools["get_portfolio_allocation"](account_mode="kis_mock")

    assert result["account_mode"] == "kis_mock"
    assert calls[0]["is_mock"] is True


@pytest.mark.asyncio
async def test_get_portfolio_allocation_can_exclude_cash(monkeypatch) -> None:
    async def fake_collect_positions(**kwargs):
        return ([], [], None, None)

    async def fail_cash_balance(**kwargs):
        raise AssertionError("cash balance must not be queried when include_cash=False")

    monkeypatch.setattr(
        portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions
    )
    monkeypatch.setattr(
        portfolio_allocation, "get_cash_balance_impl", fail_cash_balance
    )
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", lambda: [])

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        include_cash=False,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["summary"]["cash_value_krw"] == pytest.approx(0.0)
    assert result["cash"] == []


@pytest.mark.asyncio
async def test_get_portfolio_allocation_krx_etf_failure_is_degraded(
    monkeypatch,
) -> None:
    async def fake_collect_positions(**kwargs):
        return (
            [
                {
                    "account": "kis",
                    "account_name": "기본 계좌",
                    "broker": "kis",
                    "instrument_type": "equity_kr",
                    "market": "kr",
                    "symbol": "360750",
                    "name": "TIGER 미국S&P500",
                    "evaluation_amount": 100000.0,
                }
            ],
            [],
            None,
            None,
        )

    async def raise_krx():
        raise RuntimeError("KRX unavailable")

    monkeypatch.setattr(
        portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions
    )
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", raise_krx)

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        include_cash=False,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["errors"][0]["source"] == "krx_etf"
    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["kr_equity"]["value_krw"] == pytest.approx(100000.0)
    assert result["lookthrough"] == []
