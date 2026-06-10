"""ROB-269 Phase 2 — MCP tools + flag-gated registration.

These tests use a stand-in MCP recorder instead of real FastMCP so we
avoid the network / fastmcp init overhead. The recorder captures
``mcp.tool(...)`` calls, which is exactly what the registration module
exercises.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.mcp_server.tooling.investment_snapshots_registration import (
    INVESTMENT_SNAPSHOTS_TOOL_NAMES,
    register_investment_snapshots_tools,
)
from app.mcp_server.tooling.investment_snapshots_tools import (
    investment_snapshot_bundle_get,
    investment_snapshot_bundle_list,
    investment_snapshot_list,
)


@dataclass
class _RecordedTool:
    name: str
    description: str
    func: Any


@dataclass
class _RecorderMcp:
    """Stand-in for ``FastMCP`` that captures ``.tool()`` registrations."""

    registered: list[_RecordedTool] = field(default_factory=list)

    def tool(self, *, name: str, description: str):
        def _decorator(func):
            self.registered.append(
                _RecordedTool(name=name, description=description, func=func)
            )
            return func

        return _decorator


def test_register_investment_snapshots_tools_adds_expected_names():
    mcp = _RecorderMcp()
    register_investment_snapshots_tools(mcp)
    registered_names = {t.name for t in mcp.registered}
    assert registered_names == INVESTMENT_SNAPSHOTS_TOOL_NAMES


def test_investment_snapshots_tool_names_lock():
    # Lock the public surface so adding/renaming a tool is a conscious change.
    assert INVESTMENT_SNAPSHOTS_TOOL_NAMES == {
        "investment_snapshot_bundle_get",
        "investment_snapshot_bundle_list",
        "investment_snapshot_list",
    }


@pytest.mark.asyncio
async def test_investment_snapshot_bundle_get_returns_not_found_for_unknown_uuid(
    db_session,  # noqa: ARG001 — fixture ensures schema is initialised
):
    """The tool uses its own session via AsyncSessionLocal — db_session here
    only exists to ensure the test DB has the migrations applied."""
    result = await investment_snapshot_bundle_get(
        bundle_uuid=str(uuid.uuid4()),
    )
    assert result == {
        "success": False,
        "error": "not_found",
        "bundle_uuid": result["bundle_uuid"],
    }


@pytest.mark.asyncio
async def test_investment_snapshot_bundle_get_rejects_invalid_uuid_string(db_session):
    """Invalid UUID returns a structured error, not a 500."""
    _ = db_session  # noqa: F841 — keep schema fixture
    result = await investment_snapshot_bundle_get(bundle_uuid="not-a-uuid")
    assert result == {
        "success": False,
        "error": "invalid_uuid",
        "bundle_uuid": "not-a-uuid",
    }


@pytest.mark.asyncio
async def test_investment_snapshot_list_clamps_limit(db_session):
    _ = db_session
    result = await investment_snapshot_list(limit=9999)
    assert result["success"] is True
    # The Pydantic limit is clamped to <= 100 by the tool wrapper.
    assert result["limit"] <= 100


@pytest.mark.asyncio
async def test_investment_snapshot_list_invalid_since_returns_error(db_session):
    _ = db_session
    result = await investment_snapshot_list(since="not-iso8601")
    assert result == {
        "success": False,
        "error": "invalid_since",
        "since": "not-iso8601",
    }


@pytest.mark.asyncio
async def test_investment_snapshot_bundle_list_empty_filters_ok(db_session):
    _ = db_session
    result = await investment_snapshot_bundle_list(limit=5)
    assert result["success"] is True
    assert result["limit"] == 5
    # ``bundles`` is always a list (possibly empty).
    assert isinstance(result["bundles"], list)


def test_registry_skips_registration_when_flag_disabled(monkeypatch):
    """Flag-off path: register_all_tools must NOT call the snapshots registrar."""
    from app.core.config import settings
    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling import registry as registry_mod

    monkeypatch.setattr(settings, "INVESTMENT_SNAPSHOTS_MCP_ENABLED", False)

    mcp = _RecorderMcp()
    registry_mod.register_all_tools(mcp, profile=McpProfile.DEFAULT)

    registered_names = {t.name for t in mcp.registered}
    overlap = registered_names & INVESTMENT_SNAPSHOTS_TOOL_NAMES
    assert overlap == set(), f"Snapshots tools registered with flag off: {overlap}"


def test_registry_registers_when_flag_enabled(monkeypatch):
    from app.core.config import settings
    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling import registry as registry_mod

    monkeypatch.setattr(settings, "INVESTMENT_SNAPSHOTS_MCP_ENABLED", True)

    mcp = _RecorderMcp()
    registry_mod.register_all_tools(mcp, profile=McpProfile.DEFAULT)

    registered_names = {t.name for t in mcp.registered}
    assert INVESTMENT_SNAPSHOTS_TOOL_NAMES.issubset(registered_names), (
        f"Snapshots tools missing with flag on. Registered: {registered_names}"
    )
