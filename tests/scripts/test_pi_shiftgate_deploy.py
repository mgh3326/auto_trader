"""hk 5617: pi 의 shiftgate 배포 산출물(services.yaml, 싱글턴 스크립트, deploy.sh, 런북) 정적·셸 검증.

pi 호스트·DB·브로커 접근 없이 돈다: docker/shiftgate/게이트는 전부 가짜이고, 설정 규칙은
shiftgate f8a11c6 의 internal/config/config.go validate 를 테스트 안에 옮겨 적었다(고정 커밋 기준).
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PI = REPO / "deploy" / "shiftgate" / "pi"
SERVICES = PI / "services.yaml"
CUTOVER = PI / "services.cutover.yaml"
DEPLOY_SH = PI / "deploy.sh"
SWAP_SH = PI / "swap-singletons.sh"
RUNBOOK = REPO / "docs" / "runbooks" / "pi-shiftgate-deploy.md"
GATE = REPO / "scripts" / "deploy_ci_gate.py"

IMAGE = "ghcr.io/mgh3326/auto_trader"
DIGEST_HEX = "1" * 64
PINNED = f"{IMAGE}@sha256:{DIGEST_HEX}"

# shiftgate f8a11c6 config.go managedRunFlags (run_args 로 쓸 수 없는 플래그).
MANAGED_RUN_FLAGS = {
    "--name", "--label", "-l", "--network", "--net", "-p", "--publish", "-P", "--publish-all",
    "--env-file", "-e", "--env", "--rm", "-d", "--detach", "--restart", "-i", "-t",
    "--interactive", "--tty", "--privileged", "--pid", "--ipc", "--userns", "--uts", "--cap-add",
    "--security-opt", "--device", "--entrypoint", "--log-driver", "--log-opt",
}  # fmt: skip

# 현재 at-swap 정본 인자(brief 의 Key interfaces). 이 목록이 이 테스트의 독립된 기준이다.
AT_SWAP_ENV_FILES = ["/srv/at/secrets/.env.api", "/srv/at/secrets/.env.scheduler"]
AT_SWAP_API_COMMAND = [
    "/app/.venv/bin/uvicorn",
    "app.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
]
AT_SWAP_MCP_ENV = {
    "MCP_PROFILE": "default",
    "MCP_HOST": "100.90.137.33",
    "MCP_PORT": "8765",
    "MCP_TYPE": "streamable-http",
    "MCP_PATH": "/mcp",
    "MCP_USER_ID": "1",
    "AUTO_TRADER_COLOR": "pi",
    "MCP_HEARTBEAT_PATH": "/var/run/auto-trader/mcp-heartbeat/mcp-pi.json",
}
AT_SWAP_MCP_VOLUMES = [
    "-v", "/root/at-run/mcp-heartbeat:/var/run/auto-trader/mcp-heartbeat",
    "-v", "/etc/machine-id:/etc/machine-id:ro",
]  # fmt: skip
AT_SWAP_MCP_COMMAND = ["python", "-m", "app.mcp_server.main"]
AT_SWAP_SINGLETON_COMMANDS = {
    "at-worker": "/app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1",
    "at-upbit-ws": "/app/.venv/bin/python websocket_monitor.py --mode upbit",
}


def load(path: Path = SERVICES) -> dict:
    return yaml.safe_load(path.read_text())


def service(name: str, path: Path = SERVICES) -> dict:
    return next(s for s in load(path)["services"] if s["name"] == name)


def bind_host_port(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host, int(port)


# --- (a) services.yaml 이 shiftgate 규칙을 지킨다 -------------------------------------------


@pytest.mark.parametrize("path", [SERVICES, CUTOVER])
def test_services_follow_shiftgate_config_rules(path: Path) -> None:
    config = load(path)
    haproxy = config["haproxy"]
    allowed = set()
    for address in haproxy["allowed_binds"]:
        ip = ipaddress.ip_address(address)
        assert not ip.is_unspecified, "와일드카드는 allowed_binds 에 둘 수 없다"
        allowed.add(ip)
    assert allowed == {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("100.90.137.33"),
    }
    assert not haproxy.get("allow_wildcard_bind", False)
    assert any("{config}" in part for part in haproxy["check"])
    assert all(haproxy[key].startswith("/") for key in ("config", "socket"))

    names, binds, colour_ports = set(), {}, {}
    for svc in config["services"]:
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,40}", svc["name"])
        assert svc["name"] not in names
        names.add(svc["name"])
        assert svc["mode"] in ("http", "tcp")
        assert svc["binds"], "binds 가 비어 있다"
        for bind in svc["binds"]:
            host, port = bind_host_port(bind)
            ip = ipaddress.ip_address(
                host
            )  # 호스트는 IP 여야 한다(빈 호스트·* 는 와일드카드)
            assert not ip.is_unspecified
            assert ip in allowed, f"{bind} 가 allowed_binds 밖이다"
            assert bind not in binds
            binds[bind] = svc["name"]
        assert re.fullmatch(
            r"[a-z0-9]+([._-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)*",
            svc["image"],
        )
        blue, green = svc["ports"]["blue"], svc["ports"]["green"]
        assert blue != green
        for port in (blue, green):
            assert 1 <= port <= 65535
            assert port not in colour_ports, f"색 포트 {port} 충돌"
            colour_ports[port] = svc["name"]
        assert svc["network"] == "host"
        assert "container_port" not in svc  # network: host 에서는 의미가 없다
        assert all(path.startswith("/") for path in svc["env_files"])
        assert "PORT" not in svc.get("env", {})
        for key in svc.get("env", {}):
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        health = svc["health"]
        assert ("http" in health) != bool(health.get("tcp"))
        assert health["http"].startswith("/")
        assert yaml_duration(health["timeout"]) >= yaml_duration(health["interval"]) > 0
    # 루프백 바인드는 색 포트와 같으면 안 된다(shiftgate validate 의 마지막 검사).
    for bind, name in binds.items():
        host, port = bind_host_port(bind)
        if ipaddress.ip_address(host).is_loopback:
            assert port not in colour_ports, f"{name}: {bind} 가 색 포트와 충돌"


def yaml_duration(text: str) -> float:
    match = re.fullmatch(r"(\d+)(ms|s|m|h)", str(text))
    assert match, text
    return int(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]


@pytest.mark.parametrize("path", [SERVICES, CUTOVER])
def test_run_args_never_set_managed_flags(path: Path) -> None:
    for svc in load(path)["services"]:
        for arg in svc.get("run_args", []):
            flag = arg.split("=", 1)[0]
            assert flag not in MANAGED_RUN_FLAGS, (
                f"{svc['name']}: run_args 에 금지 플래그 {flag}"
            )
            if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
                for letter in arg[1:]:
                    assert (
                        f"-{letter}" not in MANAGED_RUN_FLAGS or not letter.islower()
                    ), arg
                    if not letter.islower():
                        break


def test_no_secret_values_and_env_files_are_paths_only() -> None:
    text = SERVICES.read_text()
    config = load()
    for svc in config["services"]:
        for path in svc["env_files"]:
            assert path.startswith("/srv/at/secrets/.env."), path
        for key, value in svc.get("env", {}).items():
            assert not re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", key), key
            assert not re.search(r"\b(sk-|ghp_|xox)", str(value))
    assert (
        "MCP_AUTH_TOKEN" not in text.replace("# ", "") or "MCP_AUTH_TOKEN=" not in text
    )
    assert "registry_auth_file" not in text and "pre_deploy" not in text


def test_no_follow_or_scheduler_in_deliverables() -> None:
    for path in (SERVICES, CUTOVER, DEPLOY_SH, SWAP_SH):
        body = path.read_text()
        assert (
            "shiftgate follow" not in body
            and "crontab" not in body
            and "systemctl enable" not in body
        )
    assert "registry_auth_file" not in SERVICES.read_text()  # follow 전용 키


def test_cutover_differs_only_in_the_two_binds() -> None:
    real, temporary = load(SERVICES), load(CUTOVER)
    assert real["services"][0]["binds"] == ["127.0.0.1:8000"]
    assert real["services"][1]["binds"] == ["100.90.137.33:8765"]
    assert temporary["services"][0]["binds"] == ["127.0.0.1:8100"]
    assert temporary["services"][1]["binds"] == ["100.90.137.33:8865"]
    for config in (real, temporary):
        for svc in config["services"]:
            svc.pop("binds")
    assert real == temporary


# --- (e)/2 현재 at-swap 인자와 같은 env·볼륨·명령 ----------------------------------------------


def test_env_files_and_network_match_at_swap_for_both_services() -> None:
    for name in ("at-api", "at-mcp"):
        svc = service(name)
        assert svc["env_files"] == AT_SWAP_ENV_FILES  # 순서 포함: 두 번째가 이긴다
        assert svc["network"] == "host"
    assert load()["log"] == {
        "driver": "daemon"
    }  # at-swap 은 로그 드라이버를 지정하지 않는다


def test_at_api_command_matches_at_swap() -> None:
    svc = service("at-api")
    port = svc["ports"]["blue"]
    assert [a.replace("{port}", "8000") for a in svc["args"]] == AT_SWAP_API_COMMAND
    # 앱 호스트는 바인드 호스트와 같고, HAProxy 바인드 포트가 at-swap 의 포트다.
    assert svc["binds"] == ["127.0.0.1:8000"] and port != 8000
    assert (
        "run_args" not in svc and "env" not in svc
    )  # at-swap 의 at-api 는 -e/-v 가 없다


def test_at_mcp_env_volumes_and_command_match_at_swap() -> None:
    svc = service("at-mcp")
    env = {k: str(v) for k, v in svc["env"].items()}
    bind_host, bind_port = bind_host_port(svc["binds"][0])
    # 의도된 차이 둘: 앱은 루프백 색 포트에서 뜨고, at-swap 이 직접 쓰던 tailnet 주소·포트는 HAProxy 바인드가 이어받는다.
    assert (
        env.pop("MCP_HOST") == "127.0.0.1" and AT_SWAP_MCP_ENV["MCP_HOST"] == bind_host
    )
    assert env.pop("MCP_PORT") == "{port}" and AT_SWAP_MCP_ENV["MCP_PORT"] == str(
        bind_port
    )
    expected = {
        k: v for k, v in AT_SWAP_MCP_ENV.items() if k not in ("MCP_HOST", "MCP_PORT")
    }
    # 색 전개({color})는 at-swap 의 고정 이름 pi 와 같은 모양이어야 한다.
    assert {k: v.replace("{color}", "pi") for k, v in env.items()} == expected
    assert env["MCP_PROFILE"] == "default" and env["MCP_PROFILE"].strip()  # #1189
    assert svc["run_args"] == AT_SWAP_MCP_VOLUMES
    assert svc["args"] == AT_SWAP_MCP_COMMAND
    ports = svc["ports"]
    assert bind_port not in (ports["blue"], ports["green"])


def test_mcp_streaming_timeouts_and_health_rationale() -> None:
    config = load()
    timeouts = config["haproxy"]["timeouts"]
    for key in ("client", "server", "tunnel"):
        assert yaml_duration(timeouts[key]) >= 3600, (
            f"MCP 스트림이 {key} 타임아웃에 끊긴다"
        )
    mcp = service("at-mcp")
    assert (
        mcp["health"]["http"] == "/health"
    )  # 인증 없는 GET. /mcp 는 토큰이 필요해 쓸 수 없다
    assert service("at-api")["health"]["http"] == "/healthz"
    assert (
        yaml_duration(mcp["drain_timeout"]) <= 600
    )  # 드레인은 서비스 잠금을 잡은 채 CLI 를 막는다
    text = SERVICES.read_text()
    assert "/health" in text and "스트림" in text  # 근거가 파일에 적혀 있다


# --- 싱글턴 스크립트(가짜 docker) -----------------------------------------------------------


FAKE_DOCKER = r"""#!/usr/bin/env bash
S="$FAKE_STATE"
printf '%s\n' "$*" >> "$S/log"
cmd="$1"; shift
case "$cmd" in
  inspect)
    name="${@: -1}"
    case "$*" in
      *State.Running*) [[ -f "$S/c/$name" ]] || exit 1; cat "$S/r/$name"; echo ;;
      *) [[ -f "$S/c/$name" ]] || exit 1; echo "id-$name" ;;
    esac ;;
  stop) name="${@: -1}"; echo false > "$S/r/$name" ;;
  rename) mv "$S/c/$1" "$S/c/$2"; mv "$S/r/$1" "$S/r/$2" ;;
  rm) name="${@: -1}"; rm -f "$S/c/$name" "$S/r/$name" ;;
  start) echo true > "$S/r/$1" ;;
  run)
    name=""; prev=""
    for arg in "$@"; do [[ "$prev" == --name ]] && name="$arg"; prev="$arg"; done
    [[ "$FAKE_RUN_FAIL" == "$name" ]] && exit 1
    echo "new $*" > "$S/c/$name"; echo true > "$S/r/$name" ;;
  logs)
    name="${@: -1}"
    [[ "$FAKE_LOG_BAD" == "$name" ]] || echo 'Listening started connected=True'
    ;;
  ps) ;;
  *) exit 42 ;;
