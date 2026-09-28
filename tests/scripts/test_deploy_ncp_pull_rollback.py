"""Task 836: stateful, host-free deployment invariants."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = Path(os.environ.get("DEPLOY_UNDER_TEST", REPO / "scripts/deploy-ncp-pull.sh"))
OLD = "ghcr.io/mgh3326/auto_trader@sha256:" + "1" * 64
KIS_OLD = "ghcr.io/mgh3326/auto_trader@sha256:" + "3" * 64
NEW = "ghcr.io/mgh3326/auto_trader@sha256:" + "2" * 64
MCP_PROFILES = (
    "at-mcp-analysis-readonly",
    "at-mcp-account-read",
    "at-mcp-tradingcodex-execution",
    "at-mcp-paper-001",
    "at-mcp-kiwoom",
)
INITIAL = (
    "at-api-blue",
    "at-worker",
    "at-scheduler",
    "at-upbit-ws",
    "at-kis-ws",
    "at-mcp-blue",
    *MCP_PROFILES,
)


def _run(
    tmp_path: Path,
    *,
    args: tuple[str, ...] = (),
    fail_name: str = "",
    fail_health_name: str = "",
    mismatch_name: str = "",
    success_mismatch_name: str = "",
    absent_name: str = "",
    stopped_name: str = "",
    unresolved_name: str = "",
    fail_initial_inspect_name: str = "",
    fail_haproxy_hup_once: bool = False,
    absent_haproxy: bool = False,
    fail_drain_record: bool = False,
    fail_drain_arm: bool = False,
    fail_digest_record: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict], dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log_path = tmp_path / "docker-calls.jsonl"
    log_path.touch()
    state_path = tmp_path / "state.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    initial = {name: KIS_OLD if name == "at-kis-ws" else OLD for name in INITIAL}
    if absent_name:
        del initial[absent_name]
    if unresolved_name:
        initial[unresolved_name] = "ghcr.io/mgh3326/auto_trader:mutable"
    if not absent_haproxy:
        initial["at-haproxy"] = "haproxy:3.1-alpine"
    state_path.write_text(json.dumps(initial))
    docker = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, os, pathlib, sys
        args = sys.argv[1:]
        state_file = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
        log_file = pathlib.Path(os.environ['FAKE_DOCKER_LOG'])
        state = json.loads(state_file.read_text())
        with log_file.open('a') as f:
            f.write(json.dumps(args) + '\\n')
        cmd = args[0]
        name = args[-1] if args else ''
        if cmd == 'inspect':
            if name == os.environ.get('FAKE_TRANSIENT_INSPECT_NAME'):
                marker = pathlib.Path(os.environ['FAKE_DOCKER_STATE'] + '.transient')
                if not marker.exists():
                    marker.touch()
                    print('Cannot connect to the Docker daemon', file=sys.stderr)
                    sys.exit(1)
            if name not in state:
                print('Error: No such object: ' + name, file=sys.stderr)
                sys.exit(1)
            fmt = args[2]
            value = state[name]
            if 'State.Running' in fmt:
                print('false' if name == os.environ.get('FAKE_STOPPED_NAME') else 'true')
            elif 'Config.Image' in fmt or 'RepoDigests' in fmt:
                print(value)
            else:
                print('id-' + name)
        elif cmd == 'image':
            if args[-1].startswith('id-'):
                print('none' if args[-1] == 'id-' + os.environ.get('FAKE_UNRESOLVED_NAME', '') else os.environ['FAKE_OLD_DIGEST'])
            else:
                print(os.environ['FAKE_NEW_DIGEST'])
        elif cmd == 'ps':
            print('\\n'.join(state))
        elif cmd == 'run':
            name = args[args.index('--name') + 1]
            image = next((a for a in args if a.startswith('ghcr.io/mgh3326/auto_trader@sha256:')), 'haproxy:3.1-alpine')
            if name == os.environ.get('FAKE_FAIL_NAME') and image == os.environ['FAKE_NEW_DIGEST']:
                sys.exit(23)
            if name == os.environ.get('FAKE_SUCCESS_MISMATCH_NAME') and image == os.environ['FAKE_NEW_DIGEST']:
                state[name] = os.environ['FAKE_OLD_DIGEST']
            elif name == os.environ.get('FAKE_MISMATCH_NAME') and image != os.environ['FAKE_NEW_DIGEST']:
                state[name] = os.environ['FAKE_NEW_DIGEST']
            else:
                state[name] = image
            print('id-' + name)
        elif cmd == 'logs':
            if name == os.environ.get('FAKE_FAIL_HEALTH_NAME') and state.get(name) == os.environ['FAKE_NEW_DIGEST']:
                print('disconnected')
            else:
                print('Listening started connected=True')
        elif cmd == 'rm':
            for item in args[1:]:
                if item != '-f':
                    state.pop(item, None)
        elif cmd == 'rename':
            state[args[2]] = state.pop(args[1])
        elif cmd == 'kill':
            if os.environ.get('FAKE_FAIL_HAPROXY_HUP_ONCE') == '1':
                marker = pathlib.Path(os.environ['FAKE_DOCKER_STATE'] + '.hup')
                if not marker.exists():
                    marker.touch()
                    sys.exit(23)
        elif cmd in ('pull', 'stop'):
            pass
        else:
            sys.exit(42)
        state_file.write_text(json.dumps(state))
        """
    )
    (bindir / "docker").write_text(docker)
    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'url="${!#}"\n'
        'if [[ "$FAKE_FAIL_HEALTH_NAME" == at-api-green && "$url" == *":8002/healthz" ]]; then echo 500; exit 0; fi\n'
        'if [[ "$FAKE_FAIL_HEALTH_NAME" == at-mcp-green && "$url" == *":8767/health" ]]; then echo 500; exit 0; fi\n'
        'if [[ "$*" == *--write-out* ]]; then echo 200; fi\n'
    )
    (bindir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "nohup").write_text("#!/usr/bin/env bash\nexit 0\n")
    if fail_drain_arm:
        (bindir / "mv").write_text(
            "#!/usr/bin/env bash\n"
            'for arg in "$@"; do [[ "$arg" == *drain-guard*.ready ]] && exit 23; done\n'
            'exec /bin/mv "$@"\n'
        )
    for path in bindir.iterdir():
        path.chmod(0o755)
    (run_dir / ".env.runtime").write_text("x=y\n")
    (run_dir / ".env.secrets").write_text(
        "\n".join(
            f"{name}=x"
            for name in (
                "MCP_AUTH_TOKEN",
                "MCP_ANALYSIS_READONLY_AUTH_TOKEN",
                "MCP_ACCOUNT_READ_AUTH_TOKEN",
                "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN",
                "MCP_PAPER_001_AUTH_TOKEN",
                "MCP_KIWOOM_AUTH_TOKEN",
            )
        )
        + "\n"
    )
    (run_dir / "deployed-digest").write_text(OLD + "\n")
    if fail_digest_record:
        (run_dir / "deployed-digest").unlink()
        (run_dir / "deployed-digest").mkdir()
    if fail_drain_record:
        (run_dir / "at-api-blue-drain.pid").mkdir()
    if "--rollback" in args:
        (run_dir / "deployed-digest.previous").write_text(NEW + "\n")
    (run_dir / "api-active-color").write_text("blue\n")
    (run_dir / "mcp-active-color").write_text("blue\n")
    result = subprocess.run(
        [str(DEPLOY), *args],
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AT_RUN_DIRECTORY": str(run_dir),
            "MCP_HAPROXY_TEMPLATE": str(REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"),
            "FAKE_DOCKER_STATE": str(state_path),
            "FAKE_DOCKER_LOG": str(log_path),
            "FAKE_NEW_DIGEST": NEW,
            "FAKE_OLD_DIGEST": OLD,
            "FAKE_FAIL_NAME": fail_name,
            "FAKE_FAIL_HEALTH_NAME": fail_health_name,
            "FAKE_MISMATCH_NAME": mismatch_name,
            "FAKE_SUCCESS_MISMATCH_NAME": success_mismatch_name,
            "FAKE_STOPPED_NAME": stopped_name,
            "FAKE_UNRESOLVED_NAME": unresolved_name,
            "FAKE_TRANSIENT_INSPECT_NAME": fail_initial_inspect_name,
            "FAKE_FAIL_HAPROXY_HUP_ONCE": "1" if fail_haproxy_hup_once else "0",
            "AT_HEALTHZ_ATTEMPTS": "1",
            "AT_HEALTHZ_SLEEP_SECONDS": "0",
            "MCP_HEALTH_ATTEMPTS": "1",
            "MCP_HEALTH_SLEEP_SECONDS": "0",
            "HAPROXY_READY_ATTEMPTS": "1",
            "HAPROXY_READY_INTERVAL": "0",
        },
    )
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    return result, calls, json.loads(state_path.read_text()), run_dir


