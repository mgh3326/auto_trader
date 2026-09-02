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
    bin_dir.mkdir(exist_ok=True)
    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf 'DOCKER %s\\n' "$*" >>"$DOCKER_LOG"
case "$1" in
  inspect)
    if [[ "$3" == '{{.State.Running}}' ]]; then printf '%s\\n' true
    elif [[ "$4" == at-haproxy ]]; then exit 1
    elif [[ "$4" == at-api ]]; then printf '%s\\n' "$API_IMAGE"
    elif [[ "$4" == at-scheduler ]]; then printf '%s\\n' "$SCHEDULER_IMAGE"
    elif [[ "$WORKER_IMAGE" == ABSENT ]]; then
      printf 'Error: No such object: at-worker\\n' >&2; exit 1
    else printf '%s\\n' "$WORKER_IMAGE"; fi
    ;;
  image)
    printf '%s\\n' "$NEW_DIGEST"
    ;;
  pull|rm|kill|restart)
    ;;
  logs)
    printf '%s\\n' "$WORKER_LOG"
    ;;
  run)
    container=""
    image=""
    while (($#)); do
      if [[ "$1" == --name ]]; then container="$2"; shift 2; continue; fi
      if [[ "$1" == ghcr.io/* || "$1" == haproxy:* ]]; then image="$1"; fi
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
    worker_image: str | None = None,
    health_fails: int = 0,
    worker_log: str = "Starting 1 worker processes.",
    deployed_digest: str | None = OLD_DIGEST,
    previous_digest: str | None = None,
    args: tuple[str, ...] = (),
    secret_lines: tuple[str, ...] | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    run_dir = tmp_path / "at-run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / ".env.runtime").write_text("runtime=unused\n")
    (run_dir / ".env.secrets").write_text(
        "\n".join(
            secret_lines
            or (
                "MCP_AUTH_TOKEN=test-main-token",
                "MCP_ANALYSIS_READONLY_AUTH_TOKEN=test-analysis-token",
                "MCP_ACCOUNT_READ_AUTH_TOKEN=test-account-token",
                "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN=test-execution-token",
                "MCP_PAPER_001_AUTH_TOKEN=test-paper-token",
                "MCP_KIWOOM_AUTH_TOKEN=test-kiwoom-token",
                "",
            )
        )
    )
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
        "WORKER_IMAGE": worker_image or scheduler_image,
        "WORKER_LOG": worker_log,
        "NEW_DIGEST": NEW_DIGEST,
        "DOCKER_LOG": str(log),
        "HEALTH_COUNT": str(tmp_path / "health-count"),
        "HEALTH_FAILS": str(health_fails),
        "AT_HEALTHZ_ATTEMPTS": "1",
        "AT_HEALTHZ_SLEEP_SECONDS": "0",
        **(extra_env or {}),
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


def _core_run_lines(tmp_path: Path) -> list[str]:
    return [
        line
        for line in _run_lines(tmp_path)
        if line.split()[1] in {"at-api", "at-scheduler", "at-worker"}
    ]


def test_successful_deploy_runs_all_units_by_resolved_repo_digest(
    tmp_path: Path,
) -> None:
    proc, run_dir = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    assert _core_run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-worker {NEW_DIGEST}",
    ]
    docker_log = (tmp_path / "docker.log").read_text()
    assert (
        "DOCKER run -d --name at-worker --restart unless-stopped --network host"
        in docker_log
    )
    assert (
        "/app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1"
        in docker_log
    )
    assert (run_dir / "deployed-digest").read_text() == f"{NEW_DIGEST}\n"
    assert _run_lines(tmp_path)[3:] == [
        f"RUN at-mcp-blue {NEW_DIGEST}",
        f"RUN at-mcp-analysis-readonly {NEW_DIGEST}",
        f"RUN at-mcp-account-read {NEW_DIGEST}",
        f"RUN at-mcp-tradingcodex-execution {NEW_DIGEST}",
        f"RUN at-mcp-paper-001 {NEW_DIGEST}",
        f"RUN at-mcp-kiwoom {NEW_DIGEST}",
        "RUN at-haproxy haproxy:3.1-alpine",
    ]


def test_failed_healthcheck_rolls_back_all_units_to_saved_digest_never_mutable_main(
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
    assert _core_run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-worker {NEW_DIGEST}",
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
        f"RUN at-worker {OLD_DIGEST}",
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
    assert _core_run_lines(tmp_path) == []


def test_missing_required_mcp_token_fails_closed_before_pull(tmp_path: Path) -> None:
    proc, _ = _run(
        tmp_path,
        api_image=OLD_DIGEST,
        scheduler_image=OLD_DIGEST,
        secret_lines=(
            "MCP_AUTH_TOKEN=test-main-token",
            "MCP_ANALYSIS_READONLY_AUTH_TOKEN=test-analysis-token",
            "MCP_ACCOUNT_READ_AUTH_TOKEN=test-account-token",
            "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN=test-execution-token",
            "MCP_PAPER_001_AUTH_TOKEN=test-paper-token",
            "",
        ),
    )

    assert proc.returncode == 78
    assert "MCP_KIWOOM_AUTH_TOKEN" in proc.stderr
    assert "DOCKER pull" not in (tmp_path / "docker.log").read_text()


def test_second_deploy_starts_only_inactive_color_and_switches_haproxy(
    tmp_path: Path,
) -> None:
    first, run_dir = _run(
        tmp_path,
        api_image=OLD_DIGEST,
        scheduler_image=OLD_DIGEST,
    )
    second, _ = _run(
        tmp_path,
        api_image=NEW_DIGEST,
        scheduler_image=NEW_DIGEST,
        extra_env={"MCP_DRAIN_SECONDS": "1"},
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert any(line.startswith("RUN at-mcp-green ") for line in _run_lines(tmp_path))
    assert not any(line.startswith("RUN at-mcp-blue ") for line in _run_lines(tmp_path))
    assert (run_dir / "mcp-active-color").read_text() == "green\n"
    rendered = (run_dir / "haproxy.cfg").read_text()
    assert "bind 127.0.0.1:8765" in rendered
    assert "bind 100.122.100.56:8765" in rendered
    assert "127.0.0.1:8767" in rendered
    assert "0.0.0.0" not in rendered


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
    assert _core_run_lines(tmp_path) == [
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
        f"RUN at-worker {OLD_DIGEST}",
    ]
    assert (run_dir / "deployed-digest").read_text() == f"{OLD_DIGEST}\n"
    assert (run_dir / "deployed-digest.previous").read_text() == f"{NEW_DIGEST}\n"


def test_worker_readiness_requires_running_container_and_taskiq_startup_log(
    tmp_path: Path,
) -> None:
    proc, _ = _run(
        tmp_path,
        api_image=OLD_DIGEST,
        scheduler_image=OLD_DIGEST,
        worker_log="worker process exists but TaskIQ did not start",
    )

    assert proc.returncode == 1
    assert "waiting for TaskIQ worker startup" in proc.stderr
    # If the worker rollback call is removed, this safety regression test is
    # red: all three units must return to their pinned prior digest.
    assert _core_run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-worker {NEW_DIGEST}",
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
        f"RUN at-worker {OLD_DIGEST}",
    ]


def test_first_deploy_without_worker_container_bootstraps_rollback_from_api(
    tmp_path: Path,
) -> None:
    """The at-worker unit is new: hosts deployed before it have no container.

    The first promotion must still run and must not stop at the rollback
    preflight (exit 78) just because at-worker does not exist yet.
    """
    proc, run_dir = _run(
        tmp_path,
        api_image=OLD_DIGEST,
        scheduler_image=OLD_DIGEST,
        worker_image="ABSENT",
    )

    assert proc.returncode == 0, proc.stderr
    assert "bootstrapping its rollback reference from at-api" in proc.stderr
    assert _core_run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-worker {NEW_DIGEST}",
    ]
    assert (run_dir / "deployed-digest").read_text() == f"{NEW_DIGEST}\n"


def test_first_deploy_without_worker_container_rolls_back_worker_to_api_digest(
    tmp_path: Path,
) -> None:
    proc, run_dir = _run(
        tmp_path,
        api_image=OLD_DIGEST,
        scheduler_image=OLD_DIGEST,
        worker_image="ABSENT",
        health_fails=1,
    )

    assert proc.returncode == 1
    # The worker had no prior container, so its rollback image is the API's
    # pinned digest rather than the mutable :main tag or a refusal.
    assert _run_lines(tmp_path) == [
        f"RUN at-api {NEW_DIGEST}",
        f"RUN at-scheduler {NEW_DIGEST}",
        f"RUN at-worker {NEW_DIGEST}",
        f"RUN at-api {OLD_DIGEST}",
        f"RUN at-scheduler {OLD_DIGEST}",
        f"RUN at-worker {OLD_DIGEST}",
    ]
    assert (run_dir / "deployed-digest").read_text() == f"{OLD_DIGEST}\n"


def test_tradingcodex_mcp_unit_pins_required_approval_hash_modes_only_for_itself(
    tmp_path: Path,
) -> None:
    """Mirror of the Mac launchd plist: the TradingCodex execution profile refuses
    to start unless both approval-hash modes are ``required``. The pin must be
    scoped to that one unit so api/scheduler/worker keep their env-file policy.
    """
    proc, _ = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    docker_runs = [
        line
        for line in (tmp_path / "docker.log").read_text().splitlines()
        if line.startswith("DOCKER run ")
    ]
    tradingcodex = [
        line for line in docker_runs if "--name at-mcp-tradingcodex-execution" in line
    ]
    assert len(tradingcodex) == 1
    assert "-e ORDER_APPROVAL_HASH_MODE=required" in tradingcodex[0]
    assert "-e TOSS_APPROVAL_HASH_MODE=required" in tradingcodex[0]
    others = [
        line
        for line in docker_runs
        if "--name at-mcp-tradingcodex-execution" not in line
    ]
    assert others, "expected the other units to be started too"
    assert all("APPROVAL_HASH_MODE" not in line for line in others)


def test_rendered_haproxy_config_is_readable_by_the_unprivileged_container_user(
    tmp_path: Path,
) -> None:
    """haproxy:3.x drops to user ``haproxy``; a 0600 config crash-loops it."""
    proc, run_dir = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    mode = (run_dir / "haproxy.cfg").stat().st_mode & 0o777
    assert mode & 0o044 == 0o044, f"haproxy.cfg mode {oct(mode)} is not world-readable"


def test_rendered_haproxy_exposes_fixed_profiles_on_tailnet_only(
    tmp_path: Path,
) -> None:
    """Fixed-profile containers bind loopback; the tailnet reaches them via haproxy."""
    proc, run_dir = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    cfg = (run_dir / "haproxy.cfg").read_text()
    for port in (8768, 8769, 8770, 8771, 8772):
        assert f"bind 100.122.100.56:{port}" in cfg
        assert f"127.0.0.1:{port} check" in cfg
    assert "0.0.0.0" not in cfg
    assert (
        "bind 127.0.0.1:8768" not in cfg
    )  # loopback is served by the container itself


def test_haproxy_config_render_keeps_the_bind_mounted_inode(tmp_path: Path) -> None:
    """A file bind mount follows the inode: swapping it makes reloads a no-op."""
    run_dir = tmp_path / "at-run"
    run_dir.mkdir()
    cfg = run_dir / "haproxy.cfg"
    cfg.write_text("stale\n")
    before = cfg.stat().st_ino

    proc, _ = _run(tmp_path, api_image=OLD_DIGEST, scheduler_image=OLD_DIGEST)

    assert proc.returncode == 0, proc.stderr
    assert cfg.stat().st_ino == before
    assert "stale" not in cfg.read_text()
