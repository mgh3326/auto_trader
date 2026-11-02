"""ROB-1436: a forward deploy refuses while a retired MCP unit can still serve.

Removing a unit from the deploy inventory and from HAProxy closes neither its
loopback listener nor its restart policy, and some of these units can place
orders. The fake docker stops the run at ``docker pull``, the first mutation of
a real deploy, so a passing check shows up as a recorded pull.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.scripts._deploy_gate_fakes import install_gate_fakes

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "scripts/deploy-ncp-pull.sh"
DIGEST = "ghcr.io/mgh3326/auto_trader@sha256:" + "a" * 64
RETIRED = (
    "at-mcp-analysis-readonly",
    "at-mcp-account-read",
    "at-mcp-tradingcodex-execution",
    "at-mcp-paper-001",
    "at-mcp-live-kr",
    "at-mcp-live-us",
    "at-mcp-live-crypto",
)
KEPT = (
    "at-api",
    "at-worker",
    "at-scheduler",
    "at-upbit-ws",
    "at-kis-ws",
    "at-mcp-kiwoom",
    "at-mcp-h3-crypto-paper",
    "at-mcp-h3-us-paper",
    "at-haproxy",
)
FAKE_DOCKER = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$FAKE_DOCKER_LOG"
state="$FAKE_DOCKER_STATE"
case "$1" in
 inspect)
   [[ -z "${FAKE_INSPECT_FAILS:-}" ]] || exit 1
   name="${!#}"
   [[ -f "$state/$name" ]] || { printf 'Error: No such object: %s\\n' "$name" >&2; exit 1; }
   read -r running policy < "$state/$name"
   case "$3" in
     *RestartPolicy*) printf '%s %s\\n' "$running" "$policy" ;;
     *State.Running*) printf '%s\\n' "$running" ;;
     *RepoDigests*|*Config.Image*) printf '%s\\n' "$FAKE_DIGEST" ;;
     *) printf 'id-%s\\n' "$name" ;;
   esac
   ;;
 ps)
   [[ -z "${FAKE_INSPECT_FAILS:-}" ]] || exit 1
   for container in "$state"/*; do [[ -f "$container" ]] && basename "$container"; done
   true
   ;;
 image) printf '%s\\n' "$FAKE_DIGEST" ;;
 pull) exit 3 ;;
 *) : ;;
esac
"""


def _deploy(
    tmp_path: Path,
    *args: str,
    retired: dict[str, str] | None = None,
    inspect_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    install_gate_fakes(bindir, FAKE_DOCKER, DIGEST)
    (bindir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "curl").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    for name in KEPT:
        (state / name).write_text("true unless-stopped\n")
    for name, value in (retired or {}).items():
        (state / name).write_text(value + "\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".env.api").write_text("x=y\n")
    (run_dir / ".env.scheduler").write_text(
        "MCP_AUTH_TOKEN=x\nMCP_KIWOOM_AUTH_TOKEN=x\n"
        "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN=x\nMCP_H3_US_PAPER_AUTH_TOKEN=x\n"
    )
    (run_dir / "deployed-digest").write_text(DIGEST + "\n")
    log = tmp_path / "log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AT_RUN_DIRECTORY": str(run_dir),
        "AT_SECRETS_DIRECTORY": str(run_dir),
        "MCP_HAPROXY_TEMPLATE": str(REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DIGEST": DIGEST,
    }
    if inspect_fails:
        env["FAKE_INSPECT_FAILS"] = "1"
    completed = subprocess.run(
        [str(DEPLOY), *(args or ("sha-abc1234",))],
        text=True,
        capture_output=True,
        env=env,
        timeout=600,
        check=False,
    )
    return completed, log.read_text()


def test_inventory_lists_every_retired_container() -> None:
    assert (
        "declare -a RETIRED_CONTAINERS=(" + " ".join(RETIRED) + ")"
        in DEPLOY.read_text()
    )


def test_deploy_proceeds_when_retired_units_are_absent(tmp_path: Path) -> None:
    result, log = _deploy(tmp_path)
    assert "docker pull" in log, result.stderr[-1500:]
    assert "retired MCP unit" not in result.stderr


def test_deploy_proceeds_when_retired_units_are_stopped_and_disabled(
    tmp_path: Path,
) -> None:
    result, log = _deploy(tmp_path, retired=dict.fromkeys(RETIRED, "false no"))
    assert "docker pull" in log, result.stderr[-1500:]
    assert "retired MCP unit" not in result.stderr


@pytest.mark.parametrize(
    "state",
    ["true no", "true unless-stopped", "false unless-stopped", "false always"],
)
def test_deploy_refuses_before_any_mutation_while_a_unit_can_serve(
    tmp_path: Path, state: str
) -> None:
    result, log = _deploy(tmp_path, retired={"at-mcp-tradingcodex-execution": state})
    assert result.returncode == 78
    assert (
        f"retired MCP unit is not disabled: at-mcp-tradingcodex-execution: "
        f"active (running, restart policy: {state})" in result.stderr
    )
    assert "deploy refused" in result.stderr
    assert "docker pull" not in log


def test_deploy_names_every_active_unit_in_one_refusal(tmp_path: Path) -> None:
    result, _ = _deploy(tmp_path, retired=dict.fromkeys(RETIRED, "true unless-stopped"))
    assert result.returncode == 78
    for name in RETIRED:
        assert f"retired MCP unit is not disabled: {name}:" in result.stderr


def test_deploy_refuses_when_the_daemon_cannot_answer(tmp_path: Path) -> None:
    result, log = _deploy(tmp_path, inspect_fails=True)
    assert result.returncode == 78
    assert "retired MCP unit is not disabled: at-mcp-analysis-readonly: unknown" in (
        result.stderr
    )
    assert "docker pull" not in log


def test_dry_run_reports_status_and_does_not_refuse(tmp_path: Path) -> None:
    result, log = _deploy(
        tmp_path,
        "sha-abc1234",
        "--dry-run",
        retired={"at-mcp-live-kr": "true unless-stopped", "at-mcp-live-us": "false no"},
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert (
        "retired at-mcp-live-kr: active (running, restart policy: "
        "true unless-stopped)" in result.stdout
    )
    assert "retired at-mcp-live-us: disabled" in result.stdout
    assert "retired at-mcp-live-crypto: absent" in result.stdout
    assert "docker pull" not in log


def test_rollback_is_not_blocked_by_the_check(tmp_path: Path) -> None:
    result, log = _deploy(
        tmp_path, "--rollback", retired={"at-mcp-live-kr": "true unless-stopped"}
    )
    assert "retired MCP unit" not in result.stderr
    assert "RestartPolicy" not in log