def _mutations(calls: list[list[str]], name: str) -> list[list[str]]:
    return [
        call
        for call in calls
        if call[0] in {"run", "rm", "stop", "rename"} and name in call
    ]


def test_skip_keeps_kis_instance_and_reports_retained_digest(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--skip-kis-ws",))
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == KIS_OLD
    assert _mutations(calls, "at-kis-ws") == []
    assert "at-kis-ws" in result.stdout
    assert KIS_OLD in result.stdout
    assert "skip" in result.stdout.lower()
    assert state["at-worker"] == NEW


def test_manual_rollback_skip_also_keeps_kis_instance(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--rollback", "--skip-kis-ws"))
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == KIS_OLD
    assert _mutations(calls, "at-kis-ws") == []
    assert state["at-worker"] == NEW


@pytest.mark.parametrize(
    "failure",
    (
        "at-worker-new",
        "at-scheduler",
        "at-upbit-ws",
        "at-kis-ws",
        "at-mcp-account-read",
    ),
)
def test_each_late_phase_failure_restores_all_replaced_units(
    tmp_path: Path, failure: str
) -> None:
    result, calls, state, _ = _run(tmp_path, fail_name=failure)
    assert result.returncode != 0
    for name in INITIAL:
        assert state.get(name) == (KIS_OLD if name == "at-kis-ws" else OLD), name
    assert "at-api-green" not in state
    assert "at-worker-new" not in state
    assert "expected" in result.stdout.lower()
    assert "running" in result.stdout.lower()
    if failure != "at-worker-new":
        assert any(
            call[0] == "run" and "--name" in call and "at-worker" in call
            for call in calls
        )


def test_skip_is_never_touched_by_later_rollback(tmp_path: Path) -> None:
    result, calls, state, _ = _run(
        tmp_path, args=("--skip-kis-ws",), fail_name="at-mcp-account-read"
    )
    assert result.returncode != 0
    assert state["at-kis-ws"] == KIS_OLD
    assert _mutations(calls, "at-kis-ws") == []
    assert state["at-worker"] == OLD


@pytest.mark.parametrize(
    "failure", ("at-api-green", "at-worker-new", "at-upbit-ws", "at-mcp-green")
)
def test_health_failure_boundaries_restore_prior_state(
    tmp_path: Path, failure: str
) -> None:
    result, _, state, _ = _run(tmp_path, fail_health_name=failure)
    assert result.returncode != 0
    for name in INITIAL:
        assert state[name] == (KIS_OLD if name == "at-kis-ws" else OLD), name
    assert "at-api-green" not in state
    assert "at-mcp-green" not in state


def test_rollback_order_reverses_prior_replacements(tmp_path: Path) -> None:
    result, calls, _, _ = _run(tmp_path, fail_name="at-upbit-ws")
    assert result.returncode != 0
    old_runs = [
        call[call.index("--name") + 1]
        for call in calls
        if call[0] == "run" and "--name" in call and OLD in call
    ]
    assert old_runs.index("at-upbit-ws") < old_runs.index("at-scheduler")
    assert old_runs.index("at-scheduler") < old_runs.index("at-worker")


def test_mcp_failure_reverses_profile_then_core_replacements(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, fail_name="at-mcp-account-read")
    assert result.returncode != 0
    old_runs = [
        call[call.index("--name") + 1]
        for call in calls
        if call[0] == "run" and "--name" in call and OLD in call
    ]
    assert old_runs.index("at-mcp-account-read") < old_runs.index(
        "at-mcp-analysis-readonly"
    )
    assert old_runs.index("at-mcp-analysis-readonly") < old_runs.index("at-upbit-ws")
    assert old_runs.index("at-upbit-ws") < old_runs.index("at-scheduler")
    assert old_runs.index("at-scheduler") < old_runs.index("at-worker")
    assert state["at-mcp-analysis-readonly"] == OLD


def test_rollback_digest_mismatch_is_visible_and_nonzero(tmp_path: Path) -> None:
    result, _, _, _ = _run(
        tmp_path, fail_name="at-scheduler", mismatch_name="at-worker"
    )
    assert result.returncode != 0
    assert "at-worker" in result.stdout
    assert "MISMATCH" in result.stdout


def test_success_path_digest_mismatch_rolls_back_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    result, _, state, _ = _run(tmp_path, success_mismatch_name="at-scheduler")
    assert result.returncode != 0
    assert "deployment digest mismatch" in result.stderr
    assert f"at-scheduler\t{NEW}\t{OLD}\tMISMATCH" in result.stdout
    assert state["at-worker"] == OLD
    assert state["at-scheduler"] == OLD


def test_digest_record_failure_restores_replaced_units(tmp_path: Path) -> None:
    result, _, state, _ = _run(tmp_path, fail_digest_record=True)
    assert result.returncode != 0
    assert state["at-worker"] == OLD
    assert state["at-scheduler"] == OLD
    assert "at-api-green" not in state
    assert "container\texpected\trunning\tstatus" in result.stdout
    assert (
        "MISMATCH"
        not in result.stdout.rsplit("container\texpected\trunning\tstatus", 1)[-1]
    )


def test_drain_record_failure_restores_replaced_units(tmp_path: Path) -> None:
    result, _, state, _ = _run(tmp_path, fail_drain_record=True)
    assert result.returncode != 0
    assert state["at-worker"] == OLD
    assert state["at-scheduler"] == OLD
    assert "at-api-green" not in state
    assert "container\texpected\trunning\tstatus" in result.stdout
    assert (
        "MISMATCH"
        not in result.stdout.rsplit("container\texpected\trunning\tstatus", 1)[-1]
    )


def test_drain_activation_failure_restores_containers_and_digest_files(
    tmp_path: Path,
) -> None:
    result, _, state, run_dir = _run(tmp_path, fail_drain_arm=True)
    assert result.returncode != 0
    assert state["at-worker"] == OLD
    assert state["at-scheduler"] == OLD
    assert "at-api-green" not in state
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"
    assert not (run_dir / "deployed-digest.previous").exists()
    assert (
        "MISMATCH"
        not in result.stdout.rsplit("container\texpected\trunning\tstatus", 1)[-1]
    )


def test_absent_unit_is_removed_after_failed_later_phase(tmp_path: Path) -> None:
    result, _, state, _ = _run(
        tmp_path, absent_name="at-kis-ws", fail_name="at-mcp-account-read"
    )
    assert result.returncode != 0
    assert "at-kis-ws" not in state


def test_stopped_prior_unit_fails_before_any_mutation(tmp_path: Path) -> None:
    result, calls, _, _ = _run(tmp_path, stopped_name="at-worker")
    assert result.returncode != 0
    assert "container is not running: at-worker" in result.stderr
    assert not any(
        call[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for call in calls
    )


def test_unknown_prior_digest_fails_before_any_mutation(tmp_path: Path) -> None:
    result, calls, _, _ = _run(tmp_path, unresolved_name="at-upbit-ws")
    assert result.returncode != 0
    assert "rollback digest is unavailable for at-upbit-ws" in result.stderr
    assert not any(
        call[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for call in calls
    )


def test_transient_snapshot_inspect_failure_fails_before_mutation(
    tmp_path: Path,
) -> None:
    result, calls, state, _ = _run(tmp_path, fail_initial_inspect_name="at-worker")
    assert result.returncode != 0
    assert "cannot determine container presence: at-worker" in result.stderr
    assert state["at-worker"] == OLD
    assert not any(
        call[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for call in calls
    )


def test_haproxy_reload_failure_restores_prior_route_and_containers(
    tmp_path: Path,
) -> None:
    result, _, state, run_dir = _run(tmp_path, fail_haproxy_hup_once=True)
    assert result.returncode != 0
    assert state["at-worker"] == OLD
    assert state["at-api-blue"] == OLD
    assert "at-api-green" not in state
    assert (run_dir / "api-active-color").read_text() == "blue\n"
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"
    assert "container\texpected\trunning\tstatus" in result.stdout


def test_new_haproxy_is_removed_when_later_phase_fails(tmp_path: Path) -> None:
    result, _, state, run_dir = _run(
        tmp_path, absent_haproxy=True, fail_name="at-worker-new"
    )
    assert result.returncode != 0
    assert state["at-worker"] == OLD
    assert "at-api-green" not in state
    assert "at-haproxy" not in state
    assert not (run_dir / "haproxy.cfg").exists()
    assert (run_dir / "api-active-color").read_text() == "blue\n"


def test_dry_run_has_no_mutations_and_explains_skip(tmp_path: Path) -> None:
    result, calls, state, run_dir = _run(tmp_path, args=("--dry-run", "--skip-kis-ws"))
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == KIS_OLD
    assert state["at-worker"] == OLD
    assert not any(
        call[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for call in calls
    )
    assert (run_dir / "api-active-color").read_text() == "blue\n"
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"
    assert not (run_dir / "haproxy.cfg").exists()
    assert "at-kis-ws" in result.stdout
    assert "skip" in result.stdout.lower()
