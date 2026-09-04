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
        env_file.write_text("SAFE_TEST_VALUE=1\n")
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "AT_JOB_LOCK_DIRECTORY": str(tmp_path / "locks"),
        "AT_JOB_DOCKER_CAPTURE": str(tmp_path / "docker.argv"),
    }
    if runtime_env:
        env["AT_RUNTIME_ENV_FILE"] = runtime_env_value or str(env_file)
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
    result = _run(tmp_path, "exit 99", deployed_digest=None)
    assert result.returncode == 78
    assert '"image_digest":"unresolved"' in result.stdout


def test_missing_runtime_env_file_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 99", runtime_env=False)
    assert result.returncode == 78
    assert "AT_RUNTIME_ENV_FILE" in result.stderr


def test_flock_duplicate_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 99", flock_body="exit 1")
    assert result.returncode == 75
    assert '"rc":75' in result.stdout


@pytest.mark.parametrize("job", _golden_jobs(), ids=lambda job: str(job["unit"]))
def test_wrapper_argv_matches_prefect_golden(
    tmp_path: Path, job: dict[str, object]
) -> None:
    argv = job["argv"]
    assert isinstance(argv, list)
    module_index = argv.index("-m") + 1
    capture = tmp_path / "docker.argv"
    result = _run(
        tmp_path,
        'printf "%s\\0" "$@" > "$AT_JOB_DOCKER_CAPTURE"',
        module_args=[str(token) for token in argv[module_index:]],
        runtime_env_value="/root/at-secrets/.env.api",
        bypass_runtime_file_check=True,
    )
    assert result.returncode == 0, result.stderr
    captured = capture.read_bytes().split(b"\0")[:-1]
    assert ["docker", *(token.decode() for token in captured)] == argv