esac
"""


def executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def host(tmp_path: Path) -> dict:
    state = tmp_path / "state"
    for sub in ("c", "r"):
        (state / sub).mkdir(parents=True)
    (state / "log").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "docker", FAKE_DOCKER)
    run = tmp_path / "run"
    run.mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FAKE_STATE": str(state),
        "AT_RUN_DIR": str(run),
        "AT_SWAP_WAIT_ATTEMPTS": "2",
        "AT_SWAP_WAIT_SLEEP": "0",
    }
    return {"state": state, "bindir": bindir, "run": run, "env": env, "tmp": tmp_path}


def seed_old(host: dict, *names: str) -> None:
    for name in names:
        (host["state"] / "c" / name).write_text(f"old {name}\n")
        (host["state"] / "r" / name).write_text("true\n")


def docker_log(host: dict) -> list[str]:
    return (host["state"] / "log").read_text().splitlines()


def swap(host: dict, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SWAP_SH), *args],
        env={**host["env"], **extra},
        capture_output=True,
        text=True,
        check=False,
    )


def test_swap_runs_exactly_the_at_swap_arguments(host: dict) -> None:
    seed_old(host, "at-worker", "at-upbit-ws")
    result = swap(host, PINNED)
    assert result.returncode == 0, result.stderr
    runs = [line for line in docker_log(host) if line.startswith("run ")]
    for name, command in AT_SWAP_SINGLETON_COMMANDS.items():
        expected = (
            f"run -d --name {name} --restart unless-stopped --network host "
            f"--env-file {AT_SWAP_ENV_FILES[0]} --env-file {AT_SWAP_ENV_FILES[1]} {PINNED} {command}"
        )
        assert expected in runs
    assert len(runs) == 2
    # 옛 컨테이너는 정지(-t 60)한 뒤 <이름>-prev 로 보관된다.
    log = docker_log(host)
    for name in AT_SWAP_SINGLETON_COMMANDS:
        assert f"stop -t 60 {name}" in log
        assert f"rename {name} {name}-prev" in log
        assert log.index(f"stop -t 60 {name}") < log.index(f"rename {name} {name}-prev")
        assert (host["state"] / "c" / f"{name}-prev").read_text() == f"old {name}\n"
    # 이 스크립트는 deployed-digest 를 쓰지 않는다(deploy.sh 가 모두 성공한 뒤 한 번 쓴다).
    assert not (host["run"] / "deployed-digest").exists()


def test_swap_failure_on_second_singleton_restores_both(host: dict) -> None:
    seed_old(host, "at-worker", "at-upbit-ws")
    result = swap(host, PINNED, FAKE_LOG_BAD="at-upbit-ws")
    assert result.returncode == 1
    for name in AT_SWAP_SINGLETON_COMMANDS:
        assert (host["state"] / "c" / name).read_text() == f"old {name}\n"
        assert (host["state"] / "r" / name).read_text().strip() == "true"
        assert not (host["state"] / "c" / f"{name}-prev").exists()
    assert list(host["run"].glob("swap-failure-at-upbit-ws-*.log"))  # 실패 로그 보존
    assert not (host["run"] / "deployed-digest").exists()


def test_swap_failure_on_first_singleton_never_touches_the_second(host: dict) -> None:
    seed_old(host, "at-worker", "at-upbit-ws")
    result = swap(host, PINNED, FAKE_RUN_FAIL="at-worker")
    assert result.returncode == 1
    assert (host["state"] / "c" / "at-worker").read_text() == "old at-worker\n"
    assert not any("at-upbit-ws" in line for line in docker_log(host))


def test_swap_with_no_previous_container_just_starts_and_failure_leaves_nothing(
    host: dict,
) -> None:
    result = swap(host, PINNED)
    assert result.returncode == 0, result.stderr
    assert (host["state"] / "r" / "at-worker").read_text().strip() == "true"
    failed = swap(host, PINNED, FAKE_RUN_FAIL="at-upbit-ws")
    assert failed.returncode == 1


@pytest.mark.parametrize(
    "bad", ["main", f"{IMAGE}:main", "sha256:" + DIGEST_HEX, f"{IMAGE}@sha256:abc"]
)
def test_swap_refuses_anything_but_a_pinned_image_without_touching_docker(
    host: dict, bad: str
) -> None:
    result = swap(host, bad)
    assert result.returncode == 2
    assert docker_log(host) == []


# --- (c) deploy.sh -------------------------------------------------------------------------


FAKE_GATE_OK = f"""\
import sys
open(sys.argv[0] + ".args", "w").write(" ".join(sys.argv[1:]))
print("{PINNED}")
"""
FAKE_GATE_REFUSE = """\
import sys
print("deploy gate: REFUSED (exit 78)", file=sys.stderr)
sys.exit(78)
"""
FAKE_SHIFTGATE = """#!/usr/bin/env bash
printf 'shiftgate %s\\n' "$*" >> "$FAKE_CALLS"
if [[ "$*" == *"deploy $FAKE_SG_FAIL "* ]]; then exit "${FAKE_SG_RC:-1}"; fi
exit 0
"""
FAKE_SWAP = """#!/usr/bin/env bash
printf 'swap %s\\n' "$*" >> "$FAKE_CALLS"
exit "${FAKE_SWAP_RC:-0}"
"""
FAKE_DOCKER_CALLS = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$FAKE_CALLS"
"""


