"""#1240: golden capture of what deploy-ncp-pull.sh hands docker and HAProxy.

The fixture was captured from the script as of f24eb3a (before #1240) and is
compared against the current script. The only allowed difference is how the
MCP token reaches the container: an argv pair -e MCP_AUTH_TOKEN=... before,
an --env-file pair naming a per-run token file after. Both are removed from
the argv; the env the fake daemon resolved (env files in order, then -e, a
repeated key keeping its first position and its last value) must match byte
for byte, token values included.

Regenerate only on purpose: python -m tests.scripts._deploy_ncp_pull_golden
<script> <out.json>.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tests.scripts.test_deploy_ncp_pull_rollback import REPO, _run

GOLDEN = REPO / "tests/fixtures/deploy_ncp_pull_1240_golden.json"
TEMPLATE = REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"
TOKEN_FILE_MARK = ".mcp-token-env."
H3_TOKEN = "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN"

SCENARIOS: dict[str, dict[str, Any]] = {
    "deploy": {},
    "deploy-mcp-green-active": {"active_mcp_color": "green"},
    "rollback": {"args": ("--rollback",)},
    "deploy-h3-start-fails": {"fail_name": "at-mcp-h3-crypto-paper"},
    "deploy-h3-skipped": {
        "extra_env": {"MCP_UNITS_SKIP": "h3-crypto-paper"},
        "omit_tokens": (H3_TOKEN,),
    },
}
COLORS = [(api, mcp) for api in ("blue", "green") for mcp in ("blue", "green")]

# The final dispatch runs main; everything above it only defines functions
# and read-only settings, so a copy without it can be sourced.
_DISPATCH = re.compile(r"^if \(\(DRY_RUN\)\); then dry_run\n.*\Z", re.M | re.S)


def functions_only(script: Path, out: Path) -> Path:
    source = script.read_text()
    assert len(_DISPATCH.findall(source)) == 1
    out.write_text(_DISPATCH.sub("", source))
    return out


def _normalized_argv(call: list[str], tmp: Path) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(call):
        if call[i] == "-e" and call[i + 1].startswith("MCP_AUTH_TOKEN="):
            i += 2
            continue
        if call[i] == "--env-file" and TOKEN_FILE_MARK in call[i + 1]:
            i += 2
            continue
        out.append(call[i].replace(str(tmp), "<TMP>"))
        i += 1
    return out


def capture_scenario(tmp_path: Path, script: Path, scenario: str) -> dict[str, Any]:
    result, _, _, run_dir = _run(tmp_path, deploy=script, **SCENARIOS[scenario])
    env_log = tmp_path / "docker-calls.jsonl.env"
    entries = [json.loads(line) for line in env_log.read_text().splitlines()]
    cfg = run_dir / "haproxy.cfg"
    return {
        "returncode": result.returncode,
        "runs": [
            {
                "argv": _normalized_argv(entry["args"], tmp_path),
                "env": [line.replace(str(tmp_path), "<TMP>") for line in entry["env"]],
            }
            for entry in entries
        ],
        "haproxy_cfg": cfg.read_text() if cfg.exists() else None,
    }


def render(tmp_path: Path, script: Path, api: str, mcp: str) -> str:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lib = functions_only(script, tmp_path / "deploy-functions.sh")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'lib="$1" api="$2" mcp="$3"; set --; source "$lib"; render_haproxy "$api" "$mcp"',
            "_",
            str(lib),
            api,
            mcp,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "AT_RUN_DIRECTORY": str(run_dir),
            "MCP_HAPROXY_TEMPLATE": str(TEMPLATE),
        },
    )
    assert result.returncode == 0, result.stderr
    return (run_dir / "haproxy.cfg").read_text()


def capture_all(script: Path) -> dict[str, Any]:
    golden: dict[str, Any] = {"scenarios": {}, "render": {}}
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            golden["scenarios"][scenario] = capture_scenario(
                Path(tmp), script, scenario
            )
    for api, mcp in COLORS:
        with tempfile.TemporaryDirectory() as tmp:
            golden["render"][f"{api}-{mcp}"] = render(Path(tmp), script, api, mcp)
    return golden


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(
        json.dumps(capture_all(Path(sys.argv[1])), indent=1, sort_keys=True) + "\n"
    )
