from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from app.core.config import Settings, settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.paper_execution_registration import (
    PAPER_EXECUTION_TOOL_NAMES,
    PaperOrderToolInput,
    build_paper_execution_application,
    register_paper_execution_tools,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter
from app.services.brokers.binance.paper_adapter import BinanceSpotDemoPaperAdapter
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
)
from tests._mcp_tooling_support import DummyMCP

EXPECTED_TOOLS = {
    "paper_execution_get_capabilities",
    "paper_execution_preview_order",
    "paper_execution_submit_order",
    "paper_execution_cancel_order",
    "paper_execution_get_order",
    "paper_execution_reconcile",
}


def _request() -> PaperOrderRequest:
    return PaperOrderRequest.model_validate(
        {
            "intent_id": "intent-1",
            "experiment_id": "experiment-1",
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "strategy_version_id": "strategy-v1",
            "strategy_hash": "1" * 64,
            "config_hash": "2" * 64,
            "policy_hash": "3" * 64,
            "venue": Broker.ALPACA,
            "account_mode": "paper",
            "product": "crypto",
            "symbol": "BTC/USD",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "gtc",
            "qty": Decimal("0.001"),
            "price": Decimal("50000"),
            "market_snapshot_id": "snapshot-1",
            "market_snapshot_hash": "4" * 64,
            "market_snapshot_as_of": datetime(2026, 7, 13, tzinfo=UTC),
            "market_snapshot_source": "binance_public_spot",
        }
    )


def _request_json() -> dict[str, object]:
    return {
        "intent_id": "intent-1",
        "experiment_id": "experiment-1",
        "run_id": "run-1",
        "cohort_id": "cohort-1",
        "strategy_version_id": "strategy-v1",
        "strategy_hash": "1" * 64,
        "config_hash": "2" * 64,
        "policy_hash": "3" * 64,
        "venue": "alpaca",
        "account_mode": "paper",
        "product": "crypto",
        "symbol": "BTC/USD",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "gtc",
        "qty": "0.001",
        "price": "50000",
        "market_snapshot_id": "snapshot-1",
        "market_snapshot_hash": "4" * 64,
        "market_snapshot_as_of": "2026-07-13T00:00:00+00:00",
        "market_snapshot_source": "binance_public_spot",
    }


@pytest.mark.unit
def test_config_gate_defaults_off() -> None:
    assert Settings.model_fields["PAPER_EXECUTION_ENABLED"].default is False


@pytest.mark.unit
def test_direct_registry_flag_off_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", False)
    mcp = DummyMCP()

    register_all_tools(mcp, profile=McpProfile.PAPER_EXECUTION)  # type: ignore[arg-type]

    assert mcp.tools == {}


@pytest.mark.unit
def test_direct_registry_flag_on_registers_exact_facade_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)
    mcp = DummyMCP()

    register_all_tools(mcp, profile=McpProfile.PAPER_EXECUTION)  # type: ignore[arg-type]

    assert PAPER_EXECUTION_TOOL_NAMES == EXPECTED_TOOLS
    assert set(mcp.tools) == EXPECTED_TOOLS
    forbidden_fragments = {
        "alpaca_paper",
        "binance_demo",
        "kis_live",
        "kiwoom",
        "upbit",
        "toss",
        "place_order",
        "link_native",
    }
    assert not {
        name
        for name in mcp.tools
        if any(fragment in name for fragment in forbidden_fragments)
    }


