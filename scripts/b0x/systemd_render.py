"""Fail-closed renderer for public B0X systemd source templates.

The public repository contains no operational host layout. A future installer
supplies exact local paths, renders only into a private staging directory, and
then performs a separate readback. This module never installs or enables a
unit and never reads the environment-file contents.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_TOKEN = re.compile(r"@[A-Z][A-Z0-9_]*@")
_SERVICE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_EXPECTED_TOKENS = frozenset(
    {
        "@AUTO_TRADER_WORKTREE@",
        "@B0X_ENV_FILE@",
        "@B0X_PYTHON_EXECUTABLE@",
        "@B0X_SERVICE_USER@",
        "@B0X_TIMEOUT_START_SECONDS@",
    }
)


class B0XUnitRenderError(ValueError):
    """A render input or output violates the public-template contract."""


@dataclass(frozen=True)
class B0XUnitRenderInputs:
    auto_trader_worktree: Path
    env_file: Path
    python_executable: Path
    service_user: str
    timeout_start_seconds: int


_Runner = Callable[..., subprocess.CompletedProcess[str]]


def _canonical_existing(path: Path, *, kind: str, executable: bool = False) -> Path:
    if not path.is_absolute():
        raise B0XUnitRenderError(f"{kind} must be absolute")
    if path.is_symlink():
        raise B0XUnitRenderError(f"{kind} must not be a symlink")
    if kind == "auto_trader_worktree":
        if not path.is_dir():
            raise B0XUnitRenderError(f"{kind} must be an existing directory")
    elif not path.is_file():
        raise B0XUnitRenderError(f"{kind} must be an existing file")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise B0XUnitRenderError(f"{kind} must be canonical")
    if executable and not os.access(resolved, os.X_OK):
        raise B0XUnitRenderError(f"{kind} must be executable")
    return resolved


def _git_value(runner: _Runner, worktree: Path, argv: Sequence[str]) -> str:
    result = runner(
        ["git", "-C", str(worktree), *argv],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise B0XUnitRenderError("auto_trader_worktree must be a usable Git worktree")
    return result.stdout.strip()


def _validated_values(
    inputs: B0XUnitRenderInputs, *, runner: _Runner
) -> tuple[dict[str, str], str]:
    worktree = _canonical_existing(
        inputs.auto_trader_worktree, kind="auto_trader_worktree"
    )
    git_root = _git_value(runner, worktree, ("rev-parse", "--show-toplevel"))
    if Path(git_root).resolve(strict=True) != worktree:
        raise B0XUnitRenderError("auto_trader_worktree differs from its Git root")
    git_head = _git_value(runner, worktree, ("rev-parse", "HEAD"))
    if _GIT_SHA.fullmatch(git_head) is None:
        raise B0XUnitRenderError("auto_trader_worktree HEAD is invalid")
    env_file = _canonical_existing(inputs.env_file, kind="env_file")
    python = _canonical_existing(
        inputs.python_executable, kind="python_executable", executable=True
    )
    if _SERVICE_USER.fullmatch(inputs.service_user) is None:
        raise B0XUnitRenderError("service_user is invalid")
    timeout = inputs.timeout_start_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 300
    ):
        raise B0XUnitRenderError(
            "timeout_start_seconds must be an integer from 1 to 300"
        )
    values = {
        "@AUTO_TRADER_WORKTREE@": str(worktree),
        "@B0X_ENV_FILE@": str(env_file),
        "@B0X_PYTHON_EXECUTABLE@": str(python),
        "@B0X_SERVICE_USER@": inputs.service_user,
        "@B0X_TIMEOUT_START_SECONDS@": str(timeout),
    }
    forbidden = re.compile(r"[\s'\"\\$%;]")
    if any(forbidden.search(value) for value in values.values()):
        raise B0XUnitRenderError("render values contain unsafe systemd characters")
    return values, git_head


def render_b0x_service_template(
    template_path: Path,
    *,
    staging_root: Path,
    inputs: B0XUnitRenderInputs,
    runner: _Runner = subprocess.run,
) -> dict[str, str]:
    """Create one rendered service in a private stage without overwriting."""

    template = _canonical_existing(template_path, kind="template_path")
    if not template.name.endswith(".service.in"):
        raise B0XUnitRenderError("template_path must end in .service.in")
    stage = _canonical_existing(staging_root, kind="auto_trader_worktree")
    output = stage / template.name.removesuffix(".in")
    if output.exists() or output.is_symlink():
        raise B0XUnitRenderError("render output already exists")
    raw = template.read_text(encoding="utf-8")
    tokens = frozenset(_TOKEN.findall(raw))
    if tokens != _EXPECTED_TOKENS:
        raise B0XUnitRenderError("template token set differs from the closed contract")
    rendered = raw
    values, git_head = _validated_values(inputs, runner=runner)
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    if _TOKEN.search(rendered):
        raise B0XUnitRenderError("render output retains an unresolved token")
    for required in (
        "Environment=B0X_UNIT_TEMPLATE_RENDERED=false",
        "Environment=LANE_EVENT_KICKOFF_ENABLED=false",
        "Environment=LANE_EVENT_KICKOFF_B0X_ENABLED=false",
        "ExecCondition=/usr/bin/test ${B0X_UNIT_TEMPLATE_RENDERED} = true",
        "--lane ${LANE_EVENT_KICKOFF_LANE_B0X}",
        "--dry-run",
    ):
        if required not in rendered:
            raise B0XUnitRenderError("render output lost a default-off source guard")
    encoded = rendered.encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "rendered_path": str(output),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "installed": "false",
        "enabled": "false",
        "auto_trader_head": git_head,
    }


__all__ = [
    "B0XUnitRenderError",
    "B0XUnitRenderInputs",
    "render_b0x_service_template",
]
