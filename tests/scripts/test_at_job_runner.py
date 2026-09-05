from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

RUNNER = Path("ops/ncp/bin/at-job.sh")
GOLDEN = Path("tests/fixtures/ncp_job_timers_prefect_argv.json")


def _runner_copy(
    tmp_path: Path, digest_file: Path, *, bypass_runtime_file_check: bool = False
) -> Path:
    runner = tmp_path / "at-job.sh"
    source = RUNNER.read_text().replace(
        'readonly DEPLOYED_DIGEST_FILE="/root/at-run/deployed-digest"',
        f'readonly DEPLOYED_DIGEST_FILE="{digest_file}"',
    )
    if bypass_runtime_file_check:
        source = source.replace(
            '[[ -n "${AT_RUNTIME_ENV_FILE:-}" && -f "$AT_RUNTIME_ENV_FILE" ]] || {',
            "true || {",
        )
    runner.write_text(source)
    runner.chmod(0o755)
    return runner


def _run(
    tmp_path: Path,
    docker_body: str,
    *,
    module_args: list[str] | None = None,
    runtime_env: bool = True,
    runtime_env_value: str | None = None,
    runtime_env_contents: str = "SAFE_TEST_VALUE=1\n",
    extra_env: dict[str, str] | None = None,
    bypass_runtime_file_check: bool = False,
    deployed_digest: str
    | None = "ghcr.io/mgh3326/auto_trader@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    flock_body: str = "exit 0",
) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "docker").write_text("#!/usr/bin/env bash\n" + docker_body)
    (fakebin / "flock").write_text("#!/usr/bin/env bash\n" + flock_body)
    (fakebin / "timeout").write_text('#!/usr/bin/env bash\nshift 2\n"$@"')
    for command in fakebin.iterdir():
        command.chmod(0o755)

    digest_file = tmp_path / "deployed-digest"
    if deployed_digest is not None:
        digest_file.write_text(deployed_digest)
    env_file = tmp_path / "runtime.env"
    if runtime_env:
        env_file.write_text(runtime_env_contents)
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "AT_JOB_LOCK_DIRECTORY": str(tmp_path / "locks"),
        "AT_JOB_DOCKER_CAPTURE": str(tmp_path / "docker.argv"),
    }
    if runtime_env:
        env["AT_RUNTIME_ENV_FILE"] = runtime_env_value or str(env_file)
    env.update(extra_env or {})
    return subprocess.run(
        [
            "/bin/bash",
            str(
                _runner_copy(
                    tmp_path,
                    digest_file,
                    bypass_runtime_file_check=bypass_runtime_file_check,
                )
            ),
            *(module_args or ["scripts.sync_toss_warnings"]),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _golden_jobs() -> list[dict[str, object]]:
    return json.loads(GOLDEN.read_text())["jobs"]


def _captured_argv(capture: Path) -> list[str]:
    return [token.decode() for token in capture.read_bytes().split(b"\0")[:-1]]


def _module_args(argv: list[object]) -> list[str]:
    return [str(token) for token in argv[argv.index("-m") + 1 :]]


def test_digest_failure_does_not_fall_back_to_tag(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "exit 99", deployed_digest="ghcr.io/mgh3326/auto_trader:main"
    )
    assert result.returncode == 78
    assert ":main" not in result.stdout


def test_deployed_digest_allows_job_without_at_api_inspect(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        '[[ "$1" == run ]] || exit 97',
        module_args=["scripts.sync_toss_warnings"],
    )
    assert result.returncode == 0


def test_missing_deployed_digest_is_refused(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "exit 99",
        deployed_digest=None,
        extra_env={"AT_JOB_COMMIT_ENV": "KEY", "KEY": "true"},
    )
    assert result.returncode == 78
    assert '"image_digest":"unresolved"' in result.stdout
    assert '"commit"' not in result.stdout
    assert '"commit_env"' not in result.stdout


def test_missing_runtime_env_file_is_refused(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "exit 99",
        runtime_env=False,
        extra_env={"AT_JOB_COMMIT_ENV": "KEY", "KEY": "true"},
    )
    assert result.returncode == 78
    assert "AT_RUNTIME_ENV_FILE" in result.stderr
    assert '"commit"' not in result.stdout
    assert '"commit_env"' not in result.stdout


def test_flock_duplicate_is_refused(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "exit 99",
        flock_body="exit 1",
        extra_env={"AT_JOB_COMMIT_ENV": "KEY", "KEY": "true"},
    )
    assert result.returncode == 75
    assert '"rc":75' in result.stdout
    assert '"commit"' not in result.stdout
    assert '"commit_env"' not in result.stdout


@pytest.mark.parametrize(
    "job",
    [job for job in _golden_jobs() if "argv" in job],
    ids=lambda job: str(job["unit"]),
)
def test_wrapper_argv_matches_prefect_golden(
    tmp_path: Path, job: dict[str, object]
) -> None:
    argv = job["argv"]
    assert isinstance(argv, list)
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=_module_args(argv),
        runtime_env_value="/root/at-secrets/.env.api",
        bypass_runtime_file_check=True,
    )
    assert result.returncode == 0, result.stderr
    assert ["docker", *_captured_argv(capture)] == argv


@pytest.mark.parametrize(
    "job",
    [job for job in _golden_jobs() if "argv_commit_off" in job],
    ids=lambda job: str(job["unit"]),
)
@pytest.mark.parametrize("enabled", [False, True])
def test_commit_gate_argv_matches_prefect_golden(
    tmp_path: Path, job: dict[str, object], enabled: bool
) -> None:
    commit_env = job["commit_env"]
    assert isinstance(commit_env, str)
    golden_key = "argv_commit_on" if enabled else "argv_commit_off"
    argv = job[golden_key]
    assert isinstance(argv, list)
    service_argv = job["argv_commit_off"]
    assert isinstance(service_argv, list)
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=_module_args(service_argv),
        runtime_env_value="/root/at-secrets/.env.api",
        bypass_runtime_file_check=True,
        extra_env={
            commit_env: "true" if enabled else "false",
            "AT_JOB_COMMIT_ENV": commit_env,
        },
    )
    assert result.returncode == 0, result.stderr
    assert ["docker", *_captured_argv(capture)] == argv
    assert f'"commit":{str(enabled).lower()}' in result.stdout
    assert f'"commit_env":"{commit_env}"' in result.stdout


