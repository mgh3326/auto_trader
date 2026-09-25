"""MCP surface for NHPLUG Stage 2 mock tools (#711): gates before any I/O."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.mcp_server.tooling import orders_nhplug_mock_variants as tools_module
from app.mcp_server.tooling.orders_nhplug_mock_variants import (
    NHPLUG_MOCK_MUTATION_TOOL_NAMES,
    NHPLUG_MOCK_TOOL_NAMES,
)
from app.mcp_server.tooling.route_request_lanes import DIRECT_BROKER_MUTATION_TOOLS
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit


class _Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, name: str) -> Any:
        async def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {"success": True, "spy": name}

        return record


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], _Spy]:
    spy = _Spy()
    for name in (
        "place_limit_order",
        "modify_limit_order",
        "cancel_order",
        "get_positions",
        "get_open_orders",
        "get_order_history",
        "reconcile_orders",
    ):
        monkeypatch.setattr(tools_module.operations, name, spy(name))

    async def no_client() -> Any:
        spy.calls.append("verified_client")
        raise AssertionError("a client must not be built")

    monkeypatch.setattr(tools_module, "_verified_client", no_client)
    recorder = RegistrationRecorder()
    tools_module.register(recorder)  # type: ignore[arg-type]
    return recorder.tools, spy


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "nhplug_mock_enabled", True)
    monkeypatch.setattr(settings, "nhplug_app_key", "k")
    monkeypatch.setattr(settings, "nhplug_app_secret", "s")
    monkeypatch.setattr(settings, "nhplug_mock_account_no", "a")


def test_tool_inventory_and_mutation_classification() -> None:
    recorder = RegistrationRecorder()
    tools_module.register(recorder)  # type: ignore[arg-type]
    assert set(recorder.tools) == NHPLUG_MOCK_TOOL_NAMES
    assert len(NHPLUG_MOCK_TOOL_NAMES) == 8
    assert NHPLUG_MOCK_MUTATION_TOOL_NAMES <= DIRECT_BROKER_MUTATION_TOOLS


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    (
        (
            "nhplug_mock_place_order",
            {"symbol": "005930", "side": "buy", "quantity": 1, "price": 50000},
        ),
        (
            "nhplug_mock_modify_order",
            {"order_id": "1000123", "symbol": "005930", "new_price": 49500},
        ),
        ("nhplug_mock_cancel_order", {"order_id": "1000123", "symbol": "005930"}),
    ),
)
@pytest.mark.parametrize("confirm", (False, None, 1, "true"))
@pytest.mark.asyncio
async def test_send_without_exact_confirm_never_reaches_operations(
    registered: tuple[dict[str, Any], _Spy],
    configured: None,
    tool: str,
    kwargs: dict[str, Any],
    confirm: Any,
) -> None:
    tools, spy = registered
    result = await tools[tool](**kwargs, dry_run=False, confirm=confirm)
    assert result["success"] is False
    assert result["error_code"] == "confirm_required"
    assert result["dispatch_started"] is False
    assert spy.calls == []


@pytest.mark.parametrize("order_type", ("market", "MARKET", "stop", "best_limit"))
@pytest.mark.parametrize("dry_run", (True, False))
@pytest.mark.asyncio
async def test_market_orders_are_refused_with_a_clear_error(
    registered: tuple[dict[str, Any], _Spy],
    configured: None,
    order_type: str,
    dry_run: bool,
) -> None:
    tools, spy = registered
    for tool in ("nhplug_mock_place_order", "nhplug_mock_preview_order"):
        kwargs: dict[str, Any] = {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 50000,
            "order_type": order_type,
        }
        if tool == "nhplug_mock_place_order":
            kwargs.update(dry_run=dry_run, confirm=True)
        result = await tools[tool](**kwargs)
        assert result["error_code"] == "limit_orders_only"
        assert "market" in result["error"].lower()
    assert spy.calls == []


@pytest.mark.asyncio
async def test_missing_price_is_refused_not_sent_as_market(
    registered: tuple[dict[str, Any], _Spy], configured: None
) -> None:
    tools, spy = registered
    result = await tools["nhplug_mock_place_order"](
        symbol="005930", side="buy", quantity=1, dry_run=False, confirm=True
    )
    assert result["error_code"] == "limit_price_required"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_dry_run_place_is_offline_preview(
    registered: tuple[dict[str, Any], _Spy],
) -> None:
    tools, spy = registered
    result = await tools["nhplug_mock_place_order"](
        symbol="005930", side="buy", quantity=1, price=50000
    )
    assert result["dry_run"] is True and result["dispatch_started"] is False
    assert spy.calls == []


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    (
        (
            "nhplug_mock_place_order",
            {
                "symbol": "005930",
                "side": "buy",
                "quantity": 1,
                "price": 50000,
                "dry_run": False,
                "confirm": True,
            },
        ),
        (
            "nhplug_mock_cancel_order",
            {"order_id": "1", "symbol": "005930", "dry_run": False, "confirm": True},
        ),
        ("nhplug_mock_get_positions", {}),
        ("nhplug_mock_get_open_orders", {}),
        ("nhplug_mock_get_order_history", {}),
        ("nhplug_mock_reconcile_orders", {}),
    ),
)
@pytest.mark.asyncio
async def test_unconfigured_tools_fail_closed_naming_keys_only(
    registered: tuple[dict[str, Any], _Spy],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    kwargs: dict[str, Any],
) -> None:
    monkeypatch.setattr(settings, "nhplug_mock_enabled", False)
    monkeypatch.setattr(settings, "nhplug_app_key", "SECRET_KEY_VALUE")
    monkeypatch.setattr(settings, "nhplug_app_secret", None)
    monkeypatch.setattr(settings, "nhplug_mock_account_no", None)
    tools, spy = registered
    result = await tools[tool](**kwargs)
    assert result["error_code"] == "nhplug_mock_config_invalid"
    assert "NHPLUG_MOCK_ENABLED" in result["error"]
    assert "SECRET_KEY_VALUE" not in str(result)
    assert spy.calls == []


def test_tools_register_only_in_default_and_only_behind_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = collect_profile_tools(monkeypatch, gates_enabled=False)
    assert not any(NHPLUG_MOCK_TOOL_NAMES & set(names) for names in off.values()), (
        "nhplug tools must be physically absent when the gate is off"
    )
    on = collect_profile_tools(monkeypatch, gates_enabled=True)
    assert NHPLUG_MOCK_TOOL_NAMES <= set(on["default"])
    others = {
        profile
        for profile, names in on.items()
        if profile != "default" and NHPLUG_MOCK_TOOL_NAMES & set(names)
    }
    assert others == set()


def test_no_lane_allowlist_names_nhplug_tools() -> None:
    """Account/lane assignment is a separate operator decision."""

    from pathlib import Path

    lanes = Path(__file__).resolve().parents[2] / "config" / "mcp_lane_allowlists"
    for path in lanes.glob("*.txt"):
        assert "nhplug" not in path.read_text(encoding="utf-8"), path.name
