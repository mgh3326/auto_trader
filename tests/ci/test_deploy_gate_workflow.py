"""ROB-1456: deploy.yml must gate image builds on the exact SHA's Test run.

Shell steps are extracted from the workflow YAML and executed with a fake
``gh`` on PATH (no network). A mini step runner stops at the first failing
step like GitHub Actions, so we can assert nothing downstream (QEMU, Buildx,
registry login, build) is reached on refusal.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
SHA = "a" * 40
OTHER_SHA = "b" * 40
REQUIRED_JOBS = [
    "lint",
    "taskiq-smoke",
    "test (3.13, 1)",
    "test (3.13, 2)",
    "test (3.13, 3)",
    "test (3.13, 4)",
]
GATE_STEP = "Verify CI gate for exact SHA"
RESOLVE_STEP = "Resolve build SHA"
MUTATING_USES = (
    "docker/setup-qemu-action",
    "docker/setup-buildx-action",
    "docker/login-action",
    "docker/build-push-action",
)


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _load()["jobs"]["build-and-push"]["steps"]


# ------------------------------------------------------------ shell behavior


def _fake_gh(
    bin_dir: Path, *, jobs: list[tuple[str, str, str]], runs: list[dict], main_sha: str
) -> Path:
    log = bin_dir / "gh.log"
    runs_json = json.dumps([{"workflow_runs": runs}])
    jobs_json = json.dumps(
        [
            {
                "jobs": [
                    {"name": n, "status": st, "conclusion": c, "id": i}
                    for i, (n, st, c) in enumerate(jobs, 1)
                ]
            }
        ]
    )
    script = bin_dir / "gh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$@" >> "{log}"
            case "$*" in
              *"/commits/main"*) printf '%s\\n' '{main_sha}' ;;
              *"/jobs"*) cat <<'JSON'
            {jobs_json}
            JSON
              ;;
              *"/runs?"*) cat <<'JSON'
            {runs_json}
            JSON
              ;;
              *) echo "unexpected gh call: $*" >&2; exit 99 ;;
            esac
            """
        )
    )
    script.chmod(0o755)
    return log


def _green_jobs() -> list[tuple[str, str, str]]:
    return [(n, "completed", "success") for n in REQUIRED_JOBS]


def _run(sha: str = SHA, event: str = "push") -> dict:
    return {
        "id": 100,
        "run_attempt": 1,
        "event": event,
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-11-02T00:00:00Z",
        "updated_at": "2026-11-02T00:10:00Z",
        "name": "Test",
        "path": ".github/workflows/test.yml",
    }


