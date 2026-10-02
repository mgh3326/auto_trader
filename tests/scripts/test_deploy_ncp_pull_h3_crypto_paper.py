"""#1189 (part C of #1171): the at-mcp-h3-crypto-paper unit in the NCP deploy.

Fake Docker/curl only (the stateful harnesses of the rollback and image-prune
suites). Nothing here talks to a real daemon, HAProxy or host.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, require_mcp_profile
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.h3_crypto_paper_registration import (
    H3_CRYPTO_PAPER_TOOL_NAMES,
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

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"
TAILNET = "100.122.100.56"
NAME = "h3-crypto-paper"
UNIT = "at-mcp-h3-crypto-paper"
PROFILE = "h3-crypto-paper"
PORT = "8776"
TOKEN = "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN"

# The arrays exactly as they were before #1189: the existing units must stay
# byte-identical, in order, with the new unit appended after them.
BEFORE = {
    "MCP_NAMES": "analysis-readonly account-read tradingcodex-execution paper-001 "
    "kiwoom live-kr live-us live-crypto",
    "MCP_PROFILES": "analysis_readonly account_read tradingcodex_execution "
    "hermes-paper-kis kiwoom live-kr live-us live-crypto",
    "MCP_PORTS": "8768 8769 8770 8771 8772 8773 8774 8775",
    "MCP_TOKENS": "MCP_ANALYSIS_READONLY_AUTH_TOKEN MCP_ACCOUNT_READ_AUTH_TOKEN "
    "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN MCP_PAPER_001_AUTH_TOKEN "
    "MCP_KIWOOM_AUTH_TOKEN MCP_LIVE_KR_AUTH_TOKEN MCP_LIVE_US_AUTH_TOKEN "
    "MCP_LIVE_CRYPTO_AUTH_TOKEN",
    "MCP_LIVE_ROUTE_NAMES": "live-kr live-us live-crypto",
    "APP_CONTAINERS": "at-api at-api-blue at-api-green at-worker at-worker-new "
    "at-scheduler at-upbit-ws at-kis-ws at-mcp-blue at-mcp-green "
    "at-mcp-analysis-readonly at-mcp-account-read at-mcp-tradingcodex-execution "
    "at-mcp-paper-001 at-mcp-kiwoom at-mcp-live-kr at-mcp-live-us "
    "at-mcp-live-crypto",
}
APPENDED = {
    "MCP_NAMES": NAME,
    "MCP_PROFILES": PROFILE,
    "MCP_PORTS": PORT,
    "MCP_TOKENS": TOKEN,
    "MCP_LIVE_ROUTE_NAMES": NAME,
    "APP_CONTAINERS": UNIT,
}
# Existing units and the environment their containers received before #1189.
EXISTING = {
    "analysis-readonly": ("analysis_readonly", "8768"),
    "account-read": ("account_read", "8769"),
    "tradingcodex-execution": ("tradingcodex_execution", "8770"),
    "paper-001": ("hermes-paper-kis", "8771"),
    "kiwoom": ("kiwoom", "8772"),
    "live-kr": ("live-kr", "8773"),
    "live-us": ("live-us", "8774"),
    "live-crypto": ("live-crypto", "8775"),
}


def _source() -> str:
    return DEPLOY.read_text()


def _array_text(name: str, source: str | None = None) -> str:
    match = re.search(
        rf"^declare -a {name}=\((.*)\)$", source or _source(), re.MULTILINE
    )
    assert match, name
    return match.group(1)


def _expected(name: str) -> str:
    return KIS_OLD if name == "at-kis-ws" else OLD


def _unit_runs(calls: list[list[str]], unit: str) -> list[list[str]]:
    return [c for c in calls if c[0] == "run" and c[c.index("--name") + 1] == unit]


def _env_of(tmp_path: Path, call: list[str]) -> dict[str, str]:
    # The env the container resolves (#1240: the token arrives in an env file).
    return container_env(tmp_path, call)


def _mutant(tmp_path: Path, old: str, new: str) -> Path:
    source = _source()
    assert source.count(old) == 1, old
    path = tmp_path / "deploy-mutant.sh"
    path.write_text(source.replace(old, new))
    path.chmod(0o755)
    return path


# --- A1: static wiring ---------------------------------------------------------


def test_unit_is_declared_with_exactly_its_profile_port_and_token() -> None:
    names = _array_text("MCP_NAMES").split()
    profiles = _array_text("MCP_PROFILES").split()
    ports = _array_text("MCP_PORTS").split()
    tokens = _array_text("MCP_TOKENS").split()
    assert len(names) == len(profiles) == len(ports) == len(tokens)
    i = names.index(NAME)
    assert (profiles[i], ports[i], tokens[i]) == (PROFILE, PORT, TOKEN)
    assert names.count(NAME) == ports.count(PORT) == tokens.count(TOKEN) == 1
    assert require_mcp_profile(profiles[i]) is McpProfile.H3_CRYPTO_PAPER
    assert UNIT in _array_text("APP_CONTAINERS").split()


@pytest.mark.parametrize("array", list(BEFORE))
def test_existing_units_are_byte_identical_and_the_unit_is_appended(
    array: str,
) -> None:
    assert _array_text(array) == f"{BEFORE[array]} {APPENDED[array]}"


def test_port_collides_with_nothing_and_token_is_never_assigned() -> None:
    ports = _array_text("MCP_PORTS").split()
    assert len(set(ports)) == len(ports)
    assert PORT not in {"8000", "8001", "8002", "8765", "8766", "8767"}
    tokens = _array_text("MCP_TOKENS").split()
    assert len(set(tokens)) == len(tokens) and "MCP_AUTH_TOKEN" not in tokens
    # the token is referenced by name only, never assigned a literal value
    assert not re.search(rf"{TOKEN}=", _source())


# --- A3: HAProxy ---------------------------------------------------------------


def test_haproxy_frontend_is_tailnet_only_and_shaped_like_the_others() -> None:
    text = TEMPLATE.read_text()
    slug = NAME.replace("-", "_")

    def block(kind: str, ident: str) -> list[str]:
        match = re.search(rf"^{kind} {ident}\n(.*?)(?:\n\n|\Z)", text, re.S | re.M)
        assert match, ident
        return match.group(1).rstrip("\n").split("\n")

    assert block("frontend", f"ft_mcp_{slug}_tailnet") == [
        f"    bind {TAILNET}:{PORT}",
        f"    default_backend bk_mcp_{slug}",
    ]
    # same backend shape as an existing fixed unit, with only names/port swapped
    reference = block("backend", "bk_mcp_live_crypto")
    assert block("backend", f"bk_mcp_{slug}") == [
        line.replace("live_crypto", slug).replace(":8775", f":{PORT}")
        for line in reference
    ]
    binds = re.findall(r"^\s*bind (\S+)\s*$", text, re.M)
    assert [b for b in binds if b.endswith(f":{PORT}")] == [f"{TAILNET}:{PORT}"]
    for line in text.splitlines():
        if line.strip().startswith("bind"):
            assert re.fullmatch(r"\s*bind (127\.0\.0\.1|100\.122\.100\.56):\d+", line)


@pytest.mark.parametrize(
    "bind", ["*:8776", ":8776", "[::]:8776", "0.0.0.0:8776", "10.0.0.5:8776"]
)
def test_render_refuses_a_public_or_unknown_bind_for_the_unit(
    tmp_path: Path, bind: str
) -> None:
    template = tmp_path / "haproxy.cfg.tmpl"
    template.write_text(
        TEMPLATE.read_text().replace(f"bind {TAILNET}:{PORT}", f"bind {bind}")
    )
    result, _, state, run_dir = _run(
        tmp_path, extra_env={"MCP_HAPROXY_TEMPLATE": str(template)}
    )
    assert result.returncode != 0
    assert "HAProxy binds must be loopback and tailnet only" in result.stderr
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


# --- boot: the unit's MCP_PROFILE registers exactly the H3 allowlist ----------


def test_unit_profile_boots_the_h3_surface_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    names = _array_text("MCP_NAMES").split()
    value = _array_text("MCP_PROFILES").split()[names.index(NAME)]
    profile = require_mcp_profile(value)
    assert profile is McpProfile.H3_CRYPTO_PAPER
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    assert set(recorder.tools) == set(H3_CRYPTO_PAPER_TOOL_NAMES)
    default = RegistrationRecorder()
    register_all_tools(cast(Any, default), profile=McpProfile.DEFAULT)
    assert set(recorder.tools) < set(default.tools)


# --- deploy: introduction and replacement --------------------------------------


@pytest.mark.parametrize("absent", [True, False], ids=["introduce", "replace"])
def test_deploy_starts_the_unit_with_its_profile_port_and_token(
    tmp_path: Path, absent: bool
) -> None:
    result, calls, state, run_dir = _run(
        tmp_path, absent_names=(UNIT,) if absent else ()
    )
    assert result.returncode == 0, result.stderr
    runs = _unit_runs(calls, UNIT)
    assert len(runs) == 1
    env = _env_of(tmp_path, runs[0])
    assert env["MCP_PROFILE"] == PROFILE
    assert env["MCP_PORT"] == PORT
    assert env["MCP_HOST"] == "127.0.0.1"
    assert env["MCP_TYPE"] == "streamable-http"
    assert env["MCP_AUTH_TOKEN"] == f"tok-{TOKEN}"
    assert "ORDER_APPROVAL_HASH_MODE" not in env
    assert not {"-p", "--publish", "-P"} & set(runs[0])
    # only the DEFAULT colors mount the host machine-id (#1046)
    assert "/etc/machine-id:/etc/machine-id:ro" not in runs[0]
    assert runs[0][-4:] == [NEW, "python", "-m", "app.mcp_server.main"]
    assert state[UNIT] == NEW
    assert f"{UNIT}\t{NEW}\t{NEW}\tMATCH" in result.stdout
    assert f"bind {TAILNET}:{PORT}" in (run_dir / "haproxy.cfg").read_text()
    urls = (tmp_path / "curl-urls.log").read_text().splitlines()
    assert f"http://127.0.0.1:{PORT}/health" in urls
    assert f"http://{TAILNET}:{PORT}/health" in urls


def test_existing_units_receive_the_same_environment_as_before(
    tmp_path: Path,
) -> None:
    result, calls, _, _ = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    for name, (profile, port) in EXISTING.items():
        (run,) = _unit_runs(calls, f"at-mcp-{name}")
        env = _env_of(tmp_path, run)
        assert (env["MCP_PROFILE"], env["MCP_PORT"]) == (profile, port), name
    for color in ("at-mcp-green",):
        (run,) = _unit_runs(calls, color)
        assert _env_of(tmp_path, run)["MCP_PROFILE"] == "default"


def test_missing_token_fails_closed_before_pull_or_mutation(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, omit_tokens=(TOKEN,))
    assert result.returncode == 78
    assert f"{TOKEN} is required" in result.stderr
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


def test_token_value_is_never_printed(tmp_path: Path) -> None:
    result, _, _, _ = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"tok-{TOKEN}" not in result.stdout + result.stderr


def test_skipped_unit_needs_no_token_and_is_not_touched(tmp_path: Path) -> None:
    result, calls, state, _ = _run(
        tmp_path,
        absent_names=(UNIT,),
        omit_tokens=(TOKEN,),
        extra_env={"MCP_UNITS_SKIP": NAME},
    )
    assert result.returncode == 0, result.stderr
    assert UNIT not in state
    assert _mutations(calls, UNIT) == []
    urls = (tmp_path / "curl-urls.log").read_text().splitlines()
    assert not [url for url in urls if f":{PORT}/" in url]


# --- A2: rollback --------------------------------------------------------------


@pytest.mark.parametrize("absent", [False, True], ids=["replace", "introduce"])
def test_unit_start_failure_rolls_everything_back(tmp_path: Path, absent: bool) -> None:
    result, _, state, run_dir = _run(
        tmp_path, fail_name=UNIT, absent_names=(UNIT,) if absent else ()
    )
    assert result.returncode != 0
    for name in INITIAL:
        if absent and name == UNIT:
            # introduced and failed: rollback leaves no container behind
            assert name not in state
        else:
            assert state[name] == _expected(name), name
    assert "at-api-green" not in state and "at-mcp-green" not in state
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"
    assert "MISMATCH" not in result.stdout.rsplit("container\texpected", 1)[-1]


def test_unit_health_failure_rolls_everything_back(tmp_path: Path) -> None:
    result, _, state, _ = _run(tmp_path, extra_env={"FAKE_FAIL_HEALTH_PORT": PORT})
    assert result.returncode != 0
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


def test_route_not_ready_after_reload_rolls_back_the_switch(tmp_path: Path) -> None:
    result, _, state, run_dir = _run(
        tmp_path, fail_route_url=f"http://{TAILNET}:{PORT}/health"
    )
    assert result.returncode != 0
    assert f"haproxy live MCP route not ready after reload: {UNIT}" in result.stderr
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    assert "at-mcp-green" not in state
    assert (run_dir / "mcp-active-color").read_text() == "blue\n"


def test_success_path_digest_mismatch_on_the_unit_rolls_back(tmp_path: Path) -> None:
    result, _, state, _ = _run(tmp_path, success_mismatch_name=UNIT)
    assert result.returncode != 0
    assert "deployment digest mismatch" in result.stderr
    assert f"{UNIT}\t{NEW}\t{OLD}\tMISMATCH" in result.stdout
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


def test_later_failure_restores_the_unit_with_its_own_profile(tmp_path: Path) -> None:
    # The unit is the last MCP unit, so fail the HAProxy route probe of an
    # earlier one after every unit started: rollback must restart this unit
    # from OLD with its own profile, port and token.
    result, calls, state, _ = _run(
        tmp_path, fail_route_url=f"http://{TAILNET}:8773/health"
    )
    assert result.returncode != 0
    restored = [c for c in _unit_runs(calls, UNIT) if OLD in c]
    assert restored
    env = _env_of(tmp_path, restored[-1])
    assert (env["MCP_PROFILE"], env["MCP_PORT"]) == (PROFILE, PORT)
    assert env["MCP_AUTH_TOKEN"] == f"tok-{TOKEN}"
    assert state[UNIT] == OLD


def test_manual_rollback_restores_the_unit_to_the_previous_digest(
    tmp_path: Path,
) -> None:
    # The harness records NEW as deployed-digest.previous for --rollback.
    result, calls, state, _ = _run(tmp_path, args=("--rollback",))
    assert result.returncode == 0, result.stderr
    assert state[UNIT] == NEW
    env = _env_of(tmp_path, _unit_runs(calls, UNIT)[-1])
    assert (env["MCP_PROFILE"], env["MCP_PORT"]) == (PROFILE, PORT)
    assert env["MCP_AUTH_TOKEN"] == f"tok-{TOKEN}"
    assert f"{UNIT}\t{NEW}\t{NEW}\tMATCH" in result.stdout


def test_dry_run_lists_the_unit_and_mutates_nothing(tmp_path: Path) -> None:
    result, calls, state, _ = _run(tmp_path, args=("--dry-run",))
    assert result.returncode == 0, result.stderr
    assert f"  {UNIT}: promote to" in result.stdout
    assert state[UNIT] == OLD
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )


# --- A2: #934 image prune keep-set ---------------------------------------------


def test_prune_keeps_the_image_the_unit_runs(tmp_path: Path) -> None:
    env = prune.Env(tmp_path)
    result = env.run()
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert after["containers"][UNIT]["image"] == prune.NEW_ID
    assert prune.NEW_ID in after["images"]


def test_prune_keeps_a_skipped_units_own_image(tmp_path: Path) -> None:
    env = prune.Env(tmp_path)
    state = env.state()
    state["containers"][UNIT] = {
        "image": prune.STALE_ID,
        "config": prune.STALE,
        "running": True,
    }
    env.state_path.write_text(json.dumps(state))
    before = env.state()
    result = env.run(env={"MCP_UNITS_SKIP": NAME})
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert f"{UNIT}\t{prune.STALE}\t{prune.STALE}\tMATCH" in result.stdout
    assert after["containers"][UNIT]["image"] == prune.STALE_ID
    assert (
        prune._keep_violations(
            before,
            after,
            env.calls(),
            prune._deploy_keep_set(previous=prune.PREVX_ID) | {prune.STALE_ID},
        )
        == []
    )


# --- A4: a blank profile never reaches docker ----------------------------------

_PROFILES_LINE = f"declare -a MCP_PROFILES=({BEFORE['MCP_PROFILES']} {PROFILE})"


@pytest.mark.parametrize("blank", ["''", "' '", '"\t"'], ids=["empty", "space", "tab"])
def test_blank_profile_in_the_arrays_refuses_before_pull_or_mutation(
    tmp_path: Path, blank: str
) -> None:
    deploy = _mutant(
        tmp_path,
        _PROFILES_LINE,
        f"declare -a MCP_PROFILES=({BEFORE['MCP_PROFILES']} {blank})",
    )
    result, calls, state, _ = _run(tmp_path, deploy=deploy)
    assert result.returncode == 78
    assert f"MCP_PROFILE is required for {UNIT}" in result.stderr
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


def test_misaligned_arrays_refuse_before_pull_or_mutation(tmp_path: Path) -> None:
    deploy = _mutant(
        tmp_path, _PROFILES_LINE, f"declare -a MCP_PROFILES=({BEFORE['MCP_PROFILES']})"
    )
    result, calls, _, _ = _run(tmp_path, deploy=deploy)
    assert result.returncode == 78
    assert "MCP unit arrays are misaligned" in result.stderr
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )


def test_run_mcp_refuses_a_blank_profile_even_past_the_pre_check(
    tmp_path: Path,
) -> None:
    # Defense in depth: with the pre-check bypassed, run_mcp itself refuses
    # the blank profile, docker never runs the unit, and the deploy rolls back.
    source = _source()
    mutated = source.replace(
        "validate_mcp_units || return $?; validate_mcp_tokens; }",
        "validate_mcp_tokens; }",
    ).replace(_PROFILES_LINE, f"declare -a MCP_PROFILES=({BEFORE['MCP_PROFILES']} '')")
    assert mutated.count("validate_mcp_units") == 1  # definition only
    deploy = tmp_path / "deploy-mutant.sh"
    deploy.write_text(mutated)
    deploy.chmod(0o755)
    result, calls, state, _ = _run(tmp_path, deploy=deploy)
    assert result.returncode != 0
    assert f"MCP_PROFILE is required for {UNIT}" in result.stderr
    # docker never runs the unit, not even from the rollback: restoring it
    # would need the same blank profile, so it stays absent (fail closed)
    # rather than coming up on any other surface.
    assert _unit_runs(calls, UNIT) == []
    assert UNIT not in state
    for unit in INITIAL:
        if unit != UNIT:
            assert state[unit] == _expected(unit), unit
    assert "rollback or digest verification is incomplete" in result.stderr


# --- round 1 findings: closed HAProxy shape, no traced tokens -------------------


_GLOBAL = "global\n    log stdout format raw local0\n"


@pytest.mark.parametrize(
    ("anchor", "line"),
    [
        (_GLOBAL, "    stats socket [::]:8776 v6only level admin\n"),
        (_GLOBAL, "    stats socket 0.0.0.0:8776 level admin\n"),
        (_GLOBAL, "    stats socket /run/haproxy.sock mode 600\n"),
        ("defaults\n", "    stats enable\n"),
        ("", "listen ls_open\n    bind 100.122.100.56:8776\n\n"),
        ("", "peers mypeers\n    peer local [::]:8777\n\n"),
        ("", "userlist ops\n    user admin insecure-password x\n\n"),
        (
            "    server mcp_h3_crypto_paper 127.0.0.1:8776 check\n",
            "    server extra 203.0.113.10:8776 check\n",
        ),
    ],
    ids=[
        "stats-socket-ipv6",
        "stats-socket-ipv4",
        "stats-socket-unix",
        "stats-enable",
        "listen-section",
        "peers-section",
        "userlist-section",
        "non-loopback-server",
    ],
)
def test_render_refuses_any_listener_outside_the_closed_shape(
    tmp_path: Path, anchor: str, line: str
) -> None:
    text = TEMPLATE.read_text()
    if anchor:
        assert text.count(anchor) == 1
        mutated = text.replace(anchor, anchor + line)
    else:
        mutated = text + "\n" + line
    template = tmp_path / "haproxy.cfg.tmpl"
    template.write_text(mutated)
    result, calls, state, run_dir = _run(
        tmp_path, extra_env={"MCP_HAPROXY_TEMPLATE": str(template)}
    )
    assert result.returncode != 0
    assert "HAProxy binds must be loopback and tailnet only" in result.stderr
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    cfg = run_dir / "haproxy.cfg"
    assert not cfg.exists() or line.strip() not in cfg.read_text()


def _traced(tmp_path: Path, how: str) -> Path:
    wrapper = tmp_path / "deploy-traced.sh"
    if how == "bash-x":
        body = f'exec bash -x "{DEPLOY}" "$@"\n'
    elif how.startswith("bash-env-"):
        # A BASH_ENV startup file whose trap turns tracing back on after the
        # script's own set +x: DEBUG (builder self-check after round 1), CHLD
        # (round 2 finding: any child exit re-enables it) and EXIT.
        trap = {
            "bash-env-debug-trap": "trap 'set -x' DEBUG\nset -o functrace\n",
            "bash-env-chld-trap": "trap 'set -x' CHLD\n",
            "bash-env-exit-trap": "trap 'set -x' EXIT\n",
        }[how]
        env_file = tmp_path / "bash-env.sh"
        env_file.write_text(trap)
        body = f'export BASH_ENV="{env_file}"\nexec bash "{DEPLOY}" "$@"\n'
    else:
        body = f'export SHELLOPTS\nset -o xtrace\nexec bash "{DEPLOY}" "$@"\n'
    wrapper.write_text("#!/usr/bin/env bash\n" + body)
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.parametrize(
    "how",
    [
        "bash-x",
        "shellopts",
        "bash-env-debug-trap",
        "bash-env-chld-trap",
        "bash-env-exit-trap",
    ],
)
@pytest.mark.parametrize("args", [(), ("--rollback",)], ids=["deploy", "rollback"])
def test_inherited_tracing_never_prints_a_token(
    tmp_path: Path, how: str, args: tuple[str, ...]
) -> None:
    result, _, state, _ = _run(tmp_path, args=args, deploy=_traced(tmp_path, how))
    assert result.returncode == 0, result.stderr
    assert "xtrace disabled: this script handles MCP tokens" in result.stderr
    output = result.stdout + result.stderr
    assert "tok-MCP_" not in output  # no unit's synthetic token, H3 included
    assert state[UNIT] == NEW


def test_inherited_tracing_never_prints_a_token_on_failure(tmp_path: Path) -> None:
    result, _, _, _ = _run(tmp_path, fail_name=UNIT, deploy=_traced(tmp_path, "bash-x"))
    assert result.returncode != 0
    assert "tok-MCP_" not in result.stdout + result.stderr


@pytest.mark.parametrize("ignored", ["HUP", "PIPE", "HUP PIPE INT"])
def test_signals_ignored_at_entry_neither_re_exec_nor_loop(
    tmp_path: Path, ignored: str
) -> None:
    # nohup and some service managers start the script with signals ignored;
    # those list as trap -- '' and run no code, so the clean-shell re-exec
    # must not fire (and so cannot loop) and the deploy runs once, untraced.
    wrapper = tmp_path / "deploy-ignored.sh"
    wrapper.write_text(
        f'#!/usr/bin/env bash\ntrap \'\' {ignored}\nexec bash "{DEPLOY}" "$@"\n'
    )
    wrapper.chmod(0o755)
    result, calls, state, _ = _run(tmp_path, deploy=wrapper)
    assert result.returncode == 0, result.stderr
    assert "clean shell" not in result.stderr
    assert "tok-MCP_" not in result.stdout + result.stderr
    assert len([c for c in calls if c[0] == "pull"]) == 1
    assert state[UNIT] == NEW


def test_any_startup_file_forces_the_clean_re_exec(tmp_path: Path) -> None:
    # A BASH_ENV startup file ran arbitrary code before the script; even one
    # that set no trap and no option is not trusted. The clean shell must not
    # read it again.
    marker = tmp_path / "bash-env-ran.log"
    env_file = tmp_path / "bash-env.sh"
    env_file.write_text(f'printf "ran\\n" >> "{marker}"\n')
    wrapper = tmp_path / "deploy-bash-env.sh"
    wrapper.write_text(
        f'#!/usr/bin/env bash\nexport BASH_ENV="{env_file}"\nexec bash "{DEPLOY}" "$@"\n'
    )
    wrapper.chmod(0o755)
    result, _, state, _ = _run(tmp_path, deploy=wrapper)
    assert result.returncode == 0, result.stderr
    assert "re-running in a clean shell" in result.stderr
    # read once by the first shell, never by the clean one or its children
    assert marker.read_text() == "ran\n"
    assert state[UNIT] == NEW


def test_sourced_into_a_shell_holding_a_trap_re_execs_clean(tmp_path: Path) -> None:
    # The round 2 reproduction shape: the script's code runs in a shell that
    # already holds a CHLD trap re-enabling xtrace, with no BASH_ENV involved.
    # The trap scan alone must force the clean re-exec of this very file.
    wrapper = tmp_path / "deploy-sourced.sh"
    wrapper.write_text(
        f'#!/usr/bin/env bash\ntrap \'set -x\' CHLD\nsource "{DEPLOY}" "$@"\n'
    )
    wrapper.chmod(0o755)
    result, calls, state, _ = _run(tmp_path, deploy=wrapper)
    assert result.returncode == 0, result.stderr
    assert "re-running in a clean shell" in result.stderr
    assert "tok-MCP_" not in result.stdout + result.stderr
    assert len([c for c in calls if c[0] == "pull"]) == 1
    assert state[UNIT] == NEW
