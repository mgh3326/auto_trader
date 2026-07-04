"""ROB-698: the final post-cutover healthcheck must NOT hard-fail on a broker WS
outage (e.g. KIS scheduled maintenance).

`run_healthcheck_once` in scripts/deploy-native.sh runs the server healthcheck
with AUTO_TRADER_HEALTHCHECK_SKIP_WS (default 1) — consistent with the blue-green
cutover checks (native_deploy_lib.sh), which already skip WS — so a disconnected
broker websocket cannot fail+rollback an otherwise-healthy deploy. The built-in
fallback treats WS heartbeats as advisory (logged, non-fatal). api/mcp /healthz
remain hard gates.

deploy-native.sh is a top-level script (no main-guard, not sourceable in
isolation), so this is a source-text assertion mirroring
tests/scripts/test_native_deploy_preflight.py.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"


def _run_healthcheck_once_body() -> str:
    body = DEPLOY.read_text()
    # The function closes with an unindented `}` at column 0; nested `|| { ... }`
    # blocks close with an indented `  }`, so `\n}` isolates the function body.
    m = re.search(
        r"^run_healthcheck_once\(\) \{\n(.*?)\n\}", body, re.DOTALL | re.MULTILINE
    )
    assert m, "run_healthcheck_once() not found in deploy-native.sh"
    return m.group(1)


def test_server_healthcheck_invoked_with_skip_ws() -> None:
    body = _run_healthcheck_once_body()
    # Primary path: $SERVER_HEALTHCHECK must run with SKIP_WS set (default 1),
    # never as a bare invocation (which would enforce the KIS WS deploy gate).
    assert re.search(
        r'AUTO_TRADER_HEALTHCHECK_SKIP_WS=\S+\s+"\$SERVER_HEALTHCHECK"',
        body,
    ), (
        "run_healthcheck_once must invoke $SERVER_HEALTHCHECK with "
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS set (default 1) so a broker WS outage "
        "cannot fail+rollback an otherwise-healthy deploy"
    )
    assert not re.search(r'^\s*"\$SERVER_HEALTHCHECK"\s*$', body, re.MULTILINE), (
        'a bare "$SERVER_HEALTHCHECK" invocation re-introduces the KIS WS deploy gate'
    )


def test_skip_ws_default_is_one_but_overridable() -> None:
    body = _run_healthcheck_once_body()
    # Default to skip (1) so deploys survive broker maintenance, but let an
    # operator force WS-gating again with AUTO_TRADER_HEALTHCHECK_SKIP_WS=0.
    assert "${AUTO_TRADER_HEALTHCHECK_SKIP_WS:-1}" in body, (
        "the SKIP_WS default must be 1 (skip WS gate) yet operator-overridable"
    )


def test_fallback_ws_heartbeat_is_advisory_not_fatal() -> None:
    body = _run_healthcheck_once_body()
    # The built-in fallback WS heartbeat checks must be advisory (non-fatal):
    # no `|| rc=1` on the websocket_healthcheck.py invocations.
    ws_fatal = re.findall(r"websocket_healthcheck\.py[^\n]*", body)
    fatal = [line for line in ws_fatal if "rc=1" in line]
    # Also catch a `|| rc=1` on the continuation line of a backslash-wrapped call.
    assert "|| rc=1" not in body or not re.search(
        r"websocket_healthcheck\.py\s*\\?\s*\n?\s*\|\|\s*rc=1", body
    ), (
        "fallback websocket_healthcheck.py must be advisory (|| echo ... >&2), not fatal (|| rc=1)"
    )
    assert not fatal, f"fallback WS heartbeat must be advisory, found fatal: {fatal}"