@pytest.fixture
def deploy_env(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "docker", FAKE_DOCKER_CALLS)
    run = tmp_path / "run"
    run.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("")
    gate = tmp_path / "gate.py"
    gate.write_text(FAKE_GATE_OK)
    shiftgate = executable(tmp_path / "shiftgate", FAKE_SHIFTGATE)
    swapper = executable(tmp_path / "swap.sh", FAKE_SWAP)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "AT_RUN_DIR": str(run),
        "AT_DEPLOY_GATE_PYTHON": sys.executable,
        "AT_DEPLOY_GATE_SCRIPT": str(gate),
        "AT_SHIFTGATE_BIN": str(shiftgate),
        "AT_SHIFTGATE_CONFIG": "/etc/shiftgate/services.yaml",
        "AT_SWAP_SINGLETONS": str(swapper),
        "FAKE_CALLS": str(calls),
    }
    return {"env": env, "run": run, "calls": calls, "gate": gate, "tmp": tmp_path}


def deploy(env: dict, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        env={**env["env"], **extra},
        capture_output=True,
        text=True,
        check=False,
    )


def recorded(env: dict) -> list[str]:
    return env["calls"].read_text().splitlines()


def test_gate_refusal_touches_nothing(deploy_env: dict) -> None:
    deploy_env["gate"].write_text(FAKE_GATE_REFUSE)
    (deploy_env["run"] / "deployed-digest").write_text(
        "ghcr.io/mgh3326/auto_trader@sha256:" + "9" * 64 + "\n"
    )
    result = deploy(deploy_env, "main")
    assert result.returncode == 78
    assert "no container was touched" in result.stderr
    assert recorded(deploy_env) == []  # shiftgate·swap·docker 어느 것도 호출되지 않았다
    assert (
        (deploy_env["run"] / "deployed-digest").read_text().strip().endswith("9" * 64)
    )
    assert not (deploy_env["run"] / "deployed-digest.previous").exists()


