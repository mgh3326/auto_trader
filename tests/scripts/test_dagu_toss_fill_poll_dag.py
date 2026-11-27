"""hk 6277 / hk 6635: static and behaviour checks of the Toss fill poll Dagu DAG copy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

DAG = (
    Path(__file__).resolve().parents[2]
    / "deploy/dagu/at-reconcile/at-toss-fill-poll.yaml"
)
NAME = "at-toss-fill-poll"

FAKE_DOCKER = """#!/bin/bash
# state dir: one file per container name holding its State.Status (running|created|exited)
set -u
state="${FAKE_DOCKER_STATE:?}"
case "${1:?}" in
  inspect)
    name="${*: -1}"
    f="$state/$name"
    if [ ! -f "$f" ]; then echo "Error: No such object: $name" >&2; exit 1; fi
    status="$(cat "$f")"
    case "$*" in
      *State.Running*) if [ "$status" = running ]; then echo true; else echo false; fi ;;
      *State.Status*) echo "$status" ;;
      *) echo "unexpected inspect format: $*" >&2; exit 2 ;;
    esac
    ;;
  rm)
    name="${*: -1}"
    echo "$name" >> "$state/removed"
    f="$state/$name"
    if [ ! -f "$f" ]; then echo "No such container: $name" >&2; exit 1; fi
    if [ "$(cat "$f")" = running ]; then echo "cannot remove a running container" >&2; exit 1; fi
    rm -f "$f"
    ;;
  *) echo "unexpected docker call: $*" >&2; exit 2 ;;
esac
"""

FAKE_LOGGER = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_STATE/log"
"""


def _script() -> str:
    doc = yaml.safe_load(DAG.read_text())
    (step,) = doc["steps"]
    return step["script"]


def _guard_block() -> str:
    """The shell lines before the docker run line (the overlap guard)."""
    lines = _script().splitlines()
    run = next(i for i, line in enumerate(lines) if "docker run" in line)
    return "\n".join(lines[:run])


def test_dag_commits_with_job_scoped_flag_and_never_overlaps() -> None:
    doc = yaml.safe_load(DAG.read_text())
    assert doc["schedule"] == [
        "CRON_TZ=Asia/Seoul */5 9-23 * * 1",
        "CRON_TZ=Asia/Seoul */5 * * * 2-5",
        "CRON_TZ=Asia/Seoul */5 0-10 * * 6",
    ]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    (step,) = doc["steps"]
    script = step["script"]
    assert "-e TOSS_FILL_POLL_ENABLED=true" in script
    assert script.count("docker run --rm --init") == 1
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    assert "-m scripts.toss_fill_reconcile_poller --commit || rc=$?" in script
    assert "TOSS_FILL_POLL_MARKET_GATE_ENABLED" not in script
    # Overlap is prevented by the exact container name, and only a RUNNING
    # container blocks a pass. A same-name container that is not running
    # (Created/Exited, never started) is logged with its state and removed,
    # so it cannot block the poller for hours (hk 6635). docker ps --filter
    # name= is a substring match and must not be used for this check.
    guard = _guard_block()
    assert "docker ps" not in guard
    assert "--filter name=" not in guard
    assert f"docker inspect --format '{{{{.State.Running}}}}' {NAME}" in guard
    assert "status=previous_pass_still_running" in guard
    assert "status=stale_container_removed" in guard
    assert f"docker rm {NAME}" in guard
    assert "status=stale_container_remove_failed" in guard
    # A booking pass is never signalled (per-row booking commits in separate
    # steps): no in-container timeout, and Dagu's timeout is not proxied.
    lines = script.splitlines()
    run = next(i for i, line in enumerate(lines) if "docker run" in line)
    assert "--sig-proxy=false --name at-toss-fill-poll " in script
    assert (
        '"$(cat /root/at-run/deployed-digest)" /app/.venv/bin/python -m '
        "scripts.toss_fill_reconcile_poller --commit" in script
    )
    assert "timeout " not in lines[run]


@pytest.mark.parametrize(
    ("container", "expect_block", "expect_removed"),
    [
        ("running", True, False),  # a running pass blocks; nothing removed
        ("created", False, True),  # never started: removed, run proceeds
        ("exited", False, True),  # finished container: removed, run proceeds
        (None, False, False),  # no container at all: nothing to do
    ],
)
def test_guard_blocks_only_on_running_container(
    tmp_path: Path,
    container: str | None,
    expect_block: bool,
    expect_removed: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("logger", FAKE_LOGGER)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    if container is not None:
        (state / NAME).write_text(container)
    # A different container whose name merely contains the job name must be
    # ignored by the exact-name check.
    (state / f"{NAME}-old").write_text("running")

    script = _guard_block() + "\necho PROCEED\n"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
    }
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )

    proceeded = "PROCEED" in proc.stdout
    assert proceeded is not expect_block
    assert (proc.returncode == 1) is expect_block
    removed = state / "removed"
    if expect_removed:
        assert removed.read_text().splitlines() == [NAME]
        assert not (state / NAME).exists()
        log = (state / "log").read_text()
        assert f"status=stale_container_removed state={container}" in log
    else:
        assert not removed.exists()
    if expect_block:
        assert (state / NAME).read_text() == "running"
        assert "status=previous_pass_still_running" in (state / "log").read_text()
    assert (state / f"{NAME}-old").read_text() == "running"