def _execute(
    tmp_path: Path,
    *,
    event_name: str,
    jobs: list[tuple[str, str, str]],
    runs: list[dict],
    workflow_run_sha: str = SHA,
    event_sha: str = OTHER_SHA,
    allow_unverified: str = "",
    main_sha: str = SHA,
) -> tuple[list[str], int | None, str, Path]:
    """Run resolve+gate steps (and record any later step reached)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _fake_gh(bin_dir, jobs=jobs, runs=runs, main_sha=main_sha)
    out = tmp_path / "github_output"
    out.write_text("")
    base_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_REPOSITORY": "mgh3326/auto_trader",
        "GH_TOKEN": "fake",
    }
    reached: list[str] = []
    stderr = ""
    outputs: dict[str, str] = {}
    for step in _steps():
        name = step.get("name", step.get("uses", ""))
        reached.append(name)
        if name == RESOLVE_STEP:
            step_env = {
                "EVENT_NAME": event_name,
                "WORKFLOW_RUN_SHA": workflow_run_sha
                if event_name == "workflow_run"
                else "",
                "EVENT_SHA": event_sha,
            }
        elif name == GATE_STEP:
            step_env = {
                "BUILD_SHA": outputs["sha"],
                "ALLOW_UNVERIFIED": allow_unverified,
            }
        elif step.get("id") == "moving":
            step_env = {"BUILD_SHA": outputs["sha"], "EVENT_NAME": event_name}
        else:
            # Action uses (including build mutations) are recorded, never run.
            continue
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            env={**base_env, **step_env},
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        stderr += result.stderr + result.stdout
        if result.returncode:
            return reached, result.returncode, stderr, log
        if name == RESOLVE_STEP:
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    return reached, None, stderr, log


def _mutations_reached(reached: list[str]) -> bool:
    mutating_names = {
        s["name"] for s in _steps() if str(s.get("uses", "")).startswith(MUTATING_USES)
    }
    return any(n in mutating_names for n in reached)


@pytest.mark.parametrize(("main_sha", "publish"), [(SHA, "true"), (OTHER_SHA, "false")])
def test_automatic_historical_run_does_not_authorize_moving_tags(
    tmp_path: Path, main_sha: str, publish: str
) -> None:
    _, rc, _, _ = _execute(
        tmp_path,
        event_name="workflow_run",
        jobs=_green_jobs(),
        runs=[_run(SHA)],
        main_sha=main_sha,
    )
    assert rc is None
    outputs = dict(
        line.split("=", 1)
        for line in (tmp_path / "github_output").read_text().splitlines()
    )
    assert outputs["publish"] == publish


def test_workflow_run_gates_event_sha_not_default_branch_tip(tmp_path: Path) -> None:
    reached, rc, _, log = _execute(
        tmp_path,
        event_name="workflow_run",
        jobs=_green_jobs(),
        runs=[_run(SHA)],
        workflow_run_sha=SHA,
        event_sha=OTHER_SHA,
    )
    assert rc is None
    assert _mutations_reached(reached)
    calls = log.read_text()
    assert f"head_sha={SHA}" in calls
    assert OTHER_SHA not in calls


@pytest.mark.parametrize("event_name", ["workflow_dispatch", "release"])
def test_manual_and_release_use_event_sha_and_gate(
    tmp_path: Path, event_name: str
) -> None:
    reached, rc, _, log = _execute(
        tmp_path,
        event_name=event_name,
        jobs=_green_jobs(),
        runs=[_run(OTHER_SHA)],
        event_sha=OTHER_SHA,
    )
    assert rc is None
    assert f"head_sha={OTHER_SHA}" in log.read_text()


@pytest.mark.parametrize("event_name", ["workflow_run", "workflow_dispatch", "release"])
def test_missing_check_refuses_before_build(tmp_path: Path, event_name: str) -> None:
    reached, rc, _, _ = _execute(
        tmp_path,
        event_name=event_name,
        jobs=_green_jobs()[:-1],
        runs=[_run(SHA)],
        workflow_run_sha=SHA,
        event_sha=SHA,
    )
    assert rc == 78
    assert not _mutations_reached(reached)


@pytest.mark.parametrize("bad", range(len(REQUIRED_JOBS)))
def test_red_check_refuses_before_build(tmp_path: Path, bad: int) -> None:
    jobs = _green_jobs()
    jobs[bad] = (REQUIRED_JOBS[bad], "completed", "failure")
    reached, rc, _, _ = _execute(
        tmp_path,
        event_name="workflow_run",
        jobs=jobs,
        runs=[_run(SHA)],
    )
    assert rc == 78
    assert not _mutations_reached(reached)


def test_no_test_run_for_sha_refuses(tmp_path: Path) -> None:
    reached, rc, _, _ = _execute(
        tmp_path, event_name="workflow_dispatch", jobs=[], runs=[], event_sha=SHA
    )
    assert rc == 78
    assert not _mutations_reached(reached)


def test_override_is_logged_and_lets_red_sha_through(tmp_path: Path) -> None:
    reached, rc, stderr, _ = _execute(
        tmp_path,
        event_name="workflow_dispatch",
        jobs=[],
        runs=[],
        event_sha=SHA,
        allow_unverified="incident ROB-1456 hotfix",
    )
    assert rc is None
    assert _mutations_reached(reached)
    assert "incident ROB-1456 hotfix" in stderr


def test_override_reason_is_not_shell_interpreted(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    reason = f'$(touch {marker}); `touch {marker}` "'
    _execute(
        tmp_path,
        event_name="workflow_dispatch",
        jobs=[],
        runs=[],
        event_sha=SHA,
        allow_unverified=reason,
    )
    assert not marker.exists()


@pytest.mark.parametrize("bad_sha", ["", "main", "A" * 40, "a" * 39])
def test_resolve_rejects_non_full_sha(tmp_path: Path, bad_sha: str) -> None:
    reached, rc, _, _ = _execute(
        tmp_path,
        event_name="workflow_run",
        jobs=_green_jobs(),
        runs=[_run(SHA)],
        workflow_run_sha=bad_sha,
    )
    assert rc == 78
    assert reached == [RESOLVE_STEP]
