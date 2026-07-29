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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


def build_claude_argv(command: Sequence[str], config_path: Path) -> list[str]:
    """Append the two mandatory flags to the exact Claude client argv."""
    if not command:
        raise MockSessionMcpError("Claude command is required")
    executable_name = Path(command[0]).name
    if executable_name != "claude":
        raise MockSessionMcpError(
            f"Claude adapter requires a `claude` executable, got {executable_name!r}"
        )
    conflicting = _CLAUDE_MCP_FLAGS.intersection(command)
    if conflicting:
        flags = ", ".join(sorted(conflicting))
        raise MockSessionMcpError(
            f"caller-supplied MCP flags refused; launcher owns: {flags}"
        )
    return [
        *command,
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
    ]


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


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

    with session_config(
        profile=approved_profile,
        session_id=approved_session_id,
    ) as config_path:
        argv = build_claude_argv(command, config_path)
        process = subprocess.Popen(argv, start_new_session=True)
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

        forwarded_signal: int | None = None

        def _forward(signum: int, _frame: object) -> None:
            nonlocal forwarded_signal
            forwarded_signal = signum
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass

        previous_handlers = {
            signum: signal.signal(signum, _forward)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            return_code = process.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            _terminate_process_group(process)
        if forwarded_signal is not None and return_code < 0:
            return 128 + forwarded_signal
        return return_code


def _serve_stdio(*, profile: str, session_id: str) -> int:
    """Bootstrap the MCP server only after the strict profile is validated."""
    approved_profile = validate_mock_profile(profile)
    approved_session_id = validate_session_id(session_id)
    os.environ["MCP_PROFILE"] = approved_profile
    os.environ["MCP_TYPE"] = "stdio"
    os.environ["AUTO_TRADER_MCP_SESSION_ID"] = approved_session_id
    for network_setting in ("MCP_HOST", "MCP_PORT", "MCP_PATH"):
        os.environ.pop(network_setting, None)
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
        env=dict(os.environ),
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
