from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.mcp_server.caller_identity import caller_agent_id_var
from app.mcp_server.tooling import paper_validation_handlers
from app.mcp_server.tooling.paper_validation_registration import (
    PAPER_VALIDATION_MUTATION_TOOL_NAMES,
    PAPER_VALIDATION_TOOL_NAMES,
    register_paper_validation_tools,
)
from app.services.paper_validation.contracts import (
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
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


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    def begin(self) -> _AsyncContext:
        return _AsyncContext(None)

    async def scalar(self, _statement: object) -> None:
        return None


def _request() -> TransitionRequest:
    digest = "a" * 64
    return TransitionRequest(
        identity=ValidationIdentity(
            validation_id="validation-1",
            validation_version=1,
            experiment_id=digest,
            strategy_version_id="strategy-v1",
            cohort_id="cohort-1",
            experiment_hash=digest,
            cohort_hash="b" * 64,
            strategy_hash="c" * 64,
            config_hash="d" * 64,
            policy_hash="e" * 64,
            input_hash="f" * 64,
        ),
        expected_prior_state=None,
        target_state=ValidationState.DRAFT,
        idempotency_key="register-1",
        reason_code="validation_registered",
        reason_text="register validation",
    )


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
async def test_handler_uses_server_bound_actor_and_json_safe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        settings,
        "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID",
        "authenticated-operator",
    )
    result = await mcp.tools["paper_validation_get_audit"](validation_id="validation-1")

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


@pytest.mark.asyncio
async def test_caller_header_cannot_spoof_configured_operator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(settings, "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID", "")
    token = caller_agent_id_var.set("configured-operator")
    try:
        result = await mcp.tools["paper_validation_get_audit"](
            validation_id="validation-1"
        )
    finally:
        caller_agent_id_var.reset(token)

    assert result == {
        "status": "blocked",
        "reason_code": "actor_identity_unavailable",
    }
    application.get_audit.assert_not_awaited()


@pytest.mark.parametrize(
    ("role_mapping", "reason_code"),
    [
        ({}, "actor_identity_unavailable"),
        ({"authenticated-operator": "operator"}, "evidence_stamp_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_default_composition_fails_closed_without_trusted_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    role_mapping: dict[str, str],
    reason_code: str,
) -> None:
    session = _Session()
    monkeypatch.setattr(
        paper_validation_handlers,
        "AsyncSessionLocal",
        lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        settings,
        "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID",
        "authenticated-operator",
    )
    monkeypatch.setattr(settings, "PAPER_VALIDATION_ACTOR_ROLES", role_mapping)
    mcp = DummyMCP()
    register_paper_validation_tools(mcp)

    result = await mcp.tools["paper_validation_register"](request=_request())

    assert result == {"status": "blocked", "reason_code": reason_code}


@pytest.mark.asyncio
async def test_default_audit_read_normalizes_unmapped_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    monkeypatch.setattr(
        paper_validation_handlers,
        "AsyncSessionLocal",
        lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        settings,
        "PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID",
        "unmapped-operator",
    )
    monkeypatch.setattr(settings, "PAPER_VALIDATION_ACTOR_ROLES", {})
    mcp = DummyMCP()
    register_paper_validation_tools(mcp)

    result = await mcp.tools["paper_validation_get_audit"](validation_id="validation-1")

    assert result == {
        "status": "blocked",
        "reason_code": "actor_identity_unavailable",
    }
