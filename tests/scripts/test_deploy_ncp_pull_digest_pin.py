"""Digest-pinning contract for the NCP pull deployment script.

The stubs deliberately model a mutable ``:main`` tag: image inspect returns a
new digest after pull while the old container's Config.Image remains ``:main``.
That makes a rollback-to-tag mutant observably unsafe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-ncp-pull.sh"
REPOSITORY = "ghcr.io/mgh3326/auto_trader"
OLD_DIGEST = f"{REPOSITORY}@sha256:{'1' * 64}"
NEW_DIGEST = f"{REPOSITORY}@sha256:{'2' * 64}"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stub_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf 'DOCKER %s\\n' "$*" >>"$DOCKER_LOG"
case "$1" in
  inspect)
    if [[ "$4" == at-api ]]; then printf '%s\\n' "$API_IMAGE"; else printf '%s\\n' "$SCHEDULER_IMAGE"; fi
    ;;
  image)
    printf '%s\\n' "$NEW_DIGEST"
    ;;
  pull|rm)
    ;;
  run)
    container=""
    image=""
    while (($#)); do
      if [[ "$1" == --name ]]; then container="$2"; shift 2; continue; fi
      if [[ "$1" == ghcr.io/* ]]; then image="$1"; fi
      shift
    done
    printf 'RUN %s %s\\n' "$container" "$image" >>"$DOCKER_LOG"
    ;;
  *)
    printf 'unexpected docker invocation: %s\\n' "$*" >&2
    exit 99
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -Eeuo pipefail
count=0
[[ -f "$HEALTH_COUNT" ]] && count="$(<"$HEALTH_COUNT")"
count=$((count + 1))
printf '%s\\n' "$count" >"$HEALTH_COUNT"
if ((count <= HEALTH_FAILS)); then printf '500'; else printf '200'; fi
""",
    )
    _write_executable(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    return bin_dir


def _run(
    tmp_path: Path,
    *,
    api_image: str,
    scheduler_image: str,
    health_fails: int = 0,
    deployed_digest: str | None = OLD_DIGEST,
    previous_digest: str | None = None,
    args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], Path]:
    run_dir = tmp_path / "at-run"
    run_dir.mkdir()
    (run_dir / ".env.runtime").write_text("runtime=unused\n")
    (run_dir / ".env.secrets").write_text("secret=unused\n")
    if deployed_digest is not None:
        (run_dir / "deployed-digest").write_text(f"{deployed_digest}\n")
    if previous_digest is not None:
        (run_dir / "deployed-digest.previous").write_text(f"{previous_digest}\n")
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{_stub_dir(tmp_path)}:{os.environ['PATH']}",
        "AT_RUN_DIRECTORY": str(run_dir),
        "API_IMAGE": api_image,
        "SCHEDULER_IMAGE": scheduler_image,
        "NEW_DIGEST": NEW_DIGEST,
        "DOCKER_LOG": str(log),
        "HEALTH_COUNT": str(tmp_path / "health-count"),
        "HEALTH_FAILS": str(health_fails),
        "AT_HEALTHZ_ATTEMPTS": "1",
        "AT_HEALTHZ_SLEEP_SECONDS": "0",
    }
    return (
        subprocess.run(
            [str(DEPLOY), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        ),
        run_dir,
    )


def _run_lines(tmp_path: Path) -> list[str]:
    return [
        line
        for line in (tmp_path / "docker.log").read_text().splitlines()
        if line.startswith("RUN")
    ]


def test_successful_deploy_runs_both_containers_by_resolved_repo_digest(
    tmp_path: Path,
) -> None:
    proc, run_dir = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    assert _run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
    ]
    assert (run_dir / "deployed-digest").read_text() == f"{NEW_DIGEST}\n"


def test_failed_healthcheck_rolls_back_to_saved_digest_never_mutable_main(
    tmp_path: Path,
) -> None:
    proc, run_dir = _run(
        tmp_path,
        api_image=f"{REPOSITORY}:main",
        scheduler_image=f"{REPOSITORY}:main",
        health_fails=1,
    )

    assert proc.returncode == 1
    # A :main rollback mutant makes this assertion red after the pull replaced
    # the local tag. The old digest must be used for both restored containers.
    assert _run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
    ]
    assert f"RUN at-api {REPOSITORY}:main" not in (tmp_path / "docker.log").read_text()
    assert (run_dir / "deployed-digest").read_text() == f"{OLD_DIGEST}\n"


def test_missing_digest_fallback_is_an_explicit_preflight_failure(
    tmp_path: Path,
) -> None:
    proc, _ = _run(
        tmp_path,
        api_image=f"{REPOSITORY}:main",
        scheduler_image=f"{REPOSITORY}:main",
        deployed_digest=None,
    )

    assert proc.returncode == 78
    assert "API rollback reference is unavailable" in proc.stderr
    assert "DOCKER pull" not in (tmp_path / "docker.log").read_text()
    assert _run_lines(tmp_path) == []


def test_successful_deploy_rotates_current_digest_to_previous_file(
    tmp_path: Path,
) -> None:
    proc, run_dir = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    assert (run_dir / "deployed-digest").read_text() == f"{NEW_DIGEST}\n"
    assert (run_dir / "deployed-digest.previous").read_text() == f"{OLD_DIGEST}\n"


def test_manual_rollback_uses_previous_digest_and_rotates_files(tmp_path: Path) -> None:
    proc, run_dir = _run(
        tmp_path,
        api_image=NEW_DIGEST,
        scheduler_image=NEW_DIGEST,
        deployed_digest=NEW_DIGEST,
        previous_digest=OLD_DIGEST,
        args=("--rollback",),
    )

    assert proc.returncode == 0, proc.stderr
    assert _run_lines(tmp_path) == [
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
    ]
    assert (run_dir / "deployed-digest").read_text() == f"{OLD_DIGEST}\n"
    assert (run_dir / "deployed-digest.previous").read_text() == f"{NEW_DIGEST}\n"