def test_gate_python_too_old_refuses_before_any_container(deploy_env: dict) -> None:
    old = executable(deploy_env["tmp"] / "oldpy", "#!/bin/sh\nexit 1\n")
    result = deploy(deploy_env, "main", AT_DEPLOY_GATE_PYTHON=str(old))
    assert result.returncode == 78
    assert recorded(deploy_env) == []


def test_missing_gate_script_refuses(deploy_env: dict) -> None:
    result = deploy(
        deploy_env, "main", AT_DEPLOY_GATE_SCRIPT=str(deploy_env["tmp"] / "nope.py")
    )
    assert result.returncode == 78
    assert recorded(deploy_env) == []


def test_success_order_and_single_digest_write(deploy_env: dict) -> None:
    previous = "ghcr.io/mgh3326/auto_trader@sha256:" + "9" * 64
    (deploy_env["run"] / "deployed-digest").write_text(previous + "\n")
    result = deploy(deploy_env, "main")
    assert result.returncode == 0, result.stderr
    cfg = "--config /etc/shiftgate/services.yaml"
    assert recorded(deploy_env) == [
        f"shiftgate {cfg} deploy at-api --digest sha256:{DIGEST_HEX}",
        f"shiftgate {cfg} deploy at-mcp --digest sha256:{DIGEST_HEX}",
        f"swap {PINNED}",
    ]
    assert (deploy_env["run"] / "deployed-digest").read_text() == PINNED + "\n"
    assert (
        deploy_env["run"] / "deployed-digest.previous"
    ).read_text() == previous + "\n"
    assert (
        deploy_env["gate"].with_name("gate.py.args")
    ).read_text() == f"--image {IMAGE}:main"


