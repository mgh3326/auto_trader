"""Task 975: dedicated live-* MCP units in the NCP deploy (operator Q-87 A).

Fake Docker/curl only (the stateful harnesses of the rollback and image-prune
suites). Nothing here talks to a real daemon, HAProxy or host.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.live_profile_registration import (
    LIVE_PROFILES,
    load_live_manifest,
)
from tests.mcp_server._registration_recorder import RegistrationRecorder
from tests.scripts import test_deploy_ncp_pull_image_prune as prune
from tests.scripts.test_deploy_ncp_pull_rollback import (
    DEPLOY,
    INITIAL,
    KIS_OLD,
    NEW,
    OLD,
    _mutations,
    _run,
    container_env,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"
TAILNET = "100.122.100.56"
LIVE = {
    # unit name: (MCP_PROFILE, port, token env name)
    "live-kr": ("live-kr", "8773", "MCP_LIVE_KR_AUTH_TOKEN"),
    "live-us": ("live-us", "8774", "MCP_LIVE_US_AUTH_TOKEN"),
    "live-crypto": ("live-crypto", "8775", "MCP_LIVE_CRYPTO_AUTH_TOKEN"),
}
LIVE_UNITS = tuple(f"at-mcp-{name}" for name in LIVE)
# Every other listener this host's deploy already owns: HAProxy API/MCP
# frontends, API colors and MCP colors.
FIXED_NON_UNIT_PORTS = {"8000", "8001", "8002", "8765", "8766", "8767"}


def _array(name: str) -> list[str]:
    match = re.search(
        rf"^declare -a {name}=\((.*)\)$", DEPLOY.read_text(), re.MULTILINE
    )
    assert match, name
    return match.group(1).split()


def _expected(name: str) -> str:
    return KIS_OLD if name == "at-kis-ws" else OLD


def _unit_runs(calls: list[list[str]], unit: str) -> list[list[str]]:
    return [c for c in calls if c[0] == "run" and c[c.index("--name") + 1] == unit]


def _env_of(tmp_path: Path, call: list[str]) -> dict[str, str]:
    # The env the container resolves (#1240: the token arrives in an env file).
    return container_env(tmp_path, call)


# --- static wiring -----------------------------------------------------------


def test_live_units_are_declared_with_profile_port_and_own_token_name() -> None:
    names, profiles = _array("MCP_NAMES"), _array("MCP_PROFILES")
    ports, tokens = _array("MCP_PORTS"), _array("MCP_TOKENS")
    assert len(names) == len(profiles) == len(ports) == len(tokens)
    for name, (profile, port, token) in LIVE.items():
        i = names.index(name)
        assert (profiles[i], ports[i], tokens[i]) == (profile, port, token)
    # #1189 appends the tailnet-only h3-crypto-paper unit after the live trio.
    assert _array("MCP_LIVE_ROUTE_NAMES") == [*LIVE, "h3-crypto-paper"]
    apps = _array("APP_CONTAINERS")
    for name in names:
        assert f"at-mcp-{name}" in apps, name


def test_ports_collide_with_nothing_the_deploy_or_haproxy_uses() -> None:
    ports = _array("MCP_PORTS")
    assert len(set(ports)) == len(ports)
    assert not set(ports) & FIXED_NON_UNIT_PORTS
    template_ports = re.findall(
        r"^\s*bind [0-9.]+:(\d+)\s*$", TEMPLATE.read_text(), re.M
    )
    live_ports = {port for _, port, _ in LIVE.values()}
    # each live port is bound exactly once (its tailnet frontend) ...
    for port in live_ports:
        assert template_ports.count(port) == 1, port
    # ... and no pre-existing frontend or unit shares a live port.
    others = set(template_ports) - live_ports
    assert not others & live_ports
    assert not (set(ports) - live_ports) & live_ports


def test_live_token_names_are_distinct_and_never_the_default_token() -> None:
    tokens = _array("MCP_TOKENS")
    assert len(set(tokens)) == len(tokens)
    assert "MCP_AUTH_TOKEN" not in tokens
    source = DEPLOY.read_text()
    for _, _, token in LIVE.values():
        # names only: no assignment of a literal value anywhere in the script
        assert not re.search(rf"{token}=", source), token


def test_haproxy_template_routes_live_units_tailnet_only() -> None:
    text = TEMPLATE.read_text()
    for name, (_, port, _) in LIVE.items():
        slug = name.replace("-", "_")
        block = re.search(
            rf"frontend ft_mcp_{slug}_tailnet\n(.*?)\n\n", text + "\n\n", re.S
        )
        assert block, name
        assert block.group(1).split("\n") == [
            f"    bind {TAILNET}:{port}",
            f"    default_backend bk_mcp_{slug}",
        ]
        assert f"    server mcp_{slug} 127.0.0.1:{port} check" in text
        assert f"bind 127.0.0.1:{port}" not in text
    for line in text.splitlines():
        if line.strip().startswith("bind"):
            assert re.fullmatch(r"\s*bind (127\.0\.0\.1|100\.122\.100\.56):\d+", line)


# --- boot: each unit's MCP_PROFILE registers exactly its live manifest --------


@pytest.mark.parametrize("name", list(LIVE))
def test_unit_profile_boots_its_own_live_surface_not_default(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    profile_value = _array("MCP_PROFILES")[_array("MCP_NAMES").index(name)]
    profile = resolve_mcp_profile(profile_value)
    assert profile in LIVE_PROFILES and profile.value == LIVE[name][0]
    assert profile is not McpProfile.DEFAULT
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    registered = set(recorder.tools)
    assert registered == set(
        load_live_manifest().spec_for(profile).selected_tool_names()
    )
    default = RegistrationRecorder()
    register_all_tools(cast(Any, default), profile=McpProfile.DEFAULT)
    assert registered < set(default.tools)
    others = [p for p in LIVE_PROFILES if p is not profile]
    for other in others:
        peer = RegistrationRecorder()
        register_all_tools(cast(Any, peer), profile=other)
        assert set(peer.tools) != registered, other


# --- deploy: first introduction (units absent) --------------------------------


def test_first_deploy_starts_each_live_unit_with_its_profile_port_and_token(
    tmp_path: Path,
) -> None:
    result, calls, state, run_dir = _run(tmp_path, absent_names=LIVE_UNITS)
    assert result.returncode == 0, result.stderr
    for name, (profile, port, token) in LIVE.items():
        unit = f"at-mcp-{name}"
        runs = _unit_runs(calls, unit)
        assert len(runs) == 1, unit
        env = _env_of(tmp_path, runs[0])
        assert env["MCP_PROFILE"] == profile
        assert env["MCP_PORT"] == port
        assert env["MCP_HOST"] == "127.0.0.1"
        assert env["MCP_TYPE"] == "streamable-http"
        assert env["MCP_AUTH_TOKEN"] == f"tok-{token}"
        assert "ORDER_APPROVAL_HASH_MODE" not in env
        assert "--network" in runs[0] and "host" in runs[0]
        assert not {"-p", "--publish", "-P"} & set(runs[0])
        assert runs[0][-4:] == [NEW, "python", "-m", "app.mcp_server.main"]
        assert state[unit] == NEW
        assert f"{unit}\t{NEW}\t{NEW}\tMATCH" in result.stdout
    cfg = (run_dir / "haproxy.cfg").read_text()
    for _, port, _ in LIVE.values():
        assert f"bind {TAILNET}:{port}" in cfg
    urls = (tmp_path / "curl-urls.log").read_text().splitlines()
    for _, port, _ in LIVE.values():
        assert f"http://127.0.0.1:{port}/health" in urls
        assert f"http://{TAILNET}:{port}/health" in urls


@pytest.mark.parametrize("name", list(LIVE))
def test_missing_live_token_fails_closed_before_pull_or_mutation(
    tmp_path: Path, name: str
) -> None:
    token = LIVE[name][2]
    result, calls, state, _ = _run(tmp_path, omit_tokens=(token,))
    assert result.returncode == 78
    assert f"{token} is required" in result.stderr
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )
    assert all(state[unit] == _expected(unit) for unit in INITIAL)


def test_skipped_live_units_need_no_token_and_are_not_touched(
    tmp_path: Path,
) -> None:
    result, calls, state, _ = _run(
        tmp_path,
        absent_names=LIVE_UNITS,
        omit_tokens=tuple(token for _, _, token in LIVE.values()),
        extra_env={"MCP_UNITS_SKIP": "live-kr,live-us,live-crypto"},
    )
    assert result.returncode == 0, result.stderr
    for unit in LIVE_UNITS:
        assert unit not in state
        assert _mutations(calls, unit) == []
    urls = (tmp_path / "curl-urls.log").read_text().splitlines()
    assert not [
        url
        for url in urls
        if ":877" in url and ("8773" in url or "8774" in url or "8775" in url)
    ]


# --- deploy: a failure in one live unit fails the deploy like other MCP units --


@pytest.mark.parametrize("absent", [False, True], ids=["replace", "introduce"])
@pytest.mark.parametrize("unit", LIVE_UNITS)
def test_live_unit_start_failure_rolls_everything_back(
    tmp_path: Path, unit: str, absent: bool
) -> None:
    result, _, state, run_dir = _run(
        tmp_path, fail_name=unit, absent_names=LIVE_UNITS if absent else ()
    )
    assert result.returncode != 0
    for name in INITIAL:
        if absent and name in LIVE_UNITS:
            assert name not in state, name
        else:
            assert state[name] == _expected(name), name
    assert "at-api-green" not in state and "at-mcp-green" not in state
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"
    assert "MISMATCH" not in result.stdout.rsplit("container\texpected", 1)[-1]


@pytest.mark.parametrize("name", list(LIVE))
def test_live_unit_health_failure_rolls_everything_back(
    tmp_path: Path, name: str
) -> None:
    port = LIVE[name][1]
    result, _, state, _ = _run(tmp_path, extra_env={"FAKE_FAIL_HEALTH_PORT": port})
    assert result.returncode != 0
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


@pytest.mark.parametrize("name", list(LIVE))
def test_live_route_not_ready_after_reload_rolls_back_the_switch(
    tmp_path: Path, name: str
) -> None:
    port = LIVE[name][1]
    result, _, state, run_dir = _run(
        tmp_path, fail_route_url=f"http://{TAILNET}:{port}/health"
    )
    assert result.returncode != 0
    assert (
        f"haproxy live MCP route not ready after reload: at-mcp-{name}" in result.stderr
    )
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    assert "at-mcp-green" not in state
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"
    assert (run_dir / "api-active-color").read_text() == "blue\n"


def test_success_path_digest_mismatch_on_a_live_unit_rolls_back(
    tmp_path: Path,
) -> None:
    result, _, state, _ = _run(tmp_path, success_mismatch_name="at-mcp-live-us")
    assert result.returncode != 0
    assert "deployment digest mismatch" in result.stderr
    assert f"at-mcp-live-us\t{NEW}\t{OLD}\tMISMATCH" in result.stdout
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


def test_manual_rollback_restores_live_units_to_the_previous_digest(
    tmp_path: Path,
) -> None:
    # The harness records NEW as deployed-digest.previous for --rollback.
    result, calls, state, _ = _run(tmp_path, args=("--rollback",))
    assert result.returncode == 0, result.stderr
    for name, (profile, port, token) in LIVE.items():
        unit = f"at-mcp-{name}"
        assert state[unit] == NEW
        env = _env_of(tmp_path, _unit_runs(calls, unit)[-1])
        assert (env["MCP_PROFILE"], env["MCP_PORT"]) == (profile, port)
        assert env["MCP_AUTH_TOKEN"] == f"tok-{token}"
        assert f"{unit}\t{NEW}\t{NEW}\tMATCH" in result.stdout


def test_rollback_of_a_live_unit_restarts_it_with_its_own_profile(
    tmp_path: Path,
) -> None:
    # at-mcp-live-crypto is the last unit; its failure makes the rollback
    # restore live-kr and live-us from the captured OLD digest.
    result, calls, state, _ = _run(tmp_path, fail_name="at-mcp-live-crypto")
    assert result.returncode != 0
    for name in ("live-kr", "live-us", "live-crypto"):
        unit = f"at-mcp-{name}"
        restored = [c for c in _unit_runs(calls, unit) if OLD in c]
        assert restored, unit
        env = _env_of(tmp_path, restored[-1])
        assert (env["MCP_PROFILE"], env["MCP_PORT"]) == LIVE[name][:2]
        assert env["MCP_AUTH_TOKEN"] == f"tok-{LIVE[name][2]}"
        assert state[unit] == OLD


def test_skip_kis_ws_path_is_unaffected_by_live_units(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--skip-kis-ws",))
    assert result.returncode == 0, result.stderr
    assert state["at-kis-ws"] == KIS_OLD
    assert _mutations(calls, "at-kis-ws") == []
    for unit in LIVE_UNITS:
        assert state[unit] == NEW


def test_dry_run_lists_live_units_and_mutates_nothing(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--dry-run",))
    assert result.returncode == 0, result.stderr
    for unit in LIVE_UNITS:
        assert f"  {unit}: promote to" in result.stdout
        assert state[unit] == OLD
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )


# --- HAProxy render guard ------------------------------------------------------


@pytest.mark.parametrize(
    "bind",
    ["*:8773", ":8773", "[::]:8773", "0.0.0.0:8773", "10.0.0.5:8773"],
)
def test_render_refuses_a_public_or_unknown_bind_for_a_live_route(
    tmp_path: Path, bind: str
) -> None:
    template = tmp_path / "haproxy.cfg.tmpl"
    template.write_text(
        TEMPLATE.read_text().replace(f"bind {TAILNET}:8773", f"bind {bind}")
    )
    result, _, state, run_dir = _run(
        tmp_path, extra_env={"MCP_HAPROXY_TEMPLATE": str(template)}
    )
    assert result.returncode != 0
    assert "HAProxy binds must be loopback and tailnet only" in result.stderr
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    assert (run_dir / "api-active-color").read_text() == "blue\n"


# --- #934 prune keep-set --------------------------------------------------------


def test_prune_keeps_the_image_the_live_units_run(tmp_path: Path) -> None:
    env = prune.Env(tmp_path)
    result = env.run()
    after = env.state()
    assert result.returncode == 0, result.stderr
    for unit in LIVE_UNITS:
        assert after["containers"][unit]["image"] == prune.NEW_ID
    assert prune.NEW_ID in after["images"]


@pytest.mark.parametrize("unit", LIVE_UNITS)
def test_prune_keeps_a_skipped_live_units_own_image(tmp_path: Path, unit: str) -> None:
    env = prune.Env(tmp_path)
    state = env.state()
    state["containers"][unit] = {
        "image": prune.STALE_ID,
        "config": prune.STALE,
        "running": True,
    }
    env.state_path.write_text(json.dumps(state))
    before = env.state()
    result = env.run(env={"MCP_UNITS_SKIP": unit.removeprefix("at-mcp-")})
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert f"{unit}\t{prune.STALE}\t{prune.STALE}\tMATCH" in result.stdout
    assert after["containers"][unit]["image"] == prune.STALE_ID
    assert (
        prune._keep_violations(
            before,
            after,
            env.calls(),
            prune._deploy_keep_set(previous=prune.PREVX_ID) | {prune.STALE_ID},
        )
        == []
    )
