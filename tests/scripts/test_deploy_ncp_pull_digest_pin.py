"""NCP blue/green deploy ordering, using only fake Docker and curl."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = Path(os.environ.get("DEPLOY_UNDER_TEST", REPO / "scripts/deploy-ncp-pull.sh"))
OLD = "ghcr.io/mgh3326/auto_trader@sha256:" + "1" * 64
NEW = "ghcr.io/mgh3326/auto_trader@sha256:" + "2" * 64


def run(
    tmp_path: Path,
    *,
    api_color: str | None = None,
    fail_candidate: bool = False,
    fail_mcp_port: int | None = None,
    fail_once_container: str | None = None,
    fail_proxy: bool = False,
    existing_config: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    (bindir / "docker").write_text(f"""#!/usr/bin/env bash
echo "docker $*" >> {log}
if [[ "$1" == run && "$*" == *"--name ${{FAIL_ONCE_CONTAINER:-none}}"* && ! -e "$FAIL_ONCE_MARKER" ]]; then touch "$FAIL_ONCE_MARKER"; exit 1; fi
case "$1" in
 inspect) [[ "$2" == --format ]] && {{ [[ "$3" == *Config.Image* ]] && echo {OLD} || echo id; }} ;;
 image) echo {NEW} ;;
 logs) echo 'Listening started connected=True' ;;
 *) : ;;
