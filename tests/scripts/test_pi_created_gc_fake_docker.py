"""hk 6438: fake-docker behaviour of created-gc.sh.

A fake `docker` on PATH serves a fixed created-state listing; the script must
list the eligible named job container (at-toss-fill-poll), skip service names,
and exit 1 without removals when `docker ps` fails.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/shiftgate/pi/created-gc.sh"

FAKE_DOCKER = """#!/bin/bash
# FAKE_DOCKER_STATE/<id-files>: ids (one per line), inspect-<id> (one line:
# "<name> <image> <created> <started>"), removed (appended by rm).
set -u
state="${FAKE_DOCKER_STATE:?}"
case "${1:?}" in
  ps)
    if [ "${FAKE_DOCKER_PS_FAIL:-0}" = "1" ]; then
      echo "fake docker: ps failed" >&2; exit 1
    fi
    cat "$state/ids"
    ;;
  inspect)
    id="${@: -1}"
    cat "$state/inspect-$id"
    ;;
  rm)
    printf '%s\\n' "${@: -1}" >> "$state/removed"
    ;;
  *)
    echo "fake docker: unexpected $*" >&2; exit 2 ;;
esac
"""

IMAGE = "ghcr.io/mgh3326/auto_trader:sha-abc1234"
NEVER_STARTED = "0001-01-01T00:00:00Z"


def _created_hours_ago(hours: int) -> str:
    out = subprocess.run(
        ["date", "-u", "-d", f"{hours} hours ago", "+%Y-%m-%dT%H:%M:%SZ"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _setup_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    old = _created_hours_ago(25)
    rows = {
        # Eligible: named job container, never started, 25 h old.
        "c1": f"/at-toss-fill-poll {IMAGE} {old} {NEVER_STARTED}",
        # Service names: excluded even though created + never started + old.
        "c2": f"/at-api-blue {IMAGE} {old} {NEVER_STARTED}",
        "c3": f"/shiftgate-at-mcp-green {IMAGE} {old} {NEVER_STARTED}",
        "c4": f"/at-pg {IMAGE} {old} {NEVER_STARTED}",
    }
    (state / "ids").write_text("\n".join(rows) + "\n")
    for cid, line in rows.items():
        (state / f"inspect-{cid}").write_text(line + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return state


def _run(
    state: Path, *args: str, ps_fail: bool = False
) -> subprocess.CompletedProcess[str]:
    env = dict(
        os.environ,
        PATH=f"{state.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        FAKE_DOCKER_STATE=str(state),
        FAKE_DOCKER_PS_FAIL="1" if ps_fail else "0",
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env
    )


def _removed(state: Path) -> list[str]:
    removed = state / "removed"
    if not removed.exists():
        return []
    return removed.read_text().split()


def test_lists_job_container_and_skips_service_names(tmp_path: Path) -> None:
    state = _setup_state(tmp_path)
    proc = _run(state)
    assert "name=at-pg " not in proc.stdout
    assert "name=at-toss-fill-poll" in proc.stdout
    assert "candidates=1 apply=0" in proc.stdout
    assert "at-api-blue" not in proc.stdout
    assert "shiftgate-at-mcp-green" not in proc.stdout
    assert _removed(state) == []


def test_apply_removes_only_the_job_container(tmp_path: Path) -> None:
    state = _setup_state(tmp_path)
    proc = _run(state, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "removed c1" in proc.stdout
    assert "candidates=1 apply=1" in proc.stdout
    assert _removed(state) == ["c1"]


def test_docker_ps_failure_exits_1_with_no_removal(tmp_path: Path) -> None:
    state = _setup_state(tmp_path)
    proc = _run(state, ps_fail=True)
    assert proc.returncode == 1
    assert proc.stderr.strip() != ""
    assert _removed(state) == []
