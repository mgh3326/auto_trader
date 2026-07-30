from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from app.mcp_server.tooling.kiwoom_kr_registration import (
    kiwoom_kr_profile_tool_names,
)
from scripts import mock_session_mcp
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


def _assert_kiwoom_kr_connected_surface(names: set[str]) -> None:
    from app.mcp_server.tooling.route_request_lanes import (
        DIRECT_BROKER_MUTATION_TOOLS,
    )

    assert names == kiwoom_kr_profile_tool_names()
    kiwoom_names = {name for name in names if name.startswith("kiwoom_mock")}
    assert kiwoom_names == EXPECTED_KIWOOM_KR_TOOLS
    allowed_direct = EXPECTED_KIWOOM_KR_TOOLS & DIRECT_BROKER_MUTATION_TOOLS
    assert names & DIRECT_BROKER_MUTATION_TOOLS == allowed_direct


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
    assert argv[:4] == [
        "/opt/test/bin/claude",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
    ]
    assert argv[4:] == ["--model", "opus"]
    assert argv.count("--mcp-config") == 1
    assert argv.count("--strict-mcp-config") == 1


def test_claude_top_level_flags_precede_read_only_subcommand(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    argv = build_claude_argv(
        ["/opt/test/bin/claude", "mcp", "list"],
        config_path,
    )
    assert argv == [
        "/opt/test/bin/claude",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "mcp",
        "list",
    ]


@pytest.mark.parametrize(
    "caller_flags",
    [
        ["--mcp-config", "/tmp/foreign.json"],
        ["--mcp-config=/tmp/foreign.json"],
        ["--strict-mcp-config"],
        ["--strict-mcp-config=true"],
        ["--no-strict-mcp-config"],
    ],
)
def test_caller_mcp_overrides_are_refused(
    tmp_path: Path,
    caller_flags: list[str],
) -> None:
    with pytest.raises(MockSessionMcpError, match="caller-supplied MCP flags refused"):
        build_claude_argv(
            ["/opt/test/bin/claude", *caller_flags, "mcp", "list"],
            tmp_path / "mcp.json",
        )


def test_kiwoom_child_env_removes_other_broker_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "profile.env"
    env_file.write_text(
        "\n".join(
            [
                "KIWOOM_MOCK_APP_SECRET=ROB1173_ALLOWED_CANARY",
                "KIWOOM_MOCK_US_APP_SECRET=ROB1173_FOREIGN_US_CANARY",
                "KIS_MOCK_APP_SECRET=ROB1173_FOREIGN_KIS_CANARY",
                "TOSS_API_CLIENT_SECRET=ROB1173_FOREIGN_TOSS_CANARY",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    child_env = mock_session_mcp.build_profile_environment(
        profile="kiwoom_kr",
        source_env={
            "ENV_FILE": str(env_file),
            "PATH": os.environ.get("PATH", ""),
            "BINANCE_FUTURES_DEMO_API_SECRET": "ROB1173_FOREIGN_BINANCE_CANARY",
        },
    )

    assert "KIWOOM_MOCK_APP_SECRET" in child_env
    assert "KIWOOM_MOCK_US_APP_SECRET" not in child_env
    assert "KIS_MOCK_APP_SECRET" not in child_env
    assert "TOSS_API_CLIENT_SECRET" not in child_env
    assert "BINANCE_FUTURES_DEMO_API_SECRET" not in child_env
    assert child_env["ENV_FILE"] == os.devnull
    assert not any(value.startswith("ROB1173_FOREIGN_") for value in child_env.values())


def test_serve_stdio_applies_profile_env_boundary_before_server_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, bool] = {}
    fake_main = types.ModuleType("app.mcp_server.main")

    def capture_environment() -> None:
        captured.update(
            {
                "target_present": "KIWOOM_MOCK_APP_SECRET" in os.environ,
                "kiwoom_us_present": "KIWOOM_MOCK_US_APP_SECRET" in os.environ,
                "kis_present": "KIS_MOCK_APP_SECRET" in os.environ,
                "toss_present": "TOSS_API_CLIENT_SECRET" in os.environ,
                "env_file_disabled": os.environ.get("ENV_FILE") == os.devnull,
            }
        )

    fake_main.main = capture_environment
    monkeypatch.setitem(sys.modules, "app.mcp_server.main", fake_main)
    original_env = dict(os.environ)
    original_cwd = Path.cwd()
    try:
        os.environ.update(
            {
                "ENV_FILE": os.devnull,
                "KIWOOM_MOCK_APP_SECRET": "ROB1173_ALLOWED_CHILD_CANARY",
                "KIWOOM_MOCK_US_APP_SECRET": "ROB1173_FOREIGN_US_CHILD_CANARY",
                "KIS_MOCK_APP_SECRET": "ROB1173_FOREIGN_KIS_CHILD_CANARY",
                "TOSS_API_CLIENT_SECRET": "ROB1173_FOREIGN_TOSS_CHILD_CANARY",
            }
        )
        assert (
            mock_session_mcp._serve_stdio(
                profile="kiwoom_kr",
                session_id="profile-env-child",
            )
            == 0
        )
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        os.chdir(original_cwd)

    assert captured == {
        "target_present": True,
        "kiwoom_us_present": False,
        "kis_present": False,
        "toss_present": False,
        "env_file_disabled": True,
    }


def test_post_popen_exception_cleans_child_before_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "exception-state.json"
    fake_claude = _write_stubborn_claude(tmp_path, state_path)
    monkeypatch.setenv("ENV_FILE", os.devnull)
    monkeypatch.setenv("ROB1173_TEST_STATE_FILE", str(state_path))
    real_popen = subprocess.Popen
    state: dict[str, object] | None = None

    def spawn_and_wait(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[str]:
        process = real_popen(argv, **kwargs)
        published = _wait_for_probe_state(state_path)
        assert Path(str(published["config_path"])).exists()
        return process

    real_json_dumps = json.dumps
    dump_calls = 0

    def fail_audit_json(*args: object, **kwargs: object) -> str:
        nonlocal dump_calls
        dump_calls += 1
        if dump_calls == 2:
            raise RuntimeError("post-Popen audit failure")
        return real_json_dumps(*args, **kwargs)

    monkeypatch.setattr(mock_session_mcp.subprocess, "Popen", spawn_and_wait)
    monkeypatch.setattr(mock_session_mcp.json, "dumps", fail_audit_json)
    try:
        with pytest.raises(RuntimeError, match="post-Popen audit failure"):
            mock_session_mcp.launch_claude(
                profile="kiwoom_kr",
                session_id="post-popen-exception",
                client="claude",
                command=[str(fake_claude)],
            )
        state = _wait_for_probe_state(state_path)
        assert not _pid_exists(int(state["pid"]))
        assert not _listener_accepts(int(state["port"]))
        assert not Path(str(state["config_path"])).exists()
    finally:
        _cleanup_probe(None, state)


def test_parent_signal_mask_restore_exception_cleans_post_popen_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = types.SimpleNamespace(pid=998_1173)
    terminated: list[object] = []
    config_path: Path | None = None
    real_pthread_sigmask = signal.pthread_sigmask
    parent_restore_calls = 0

    def fake_popen(
        argv: list[str],
        **_kwargs: object,
    ) -> object:
        nonlocal config_path
        config_path = Path(argv[argv.index("--mcp-config") + 1])
        assert config_path.exists()
        return fake_process

    def fail_first_parent_restore(
        how: signal.Sigmasks,
        mask: set[signal.Signals] | tuple[signal.Signals, ...],
    ) -> set[signal.Signals]:
        nonlocal parent_restore_calls
        if how == signal.SIG_SETMASK:
            parent_restore_calls += 1
            if parent_restore_calls == 1:
                raise RuntimeError("parent signal-mask restore failure")
        return real_pthread_sigmask(how, mask)

    monkeypatch.setattr(mock_session_mcp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        mock_session_mcp,
        "_terminate_process_group",
        terminated.append,
    )
    monkeypatch.setattr(signal, "pthread_sigmask", fail_first_parent_restore)

    with pytest.raises(RuntimeError, match="parent signal-mask restore failure"):
        mock_session_mcp.launch_claude(
            profile="kiwoom_kr",
            session_id="mask-restore-exception",
            client="claude",
            command=["/opt/test/bin/claude"],
        )

    assert terminated == [fake_process]
    assert parent_restore_calls == 2
    assert config_path is not None
    assert not config_path.exists()


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
    _assert_kiwoom_kr_connected_surface(names)


def test_central_contract_rejects_nonlocal_broker_alias() -> None:
    with pytest.raises(AssertionError):
        _assert_kiwoom_kr_connected_surface(
            kiwoom_kr_profile_tool_names() | {"kis_mock_cancel_order"}
        )


def test_closed_world_rejects_unclassified_broker_alias() -> None:
    with pytest.raises(AssertionError):
        _assert_kiwoom_kr_connected_surface(
            kiwoom_kr_profile_tool_names() | {"kis_mock_shadow_place_order"}
        )


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


def _process_census() -> dict[int, tuple[int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    census: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            census[int(fields[0])] = (int(fields[1]), fields[2])
    return census


def _descendant_pids(
    parent_pid: int,
    census: dict[int, tuple[int, str]],
) -> set[int]:
    descendants: set[int] = set()
    frontier = {parent_pid}
    while frontier:
        children = {
            pid
            for pid, (ppid, comm) in census.items()
            if ppid in frontier and Path(comm).name != "ps"
        }
        children -= descendants
        if not children:
            break
        descendants |= children
        frontier = children
    return descendants


@pytest.mark.integration
def test_stdio_child_has_no_listener_and_no_orphan_after_session_exit() -> None:
    session_id = "orphan-proof-rob1173"
    before = _descendant_pids(os.getpid(), _process_census())

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
                pids = _descendant_pids(os.getpid(), _process_census()) - before
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
    remaining = child_pids & _process_census().keys()
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = child_pids & _process_census().keys()
    assert not remaining
    assert child_pids


def _write_stubborn_claude(tmp_path: Path, state_path: Path) -> Path:
    executable = tmp_path / "claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(signum, signal.SIG_IGN)
blocked_signals = signal.pthread_sigmask(signal.SIG_BLOCK, set())
config_index = sys.argv.index("--mcp-config") + 1
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
Path(os.environ["ROB1173_TEST_STATE_FILE"]).write_text(
    json.dumps(
        {
            "pid": os.getpid(),
            "port": listener.getsockname()[1],
            "config_path": sys.argv[config_index],
            "blocked_managed_signals": sorted(
                int(signum)
                for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
                if signum in blocked_signals
            ),
        }
    ),
    encoding="utf-8",
)
while True:
    time.sleep(60)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _write_signal_observing_claude(tmp_path: Path, state_path: Path) -> Path:
    executable = tmp_path / "claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

config_index = sys.argv.index("--mcp-config") + 1
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
blocked_signals = signal.pthread_sigmask(signal.SIG_BLOCK, set())
state_path = Path(os.environ["ROB1173_TEST_STATE_FILE"])
state = {
    "pid": os.getpid(),
    "port": listener.getsockname()[1],
    "config_path": sys.argv[config_index],
    "blocked_managed_signals": sorted(
        int(signum)
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        if signum in blocked_signals
    ),
    "received_signal": None,
}


def publish() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def handle(signum, _frame) -> None:
    state["received_signal"] = signum
    publish()
    listener.close()
    raise SystemExit(0)


for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
    signal.signal(signum, handle)
publish()
while True:
    time.sleep(60)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _write_popen_gap_harness(tmp_path: Path) -> Path:
    harness = tmp_path / "popen-gap-harness.py"
    harness.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from scripts import mock_session_mcp

real_popen = subprocess.Popen
state_path = Path(os.environ["ROB1173_TEST_STATE_FILE"])


def spawn_then_sighup(argv, **kwargs):
    parent_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    process = real_popen(argv, **kwargs)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            break
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    else:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise RuntimeError("fake Claude did not publish lifecycle state")
    state["parent_blocked_managed_signals_during_popen"] = sorted(
        int(signum)
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        if signum in parent_mask
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    os.kill(os.getpid(), signal.SIGHUP)
    return process


mock_session_mcp.subprocess.Popen = spawn_then_sighup
raise SystemExit(
    mock_session_mcp.launch_claude(
        profile="kiwoom_kr",
        session_id="popen-assignment-gap",
        client="claude",
        command=[sys.argv[1]],
    )
)
""",
        encoding="utf-8",
    )
    harness.chmod(0o700)
    return harness


def _wait_for_probe_state(state_path: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise AssertionError("fake Claude did not publish lifecycle state")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _listener_accepts(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _cleanup_probe(
    wrapper: subprocess.Popen[str] | None,
    state: dict[str, object] | None,
) -> None:
    if wrapper is not None and wrapper.poll() is None:
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait(timeout=5)
    if state is None:
        return
    child_pid = int(state["pid"])
    if _pid_exists(child_pid):
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    config_path = Path(str(state["config_path"]))
    temp_dir = config_path.parent
    if temp_dir.name.startswith("auto-trader-"):
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_sighup_in_popen_assignment_gap_is_bounded_and_leak_free(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "popen-gap-state.json"
    fake_claude = _write_stubborn_claude(tmp_path, state_path)
    harness = _write_popen_gap_harness(tmp_path)
    env = dict(os.environ)
    env["ENV_FILE"] = os.devnull
    env["ROB1173_TEST_STATE_FILE"] = str(state_path)
    wrapper: subprocess.Popen[str] | None = None
    state: dict[str, object] | None = None
    started = time.monotonic()
    try:
        wrapper = subprocess.Popen(
            [sys.executable, str(harness), str(fake_claude)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        wrapper.wait(timeout=7)
        elapsed = time.monotonic() - started
        state = _wait_for_probe_state(state_path)
        observed = {
            "rc": wrapper.returncode,
            "child_alive_after_parent_exit": _pid_exists(int(state["pid"])),
            "listener_alive_after_parent_exit": _listener_accepts(int(state["port"])),
            "config_exists_after_parent_exit": Path(str(state["config_path"])).exists(),
            "parent_blocked_managed_signals_during_popen": state[
                "parent_blocked_managed_signals_during_popen"
            ],
            "child_blocked_managed_signals": state["blocked_managed_signals"],
        }
        assert elapsed < 7
        assert observed == {
            "rc": 128 + int(signal.SIGHUP),
            "child_alive_after_parent_exit": False,
            "listener_alive_after_parent_exit": False,
            "config_exists_after_parent_exit": False,
            "parent_blocked_managed_signals_during_popen": sorted(
                int(signum) for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
            ),
            "child_blocked_managed_signals": [],
        }
    finally:
        _cleanup_probe(wrapper, state)


@pytest.mark.parametrize(
    "signum",
    [signal.SIGHUP, signal.SIGTERM, signal.SIGINT],
    ids=["sighup", "sigterm", "sigint"],
)
def test_wrapper_restores_child_signal_mask_and_forwards_managed_signal(
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    state_path = tmp_path / f"forwarded-{signum.name.lower()}-state.json"
    fake_claude = _write_signal_observing_claude(tmp_path, state_path)
    env = dict(os.environ)
    env["ENV_FILE"] = os.devnull
    env["ROB1173_TEST_STATE_FILE"] = str(state_path)
    wrapper: subprocess.Popen[str] | None = None
    state: dict[str, object] | None = None
    try:
        wrapper = subprocess.Popen(
            [
                sys.executable,
                str(Path(mock_session_mcp.__file__).resolve()),
                "run",
                "--profile",
                "kiwoom_kr",
                "--session-id",
                f"signal-mask-forwarding-{signum.name.lower()}",
                "--client",
                "claude",
                "--",
                str(fake_claude),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        state = _wait_for_probe_state(state_path)
        assert state["blocked_managed_signals"] == []
        assert _listener_accepts(int(state["port"]))

        wrapper.send_signal(signum)
        assert wrapper.wait(timeout=5) == 128 + int(signum)
        state = _wait_for_probe_state(state_path)

        assert state["received_signal"] == int(signum)
        assert not _pid_exists(int(state["pid"]))
        assert not _listener_accepts(int(state["port"]))
        assert not Path(str(state["config_path"])).exists()
    finally:
        _cleanup_probe(wrapper, state)


@pytest.mark.parametrize(
    "signum",
    [signal.SIGHUP, signal.SIGTERM],
    ids=["sighup", "sigterm-stubborn-child"],
)
def test_wrapper_signal_cleanup_is_bounded_and_leak_free(
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    state_path = tmp_path / "state.json"
    fake_claude = _write_stubborn_claude(tmp_path, state_path)
    env = dict(os.environ)
    env["ENV_FILE"] = os.devnull
    env["ROB1173_TEST_STATE_FILE"] = str(state_path)
    wrapper: subprocess.Popen[str] | None = None
    state: dict[str, object] | None = None
    try:
        wrapper = subprocess.Popen(
            [
                sys.executable,
                str(Path(mock_session_mcp.__file__).resolve()),
                "run",
                "--profile",
                "kiwoom_kr",
                "--session-id",
                f"lifecycle-{signum.name.lower()}",
                "--client",
                "claude",
                "--",
                str(fake_claude),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        state = _wait_for_probe_state(state_path)
        assert _listener_accepts(int(state["port"]))
        wrapper.send_signal(signum)
        try:
            return_code = wrapper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("wrapper did not bound shutdown for a stubborn child")
        assert return_code == 128 + int(signum)

        deadline = time.monotonic() + 2
        while _pid_exists(int(state["pid"])) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_exists(int(state["pid"]))
        assert not _listener_accepts(int(state["port"]))
        assert not Path(str(state["config_path"])).exists()
    finally:
        _cleanup_probe(wrapper, state)
