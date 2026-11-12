from __future__ import annotations

import os
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


@pytest.mark.parametrize(("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30])
def test_all_service_binds_use_only_committed_private_addresses(variant, config) -> None:
    del variant
    for service in config["services"]:
        for bind in service.get("binds", []):
            address, port = bind.rsplit(":", 1)
            assert address in ALLOWED_ADDRESSES
            assert 1 <= int(port) <= 65535


@pytest.mark.parametrize(("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30])
def test_no_wildcard_bind_is_allowed(variant, config) -> None:
    del variant
    for service in config["services"]:
        for bind in service.get("binds", []):
            address, _ = bind.rsplit(":", 1)
            assert address not in {"0.0.0.0", "::", "*", ""}


@pytest.mark.parametrize(("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30])
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


@pytest.mark.parametrize(("variant", "config"), _all_configs(), ids=lambda v: str(v)[:30])
def test_retired_ports_are_unused(variant, config) -> None:
    del variant
    assert RETIRED_PORTS.isdisjoint(_ports_in_config(config))


def _haproxy_check(tmp_path: Path, config_text: str) -> subprocess.CompletedProcess[str]:
    config_file = tmp_path / "candidate.cfg"
    config_file.write_text(config_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_haproxy = bin_dir / "haproxy"
    fake_haproxy.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$HAPROXY_CALL_LOG\"\n")
    fake_haproxy.chmod(0o755)
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
    stats socket /var/run/haproxy/admin.sock mode 600 level admin

defaults
    log global
    mode http
    option dontlognull
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    timeout tunnel 1h
    timeout http-request 10s

frontend api
    mode http
    bind 127.0.0.1:8000
    bind 100.122.100.56:8000
    default_backend api_backend

backend api_backend
    mode http
    option httpchk GET /healthz
    http-check expect status 200
    default-server inter 5s fall 2 rise 1
    server api_blue 127.0.0.1:18000 check
"""


def test_haproxy_check_accepts_closed_shape_socket_and_required_binds(tmp_path: Path) -> None:
    result = _haproxy_check(tmp_path, VALID_HAPROXY)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "haproxy-call.log").read_text().strip() == "-c -f " + str(
        tmp_path / "candidate.cfg"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("stats socket /var/run/haproxy/admin.sock mode 600 level admin\n", ""),
        lambda text: text.replace(
            "stats socket /var/run/haproxy/admin.sock mode 600 level admin\n",
            "stats socket /var/run/haproxy/admin.sock mode 600 level admin\n"
            "    stats socket /tmp/second.sock mode 600 level admin\n",
        ),
        lambda text: text.replace(
            "bind 127.0.0.1:8000", "bind 0.0.0.0:8000"
        ),
        lambda text: text.replace("bind 100.122.100.56:8000\n", ""),
        lambda text: text.replace(
            "    server api_blue 127.0.0.1:18000 check",
            "    server api_blue 203.0.113.10:18000 check",
        ),
        lambda text: text.replace(
            "defaults\n", "listen unexpected\n    bind 127.0.0.1:8001\n\ndefaults\n"
        ),
    ],
    ids=["missing-socket", "extra-socket", "wildcard-bind", "missing-required-bind", "public-server", "open-section"],
)
def test_haproxy_check_rejects_outside_closed_shape(tmp_path: Path, mutation) -> None:
    result = _haproxy_check(tmp_path, mutation(VALID_HAPROXY))
    assert result.returncode == 78
    assert not (tmp_path / "haproxy-call.log").exists()