def test_allow_unverified_reaches_the_gate_only(deploy_env: dict) -> None:
    result = deploy(deploy_env, "--allow-unverified", "CI 장애 hk 1", "main")
    assert result.returncode == 0, result.stderr
    args = (deploy_env["gate"].with_name("gate.py.args")).read_text()
    assert args == f"--image {IMAGE}:main --allow-unverified CI 장애 hk 1"


@pytest.mark.parametrize("failing", ["at-api", "at-mcp"])
def test_shiftgate_failure_stops_before_later_stages_and_keeps_digest(
    deploy_env: dict, failing: str
) -> None:
    (deploy_env["run"] / "deployed-digest").write_text("old\n")
    result = deploy(deploy_env, "main", FAKE_SG_FAIL=failing)
    assert result.returncode == 1
    calls = recorded(deploy_env)
    assert not any(call.startswith("swap ") for call in calls)
    if failing == "at-api":
        assert not any("deploy at-mcp" in call for call in calls)
    assert any(" status" in call for call in calls)  # 실패 시 상태 출력
    assert any(call.startswith("docker ps") for call in calls)
    assert (deploy_env["run"] / "deployed-digest").read_text() == "old\n"


def test_recovery_exit_code_is_propagated_with_a_hint(deploy_env: dict) -> None:
    result = deploy(deploy_env, "main", FAKE_SG_FAIL="at-api", FAKE_SG_RC="3")
    assert result.returncode == 3
    assert "recover" in result.stderr


