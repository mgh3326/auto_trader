from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SHIFTGATE_DIR = ROOT / "ops/ncp/shiftgate"
STEADY_CONFIG = SHIFTGATE_DIR / "services.at.yaml"
S1_CONFIG = SHIFTGATE_DIR / "services.at.s1.yaml"
CHECK_SCRIPT = SHIFTGATE_DIR / "haproxy-check.sh"
RETIRED_PORTS = {8768, 8769, 8770, 8771, 8773, 8774, 8775}
ALLOWED_ADDRESSES = {"127.0.0.1", "100.122.100.56"}


def _config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _all_configs() -> list[tuple[str, dict]]:
    return [("steady", _config(STEADY_CONFIG)), ("s1", _config(S1_CONFIG))]


def _ports_in_config(config: dict) -> set[int]:
    ports: set[int] = set()
    for service in config["services"]:
        ports.update(service.get("ports", {}).values())
        for bind in service.get("binds", []):
            _, port = bind.rsplit(":", 1)
            ports.add(int(port))
        upstream = service.get("upstream", "")
        if upstream:
            port = upstream.rsplit(":", 1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return ports


def test_mcp_workloads_have_nonempty_profile_in_steady_config() -> None:
    mcp_workloads = [
        service
        for service in _config(STEADY_CONFIG)["services"]
        if service["name"].startswith("at-mcp-") or service["name"] == "at-mcp"
        if "image" in service
    ]
    assert [service["name"] for service in mcp_workloads] == ["at-mcp"]
    assert mcp_workloads[0].get("env", {}).get("MCP_PROFILE", "").strip()


@pytest.mark.parametrize(
    ("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30]
)
def test_all_service_binds_use_only_committed_private_addresses(
    variant, config
) -> None:
    del variant
    for service in config["services"]:
        for bind in service.get("binds", []):
            address, port = bind.rsplit(":", 1)
            assert address in ALLOWED_ADDRESSES
            assert 1 <= int(port) <= 65535


@pytest.mark.parametrize(
    ("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30]
)
def test_no_wildcard_bind_is_allowed(variant, config) -> None:
    del variant
    for service in config["services"]:
        for bind in service.get("binds", []):
            address, _ = bind.rsplit(":", 1)
            assert address not in {"0.0.0.0", "::", "*", ""}


@pytest.mark.parametrize(
    ("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30]
)
def test_colour_ports_are_bind_port_plus_10000_and_20000(variant, config) -> None:
    del variant
    for service in config["services"]:
        if "image" not in service:
            continue
        bound_ports = {int(bind.rsplit(":", 1)[1]) for bind in service["binds"]}
        assert len(bound_ports) == 1
        bind_port = bound_ports.pop()
        assert service["ports"] == {
            "blue": bind_port + 10000,
            "green": bind_port + 20000,
        }


@pytest.mark.parametrize(
    ("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30]
)
def test_retired_ports_are_unused(variant, config) -> None:
    del variant
    assert RETIRED_PORTS.isdisjoint(_ports_in_config(config))


@pytest.mark.parametrize(
    "config_path", [STEADY_CONFIG, S1_CONFIG], ids=["steady", "s1"]
)
def test_live_check_values_remain_documented_placeholders(config_path: Path) -> None:
    text = config_path.read_text()
    config = yaml.safe_load(text)
    assert config["log"]["driver"] == "__LIVE_CHECK_LOG_DRIVER__"
    assert config["haproxy"]["socket"] == "/__LIVE_CHECK_HAPROXY_SOCKET__"
    assert (
        config["haproxy"]["socket_in_config"]
        == "/__LIVE_CHECK_HAPROXY_SOCKET_IN_CONFIG__"
    )
    assert text.count("docs/design/4155-shiftgate-adoption.md") >= 3


def _haproxy_check(
    tmp_path: Path, config_text: str
) -> subprocess.CompletedProcess[str]:
    config_file = tmp_path / "candidate.cfg"
    config_file.write_text(config_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" > "$HAPROXY_CALL_LOG"\n')
    fake_docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HAPROXY_CALL_LOG": str(tmp_path / "haproxy-call.log"),
    }
    return subprocess.run(
        ["bash", str(CHECK_SCRIPT), str(config_file)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


VALID_HAPROXY = """global
    log stdout format raw local0
    master-worker
    stats socket /var/run/haproxy/admin_socket.sock mode 600 level admin

defaults
    log global
    mode http
    option dontlognull
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    timeout tunnel 1h
    timeout http-request 10s

frontend ft_at_api
    mode http
    bind 127.0.0.1:8000
    bind 100.122.100.56:8000
    default_backend bk_at_api

backend bk_at_api
    mode http
    option httpchk GET /healthz
    http-check expect status 200
    default-server inter 5s fall 2 rise 1
    server blue 127.0.0.1:18000 check
    server green 127.0.0.1:28000 check disabled
"""


def _rendered_shiftgate_config(
    source: Path,
    *,
    legacy_upstream_ports: dict[str, int] | None = None,
) -> str:
    config = _config(source)
    legacy_upstream_ports = legacy_upstream_ports or {
        "at-api-legacy": 8001,
        "at-mcp-legacy": 8766,
    }
    rendered = [VALID_HAPROXY.split("frontend ft_at_api", maxsplit=1)[0].rstrip()]

    for service in config["services"]:
        section_name = service["name"].replace("-", "_")
        frontend = f"ft_{section_name}"
        backend = f"bk_{section_name}"
        binds = service.get("binds", [])
        health_path = service.get("health", {}).get("http", "/health")
        frontend_lines = [
            f"frontend {frontend}",
            "    mode http",
            *(f"    bind {bind}" for bind in binds),
            f"    default_backend {backend}",
        ]
        backend_lines = [
            "",
            f"backend {backend}",
            "    mode http",
            f"    option httpchk GET {health_path}",
            "    http-check expect status 200",
            "    default-server inter 5s fall 2 rise 1",
        ]
        ports = service.get("ports")
        if ports:
            for color, port in ports.items():
                suffix = " disabled" if color == "green" else ""
                backend_lines.append(
                    f"    server {color} 127.0.0.1:{port} check{suffix}"
                )
        else:
            upstream = service["upstream"]
            host, port = upstream.rsplit(":", maxsplit=1)
            if not port.isdigit():
                port = str(legacy_upstream_ports[service["name"]])
            backend_lines.append(f"    server {section_name} {host}:{port} check")
        rendered.extend(frontend_lines + backend_lines)

    return "\n".join(rendered) + "\n"


def test_haproxy_check_accepts_closed_shape_socket_and_required_binds(
    tmp_path: Path,
) -> None:
    result = _haproxy_check(tmp_path, VALID_HAPROXY)
    assert result.returncode == 0, result.stderr
    config_dir = tmp_path.resolve()
    assert (tmp_path / "haproxy-call.log").read_text().strip() == (
        f"run --rm --network none -v {config_dir}:{config_dir}:ro "
        f"haproxy:3.1-alpine haproxy -c -f {config_dir / 'candidate.cfg'}"
    )


@pytest.mark.parametrize(
    "source",
    [STEADY_CONFIG, S1_CONFIG],
    ids=["steady", "s1"],
)
def test_haproxy_check_accepts_current_rendered_shiftgate_configs(
    tmp_path: Path, source: Path
) -> None:
    result = _haproxy_check(tmp_path, _rendered_shiftgate_config(source))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("api_port", [8001, 8002])
@pytest.mark.parametrize("mcp_port", [8766, 8767])
def test_haproxy_check_scopes_every_legacy_color_to_s1_backend_names(
    tmp_path: Path, api_port: int, mcp_port: int
) -> None:
    result = _haproxy_check(
        tmp_path,
        _rendered_shiftgate_config(
            S1_CONFIG,
            legacy_upstream_ports={
                "at-api-legacy": api_port,
                "at-mcp-legacy": mcp_port,
            },
        ),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("mutation", "scope", "offending_token"),
    [
        (
            lambda text: text.replace("bind 127.0.0.1:8000", "bind 127.0.0.1:8001", 1),
            "frontend ft_at_api",
            "127.0.0.1:8001",
        ),
        (
            lambda text: text.replace(
                "    bind 127.0.0.1:8000", "bind 127.0.0.1:8001", 1
            ),
            "frontend ft_at_api",
            "127.0.0.1:8001",
        ),
        (
            lambda text: text.replace("bind 127.0.0.1:8765", "bind 127.0.0.1:8766", 1),
            "frontend ft_at_mcp",
            "127.0.0.1:8766",
        ),
        (
            lambda text: text.replace(
                "server blue 127.0.0.1:18000 check",
                "server blue 127.0.0.1:18001 check",
                1,
            ),
            "backend bk_at_api",
            "127.0.0.1:18001",
        ),
        (
            lambda text: text.replace(
                "server blue 127.0.0.1:18000 check",
                "server blue 127.0.0.1:18000.* check",
                1,
            ),
            "backend bk_at_api",
            "127.0.0.1:18000.*",
        ),
        (
            lambda text: text.replace(
                "    server blue 127.0.0.1:18000 check",
                "server blue 127.0.0.1:18001 check",
                1,
            ),
            "backend bk_at_api",
            "127.0.0.1:18001",
        ),
        (
            lambda text: text.replace(
                "server blue 127.0.0.1:18765 check",
                "server blue 127.0.0.1:18766 check",
                1,
            ),
            "backend bk_at_mcp",
            "127.0.0.1:18766",
        ),
        (
            lambda text: text.replace(
                "server blue 127.0.0.1:18000 check",
                "server blue 127.0.0.1:8001 check",
                1,
            ),
            "backend bk_at_api",
            "127.0.0.1:8001",
        ),
        (
            lambda text: text.replace(
                "server blue 127.0.0.1:18765 check",
                "server blue 127.0.0.1:8766 check",
                1,
            ),
            "backend bk_at_mcp",
            "127.0.0.1:8766",
        ),
    ],
    ids=[
        "api-bind-outside-d9",
        "api-unindented-bind-outside-d9",
        "mcp-bind-outside-d9",
        "api-server-outside-d9",
        "server-port-regex-metacharacters",
        "api-unindented-server-outside-d9",
        "mcp-server-outside-d9",
        "legacy-port-outside-s1",
        "legacy-port-outside-mcp-backend",
    ],
)
def test_haproxy_check_rejects_service_port_outside_scoped_set_and_names_line(
    tmp_path: Path, mutation, scope: str, offending_token: str
) -> None:
    candidate = mutation(_rendered_shiftgate_config(STEADY_CONFIG))
    offending_line = next(
        number
        for number, line in enumerate(candidate.splitlines(), start=1)
        if offending_token in line
    )

    result = _haproxy_check(tmp_path, candidate)

    assert result.returncode == 78
    assert f"outside allowed set for {scope} on line {offending_line}" in result.stderr
    assert not (tmp_path / "haproxy-call.log").exists()


@pytest.mark.parametrize("port", sorted(RETIRED_PORTS))
@pytest.mark.parametrize("in_comment", [False, True], ids=["directive", "comment"])
def test_haproxy_check_rejects_retired_port_and_names_line(
    tmp_path: Path, port: int, in_comment: bool
) -> None:
    if in_comment:
        candidate = VALID_HAPROXY.replace(
            "frontend ft_at_api", f"# retired endpoint {port}\nfrontend ft_at_api"
        )
        offending_text = f"# retired endpoint {port}"
    else:
        candidate = VALID_HAPROXY.replace(
            "option httpchk GET /healthz",
            f"option httpchk GET /healthz/{port}",
        )
        offending_text = f"/healthz/{port}"
    offending_line = next(
        number
        for number, line in enumerate(candidate.splitlines(), start=1)
        if offending_text in line
    )

    result = _haproxy_check(tmp_path, candidate)

    assert result.returncode == 78
    assert f"retired port {port} on line {offending_line}" in result.stderr
    assert not (tmp_path / "haproxy-call.log").exists()


def test_haproxy_check_validates_with_the_deploy_scripts_pinned_image() -> None:
    deploy = (ROOT / "scripts" / "deploy-ncp-pull.sh").read_text()
    check = CHECK_SCRIPT.read_text()
    match = re.search(r'^readonly HAPROXY_IMAGE="([^"]+)"$', deploy, re.MULTILINE)
    assert match is not None
    assert f'readonly HAPROXY_IMAGE="{match.group(1)}"' in check
    # The NCP host has no haproxy binary; the check must not depend on one.
    assert "exec haproxy" not in check
    assert "command -v haproxy" not in check


def test_haproxy_check_rejects_unsubstituted_socket_placeholder(tmp_path: Path) -> None:
    candidate = VALID_HAPROXY.replace(
        "/var/run/haproxy/admin_socket.sock",
        "/__LIVE_CHECK_HAPROXY_SOCKET_IN_CONFIG__",
    )
    result = _haproxy_check(tmp_path, candidate)
    assert result.returncode == 78
    assert not (tmp_path / "haproxy-call.log").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(
            "stats socket /var/run/haproxy/admin_socket.sock mode 600 level admin\n", ""
        ),
        lambda text: text.replace(
            "stats socket /var/run/haproxy/admin_socket.sock mode 600 level admin\n",
            "stats socket /var/run/haproxy/admin_socket.sock mode 600 level admin\n"
            "    stats socket /tmp/second.sock mode 600 level admin\n",
        ),
        lambda text: text.replace("bind 127.0.0.1:8000", "bind 0.0.0.0:8000"),
        lambda text: text.replace("bind 100.122.100.56:8000\n", ""),
        lambda text: text.replace(
            "    server blue 127.0.0.1:18000 check",
            "    server blue 203.0.113.10:18000 check",
        ),
        lambda text: text.replace(
            "    log stdout format raw local0",
            "    log 203.0.113.10:514 local0",
        ),
        lambda text: text.replace(
            "    server blue 127.0.0.1:18000 check",
            "    server blue 127.0.0.1:18000 check addr 203.0.113.10",
        ),
        lambda text: text.replace(
            "    server green 127.0.0.1:28000 check disabled",
            "    server green 127.0.0.1:28000 check backup",
        ),
        lambda text: text.replace(
            "defaults\n", "listen unexpected\n    bind 127.0.0.1:8001\n\ndefaults\n"
        ),
    ],
    ids=[
        "missing-socket",
        "extra-socket",
        "wildcard-bind",
        "missing-required-bind",
        "public-server",
        "remote-log",
        "server-extra-arguments",
        "unknown-server-suffix",
        "open-section",
    ],
)
def test_haproxy_check_rejects_outside_closed_shape(tmp_path: Path, mutation) -> None:
    result = _haproxy_check(tmp_path, mutation(VALID_HAPROXY))
    assert result.returncode == 78
    assert not (tmp_path / "haproxy-call.log").exists()
