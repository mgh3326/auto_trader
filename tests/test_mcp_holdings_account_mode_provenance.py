"""ROB-357 — holdings ``account_mode`` provenance for crypto/Upbit.

Regression coverage for the bug where ``get_holdings(market="crypto")`` /
``get_holdings(account="upbit")`` stamped ``account_mode="kis_live"`` — the
KIS routing default — on responses whose positions are entirely Upbit.

The fix labels Upbit holdings as ``upbit_live`` (per-account and, for a
crypto/upbit-scoped query without an explicit KIS/paper selector, top-level)
WITHOUT altering the KIS order-routing default (``normalize_account_mode()``
still resolves to ``kis_live`` — covered by ``test_mcp_account_modes``).
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import portfolio_holdings
from tests._mcp_tooling_support import DummyMCP


def _upbit_position(symbol: str = "KRW-BTC") -> dict:
    return {
        "account": "upbit",
        "account_name": "업비트",
        "broker": "upbit",
        "source": "upbit_api",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": 100.0,
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


def _kis_position(symbol: str = "005930") -> dict:
    return {
        "account": "kis",
        "account_name": "기본 계좌",
        "broker": "kis",
        "source": "kis_api",
        "instrument_type": "equity_kr",
        "market": "kr",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": 100.0,
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


# ---------------------------------------------------------------------------
# Pure provenance helper.
# ---------------------------------------------------------------------------
def test_provenance_account_mode_upbit_is_upbit_live():
    assert (
        portfolio_holdings._provenance_account_mode(
            broker="upbit", source="upbit_api", routing_mode="kis_live"
        )
        == "upbit_live"
    )


def test_provenance_account_mode_kis_keeps_routing_mode():
    assert (
        portfolio_holdings._provenance_account_mode(
            broker="kis", source="kis_api", routing_mode="kis_live"
        )
        == "kis_live"
    )
    assert (
        portfolio_holdings._provenance_account_mode(
            broker="kis", source="kis_api", routing_mode="kis_mock"
        )
        == "kis_mock"
    )


# ---------------------------------------------------------------------------
# Per-account label inside the holdings impl.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_holdings_impl_labels_upbit_account_upbit_live(monkeypatch):
    async def fake_collect(**_kwargs):
        return [_upbit_position("KRW-BTC"), _kis_position("005930")], [], None, None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False,
        routing_account_mode="kis_live",
    )

    by_account = {a["account"]: a for a in result["accounts"]}
    assert by_account["upbit"]["account_mode"] == "upbit_live"
    assert by_account["kis"]["account_mode"] == "kis_live"


# ---------------------------------------------------------------------------
# Tool-level top-level label.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_holdings_tool_crypto_scope_top_level_upbit_live(monkeypatch):
    async def fake_collect(**_kwargs):
        return [_upbit_position("KRW-BTC")], [], "crypto", "upbit"

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_holdings"](
        market="crypto", include_current_price=False
    )

    assert result["account_mode"] == "upbit_live"


@pytest.mark.asyncio
async def test_get_holdings_tool_account_upbit_top_level_upbit_live(monkeypatch):
    async def fake_collect(**_kwargs):
        return [_upbit_position("KRW-XRP")], [], None, "upbit"

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_holdings"](
        account="upbit", include_current_price=False
    )

    assert result["account_mode"] == "upbit_live"


@pytest.mark.asyncio
async def test_get_holdings_tool_kr_kis_live_unchanged(monkeypatch):
    """Explicit KIS routing on a KR query must stay kis_live (no regression)."""

    async def fake_collect(**_kwargs):
        return [_kis_position("005930")], [], "equity_kr", None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_holdings"](
        market="kr", account_mode="kis_live", include_current_price=False
    )

    assert result["account_mode"] == "kis_live"
