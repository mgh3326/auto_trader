#!/usr/bin/env python3
"""Session-owned, strict MCP launcher for approved mock profiles.

This is the repo-owned seam consumed by a herdr launcher.  It deliberately
supports Claude only: Codex and Kiro do not currently have a profile-isolated
adapter, so mock-lane attempts fail before a config or client process exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE_MOCK_PROFILES = frozenset(
    {
        "hermes-paper-kis",
        "kiwoom_kr",
        "us-paper",
    }
)
LEGACY_MOCK_PROFILES = frozenset({"default", "kiwoom"})
SUPPORTED_CLIENT = "claude"
UNSUPPORTED_MOCK_CLIENTS = frozenset({"codex", "kiro"})
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_CLAUDE_MCP_FLAGS = frozenset({"--mcp-config", "--strict-mcp-config"})
_CLAUDE_MCP_OVERRIDE_FLAGS = _CLAUDE_MCP_FLAGS | frozenset({"--no-strict-mcp-config"})
_TERM_GRACE_SECONDS = 1.0
_KILL_GRACE_SECONDS = 1.0
_SIGNAL_POLL_SECONDS = 0.05
_FORWARDED_SIGNAL_GRACE_SECONDS = 0.25
_MANAGED_CLIENT_SIGNALS = (
    signal.SIGHUP,
    signal.SIGTERM,
    signal.SIGINT,
)
_PROFILE_ALLOWED_BROKER_SCOPES: dict[str, frozenset[str]] = {
    "hermes-paper-kis": frozenset({"kis_mock"}),
    "kiwoom_kr": frozenset({"kiwoom_kr"}),
    "us-paper": frozenset({"alpaca_paper"}),
}
_BROKER_ENV_SCOPES: tuple[tuple[str, str], ...] = (
    ("KIWOOM_MOCK_US_", "kiwoom_us"),
    ("KIWOOM_MOCK_", "kiwoom_kr"),
    ("KIWOOM_", "kiwoom_other"),
    ("KIS_MOCK_", "kis_mock"),
    ("KIS_", "kis_other"),
    ("ALPACA_PAPER_", "alpaca_paper"),
    ("ALPACA_", "alpaca_other"),
    ("APCA_", "alpaca_other"),
    ("BINANCE_", "binance"),
    ("TOSS_", "toss"),
    ("UPBIT_", "upbit"),
)
# These legacy Settings fields are required at model construction even when the
# selected MCP profile cannot register their broker order surfaces. Empty,
# launcher-owned placeholders satisfy schema construction without inheriting
# the real foreign credentials.
_REQUIRED_FOREIGN_SETTINGS_PLACEHOLDERS = frozenset(
    {
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
    }
)


class MockSessionMcpError(RuntimeError):
    """Fail-closed mock-session configuration error."""


def validate_mock_profile(profile: str) -> str:
    """Return an approved profile or reject legacy/default/unknown profiles."""
    normalized = profile.strip()
    if normalized in LEGACY_MOCK_PROFILES:
        raise MockSessionMcpError(
            f"legacy mock MCP profile refused before spawn: {normalized!r}"
        )
    if normalized not in SAFE_MOCK_PROFILES:
        allowed = ", ".join(sorted(SAFE_MOCK_PROFILES))
        raise MockSessionMcpError(
            f"unknown mock MCP profile refused before spawn: {normalized!r}; "
            f"allowed: {allowed}"
        )
    return normalized


def validate_session_id(session_id: str) -> str:
    """Keep the session identifier bounded and safe for argv/temp-path evidence."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise MockSessionMcpError(
            "session id must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}"
        )
    return session_id


def validate_client(client: str) -> str:
    """Fail before config creation when no profile-isolated adapter exists."""
    normalized = client.strip().lower()
    if normalized in UNSUPPORTED_MOCK_CLIENTS:
        raise MockSessionMcpError(
            f"{normalized} mock lane refused before spawn: "
            "no profile-isolated MCP adapter"
        )
    if normalized != SUPPORTED_CLIENT:
        raise MockSessionMcpError(
            f"unknown mock client refused before spawn: {normalized!r}"
        )
    return normalized


def _repo_python() -> Path:
    executable = REPO_ROOT / ".venv" / "bin" / "python"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise MockSessionMcpError(
            f"repo virtualenv Python is unavailable: {executable}; "
            "run `uv sync --all-groups` first"
        )
    return executable


def build_mcp_config(*, profile: str, session_id: str) -> dict[str, Any]:
    """Build one stdio server definition without embedding any credential."""
    approved_profile = validate_mock_profile(profile)
    approved_session_id = validate_session_id(session_id)
    server_name = f"auto_trader_{approved_profile.replace('-', '_')}"
    return {
        "mcpServers": {
            server_name: {
                "type": "stdio",
                "command": str(_repo_python()),
                "args": [
                    str(Path(__file__).resolve()),
                    "_serve-stdio",
                    "--profile",
                    approved_profile,
                    "--session-id",
                    approved_session_id,
                ],
            }
        }
    }


