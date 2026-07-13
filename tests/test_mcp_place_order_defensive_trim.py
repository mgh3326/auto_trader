"""Regression tests for the fail-closed defensive_trim direct path."""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock

import pytest

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.caller_identity import caller_agent_id_var, caller_source_var
from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.orders_registration import register_order_tools
from tests._mcp_tooling_support import build_tools


def _mock_crypto_sell_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 1000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "balance": "2.0",
                    "locked": "0",
                    "avg_buy_price": "1000.0",
                }
            ]
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_defensive_trim_fails_closed_with_proposal_guidance() -> None:
    with pytest.raises(
        ValueError,
        match="defensive_trim_direct_path_disabled_use_order_proposal_create",
    ):
        await order_validation._validate_defensive_trim_preconditions(
            defensive_trim=True,
            approval_issue_id="legacy audit note",
            side="sell",
            order_type="limit",
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["limit", "market"])
async def test_place_order_defensive_trim_direct_path_returns_guidance(
    order_type,
) -> None:
    result = await build_tools()["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type=order_type,
        quantity=1.0,
        price=1005.0,
        defensive_trim=True,
        approval_issue_id="legacy audit note",
        dry_run=True,
    )

    assert result["success"] is False
    assert result["error"] == (
        "defensive_trim_direct_path_disabled_use_order_proposal_create"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generic_paper_loss_cut_cannot_bypass_proposal_path() -> None:
    result = await build_tools()["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=1005.0,
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        account_type="paper",
        dry_run=True,
    )

    assert result["success"] is False
    assert result["error"] == (
        "loss_cut_direct_path_disabled_use_order_proposal_create"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_defensive_sell_keeps_existing_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_crypto_sell_context(monkeypatch)

    result = await build_tools()["place_order"](
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
def test_place_order_description_routes_defensive_trim_to_proposals() -> None:
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
    assert "order_proposal_create" in description
    assert "approval issue status=done" not in description
    assert "requester_agent_id" not in description


@pytest.mark.unit
def test_place_order_signature_removes_requester_agent_id() -> None:
    signature = inspect.signature(build_tools()["place_order"])
    assert "requester_agent_id" not in signature.parameters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_order_history_persists_caller_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.pushed: list[tuple[str, str]] = []

        async def rpush(self, key: str, value: str) -> None:
            self.pushed.append((key, value))

        async def expire(self, key: str, seconds: int) -> None:
            del key, seconds

    fake_redis = FakeRedis()
    monkeypatch.setattr(settings, "redis_url", "redis://test", raising=False)
    monkeypatch.setattr("redis.asyncio.from_url", AsyncMock(return_value=fake_redis))
    agent_token = caller_agent_id_var.set("trader-test")
    source_token = caller_source_var.set("http_header")
    try:
        await order_validation._record_order_history(
            symbol="KRW-BTC",
            side="sell",
            order_type="limit",
            quantity=1.0,
            price=1005.0,
            amount=1005.0,
            reason="defensive trim",
            dry_run=False,
            defensive_trim=True,
            approval_issue_id="legacy audit note",
            requester_agent_id="trader-test",
        )
    finally:
        caller_source_var.reset(source_token)
        caller_agent_id_var.reset(agent_token)

    record = json.loads(fake_redis.pushed[0][1])
    assert record["caller_source"] == "http_header"


@pytest.mark.unit
def test_kis_live_description_routes_defensive_trim_to_proposals() -> None:
    from app.mcp_server.tooling.orders_kis_variants import register_kis_live_order_tools

    captured: dict[str, str] = {}

    class CapturingMCP:
        def tool(self, *, name: str, description: str = ""):
            captured[name] = description

            def decorator(func):
                return func

            return decorator

    register_kis_live_order_tools(CapturingMCP())
    assert "order_proposal_create" in captured["kis_live_place_order"]
