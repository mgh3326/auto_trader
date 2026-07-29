from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from scripts.mock_session_mcp import (
    MockSessionMcpError,
    build_claude_argv,
    build_mcp_config,
    connected_tool_names,
    session_config,
    validate_client,
    validate_mock_profile,
)

EXPECTED_KIWOOM_KR_TOOLS = {
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_get_order_detail",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_orderable_cash",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_preview_order",
}
FORBIDDEN_GENERIC_ORDER_TOOLS = {
    "cancel_order",
    "get_order_history",
    "live_reconcile_orders",
    "modify_order",
    "place_order",
}
FORBIDDEN_CREDENTIAL_MARKERS = {
    "api_key",
    "api_secret",
    "authorization",
    "bearer",
    "credential",
    "password",
    "private_key",
    "secret_key",
    "token",
}


@pytest.mark.parametrize("profile", ["", "unknown", "default", "kiwoom"])
def test_unknown_and_legacy_profiles_fail_closed(profile: str) -> None:
    with pytest.raises(MockSessionMcpError, match="refused before spawn"):
        validate_mock_profile(profile)


@pytest.mark.parametrize("client", ["codex", "kiro"])
def test_clients_without_isolated_adapter_fail_before_spawn(
    client: str, tmp_path: Path
) -> None:
    before = set(tmp_path.iterdir())
    with pytest.raises(MockSessionMcpError, match="no profile-isolated MCP adapter"):
        validate_client(client)
    assert set(tmp_path.iterdir()) == before


def test_strict_config_contains_one_stdio_server(tmp_path: Path) -> None:
    with session_config(
        profile="kiwoom_kr",
        session_id="strict-single-server",
        parent_dir=tmp_path,
    ) as config_path:
        config = json.loads(config_path.read_text())
        assert len(config["mcpServers"]) == 1
        server = next(iter(config["mcpServers"].values()))
        assert server["type"] == "stdio"
        assert "env" not in server
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == []


def test_generated_config_has_no_literal_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal_canary = "ROB1173_LITERAL_CREDENTIAL_CANARY"
    monkeypatch.setenv("KIWOOM_MOCK_APP_SECRET", literal_canary)
    serialized = json.dumps(
        build_mcp_config(profile="kiwoom_kr", session_id="no-literals")
    )
    lowered = serialized.lower()
    assert literal_canary not in serialized
    assert not any(marker in lowered for marker in FORBIDDEN_CREDENTIAL_MARKERS)


def test_claude_argv_forces_owned_strict_flags(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    argv = build_claude_argv(
        ["/opt/test/bin/claude", "--model", "opus"],
        config_path,
    )
    assert argv[-3:] == [
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
    ]
    assert argv.count("--mcp-config") == 1
    assert argv.count("--strict-mcp-config") == 1


@pytest.mark.integration
def test_connected_kiwoom_kr_tool_list_is_exact_and_excludes_other_orders() -> None:
    names = set(
        asyncio.run(
            connected_tool_names(
                profile="kiwoom_kr",
                session_id="connected-kiwoom-kr",
            )
        )
    )
    kiwoom_names = {name for name in names if name.startswith("kiwoom_mock")}
    assert kiwoom_names == EXPECTED_KIWOOM_KR_TOOLS
    assert not any(name.startswith("kiwoom_mock_us_") for name in names)
    assert not any(name.startswith(("kis_live_", "toss_")) for name in names)
    assert FORBIDDEN_GENERIC_ORDER_TOOLS.isdisjoint(names)


@pytest.mark.integration
def test_concurrent_profiles_do_not_cross_contaminate() -> None:
    async def _gather() -> tuple[set[str], set[str]]:
        kiwoom, kis = await asyncio.gather(
            connected_tool_names(
                profile="kiwoom_kr",
                session_id="concurrent-kiwoom-kr",
            ),
            connected_tool_names(
                profile="hermes-paper-kis",
                session_id="concurrent-hermes-kis",
            ),
        )
        return set(kiwoom), set(kis)

    kiwoom_names, kis_names = asyncio.run(_gather())
    assert EXPECTED_KIWOOM_KR_TOOLS <= kiwoom_names
    assert EXPECTED_KIWOOM_KR_TOOLS.isdisjoint(kis_names)
    assert "kis_mock_place_order" in kis_names
    assert "kis_mock_place_order" not in kiwoom_names


def _pids_for_session(session_id: str) -> set[int]:
    result = subprocess.run(
        ["pgrep", "-f", f"mock_session_mcp.py _serve-stdio .*{session_id}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        int(line)
        for line in result.stdout.splitlines()
        if line.strip().isdigit() and int(line) != os.getpid()
    }


@pytest.mark.integration
def test_stdio_child_has_no_listener_and_no_orphan_after_session_exit() -> None:
    session_id = "orphan-proof-rob1173"

    async def _observe() -> tuple[set[int], dict[int, str]]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        config = build_mcp_config(profile="kiwoom_kr", session_id=session_id)
        server = next(iter(config["mcpServers"].values()))
        params = StdioServerParameters(
            command=server["command"],
            args=server["args"],
            env=dict(os.environ),
            cwd=Path(__file__).resolve().parents[2],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                pids = _pids_for_session(session_id)
                assert pids
                listeners: dict[int, str] = {}
                for pid in pids:
                    result = subprocess.run(
                        [
                            "lsof",
                            "-nP",
                            "-a",
                            "-p",
                            str(pid),
                            "-iTCP",
                            "-sTCP:LISTEN",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    listeners[pid] = result.stdout
                return pids, listeners

    child_pids, listeners = asyncio.run(_observe())
    assert all(not output.strip() for output in listeners.values())

    deadline = time.monotonic() + 5
    remaining = _pids_for_session(session_id)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = _pids_for_session(session_id)
    assert not remaining
    assert child_pids
