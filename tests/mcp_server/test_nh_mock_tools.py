"""#849 nh_mock_* MCP surface: registration scope, flag default, strict wire schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastmcp import Client, FastMCP

from app.core.config import Settings, settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import orders_nh_mock_variants
from app.mcp_server.tooling.orders_nh_mock_variants import (
    NH_MOCK_MUTATION_TOOL_NAMES,
    NH_MOCK_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.services.nhplug_mock import operations
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_PROFILES = (McpProfile.LIVE_KR, McpProfile.LIVE_US, McpProfile.LIVE_CRYPTO)

EXPECTED_NAMES = {
    "nh_mock_preview_order",
    "nh_mock_place_order",
    "nh_mock_modify_order",
    "nh_mock_cancel_order",
    "nh_mock_get_order_detail",
    "nh_mock_get_order_history",
    "nh_mock_get_orderable_cash",
    "nh_mock_get_positions",
    "nh_mock_reconcile_orders",
}


def test_tool_inventory_is_the_kiwoom_kr_mirror_plus_reconcile() -> None:
    assert NH_MOCK_TOOL_NAMES == EXPECTED_NAMES
    assert NH_MOCK_MUTATION_TOOL_NAMES == {
        "nh_mock_place_order",
        "nh_mock_modify_order",
        "nh_mock_cancel_order",
    }


def test_registration_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NH_MOCK_MCP_ENABLED", raising=False)
    assert Settings.model_fields["nh_mock_mcp_enabled"].default is False
    assert Settings(_env_file=None).nh_mock_mcp_enabled is False
    env_example = (REPO_ROOT / "env.example").read_text(encoding="utf-8")
    assignments = [
        line.strip()
        for line in env_example.splitlines()
        if line.strip().startswith("NH_MOCK_MCP_ENABLED=")
    ]
    assert assignments == ["NH_MOCK_MCP_ENABLED=false"]


def test_gates_off_registers_no_nh_mock_tool_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = collect_profile_tools(monkeypatch, gates_enabled=False)
    for profile, names in profiles.items():
        assert not (set(names) & NH_MOCK_TOOL_NAMES), profile


def test_only_the_default_profile_registers_them_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = collect_profile_tools(monkeypatch, gates_enabled=True)
    holders = {
        profile
        for profile, names in profiles.items()
        if set(names) & NH_MOCK_TOOL_NAMES
    }
    assert holders == {McpProfile.DEFAULT.value}
    assert NH_MOCK_TOOL_NAMES <= set(profiles[McpProfile.DEFAULT.value])


def test_flag_alone_controls_default_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = collect_profile_tools(monkeypatch, gates_enabled=True)
    with monkeypatch.context() as patch:
        patch.setattr(settings, "nh_mock_mcp_enabled", False)
        recorder = RegistrationRecorder()
        register_all_tools(recorder, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    assert NH_MOCK_TOOL_NAMES <= set(on[McpProfile.DEFAULT.value])
    assert not (set(recorder.tools) & NH_MOCK_TOOL_NAMES)


def test_live_profiles_and_live_lane_allowlists_never_list_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = collect_profile_tools(monkeypatch, gates_enabled=True)
    for profile in LIVE_PROFILES:
        assert not (set(profiles[profile.value]) & NH_MOCK_TOOL_NAMES), profile
    manifest = (REPO_ROOT / "config/mcp_profiles/live.yaml").read_text("utf-8")
    assert "nh_mock" not in manifest
    assert yaml.safe_load(manifest)  # still a valid manifest
    for lane in sorted((REPO_ROOT / "config/mcp_lane_allowlists").glob("*.txt")):
        assert "nh_mock" not in lane.read_text("utf-8"), lane.name


def test_route_request_blocks_the_mutations() -> None:
    from app.mcp_server.tooling.route_request_lanes import (
        DIRECT_BROKER_MUTATION_TOOLS,
        MUTATION_TOOLS,
        READ_ONLY_ADVISORY_TOOLS,
    )

    assert NH_MOCK_TOOL_NAMES <= MUTATION_TOOLS
    assert NH_MOCK_MUTATION_TOOL_NAMES <= DIRECT_BROKER_MUTATION_TOOLS
    assert not (NH_MOCK_TOOL_NAMES & READ_ONLY_ADVISORY_TOOLS)


def test_mcp_module_delegates_every_tool_to_operations() -> None:
    import ast

    source = Path(orders_nh_mock_variants.__file__).read_text("utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports <= {
        "__future__",
        "typing",
        "pydantic",
        "fastmcp",
        "app.services.nhplug_mock",
    }


# ---------------------------------------------------------------------------
# Strict wire schemas through a real FastMCP client (no network, no DB)
# ---------------------------------------------------------------------------


def _server() -> FastMCP:
    server = FastMCP("nh-mock-wire-test")
    orders_nh_mock_variants.register(server)
    return server


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("client")
        raise AssertionError("dry-run and schema refusals must not build a client")

    monkeypatch.setattr(operations, "_new_client", forbidden)
    return calls


PLACE = {"symbol": "005930", "side": "buy", "quantity": 1, "price": 50000}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    (
        {"dry_run": "false"},
        {"dry_run": 0},
        {"confirm": "true"},
        {"confirm": 1},
        {"quantity": "1"},
        {"quantity": 1.0},
        {"price": "50000"},
        {"order_type": 1},
    ),
)
async def test_wire_rejects_coercible_values(
    override: dict[str, Any], no_network: list[str]
) -> None:
    async with Client(_server()) as client:
        result = await client.call_tool(
            "nh_mock_place_order", {**PLACE, **override}, raise_on_error=False
        )
    assert result.is_error
    assert no_network == []


@pytest.mark.asyncio
async def test_wire_defaults_are_dry_run_and_limit(no_network: list[str]) -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("nh_mock_place_order", PLACE)
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "dry_run"
    assert payload["sent"] is False
    assert payload["plan"]["order_type"] == "limit"
    assert no_network == []


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ("market", "MARKET", None, ""))
async def test_wire_refuses_non_limit_even_in_dry_run(
    order_type: Any, no_network: list[str]
) -> None:
    async with Client(_server()) as client:
        result = await client.call_tool(
            "nh_mock_place_order", {**PLACE, "order_type": order_type}
        )
    payload = result.structured_content
    assert payload is not None
    assert payload["error"] == "limit_order_only"
    assert no_network == []


@pytest.mark.asyncio
async def test_wire_confirmed_send_without_confirm_is_refused(
    no_network: list[str],
) -> None:
    async with Client(_server()) as client:
        result = await client.call_tool(
            "nh_mock_place_order", {**PLACE, "dry_run": False}
        )
    payload = result.structured_content
    assert payload is not None
    assert payload["error"] == "confirm_required"
    assert no_network == []
