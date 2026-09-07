"""Real-server proof for the closed #137 fill/watch context MCP profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import NotFoundError

from app.core.config import Settings, settings
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.fill_watch_context_registration import (
    FILL_WATCH_CONTEXT_TOOL_NAMES,
    ContextProfileSurfaceViolation,
    assert_provisioned_surface,
    build_fill_watch_context_server,
    provisioned_tool_names,
)
from tests.mcp_server._registration_recorder import collect_profile_tools
from tests.services.fill_watch_context.conftest import context_artifact

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_AT_BOUNDARY = (
    "order_proposal_create",
    "toss_place_order",
    "get_holdings",
    "toss_get_positions",
    "investment_watch_create",
    "route_request",
    "session_bootstrap_pack",
    "execute_shell_command",
)


def _error_text(result: object) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in (getattr(result, "content", None) or [])
    )


def test_context_profile_and_independent_arming_flag_are_explicit_and_default_off() -> (
    None
):
    assert resolve_mcp_profile("fill-watch-context") is McpProfile.FILL_WATCH_CONTEXT
    assert (
        Settings.model_fields["FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED"].default is False
    )


@pytest.mark.asyncio
async def test_real_server_serves_exact_context_only_tool_set() -> None:
    server = build_fill_watch_context_server()

    assert await provisioned_tool_names(server) == FILL_WATCH_CONTEXT_TOOL_NAMES
    assert await assert_provisioned_surface(server) == FILL_WATCH_CONTEXT_TOOL_NAMES
    async with Client(server) as client:
        assert frozenset(tool.name for tool in await client.list_tools()) == (
            FILL_WATCH_CONTEXT_TOOL_NAMES
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", FORBIDDEN_AT_BOUNDARY)
async def test_forbidden_tools_are_absent_and_server_refuses_raw_call(
    denied: str,
) -> None:
    server = build_fill_watch_context_server()

    async with Client(server) as client:
        offered = frozenset(tool.name for tool in await client.list_tools())
        assert denied not in offered
        result = await client.call_tool_mcp(denied, {})

    assert result.isError is True
    assert "Unknown tool" in _error_text(result)
    assert denied in _error_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", FORBIDDEN_AT_BOUNDARY)
async def test_server_call_path_has_no_client_side_filter(denied: str) -> None:
    server = build_fill_watch_context_server()

    assert await server.get_tool(denied) is None
    with pytest.raises(NotFoundError):
        await server.call_tool(denied, {})


@pytest.mark.asyncio
async def test_allowed_tool_has_a_distinct_disabled_result_not_an_absence_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED", False)
    server = build_fill_watch_context_server()

    async with Client(server) as client:
        result = await client.call_tool_mcp(
            "fill_watch_context_consume_artifact",
            {"artifact": context_artifact(13721)},
        )

    assert result.isError is False
    body = json.loads(_error_text(result))
    assert body["delivery_ack"] == {
        "accepted": False,
        "persisted": False,
        "reason": "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED is false",
    }
    assert body["consumption"] is None


@pytest.mark.asyncio
async def test_surface_attestation_rejects_a_leaked_capability() -> None:
    server = build_fill_watch_context_server()

    @server.tool(name="toss_place_order")
    def leaked() -> str:  # pragma: no cover - must never be invoked
        return "leak"

    with pytest.raises(ContextProfileSurfaceViolation, match="toss_place_order"):
        await assert_provisioned_surface(server)


def test_registration_inventory_has_no_tool_outside_the_new_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = collect_profile_tools(monkeypatch, gates_enabled=True)
    assert inventory[McpProfile.FILL_WATCH_CONTEXT.value] == sorted(
        FILL_WATCH_CONTEXT_TOOL_NAMES
    )
    assert all(
        tool not in inventory[McpProfile.FILL_WATCH_CONTEXT.value]
        for tool in FORBIDDEN_AT_BOUNDARY
    )


def test_isolated_entrypoint_checks_the_default_false_gate_before_reading_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from scripts.fill_watch_context_consumer import _amain

    monkeypatch.setattr(settings, "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED", False)
    absent_artifact = tmp_path / "not-read.json"
    assert not absent_artifact.exists()

    import asyncio

    assert asyncio.run(_amain(absent_artifact)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert payload["delivery_ack"] == {"accepted": False, "persisted": False}


def test_one_shot_process_checks_default_false_gate_before_artifact_read(
    tmp_path: Path,
) -> None:
    """Exercise the isolated command as a real process with no input file."""
    absent_artifact = tmp_path / "not-read-by-subprocess.json"
    environment = os.environ | {"FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED": "false"}

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.fill_watch_context_consumer",
            "--artifact",
            str(absent_artifact),
        ],
        check=False,
        cwd=ROOT,
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "delivery_ack": {"accepted": False, "persisted": False},
        "reason": "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED is false",
        "status": "disabled",
    }
