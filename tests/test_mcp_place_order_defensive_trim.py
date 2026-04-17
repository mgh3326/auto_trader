"""Unit tests for defensive_trim gating in place_order."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.tooling import order_execution, order_validation
from app.mcp_server.tooling.orders_registration import register_order_tools
from tests._mcp_tooling_support import build_tools

TRADER_AGENT_ID = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"


def _mock_crypto_sell_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_price: float = 1000.0,
    balance: str = "2.0",
    avg_buy_price: str = "1000.0",
) -> None:
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": current_price}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "balance": balance,
                    "locked": "0",
                    "avg_buy_price": avg_buy_price,
                }
            ]
        ),
    )


@pytest.fixture(autouse=True)
def _set_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trader_agent_id", TRADER_AGENT_ID, raising=False)
    monkeypatch.setattr(
        settings,
        "paperclip_api_url",
        "https://paperclip.local",
        raising=False,
    )
    monkeypatch.setattr(settings, "paperclip_api_key", "test-token", raising=False)
    order_validation._defensive_trim_success_cache.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_defensive_trim_schema_blocks_market_even_with_flag() -> None:
    """defensive_trim path rejects market orders."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "defensive_trim requires order_type='limit' (market orders are blocked)"
    )


@pytest.mark.unit
def test_place_order_description_documents_four_and_defensive_trim_gate() -> None:
    """Public tool description documents every defensive_trim gate."""

    class CapturingMCP:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, name: str, description: str):
            self.descriptions[name] = description

            def decorator(func):
                return func

            return decorator

    mcp = CapturingMCP()
    register_order_tools(mcp)  # type: ignore[arg-type]

    description = mcp.descriptions["place_order"]
    assert "defensive_trim=True" in description
    assert "(a) side='sell'" in description
    assert "(b) order_type='limit'" in description
    assert "(c) valid approval_issue_id" in description
    assert "(d) requester_agent_id matching Trader" in description
    assert "approval issue status=done" in description
    assert "requester_agent_id is caller-asserted" in description
    assert "ST-3" in description
    assert "ROB-164/ROB-166" in description


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_limit_below_floor_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """defensive_trim=False keeps avg*1.01 floor enforcement."""
    tools = build_tools()
    _mock_crypto_sell_context(monkeypatch, current_price=1000.0, avg_buy_price="1000.0")

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert "below minimum" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_with_valid_approval_and_trader_caller_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid 4-gate combination allows floor bypass in preview."""
    tools = build_tools()
    _mock_crypto_sell_context(monkeypatch, current_price=1000.0, avg_buy_price="1000.0")
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(return_value="done"),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["defensive_trim"] is True
    assert result["approval_issue_id"] == "ROB-164"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_floor_bypass_logs_structured_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """defensive_trim floor bypass emits the ST-2 audit warning fields."""
    tools = build_tools()
    _mock_crypto_sell_context(monkeypatch, current_price=1000.0, avg_buy_price="1000.0")
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(return_value="done"),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is True
    warning_records = [
        record
        for record in caplog.records
        if record.message.startswith("defensive_trim_bypass_active")
    ]
    assert {record.phase for record in warning_records} == {"execution", "preview"}
    for record in warning_records:
        assert record.approval_issue_id == "ROB-164"
        assert record.requester_agent_id == TRADER_AGENT_ID
        assert record.symbol == "KRW-BTC"
        assert record.price == pytest.approx(1005.0)
        assert record.min_sell_price == pytest.approx(1010.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_missing_approval_id_rejected() -> None:
    """defensive_trim requires approval_issue_id."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert result["error"] == "defensive_trim=True requires approval_issue_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_malformed_approval_id_rejected() -> None:
    """approval_issue_id must match ticket-like format."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="bad-format",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"] == "approval_issue_id format invalid (expected e.g. 'ROB-164')"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_approval_not_done_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval must exist and be in done status."""
    tools = build_tools()
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(return_value="in_progress"),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"] == "approval_issue_id ROB-164 not found or not in 'done' status"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_paperclip_api_timeout_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paperclip API timeout fails closed."""
    tools = build_tools()
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(side_effect=TimeoutError("timeout")),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"] == "approval_issue_id ROB-164 not found or not in 'done' status"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_non_trader_caller_rejected() -> None:
    """Only trader agent can use defensive_trim."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id="other-agent",
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "defensive_trim requires Trader agent caller (got requester_agent_id=other-agent)"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_missing_caller_id_rejected() -> None:
    """defensive_trim requires caller identity."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        dry_run=True,
    )

    assert result["success"] is False
    assert result["error"] == "requester_agent_id is required for defensive_trim"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_still_rejects_below_current_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """defensive_trim bypasses floor only, not current-price guard."""
    tools = build_tools()
    _mock_crypto_sell_context(monkeypatch, current_price=1000.0, avg_buy_price="900.0")
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(return_value="done"),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=990.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert "below current price" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_journal_and_redis_record_defensive_trim_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful defensive trim sell records journal and Redis audit fields."""
    tools = build_tools()
    _mock_crypto_sell_context(monkeypatch, current_price=1000.0, avg_buy_price="1000.0")
    monkeypatch.setattr(
        order_validation,
        "_fetch_approval_issue_status",
        AsyncMock(return_value="done"),
    )

    record_mock = AsyncMock()
    monkeypatch.setattr(order_execution, "_record_order_history", record_mock)
    monkeypatch.setattr(
        upbit_service,
        "place_sell_order",
        AsyncMock(return_value={"uuid": "defensive-trim-uuid"}),
    )
    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=9191)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())
    close_mock = AsyncMock(
        return_value={
            "journals_closed": 1,
            "journals_kept": 0,
            "closed_ids": [71],
            "total_pnl_pct": 0.5,
        }
    )
    monkeypatch.setattr(order_execution, "_close_journals_on_sell", close_mock)

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["journals_closed"] == 1

    record_mock.assert_awaited_once()
    kwargs = record_mock.await_args.kwargs
    assert kwargs["defensive_trim"] is True
    assert kwargs["approval_issue_id"] == "ROB-164"
    assert kwargs["requester_agent_id"] == TRADER_AGENT_ID

    close_mock.assert_awaited_once()
    close_kwargs = close_mock.await_args.kwargs
    assert close_kwargs["defensive_trim_ctx"].approval_issue_id == "ROB-164"
    assert close_kwargs["defensive_trim_ctx"].requester_agent_id == TRADER_AGENT_ID


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_buy_side_rejected() -> None:
    """defensive_trim is sell-only."""
    tools = build_tools()

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        defensive_trim=True,
        approval_issue_id="ROB-164",
        requester_agent_id=TRADER_AGENT_ID,
        dry_run=True,
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "defensive_trim requires side='sell' (buy orders always use existing path)"
    )
