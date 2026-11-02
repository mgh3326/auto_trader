"""Offline registry/Actions fakes used with the real exact-SHA deployment gate.

The shell front end handles only registry reads; other Docker commands exec the
existing stateful fake directly. No extra Python interpreter per Docker call.
"""

from __future__ import annotations

import json
import shlex
import shutil
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE_HELPER = REPO / "scripts" / "deploy_ci_gate.py"
GATE_SHA = "a" * 40

_FRONT_DOCKER = """\
#!/bin/sh
case "${1-} ${2-} ${3-}" in
  'buildx imagetools inspect')
    printf '%s\\n' "$*" >> __LOG__
    for arg do format="$arg"; done
    case "$format" in
      *'.Manifest'*) printf '%s\\n' __MANIFEST__ ;;
      *'.Image'*) printf '%s\\n' __CONFIG__ ;;
      *) exit 42 ;;
    esac
    ;;
  *) exec __BACKEND__ "$@" ;;
esac
"""

_FAKE_GH = """\
#!/bin/sh
printf '%s\\n' "$*" >> __LOG__
[ "$#" -eq 6 ] && [ "$1 $2 $3 $4 $5" = 'api --method GET --paginate --slurp' ] || exit 42
case "$6" in
  *'/actions/workflows/test.yml/runs?'*) printf '%s\\n' __RUNS__ ;;
  *'/actions/runs/100/attempts/1/jobs?'*) printf '%s\\n' __JOBS__ ;;
  *) exit 42 ;;
esac
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_gate_fakes(bindir: Path, docker_script: str, digest: str) -> None:
    """Serve gate evidence, preserving the original stateful Docker fake."""
    backend = bindir / "_docker_backend"
    _write_exec(backend, docker_script)
    log = shlex.quote(str(bindir / "gate-calls.log"))
    manifest = json.dumps({"digest": digest.split("@", 1)[1]})
    config = json.dumps(
        {"config": {"Labels": {"org.opencontainers.image.revision": GATE_SHA}}}
    )
    front = (
        _FRONT_DOCKER.replace("__LOG__", log)
        .replace("__MANIFEST__", shlex.quote(manifest))
        .replace("__CONFIG__", shlex.quote(config))
        .replace("__BACKEND__", shlex.quote(str(backend)))
    )
    _write_exec(bindir / "docker", front)
    runs = [{"id": 100, "run_attempt": 1, "event": "push", "head_sha": GATE_SHA}]
    names = ["lint", "taskiq-smoke", *(f"test (3.13, {n})" for n in range(1, 5))]
    jobs = [
        {"id": n, "name": name, "status": "completed", "conclusion": "success"}
        for n, name in enumerate(names, 1)
    ]
    gh = (
        _FAKE_GH.replace("__LOG__", log)
        .replace("__RUNS__", shlex.quote(json.dumps([{"workflow_runs": runs}])))
        .replace("__JOBS__", shlex.quote(json.dumps([{"jobs": jobs}])))
    )
    _write_exec(bindir / "gh", gh)


def install_gate_helper(script_directory: Path) -> Path:
    """Copy the standalone helper beside an installed/mutated deploy script."""
    target = script_directory / GATE_HELPER.name
    shutil.copy2(GATE_HELPER, target)
    return target
