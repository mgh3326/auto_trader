"""#1240 (#1189 follow-up, operator hk 1235 A): deploy-ncp-pull.sh hardening.

A1: MCP token values never live in a shell variable or an argv, so no tracing
route can print one. Each route is run against the real script and against a
copy without its clean-shell re-exec header; in the copy tracing is provably
on while every token is handled, and the synthetic token values (tok-MCP_...)
still never appear in stdout, stderr or any docker argv.
A2: the closed HAProxy shape matches each line whole against an allowlist.
A3: every container's resolved env and argv and the rendered HAProxy config
are byte-identical to the pre-#1240 golden capture, apart from how the token
reaches the container.

Fake Docker/curl only. Nothing here talks to a real daemon, HAProxy or host;
every token is a synthetic tok-<NAME> value in a throwaway file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.scripts import _deploy_ncp_pull_golden as golden
from tests.scripts.test_deploy_ncp_pull_rollback import (
    DEPLOY,
    INITIAL,
    KIS_OLD,
    NEW,
    OLD,
    _run,
    container_env,
)

pytestmark = pytest.mark.unit

SENTINEL = "tok-MCP_"
H3_UNIT = "at-mcp-h3-crypto-paper"
TEMPLATE = golden.TEMPLATE

_HEADER = re.compile(
    r"^inherited_code_trap\(\) \{\n.*?^  exec env -u BASH_ENV [^\n]*\nfi\n",
    re.M | re.S,
)


def _without_header(tmp_path: Path, source: str | None = None) -> Path:
    text = source if source is not None else DEPLOY.read_text()
    assert len(_HEADER.findall(text)) == 1
    path = tmp_path / "deploy-no-reexec.sh"
    path.write_text(_HEADER.sub("", text))
    path.chmod(0o755)
    return path


# Every known way to get tracing on around the token handling: r1 (bash -x,
# exported SHELLOPTS), the builder self-check (BASH_ENV DEBUG trap), r2 (CHLD
# trap, also from a sourcing shell), r3 (a one-shot startup file that unsets
# BASH_ENV and leaves a delayed DEBUG trap without functrace), plus EXIT and
# RETURN traps and BASH_XTRACEFD sending the trace to stdout.
ROUTES = {
    "bash-x": ("", 'exec bash -x "$SCRIPT" "$@"'),
    # the trace goes to its own fd and file (fd 1 would corrupt every $(...))
    "xtracefd-file": (
        "",
        'exec 9>>"$TRACE_FILE"\nBASH_XTRACEFD=9 exec bash -x "$SCRIPT" "$@"',
    ),
    "shellopts": ("", 'export SHELLOPTS\nset -o xtrace\nexec bash "$SCRIPT" "$@"'),
    "bash-env-debug-functrace": ("trap 'set -x' DEBUG\nset -o functrace\n", None),
    "bash-env-chld": ("trap 'set -x' CHLD\n", None),
    "bash-env-exit": ("trap 'set -x' EXIT\n", None),
    "bash-env-return-functrace": ("trap 'set -x' RETURN\nset -o functrace\n", None),
    "bash-env-oneshot-debug-r3": (
        "unset BASH_ENV ENV\n"
        'trap \'if [[ "$BASH_COMMAND" == main || "$BASH_COMMAND" == manual_rollback ]];'
        " then set -x; fi' DEBUG\n",
        None,
    ),
    "sourced-chld-r2": ("", 'trap \'set -x\' CHLD\nsource "$SCRIPT" "$@"'),
}
# Routes where xtrace is on before the first token is read in the copy
# without the header; the exit trap only turns it on as the shell ends.
TRACED_DURING_TOKENS = set(ROUTES) - {"bash-env-exit"}


def _wrapper(tmp_path: Path, script: Path, route: str) -> Path:
    startup, body = ROUTES[route]
    lines = [
        "#!/usr/bin/env bash",
        f'SCRIPT="{script}"',
        f'TRACE_FILE="{tmp_path / "xtrace.log"}"',
    ]
    if startup:
        env_file = tmp_path / "bash-env.sh"
        env_file.write_text(startup)
        lines += [f'export BASH_ENV="{env_file}"', 'exec bash "$SCRIPT" "$@"']
    else:
        lines.append(body)
    wrapper = tmp_path / "deploy-traced.sh"
    wrapper.write_text("\n".join(lines) + "\n")
    wrapper.chmod(0o755)
    return wrapper


def _trace_file(tmp_path: Path) -> str:
    path = tmp_path / "xtrace.log"
    return path.read_text() if path.exists() else ""


def _leaks(tmp_path: Path, result: subprocess.CompletedProcess[str]) -> list[str]:
    where = []
    if SENTINEL in result.stdout:
        where.append("stdout")
    if SENTINEL in result.stderr:
        where.append("stderr")
    if SENTINEL in _trace_file(tmp_path):
        where.append("xtrace fd")
    if SENTINEL in (tmp_path / "docker-calls.jsonl").read_text():
        where.append("docker argv")
    return where


def _assert_traced_token_handling(output: str) -> None:
    # xtrace really was on while the H3 token was read and handed to docker.
    assert "+ mcp_token_env_line MCP_H3_CRYPTO_PAPER_AUTH_TOKEN" in output
    assert re.search(
        r"\+ docker run -d --name at-mcp-h3-crypto-paper .*--env-file ", output
    )


# --- A1: no tracing route prints a token ----------------------------------------


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_no_tracing_route_prints_a_token_through_the_real_script(
    tmp_path: Path, route: str
) -> None:
    result, _, state, _ = _run(tmp_path, deploy=_wrapper(tmp_path, DEPLOY, route))
    assert result.returncode == 0, result.stderr
    assert _leaks(tmp_path, result) == []
    assert state[H3_UNIT] == NEW


@pytest.mark.parametrize(
    ("route", "args"),
    [(route, ()) for route in sorted(ROUTES)]
    + [
        (route, ("--rollback",))
        for route in ("bash-x", "bash-env-chld", "bash-env-oneshot-debug-r3")
    ],
    ids=lambda v: "rollback" if v == ("--rollback",) else ("deploy" if v == () else v),
)
def test_no_tracing_route_prints_a_token_even_without_the_re_exec_header(
    tmp_path: Path, route: str, args: tuple[str, ...]
) -> None:
    script = _without_header(tmp_path)
    result, _, state, _ = _run(
        tmp_path, args=args, deploy=_wrapper(tmp_path, script, route)
    )
    assert result.returncode == 0, result.stderr
    assert "clean shell" not in result.stderr
    if route in TRACED_DURING_TOKENS:
        _assert_traced_token_handling(
            result.stdout + result.stderr + _trace_file(tmp_path)
        )
    assert _leaks(tmp_path, result) == []
    assert state[H3_UNIT] == NEW


def test_no_token_is_printed_when_a_traced_unit_start_fails(tmp_path: Path) -> None:
    script = _without_header(tmp_path)
    result, _, state, _ = _run(
        tmp_path, fail_name=H3_UNIT, deploy=_wrapper(tmp_path, script, "bash-x")
    )
    assert result.returncode != 0
    _assert_traced_token_handling(result.stderr)
    assert _leaks(tmp_path, result) == []
    for unit in INITIAL:
        assert state[unit] == (KIS_OLD if unit == "at-kis-ws" else OLD), unit


def test_the_trace_detector_catches_a_token_put_back_in_a_shell_variable(
    tmp_path: Path,
) -> None:
    # Mutant teeth: the pre-#1240 shape (the value captured into a variable)
    # must be visible to exactly the checks above.
    source = DEPLOY.read_text()
    old = 'mcp_token_env_line "$token_env" >"$token_file" ||'
    new = 'token="$(mcp_token_env_line "$token_env")" && printf "%s\\n" "$token" >"$token_file" ||'
    assert source.count(old) == 1
    script = _without_header(tmp_path, source.replace(old, new))
    result, _, _, _ = _run(tmp_path, deploy=_wrapper(tmp_path, script, "bash-x"))
    assert result.returncode == 0, result.stderr
    assert "stderr" in _leaks(tmp_path, result)


def test_token_values_are_read_only_into_redirected_output() -> None:
    source = DEPLOY.read_text()
    assert "env_value" not in source
    assert not re.search(r"MCP_AUTH_TOKEN=\$", source)  # never an -e argv value
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    uses = [m.start() for m in re.finditer(r"\bmcp_token_env_line\b(?!\(\))", code)]
    assert len(uses) == 3  # two presence checks and the run_mcp copy
    for at in uses:
        before, after = code[max(0, at - 3) : at], code[at:]
        # never captured by a command substitution or a pipe; always redirected
        assert "$(" not in before and "`" not in before, after[:80]
        assert re.match(
            r'mcp_token_env_line (MCP_AUTH_TOKEN|"\$\{MCP_TOKENS\[\$i\]\}"|"\$token_env") '
            r'>(/dev/null|"\$token_file") \|\| ',
            after,
        ), after[:80]


def test_the_token_file_is_private_and_removed_on_every_outcome(
    tmp_path: Path,
) -> None:
    # A failed unit start exercises deploy starts, the failing start and every
    # restore; each MCP start gets its own 0600 file, gone once run returns.
    result, calls, _, run_dir = _run(tmp_path, fail_name=H3_UNIT)
    assert result.returncode != 0
    env_log = tmp_path / "docker-calls.jsonl.env"
    modes: dict[str, str] = {}
    for line in env_log.read_text().splitlines():
        modes.update(json.loads(line)["modes"])
    mcp_runs = [
        c
        for c in calls
        if c[0] == "run" and c[c.index("--name") + 1].startswith("at-mcp-")
    ]
    token_files = [
        value
        for call in mcp_runs
        for flag, value in zip(call, call[1:], strict=False)
        if flag == "--env-file" and golden.TOKEN_FILE_MARK in value
    ]
    assert len(token_files) == len(mcp_runs) == len(set(token_files)) > 9
    for path in token_files:
        assert Path(path).parent == run_dir
        assert modes[path] == "0o600"
        assert not Path(path).exists()
    assert not list(run_dir.glob(".mcp-token-env.*"))


def test_each_unit_receives_its_own_token_and_the_default_colors_theirs(
    tmp_path: Path,
) -> None:
    result, calls, _, _ = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    source = DEPLOY.read_text()
    names = re.search(r"^declare -a MCP_NAMES=\((.*)\)$", source, re.M)
    tokens = re.search(r"^declare -a MCP_TOKENS=\((.*)\)$", source, re.M)
    assert names and tokens
    expected = dict(zip(names.group(1).split(), tokens.group(1).split(), strict=True))
    expected["green"] = "MCP_AUTH_TOKEN"
    for name, token in expected.items():
        (run,) = [
            c
            for c in calls
            if c[0] == "run" and c[c.index("--name") + 1] == f"at-mcp-{name}"
        ]
        assert "-e" in run and not any(v.startswith("MCP_AUTH_TOKEN") for v in run)
        assert container_env(tmp_path, run)["MCP_AUTH_TOKEN"] == f"tok-{token}", name


@pytest.mark.parametrize(
    "value",
    ["tok\tinner-tab", "tok-cr\r", "tok\x1bescape"],
    ids=["tab", "carriage-return", "escape"],
)
def test_a_token_with_a_control_character_refuses_before_any_mutation(
    tmp_path: Path, value: str
) -> None:
    # docker env files cannot carry such a value verbatim; refuse, never mangle.
    run_dir_secrets = {"MCP_H3_CRYPTO_PAPER_AUTH_TOKEN": value}
    result, calls, state, _ = _run(
        tmp_path, omit_tokens=tuple(run_dir_secrets), extra_secrets=run_dir_secrets
    )
    assert result.returncode == 78
    assert "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN is required" in result.stderr
    assert not [c for c in calls if c[0] in {"pull", "run", "rm"}]
    for unit in INITIAL:
        assert state[unit] == (KIS_OLD if unit == "at-kis-ws" else OLD), unit


# --- A2: HAProxy closed shape, matched whole -------------------------------------


H3_SERVER = "    server mcp_h3_crypto_paper 127.0.0.1:8776 check\n"
H3_DEFAULT = "    default-server inter 5s fall 2 rise 1\n"
LOG_GLOBAL = "    log stdout format raw local0\n"
H3_CHECK = "    http-check expect status 200\n"


def _rendered_template() -> str:
    return (
        TEMPLATE.read_text()
        .replace("__API_ACTIVE_PORT__", "8001")
        .replace("__MCP_ACTIVE_PORT__", "8766")
    )


def _replace_last(text: str, old: str, new: str) -> str:
    head, sep, tail = text.rpartition(old)
    assert sep, old
    return head + new + tail


def _shape_status(tmp_path: Path, config: str) -> int:
    lib = golden.functions_only(DEPLOY, tmp_path / "deploy-functions.sh")
    cfg = tmp_path / "haproxy.cfg"
    cfg.write_text(config)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'lib="$1" cfg="$2"; set --; source "$lib"; haproxy_shape_is_closed "$cfg"',
            "_",
            str(lib),
            str(cfg),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ["PATH"], "AT_RUN_DIRECTORY": str(tmp_path)},
    )
    assert result.stderr == ""
    return result.returncode


REFUSED_SERVER = {
    "socks4-coderabbit": "server mcp_h3_crypto_paper 127.0.0.1:8776 check socks4 203.0.113.10:1080",
    "check-via-socks4": "server mcp_h3_crypto_paper 127.0.0.1:8776 check check-via-socks4 socks4 203.0.113.10:1080",
    "source": "server mcp_h3_crypto_paper 127.0.0.1:8776 check source 203.0.113.10",
    "source-usesrc": "server mcp_h3_crypto_paper 127.0.0.1:8776 check source 0.0.0.0 usesrc clientip",
    "addr-port": "server mcp_h3_crypto_paper 127.0.0.1:8776 check addr 203.0.113.10 port 80",
    "port": "server mcp_h3_crypto_paper 127.0.0.1:8776 check port 9999",
    "redir": "server mcp_h3_crypto_paper 127.0.0.1:8776 check redir http://203.0.113.10",
    "ssl-sni": "server mcp_h3_crypto_paper 127.0.0.1:8776 check ssl verify none sni str(x.example)",
    "send-proxy": "server mcp_h3_crypto_paper 127.0.0.1:8776 check send-proxy",
    "resolvers": "server mcp_h3_crypto_paper 127.0.0.1:8776 check resolvers dns",
    "track": "server mcp_h3_crypto_paper 127.0.0.1:8776 check track bk_api/api_active",
    "weight-unknown": "server mcp_h3_crypto_paper 127.0.0.1:8776 check weight 10",
    "unknown-keyword": "server mcp_h3_crypto_paper 127.0.0.1:8776 check frobnicate",
    "upper-case": "server mcp_h3_crypto_paper 127.0.0.1:8776 CHECK",
    "quoted": 'server mcp_h3_crypto_paper 127.0.0.1:8776 "check"',
    "escaped": "server mcp_h3_crypto_paper 127.0.0.1:8776 ch\\eck",
    "hex-escape": "server mcp_h3_crypto_paper 127.0.0.1:8776 check soc\\x6bs4 203.0.113.10:1080",
    "env-expansion": 'server mcp_h3_crypto_paper "${ADDR}" check',
    "trailing-comment": "server mcp_h3_crypto_paper 127.0.0.1:8776 check # socks4 203.0.113.10:1080",
    "inter-no-value": "server mcp_h3_crypto_paper 127.0.0.1:8776 check inter",
    "fall-no-value": "server mcp_h3_crypto_paper 127.0.0.1:8776 check inter 5s fall",
    "inter-not-duration": "server mcp_h3_crypto_paper 127.0.0.1:8776 check inter abc",
    "rise-not-count": "server mcp_h3_crypto_paper 127.0.0.1:8776 check rise 2x",
    "carriage-return": "server mcp_h3_crypto_paper 127.0.0.1:8776 check\r",
    "vertical-tab": "server mcp_h3_crypto_paper 127.0.0.1:8776 check\vsocks4\v203.0.113.10:1080",
    "localhost-name": "server mcp_h3_crypto_paper localhost:8776 check",
    "ipv4-prefix": "server mcp_h3_crypto_paper ipv4@127.0.0.1:8776 check",
    "non-loopback": "server mcp_h3_crypto_paper 203.0.113.10:8776 check",
    "address-suffix": "server mcp_h3_crypto_paper 127.0.0.1:8776:80 check",
    "no-address": "server mcp_h3_crypto_paper",
    "quoted-name": 'server "mcp h3" 127.0.0.1:8776 check',
    "line-continuation": "server mcp_h3_crypto_paper 127.0.0.1:8776 check \\\n        socks4 203.0.113.10:1080",
}
REFUSED_DEFAULT_SERVER = {
    "socks4": "default-server inter 5s fall 2 rise 1 socks4 203.0.113.10:1080",
    "source": "default-server source 203.0.113.10",
    "addr": "default-server inter 5s fall 2 rise 1 addr 203.0.113.10",
    "init-addr": "default-server init-addr 203.0.113.10",
    "empty": "default-server",
}
REFUSED_OTHER = {
    "remote-log": (LOG_GLOBAL, "    log 203.0.113.10:514 local0\n"),
    "http-check-connect": (
        H3_CHECK,
        "    http-check connect addr 203.0.113.10 port 80\n",
    ),
    "http-check-send": (
        H3_CHECK,
        "    http-check send meth GET uri / hdr Host x.example\n",
    ),
    "option-proxy-header": (H3_CHECK, "    option http-use-proxy-header\n"),
    "httpchk-absolute-uri": (
        "    option httpchk GET /health\n",
        "    option httpchk GET http://203.0.113.10/\n",
    ),
    "timeout-extra": (
        "    timeout connect 5s\n",
        "    timeout connect 5s 203.0.113.10\n",
    ),
    "mode-tcp": ("    mode http\n", "    mode tcp\n"),
    "default-backend-extra": (
        "    default_backend bk_mcp_h3_crypto_paper\n",
        "    default_backend bk_mcp_h3_crypto_paper x\n",
    ),
    "backend-extra-word": (
        "backend bk_mcp_h3_crypto_paper\n",
        "backend bk_mcp_h3_crypto_paper x\n",
    ),
    "global-comment": ("global\n", "global # x\n"),
    "bind-extra-option": (
        "    bind 100.122.100.56:8776\n",
        "    bind 100.122.100.56:8776 v4v6\n",
    ),
}
ALLOWED_SERVER = {
    "bare": "server mcp_h3_crypto_paper 127.0.0.1:8776",
    "check": "server mcp_h3_crypto_paper 127.0.0.1:8776 check",
    "check-inter-fall-rise": "server mcp_h3_crypto_paper 127.0.0.1:8776 check inter 5s fall 2 rise 1",
    "reordered": "server mcp_h3_crypto_paper 127.0.0.1:8776 inter 500ms rise 3 check fall 1",
    "tabs": "\tserver\tmcp_h3_crypto_paper\t127.0.0.1:8776\tcheck",
}
ALLOWED_DEFAULT_SERVER = {
    "template": "default-server inter 5s fall 2 rise 1",
    "inter-only": "default-server inter 2s",
    "check": "default-server check",
    "plain-number": "default-server inter 5000 fall 3 rise 2",
}


def test_the_template_holds_the_lines_these_cases_replace() -> None:
    text = TEMPLATE.read_text()
    for line in (H3_SERVER, H3_DEFAULT, LOG_GLOBAL, H3_CHECK):
        assert line in text


def test_template_renders_inside_the_shape(tmp_path: Path) -> None:
    assert _shape_status(tmp_path, _rendered_template()) == 0


@pytest.mark.parametrize("line", REFUSED_SERVER.values(), ids=REFUSED_SERVER.keys())
def test_server_with_any_other_option_or_address_is_refused(
    tmp_path: Path, line: str
) -> None:
    config = _replace_last(_rendered_template(), H3_SERVER, f"    {line}\n")
    assert _shape_status(tmp_path, config) == 1


@pytest.mark.parametrize(
    "line", REFUSED_DEFAULT_SERVER.values(), ids=REFUSED_DEFAULT_SERVER.keys()
)
def test_default_server_with_any_other_option_is_refused(
    tmp_path: Path, line: str
) -> None:
    config = _replace_last(_rendered_template(), H3_DEFAULT, f"    {line}\n")
    assert _shape_status(tmp_path, config) == 1


@pytest.mark.parametrize(
    ("old", "new"), REFUSED_OTHER.values(), ids=REFUSED_OTHER.keys()
)
def test_other_directives_with_an_off_box_or_unknown_form_are_refused(
    tmp_path: Path, old: str, new: str
) -> None:
    config = _replace_last(_rendered_template(), old, new)
    assert _shape_status(tmp_path, config) == 1


@pytest.mark.parametrize("line", ALLOWED_SERVER.values(), ids=ALLOWED_SERVER.keys())
def test_each_allowed_server_form_passes(tmp_path: Path, line: str) -> None:
    config = _replace_last(_rendered_template(), H3_SERVER, f"    {line}\n")
    assert _shape_status(tmp_path, config) == 0


@pytest.mark.parametrize(
    "line", ALLOWED_DEFAULT_SERVER.values(), ids=ALLOWED_DEFAULT_SERVER.keys()
)
def test_each_allowed_default_server_form_passes(tmp_path: Path, line: str) -> None:
    config = _replace_last(_rendered_template(), H3_DEFAULT, f"    {line}\n")
    assert _shape_status(tmp_path, config) == 0


def test_render_refuses_the_coderabbit_socks4_line_end_to_end(tmp_path: Path) -> None:
    template = tmp_path / "haproxy.cfg.tmpl"
    template.write_text(
        _replace_last(
            TEMPLATE.read_text(),
            H3_SERVER,
            "    " + REFUSED_SERVER["socks4-coderabbit"] + "\n",
        )
    )
    result, _, state, run_dir = _run(
        tmp_path, extra_env={"MCP_HAPROXY_TEMPLATE": str(template)}
    )
    assert result.returncode != 0
    assert "closed config shape" in result.stderr
    for unit in INITIAL:
        assert state[unit] == (KIS_OLD if unit == "at-kis-ws" else OLD), unit
    cfg = run_dir / "haproxy.cfg"
    assert not cfg.exists() or "socks4" not in cfg.read_text()


# --- A3: golden, byte-identical apart from how the token arrives -----------------


def _golden() -> dict:
    return json.loads(golden.GOLDEN.read_text())


@pytest.mark.parametrize("scenario", sorted(golden.SCENARIOS))
def test_containers_get_the_same_argv_and_env_as_before(
    tmp_path: Path, scenario: str
) -> None:
    expected = _golden()["scenarios"][scenario]
    actual = golden.capture_scenario(tmp_path, DEPLOY, scenario)
    assert actual["returncode"] == expected["returncode"]
    assert len(actual["runs"]) == len(expected["runs"])
    for got, want in zip(actual["runs"], expected["runs"], strict=True):
        assert got == want
    assert actual["haproxy_cfg"] == expected["haproxy_cfg"]


@pytest.mark.parametrize("colors", golden.COLORS, ids=lambda c: "-".join(c))
def test_rendered_haproxy_config_is_byte_identical(
    tmp_path: Path, colors: tuple[str, str]
) -> None:
    api, mcp = colors
    expected = _golden()["render"][f"{api}-{mcp}"]
    assert golden.render(tmp_path, DEPLOY, api, mcp) == expected


def test_only_the_token_transport_differs_in_the_mcp_argv(tmp_path: Path) -> None:
    result, calls, _, _ = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    for call in calls:
        if call[0] != "run" or not call[call.index("--name") + 1].startswith("at-mcp-"):
            continue
        files = [v for f, v in zip(call, call[1:], strict=False) if f == "--env-file"]
        # the shared runtime and secrets files first, the token file last
        assert [Path(p).name for p in files[:2]] == [".env.runtime", ".env.secrets"]
        assert len(files) == 3 and golden.TOKEN_FILE_MARK in files[2]


# --- the reader keeps the pre-#1240 env_value semantics ---------------------------

# (runtime file, secrets file, value env_value returned before #1240 or None)
READER_CASES = {
    "runtime-only": ("K=abc\n", "", "abc"),
    "secrets-only": ("", "K=abc\n", "abc"),
    "later-file-wins": ("K=a\n", "K=b\n", "b"),
    "last-line-empty": ("K=a\nK=\n", "", None),
    "later-empty-keeps-earlier": ("K=a\n", "K=\n", "a"),
    "whitespace-only": ("K=a\n", "K=   \n", None),
    "export-double-quoted": ('  export K="q v"\n', "", "q v"),
    "single-quoted": ("K='x'\n", "", "x"),
    "mixed-quotes": ("K=\"x'\n", "", "x"),
    "inner-quotes-kept": ("K='\"x\"'\n", "", '"x"'),
    "empty-quoted-then-later": ('K=""\n', "K=y\n", "y"),
    "trailing-space-kept": ("K=a b \n", "", "a b "),
    "leading-space-kept": ("K= lead\n", "", " lead"),
    "other-key": ("XK=no\n", "", None),
    "space-before-equals": ("K =no\n", "", None),
    "commented": ("#K=no\n", "", None),
    "equals-in-value": ("K=a=b=c\n", "", "a=b=c"),
    "last-of-file": ("K=x\nK=y\nother=1\n", "K=\n", "y"),
    "export-two-spaces": ("export  K=e\n", "", "e"),
    "leading-tab": ("\tK=t\n", "", "t"),
    "lone-double-quote": ('K="\n', "", None),
    "lone-single-quote": ("K='\n", "", None),
    "shell-metacharacters": ("K=$weird`chars;|&\n", "", "$weird`chars;|&"),
    "utf8": ("K=ünï\n", "", "ünï"),
}


@pytest.mark.parametrize(
    ("runtime", "secrets", "expected"), READER_CASES.values(), ids=READER_CASES.keys()
)
def test_token_reader_keeps_the_old_env_value_semantics(
    tmp_path: Path, runtime: str, secrets: str, expected: str | None
) -> None:
    lib = golden.functions_only(DEPLOY, tmp_path / "deploy-functions.sh")
    (tmp_path / "rt").write_text(runtime)
    (tmp_path / "se").write_text(secrets)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'lib="$1"; set --; source "$lib"; mcp_token_env_line K',
            "_",
            str(lib),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "AT_RUN_DIRECTORY": str(tmp_path),
            "AT_RUNTIME_ENV_FILE": str(tmp_path / "rt"),
            "AT_SECRETS_ENV_FILE": str(tmp_path / "se"),
        },
    )
    if expected is None:
        assert (result.returncode, result.stdout) == (1, "")
    else:
        assert (result.returncode, result.stdout) == (0, f"MCP_AUTH_TOKEN={expected}\n")