esac
""")
    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'url="${!#}"\n'
        'if [[ "${FAIL_CANDIDATE:-}" == true && "$url" == *":8001/healthz" ]]; then echo 500; exit 0; fi\n'
        'if [[ -n "${FAIL_MCP_PORT:-}" && "$url" == *":${FAIL_MCP_PORT}/health" ]]; then echo 500; exit 0; fi\n'
        'if [[ "${FAIL_PROXY:-}" == true && "$url" == *":8000/healthz" ]]; then echo 500; exit 22; fi\n'
        "echo 200\n"
    )
    (bindir / "sleep").write_text("#!/usr/bin/env bash\n:")
    for path in bindir.iterdir():
        path.chmod(0o755)
    run_dir = tmp_path / "at-run"
    run_dir.mkdir()
    (run_dir / ".env.runtime").write_text("x=y\n")
    (run_dir / ".env.secrets").write_text(
        "\n".join(
            [
                "MCP_AUTH_TOKEN=x",
                "MCP_ANALYSIS_READONLY_AUTH_TOKEN=x",
                "MCP_ACCOUNT_READ_AUTH_TOKEN=x",
                "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN=x",
                "MCP_PAPER_001_AUTH_TOKEN=x",
                "MCP_KIWOOM_AUTH_TOKEN=x",
            ]
        )
    )
    (run_dir / "deployed-digest").write_text(OLD + "\n")
    if api_color:
        (run_dir / "api-active-color").write_text(api_color + "\n")
    if existing_config is not None:
        (run_dir / "haproxy.cfg").write_text(existing_config)
    p = subprocess.run(
        [str(DEPLOY)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AT_RUN_DIRECTORY": str(run_dir),
            "MCP_HAPROXY_TEMPLATE": str(REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"),
            "AT_HEALTHZ_ATTEMPTS": "1",
            "AT_HEALTHZ_SLEEP_SECONDS": "0",
            "FAIL_CANDIDATE": str(fail_candidate).lower(),
            "FAIL_MCP_PORT": str(fail_mcp_port or ""),
            "FAIL_ONCE_CONTAINER": fail_once_container or "none",
            "FAIL_ONCE_MARKER": str(tmp_path / "fail-once"),
            "FAIL_PROXY": str(fail_proxy).lower(),
            "MCP_HEALTH_ATTEMPTS": "1",
            "MCP_HEALTH_SLEEP_SECONDS": "0",
        },
    )
    return p, log.read_text(), run_dir


def test_first_cutover_starts_loopback_blue_then_removes_legacy_api(
    tmp_path: Path,
) -> None:
    p, log, run_dir = run(tmp_path)
    assert p.returncode == 0, p.stderr
    start = log.index("--name at-api-blue")
    assert "--host 127.0.0.1 --port 8001" in log[start:]
    assert (
        start < log.index("docker rm -f at-api\n") < log.index("--name at-worker-new")
    )
    assert run_dir.joinpath("api-active-color").read_text() == "blue\n"
    cfg = run_dir.joinpath("haproxy.cfg").read_text()
    assert "bind 127.0.0.1:8000" in cfg and "bind 100.122.100.56:8000" in cfg
    assert "0.0.0.0" not in cfg


def test_second_deploy_starts_inactive_green_and_drains_only_after_hup(
    tmp_path: Path,
) -> None:
    p, log, run_dir = run(tmp_path, api_color="blue")
    assert p.returncode == 0, p.stderr
    assert "--name at-api-green" in log and "--host 127.0.0.1 --port 8002" in log
    assert log.index("--name at-api-green") < log.index("kill -s HUP at-haproxy")
    assert log.index("kill -s HUP at-haproxy") < log.index("rm -f at-api-blue")
    assert run_dir.joinpath("api-active-color").read_text() == "green\n"


def test_worker_is_ready_before_old_worker_is_stopped(tmp_path: Path) -> None:
    p, log, _ = run(tmp_path, api_color="blue")
    assert p.returncode == 0, p.stderr
    assert log.index("--name at-worker-new") < log.index("stop -t 60 at-worker")


def test_unhealthy_candidate_keeps_legacy_api_and_exits_nonzero(tmp_path: Path) -> None:
    p, log, run_dir = run(tmp_path, fail_candidate=True)
    assert p.returncode != 0
    assert "docker rm -f at-api\n" not in log
    assert not run_dir.joinpath("api-active-color").exists()


def test_tradingcodex_only_gets_required_policy_and_heartbeat_mount(
    tmp_path: Path,
) -> None:
    p, log, _ = run(tmp_path)
    assert p.returncode == 0, p.stderr
    tradingcodex = next(
        line
        for line in log.splitlines()
        if line.startswith("docker run") and "at-mcp-tradingcodex-execution" in line
    )
    assert "ORDER_APPROVAL_HASH_MODE=required" in tradingcodex
    assert "TOSS_APPROVAL_HASH_MODE=required" in tradingcodex
    assert "MCP_HEARTBEAT_PATH=" in tradingcodex
    paper = next(
        line
        for line in log.splitlines()
        if line.startswith("docker run") and "at-mcp-paper-001" in line
    )
    assert "ORDER_APPROVAL_HASH_MODE=required" not in paper


def test_mcp_profile_failure_restores_captured_profile_images(tmp_path: Path) -> None:
    p, log, _ = run(tmp_path, fail_mcp_port=8770)
    assert p.returncode != 0
    tradingcodex_runs = [
        line
        for line in log.splitlines()
        if line.startswith("docker run") and "at-mcp-tradingcodex-execution" in line
    ]
    assert any(NEW in line for line in tradingcodex_runs)
    assert any(OLD in line for line in tradingcodex_runs)


def test_scheduler_failure_restores_its_previous_image(tmp_path: Path) -> None:
    p, log, _ = run(tmp_path, api_color="blue", fail_once_container="at-scheduler")
    assert p.returncode != 0
    scheduler_runs = [
        line
        for line in log.splitlines()
        if line.startswith("docker run") and "--name at-scheduler" in line
    ]
    assert any(NEW in line for line in scheduler_runs)
    assert any(OLD in line for line in scheduler_runs)


def test_ws_failure_restores_every_singleton_previous_image(tmp_path: Path) -> None:
    p, log, _ = run(tmp_path, api_color="blue", fail_once_container="at-upbit-ws")
    assert p.returncode != 0
    for name in ("at-scheduler", "at-upbit-ws", "at-kis-ws"):
        runs = [
            line
            for line in log.splitlines()
            if line.startswith("docker run") and f"--name {name}" in line
        ]
        assert any(OLD in line for line in runs), name
    assert any(
        NEW in line
        for line in log.splitlines()
        if line.startswith("docker run") and "--name at-upbit-ws" in line
    )


def test_failed_post_hup_probe_restores_the_prior_haproxy_config(
    tmp_path: Path,
) -> None:
    p, _, run_dir = run(
        tmp_path,
        api_color="blue",
        fail_proxy=True,
        existing_config="old-haproxy-config\n",
    )
    assert p.returncode != 0
    assert run_dir.joinpath("haproxy.cfg").read_text() == "old-haproxy-config\n"
    assert run_dir.joinpath("api-active-color").read_text() == "blue\n"


def test_source_keeps_mutants_red() -> None:
    text = DEPLOY.read_text()
    assert "--host 127.0.0.1 --port" in text
    assert 'wait_health "$(api_port "$new")"' in text
    assert 'schedule_drain "at-api-${old}" "$API_DRAIN_SECONDS"' in text
    assert "wait_worker at-worker-new" in text