@pytest.mark.unit
def test_mutation_tool_dtos_have_no_caller_owned_identity_fields() -> None:
    mcp = DummyMCP()
    register_paper_execution_tools(mcp)  # type: ignore[arg-type]

    forbidden = {
        "origin",
        "idempotency_key",
        "client_order_id",
        "native_client_order_id",
        "native_order_id",
    }
    for name in EXPECTED_TOOLS - {"paper_execution_get_capabilities"}:
        signature = inspect.signature(mcp.tools[name])
        assert forbidden.isdisjoint(signature.parameters)

    assert forbidden.isdisjoint(PaperOrderRequest.model_fields)
    assert forbidden.isdisjoint(PaperOrderToolInput.model_fields)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "method_name"),
    [
        ("paper_execution_preview_order", "preview"),
        ("paper_execution_submit_order", "submit"),
        ("paper_execution_cancel_order", "cancel"),
        ("paper_execution_get_order", "get_order"),
        ("paper_execution_reconcile", "reconcile"),
    ],
)
async def test_typed_tools_delegate_to_injected_application(
    tool_name: str,
    method_name: str,
) -> None:
    expected = PaperOperationResult(
        operation=PaperOperation(method_name),
        status=PaperOperationStatus.BLOCKED,
        reason_code="provenance_verifier_unavailable",
        venue=Broker.ALPACA,
    )
    application = type(
        "FakeApplication",
        (),
        {
            name: AsyncMock(return_value=expected)
            for name in ("preview", "submit", "cancel", "get_order", "reconcile")
        },
    )()
    mcp = DummyMCP()
    register_paper_execution_tools(
        mcp,  # type: ignore[arg-type]
        application_provider=lambda: application,
    )
    request = _request()

    result = await mcp.tools[tool_name](request=request)

    getattr(application, method_name).assert_awaited_once_with(request)
    assert result["reason_code"] == "provenance_verifier_unavailable"
    assert result["status"] == "blocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_composition_fails_closed_without_provenance_verifier() -> None:
    mcp = DummyMCP()
    register_paper_execution_tools(mcp)  # type: ignore[arg-type]

    result = await mcp.tools["paper_execution_submit_order"](request=_request())

    assert result["status"] == "blocked"
    assert result["reason_code"] == "provenance_verifier_unavailable"


@pytest.mark.unit
def test_production_composition_registers_both_guarded_adapters() -> None:
    application = build_paper_execution_application(verifier=None)

    assert application._registry.adapters.keys() == {  # noqa: SLF001
        Broker.BINANCE,
        Broker.ALPACA,
    }
    assert isinstance(  # noqa: SLF001
        application._registry.resolve(Broker.BINANCE),  # noqa: SLF001
        BinanceSpotDemoPaperAdapter,
    )
    assert isinstance(  # noqa: SLF001
        application._registry.resolve(Broker.ALPACA),  # noqa: SLF001
        AlpacaCryptoPaperAdapter,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_read_works_without_verifier_and_is_json_safe() -> None:
    mcp = DummyMCP()
    register_paper_execution_tools(mcp)  # type: ignore[arg-type]

    result = await mcp.tools["paper_execution_get_capabilities"]()

    assert result["status"] == "ok"
    assert {entry["venue"] for entry in result["capabilities"]} == {
        "alpaca",
        "binance",
    }
    json.dumps(result, allow_nan=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_fastmcp_accepts_json_shaped_typed_request() -> None:
    expected = PaperOperationResult(
        operation=PaperOperation.SUBMIT,
        status=PaperOperationStatus.BLOCKED,
        reason_code="provenance_verifier_unavailable",
        venue=Broker.ALPACA,
    )
    application = type(
        "FakeApplication",
        (),
        {
            name: AsyncMock(return_value=expected)
            for name in ("preview", "submit", "cancel", "get_order", "reconcile")
        },
    )()
    mcp = FastMCP(name="paper-execution-json-boundary", on_duplicate="error")
    register_paper_execution_tools(
        mcp,
        application_provider=lambda: application,
    )

    await mcp.call_tool(
        "paper_execution_submit_order",
        {"request": _request_json()},
    )

    submitted = application.submit.await_args.args[0]
    assert isinstance(submitted, PaperOrderRequest)
    assert submitted.venue is Broker.ALPACA
    assert submitted.qty == Decimal("0.001")
    assert submitted.market_snapshot_as_of == datetime(2026, 7, 13, tzinfo=UTC)