def test_commit_gate_uses_runtime_env_file_without_sourcing(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=["scripts.build_invest_crypto_screener_snapshots", "--all"],
        runtime_env_contents=(
            f'UNTRUSTED=$(touch "{marker}")\n'
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=on\n"
        ),
        bypass_runtime_file_check=True,
        extra_env={"AT_JOB_COMMIT_ENV": "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED"},
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert _captured_argv(capture)[-1] == "--commit"


def test_process_env_empty_value_overrides_runtime_env_file(tmp_path: Path) -> None:
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=["scripts.build_invest_crypto_screener_snapshots", "--all"],
        runtime_env_contents="INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=on\n",
        bypass_runtime_file_check=True,
        extra_env={
            "AT_JOB_COMMIT_ENV": "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED",
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED": "",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _captured_argv(capture)[-1] == "--all"
    assert '"commit":false' in result.stdout


# Prefect's _env_file_commit_gate_enabled() is the semantic source: a present
# process value (including empty) wins; otherwise first non-comment KEY= line
# wins after whitespace trim, all outer double quotes, then all outer singles.
@pytest.mark.parametrize(
    ("case", "process_value", "runtime_env_contents", "expected_commit"),
    [
        ("process-empty", "", "KEY=true\n", False),
        ("process-TRUE", "TRUE", "KEY=no\n", True),
        ("process-On", "On", "KEY=no\n", True),
        ("process-yes", "yes", "KEY=no\n", True),
        ("process-zero", "0", "KEY=true\n", False),
        ("process-no", "no", "KEY=true\n", False),
        ("file-double-quoted", None, 'KEY="true"\n', True),
        ("file-whitespace", None, "  KEY=  true  \n", True),
        ("file-single-quoted", None, "KEY='true'\n", True),
        ("file-comment", None, "#KEY=true\n", False),
        ("file-first-key-wins", None, "KEY=no\nKEY=true\n", False),
        ("file-repeated-double-quotes", None, 'KEY=""true""\n', True),
        ("file-repeated-single-quotes", None, "KEY=''true''\n", True),
    ],
)
def test_commit_gate_prefect_parity_matrix(
    tmp_path: Path,
    case: str,
    process_value: str | None,
    runtime_env_contents: str,
    expected_commit: bool,
) -> None:
    capture = tmp_path / "docker.argv"
    extra_env = {"AT_JOB_COMMIT_ENV": "KEY"}
    if process_value is not None:
        extra_env["KEY"] = process_value
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=["scripts.build_invest_crypto_screener_snapshots", "--all"],
        runtime_env_contents=runtime_env_contents,
        extra_env=extra_env,
    )
    assert result.returncode == 0, case
    assert (_captured_argv(capture)[-1] == "--commit") is expected_commit, case
    assert f'"commit":{str(expected_commit).lower()}' in result.stdout


def test_multi_step_runs_all_steps_and_propagates_first_failure(tmp_path: Path) -> None:
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" >> "$AT_JOB_DOCKER_CAPTURE"; '
        'printf "\\n" >> "$AT_JOB_DOCKER_CAPTURE"; '
        'case "$*" in *"--market kr"*) exit 17 ;; *) exit 0 ;; esac',
        module_args=[
            "scripts.sync_toss_symbol_master",
            "--market",
            "kr",
            "--all",
            "--commit",
            "--at-job-step",
            "scripts.sync_toss_symbol_master",
            "--market",
            "us",
            "--all",
            "--commit",
        ],
        bypass_runtime_file_check=True,
        extra_env={"AT_JOB_STEPS": "2"},
    )
    assert result.returncode == 17
    calls = [line for line in capture.read_bytes().split(b"\n") if line]
    assert len(calls) == 2
    assert b"--market\0kr\0" in calls[0]
    assert b"--market\0us\0" in calls[1]
    assert '"step":1,"steps_total":2' in result.stdout
    assert '"step":2,"steps_total":2' in result.stdout
    assert '{"steps_total":2,"steps_failed":[1]}' in result.stdout
