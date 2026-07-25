"""Exact MCP surface and server-owned identity for ROB-849 kill switch."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.paper_cohort_control_registration import (
    PAPER_COHORT_CONTROL_TOOL_NAMES,
    register_paper_cohort_control_tools,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.services.paper_cohort.contracts import (
    PaperCohortKillRequest,
    PaperCohortKillResult,
)
from tests._mcp_tooling_support import DummyMCP


def _request() -> PaperCohortKillRequest:
    return PaperCohortKillRequest(
        cohort_id="cohort-1",
        idempotency_key="kill-1",
        reason_code="operator_kill",
        reason_text="stop this paper cohort",
    )


@pytest.mark.unit
def test_registers_one_typed_tool_without_caller_owned_identity() -> None:
    mcp = DummyMCP()
    register_paper_cohort_control_tools(mcp)  # type: ignore[arg-type]

    assert (
        set(mcp.tools)
        == PAPER_COHORT_CONTROL_TOOL_NAMES
        == {"paper_cohort_kill_switch"}
    )
    signature = inspect.signature(mcp.tools["paper_cohort_kill_switch"])
    assert set(signature.parameters) == {"request"}
    assert {"actor_id", "actor_role", "link_id", "validation_id"}.isdisjoint(
        PaperCohortKillRequest.model_fields
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_uses_only_configured_server_actor(monkeypatch) -> None:
    application = AsyncMock()
    application.kill_switch.return_value = PaperCohortKillResult(
        status="fenced",
        fence_id="fence-1",
        cohort_id="cohort-1",
        fenced_at=datetime(2026, 7, 14, tzinfo=UTC),
        replayed=False,
        cleanup_status="complete",
    )
    monkeypatch.setattr(
        settings, "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID", "server-operator"
    )
    mcp = DummyMCP()
    register_paper_cohort_control_tools(  # type: ignore[arg-type]
        mcp, application_provider=lambda: application
    )

    result = await mcp.tools["paper_cohort_kill_switch"](request=_request())

    application.kill_switch.assert_awaited_once_with("server-operator", _request())
    assert result["status"] == "fenced"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_server_actor_fails_closed_before_application(
    monkeypatch,
) -> None:
    application = AsyncMock()
    monkeypatch.setattr(settings, "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID", "")
    mcp = DummyMCP()
    register_paper_cohort_control_tools(  # type: ignore[arg-type]
        mcp, application_provider=lambda: application
    )

    result = await mcp.tools["paper_cohort_kill_switch"](request=_request())

    assert result == {
        "status": "blocked",
        "reason_code": "actor_identity_unavailable",
    }
    application.kill_switch.assert_not_awaited()


@pytest.mark.unit
def test_control_tool_is_default_off_and_paper_execution_profile_only(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", False)
    disabled = DummyMCP()
    register_all_tools(disabled, profile=McpProfile.PAPER_EXECUTION)  # type: ignore[arg-type]
    assert PAPER_COHORT_CONTROL_TOOL_NAMES.isdisjoint(disabled.tools)

    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)
    enabled = DummyMCP()
    register_all_tools(enabled, profile=McpProfile.PAPER_EXECUTION)  # type: ignore[arg-type]
    assert PAPER_COHORT_CONTROL_TOOL_NAMES <= enabled.tools.keys()

    for profile in McpProfile:
        if profile is McpProfile.PAPER_EXECUTION:
            continue
        other = DummyMCP()
        register_all_tools(other, profile=profile)  # type: ignore[arg-type]
        assert PAPER_COHORT_CONTROL_TOOL_NAMES.isdisjoint(other.tools)