def test_singleton_failure_keeps_digest_unwritten(deploy_env: dict) -> None:
    (deploy_env["run"] / "deployed-digest").write_text("old\n")
    result = deploy(deploy_env, "main", FAKE_SWAP_RC="1")
    assert result.returncode == 1
    assert (deploy_env["run"] / "deployed-digest").read_text() == "old\n"
    assert not (deploy_env["run"] / "deployed-digest.previous").exists()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["sha256:" + DIGEST_HEX],
        [f"{IMAGE}@sha256:{DIGEST_HEX}"],
        ["main", "extra"],
        ["--nope", "main"],
        ["--allow-unverified"],
    ],
)
def test_usage_errors_touch_nothing(deploy_env: dict, args: list[str]) -> None:
    result = deploy(deploy_env, *args)
    assert result.returncode == 2
    assert recorded(deploy_env) == []


def test_scripts_are_valid_bash_and_executable() -> None:
    for script in (DEPLOY_SH, SWAP_SH):
        assert script.stat().st_mode & stat.S_IXUSR
        assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
        assert script.read_text().startswith("#!/usr/bin/env bash\n")


# --- (d)/(e) 런북: 명령 플래그가 스크립트와 일치한다 ----------------------------------------------

# shiftgate f8a11c6 cmd/shiftgate/main.go usage: 서브커맨드와 허용 플래그.
SHIFTGATE_SUBCOMMANDS: dict[str, set[str]] = {
    "deploy": {"--digest"},
    "rollback": set(),
    "recover": set(),
    "status": {"--json"},
    "check": {"--ping-url-env"},  # --heal 은 이 런북에서 쓰지 않는다(아래 테스트)
    "render": {"--no-reload", "--dry-run"},  # --allow-wildcard-bind 는 쓰지 않는다
    "prune": set(),
}