@contextmanager
def session_config(
    *,
    profile: str,
    session_id: str,
    parent_dir: Path | None = None,
) -> Iterator[Path]:
    """Write a mode-0600 per-session config and delete it on session exit."""
    approved_profile = validate_mock_profile(profile)
    approved_session_id = validate_session_id(session_id)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f"auto-trader-{approved_session_id}-",
            dir=parent_dir,
        )
    )
    config_path = temp_dir / f"mcp-{approved_profile}.json"
    try:
        config_path.write_text(
            json.dumps(
                build_mcp_config(
                    profile=approved_profile,
                    session_id=approved_session_id,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        yield config_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _broker_env_scope(name: str) -> str | None:
    upper_name = name.upper()
    for prefix, scope in _BROKER_ENV_SCOPES:
        if upper_name.startswith(prefix):
            return scope
    return None


def _env_file_values(source_env: Mapping[str, str]) -> dict[str, str]:
    configured_path = source_env.get("ENV_FILE", ".env").strip()
    if not configured_path or configured_path == os.devnull:
        return {}
    env_path = Path(configured_path)
    if not env_path.is_absolute():
        env_path = REPO_ROOT / env_path
    if not env_path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }


def build_profile_environment(
    *,
    profile: str,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the inherited env while excluding every foreign broker scope."""
    approved_profile = validate_mock_profile(profile)
    ambient = dict(os.environ if source_env is None else source_env)
    merged = _env_file_values(ambient)
    merged.update(ambient)
    allowed_scopes = _PROFILE_ALLOWED_BROKER_SCOPES[approved_profile]
    child_env = {
        name: value
        for name, value in merged.items()
        if (scope := _broker_env_scope(name)) is None or scope in allowed_scopes
    }
    for name in _REQUIRED_FOREIGN_SETTINGS_PLACEHOLDERS:
        if _broker_env_scope(name) not in allowed_scopes:
            child_env[name] = ""
    # Settings must not reopen the repo-wide .env after this profile filter.
    child_env["ENV_FILE"] = os.devnull
    child_env["MCP_PROFILE"] = approved_profile
    child_env["MCP_TYPE"] = "stdio"
    for network_setting in ("MCP_HOST", "MCP_PORT", "MCP_PATH"):
        child_env.pop(network_setting, None)
    return child_env


def build_claude_argv(command: Sequence[str], config_path: Path) -> list[str]:
    """Place launcher-owned top-level flags immediately after the executable."""
    if not command:
        raise MockSessionMcpError("Claude command is required")
    executable_name = Path(command[0]).name
    if executable_name != "claude":
        raise MockSessionMcpError(
            f"Claude adapter requires a `claude` executable, got {executable_name!r}"
        )
    conflicting = {
        token
        for token in command[1:]
        if token in _CLAUDE_MCP_OVERRIDE_FLAGS
        or any(token.startswith(f"{flag}=") for flag in _CLAUDE_MCP_OVERRIDE_FLAGS)
    }
    if conflicting:
        flags = ", ".join(sorted(conflicting))
        raise MockSessionMcpError(
            f"caller-supplied MCP flags refused; launcher owns: {flags}"
        )
    return [
        command[0],
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        *command[1:],
    ]


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.02)
    return not _process_group_exists(process_group_id)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    process_group_id = process.pid
    process.poll()
    if not _process_group_exists(process_group_id):
        process.wait(timeout=0)
        return

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=0)
        return

    try:
        process.wait(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if _wait_for_process_group_exit(process_group_id, _TERM_GRACE_SECONDS):
        process.wait(timeout=0)
        return

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        process.wait(timeout=0)
        return
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MockSessionMcpError(
            f"Claude child pid {process.pid} could not be reaped after SIGKILL"
        ) from exc
    if not _wait_for_process_group_exit(process_group_id, _KILL_GRACE_SECONDS):
        raise MockSessionMcpError(
            f"Claude process group {process_group_id} survived SIGKILL"
        )


def _forward_process_group_signal(
    process: subprocess.Popen[Any],
    signum: int,
) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def launch_claude(
    *,
    profile: str,
    session_id: str,
    client: str,
    command: Sequence[str],
) -> int:
    """Run Claude and own its process group/config for the full session."""
    approved_profile = validate_mock_profile(profile)
    approved_session_id = validate_session_id(session_id)
    validate_client(client)

    pending_signal: int | None = None
    child_return_code: int | None = None
    process: subprocess.Popen[Any] | None = None
    previous_handlers: dict[int, Any] = {}
    previous_signal_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        _MANAGED_CLIENT_SIGNALS,
    )
    signals_blocked = True

    def _record_signal(signum: int, _frame: object) -> None:
        nonlocal pending_signal
        if pending_signal is None:
            pending_signal = signum

    def _restore_child_signal_mask() -> None:
        # Popen executes this in its single-threaded fork child immediately
        # before exec. Restore the mask that existed before the parent entered
        # its spawn critical section; Claude must not inherit our temporary
        # SIGHUP/SIGTERM/SIGINT block.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)

    try:
        for signum in _MANAGED_CLIENT_SIGNALS:
            previous_handlers[signum] = signal.signal(signum, _record_signal)

        with session_config(
            profile=approved_profile,
            session_id=approved_session_id,
        ) as config_path:
            argv = build_claude_argv(command, config_path)
            child_env = build_profile_environment(profile=approved_profile)
            process = subprocess.Popen(
                argv,
                env=child_env,
                start_new_session=True,
                preexec_fn=_restore_child_signal_mask,
            )
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
                signals_blocked = False
                if pending_signal is None:
                    print(
                        json.dumps(
                            {
                                "event": "mock_mcp_client_started",
                                "client": SUPPORTED_CLIENT,
                                "profile": approved_profile,
                                "session_id": approved_session_id,
                                "pid": process.pid,
                                "argv_contract": [
                                    Path(argv[0]).name,
                                    "--mcp-config",
                                    str(config_path),
                                    "--strict-mcp-config",
                                ],
                                "server_count": 1,
                                "transport": "stdio",
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                while pending_signal is None and child_return_code is None:
                    try:
                        child_return_code = process.wait(timeout=_SIGNAL_POLL_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
                if pending_signal is not None:
                    _forward_process_group_signal(
                        process,
                        pending_signal,
                    )
                    try:
                        child_return_code = process.wait(
                            timeout=_FORWARDED_SIGNAL_GRACE_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                _terminate_process_group(process)
    finally:
        try:
            if signals_blocked:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    if pending_signal is not None:
        return 128 + pending_signal
    if process is None or child_return_code is None:
        raise MockSessionMcpError("Claude child exited without a return code")
    return child_return_code


def _serve_stdio(*, profile: str, session_id: str) -> int:
    """Bootstrap the MCP server only after the strict profile is validated."""
    approved_profile = validate_mock_profile(profile)
    approved_session_id = validate_session_id(session_id)
    child_env = build_profile_environment(
        profile=approved_profile,
        source_env=os.environ,
    )
    os.environ.clear()
    os.environ.update(child_env)
    os.environ["AUTO_TRADER_MCP_SESSION_ID"] = approved_session_id
    os.chdir(REPO_ROOT)

    from app.mcp_server.main import main

    main()
    return 0


async def connected_tool_names(*, profile: str, session_id: str) -> list[str]:
    """Connect to a real stdio child and return its complete advertised tool list."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = build_mcp_config(profile=profile, session_id=session_id)
    server = next(iter(config["mcpServers"].values()))
    parameters = StdioServerParameters(
        command=server["command"],
        args=server["args"],
        env=build_profile_environment(profile=profile),
        cwd=REPO_ROOT,
    )
    names: list[str] = []
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            cursor: str | None = None
            while True:
                result = await session.list_tools(cursor=cursor)
                names.extend(tool.name for tool in result.tools)
                cursor = result.nextCursor
                if not cursor:
                    break
    return sorted(names)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run = subparsers.add_parser(
        "run",
        help="launch a Claude mock session with one strict stdio MCP child",
    )
    run.add_argument("--profile", required=True)
    run.add_argument("--session-id", required=True)
    run.add_argument("--client", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)

    verify = subparsers.add_parser(
        "verify",
        help="connect without tool calls and print the complete tool list",
    )
    verify.add_argument("--profile", required=True)
    verify.add_argument("--session-id", required=True)

    child = subparsers.add_parser("_serve-stdio")
    child.add_argument("--profile", required=True)
    child.add_argument("--session-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.subcommand == "run":
            command = list(args.command)
            if command and command[0] == "--":
                command.pop(0)
            return launch_claude(
                profile=args.profile,
                session_id=args.session_id,
                client=args.client,
                command=command,
            )
        if args.subcommand == "verify":
            names = asyncio.run(
                connected_tool_names(
                    profile=args.profile,
                    session_id=args.session_id,
                )
            )
            print(
                json.dumps(
                    {
                        "profile": validate_mock_profile(args.profile),
                        "session_id": validate_session_id(args.session_id),
                        "tool_count": len(names),
                        "tools": names,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.subcommand == "_serve-stdio":
            return _serve_stdio(
                profile=args.profile,
                session_id=args.session_id,
            )
    except MockSessionMcpError as exc:
        print(f"mock-session-mcp: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled subcommand: {args.subcommand}")


if __name__ == "__main__":
    raise SystemExit(main())
