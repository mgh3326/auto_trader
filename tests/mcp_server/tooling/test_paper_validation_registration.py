from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.caller_identity import caller_agent_id_var
from app.mcp_server.tooling.paper_validation_registration import (
    PAPER_VALIDATION_MUTATION_TOOL_NAMES,
    PAPER_VALIDATION_TOOL_NAMES,
    register_paper_validation_tools,
)
from tests._mcp_tooling_support import DummyMCP

EXPECTED_TOOLS = {
    "paper_validation_register",
    "paper_validation_advance",
    "paper_validation_append_hypothesis",
    "paper_validation_append_review",
    "paper_validation_get_audit",
    "paper_validation_authorize_order_submit",
    "paper_validation_confirm_promotion",
    "paper_validation_reject_or_abort",
}


def test_validation_registrar_is_independent_and_has_exact_allowlist() -> None:
    application = type("FakeApplication", (), {})()
    mcp = DummyMCP()
    register_paper_validation_tools(
        mcp,
        application_provider=lambda: application,  # type: ignore[arg-type]
    )

    assert PAPER_VALIDATION_TOOL_NAMES == EXPECTED_TOOLS
    assert set(mcp.tools) == EXPECTED_TOOLS
    assert PAPER_VALIDATION_MUTATION_TOOL_NAMES == EXPECTED_TOOLS - {
        "paper_validation_get_audit"
    }


def test_validation_payloads_never_accept_actor_identity_or_role() -> None:
    application = type("FakeApplication", (), {})()
    mcp = DummyMCP()
    register_paper_validation_tools(
        mcp,
        application_provider=lambda: application,  # type: ignore[arg-type]
    )

    for handler in mcp.tools.values():
        assert {"actor_id", "actor_role", "caller_id"}.isdisjoint(
            inspect.signature(handler).parameters
        )


@pytest.mark.asyncio
async def test_handler_uses_request_context_caller_and_json_safe_result() -> None:
    application = type(
        "FakeApplication",
        (),
        {
            "get_audit": AsyncMock(
                return_value={"transitions": [], "hypotheses": [], "reviews": []}
            )
        },
    )()
    mcp = DummyMCP()
    register_paper_validation_tools(
        mcp,
        application_provider=lambda: application,  # type: ignore[arg-type]
    )
    token = caller_agent_id_var.set("authenticated-operator")
    try:
        result = await mcp.tools["paper_validation_get_audit"](
            validation_id="validation-1"
        )
    finally:
        caller_agent_id_var.reset(token)

    application.get_audit.assert_awaited_once_with(
        "authenticated-operator", "validation-1"
    )
    assert result == {"transitions": [], "hypotheses": [], "reviews": []}


@pytest.mark.asyncio
async def test_missing_request_context_identity_fails_closed_before_application() -> (
    None
):
    application = type(
        "FakeApplication",
        (),
        {"get_audit": AsyncMock()},
    )()
    mcp = DummyMCP()
    register_paper_validation_tools(
        mcp,
        application_provider=lambda: application,  # type: ignore[arg-type]
    )

    result = await mcp.tools["paper_validation_get_audit"](validation_id="validation-1")

    assert result == {"status": "blocked", "reason_code": "actor_identity_unavailable"}
    application.get_audit.assert_not_awaited()
