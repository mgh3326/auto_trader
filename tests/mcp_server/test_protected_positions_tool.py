"""#728 read-only protected-position MCP registration and DB-read coverage."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import protected_positions
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedQuantityService,
)
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)


def _provider():
    async def observe() -> BrokerPositionObservation:
        return BrokerPositionObservation(
            held=Decimal("10"),
            sellable=Decimal("8"),
            observed_at=datetime.now(UTC),
        )

    return observe


@pytest.mark.unit
def test_registered_surface_has_one_read_tool_and_no_writer_ast() -> None:
    recorder = RegistrationRecorder()
    protected_positions.register_protected_position_tools(recorder)  # type: ignore[arg-type]
    source = Path(protected_positions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    call_names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert set(recorder.tools) == {"get_protected_positions"}
    assert protected_positions.PROTECTED_POSITION_TOOL_NAMES == {
        "get_protected_positions"
    }
    assert "save" not in call_names
    assert "delete" not in call_names
    assert "update" not in call_names
    assert not any("brokers" in module for module in imports)


@pytest.mark.unit
def test_read_tool_is_present_only_on_reviewed_read_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = collect_profile_tools(monkeypatch, gates_enabled=True)
    for profile in (
        McpProfile.DEFAULT,
        McpProfile.ANALYSIS_READONLY,
        McpProfile.ACCOUNT_READ,
        McpProfile.TRADINGCODEX_EXECUTION,
        McpProfile.CRYPTO,
        McpProfile.DB_PAPER,
        McpProfile.HERMES_PAPER_KIS,
        McpProfile.KIWOOM,
        McpProfile.KIWOOM_KR,
        McpProfile.US_PAPER,
    ):
        assert "get_protected_positions" in tools[profile.value]
    for profile in (
        McpProfile.SHADOW_REPLAY,
        McpProfile.WATCH_REPRICING,
        McpProfile.FILL_WATCH_CONTEXT,
    ):
        assert "get_protected_positions" not in tools[profile.value]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_tool_returns_durable_decimal_heads_without_broker_calls(
    db_session,
) -> None:
    symbol = f"TMCP{uuid4().hex[:8].upper()}"
    await ProtectedQuantityService(db_session).save(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        protected_quantity="6.25",
        expected_revision=None,
        reason="mcp visibility test",
        idempotency_key=f"mcp-{uuid4()}",
        actor_user_id=728100,
        origin="invest_ui",
        observation_provider=_provider(),
        confirm_protection_change=True,
    )
    result = await protected_positions.get_protected_positions_impl(
        account_scope="kis_live"
    )
    position = next(item for item in result["positions"] if item["symbol"] == symbol)
    assert result["success"] is True
    assert position["protected_quantity"] == "6.25000000"
    assert position["last_confirmed_broker_held"] == "10.00000000"
    assert position["revision"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_tool_rejects_invalid_scope_without_mutating(db_session) -> None:
    result = await protected_positions.get_protected_positions_impl(
        account_scope="not-a-scope"
    )
    assert result["success"] is False
    assert result["error"] == "invalid_protection_scope"