def runbook_commands() -> list[str]:
    text = RUNBOOK.read_text()
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            if line.strip():
                commands.append(line.strip())
    return commands


def script_flags(script: Path) -> set[str]:
    usage = subprocess.run(
        ["bash", str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert usage.returncode == 0
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", usage.stderr))


def gate_flags() -> set[str]:
    result = subprocess.run(
        [sys.executable, str(GATE), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", result.stdout)) - {"--help"}


def test_runbook_has_the_required_sections() -> None:
    text = RUNBOOK.read_text()
    for needle in (
        "## 1. 1회성 호스트 준비",
        "## 3. 컷오버 순서",
        "다운타임",
        "### 3.3 컷오버 롤백",
        "## 4. 일상 배포",
        "## 5. 롤백",
        "## 6. 확인 명령",
    ):
        assert needle in text, needle
    for needle in (
        "haproxy -v",
        "go build",
        "docker buildx imagetools inspect",
        "shiftgate render --dry-run",
        "shiftgate rollback at-api",
        "shiftgate status",
        "shiftgate check",
    ):
        assert needle in runbook_commands() or any(
            needle in c for c in runbook_commands()
        ), needle


def test_runbook_shiftgate_commands_use_only_real_subcommands_and_flags() -> None:
    seen = set()
    for command in runbook_commands():
        tokens = shlex.split(command)
        if not tokens or tokens[0] != "shiftgate":
            continue
        rest = tokens[1:]
        if rest[:1] == ["--config"]:
            rest = rest[2:]
        assert rest, command
        sub, *args = rest
        assert sub in SHIFTGATE_SUBCOMMANDS, f"없는 서브커맨드: {command}"
        seen.add(sub)
        flags = {a for a in args if a.startswith("--")}
        assert flags <= SHIFTGATE_SUBCOMMANDS[sub], (
            f"허용되지 않은 플래그 {flags - SHIFTGATE_SUBCOMMANDS[sub]}: {command}"
        )
        if sub == "deploy":
            assert re.fullmatch(
                r"deploy at-(api|mcp) --digest <digest>", " ".join(rest)
            ), command
        if sub in ("rollback", "recover"):
            assert args in (["at-api"], ["at-mcp"]), command
    assert {"deploy", "rollback", "recover", "status", "check", "render"} <= seen


def test_runbook_adapter_commands_match_script_usage() -> None:
    deploy_flags, swap_flags, gate = (
        script_flags(DEPLOY_SH),
        script_flags(SWAP_SH),
        gate_flags(),
    )
    assert deploy_flags == {"--allow-unverified"}
    assert swap_flags == {"--wait-attempts"}
    assert {
        "--image",
        "--allow-unverified",
    } <= gate  # deploy.sh 가 게이트에 넘기는 플래그가 실제 argparse 에 있다
    used = set()
    for command in runbook_commands():
        tokens = shlex.split(command)
        if not tokens:
            continue
        if tokens[0].endswith("/deploy.sh"):
            assert tokens[0] == "/root/at-run/shiftgate-pi/deploy.sh"
            flags = {t for t in tokens[1:] if t.startswith("--")}
            assert flags <= deploy_flags, command
            assert tokens[-1] == "main", "deploy.sh 의 인자는 digest 가 아니라 태그다"
            used.add("deploy")
        if tokens[0].endswith("/swap-singletons.sh"):
            assert tokens[0] == "/root/at-run/shiftgate-pi/swap-singletons.sh"
            assert not {t for t in tokens[1:] if t.startswith("--")} - swap_flags, (
                command
            )
            assert re.fullmatch(rf"{re.escape(IMAGE)}@sha256:<64hex>", tokens[-1]), (
                command
            )
            used.add("swap")
    assert used == {"deploy", "swap"}


def test_runbook_install_sources_exist_and_are_installed_where_the_scripts_look() -> (
    None
):
    installs = [
        shlex.split(c) for c in runbook_commands() if c.startswith("install -m")
    ]
    sources = {
        src for tokens in installs for src in tokens[3:-1] if not src.startswith("/")
    }  # 레포 상대 경로만
    for source in sources:
        assert (REPO / source).is_file(), source
    # deploy.sh 는 deploy_ci_gate.py 를 옆에서 찾으므로 같은 디렉터리에 설치된다.
    host_dir = [t for t in installs if t[-1] == "/root/at-run/shiftgate-pi/"]
    assert host_dir and {
        "scripts/deploy_ci_gate.py",
        "deploy/shiftgate/pi/deploy.sh",
    } <= set(host_dir[0])
    assert any(
        t[-1] == "/etc/shiftgate/services.yaml"
        and "deploy/shiftgate/pi/services.cutover.yaml" in t
        for t in installs
    )
    assert any(
        t[-1] == "/etc/shiftgate/services.yaml"
        and "deploy/shiftgate/pi/services.yaml" in t
        for t in installs
    )


def test_runbook_never_schedules_follow_or_heals_and_never_reads_secrets() -> None:
    commands = "\n".join(runbook_commands())
    assert "shiftgate follow" not in commands and "--heal" not in commands
    assert "--allow-wildcard-bind" not in commands
    assert not re.search(
        r"cat\s+\S*\.env|\.env\.api\b.*\bcat|source\s+\S*\.env", commands
    )
    text = RUNBOOK.read_text()
    assert "f8a11c6" in text and "[live check]" in text
    assert "cloudflared" in text and "관찰 창" in text


def test_runbook_cutover_ports_and_paths_match_services_yaml() -> None:
    text = RUNBOOK.read_text()
    api, mcp = service("at-api"), service("at-mcp")
    assert (
        api["binds"] == ["127.0.0.1:8000"] and "http://127.0.0.1:8000/healthz" in text
    )
    assert (
        mcp["binds"] == ["100.90.137.33:8765"]
        and "http://100.90.137.33:8765/health" in text
    )
    cutover_api, cutover_mcp = service("at-api", CUTOVER), service("at-mcp", CUTOVER)
    assert "http://127.0.0.1:8100/healthz" in text and cutover_api["binds"] == [
        "127.0.0.1:8100"
    ]
    assert "http://100.90.137.33:8865/health" in text and cutover_mcp["binds"] == [
        "100.90.137.33:8865"
    ]
    for port in (18000, 28000, 18765, 28765, 8100, 8865):
        assert str(port) in text
    assert os.path.basename(str(GATE)) == "deploy_ci_gate.py"
