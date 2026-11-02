"""Offline fakes for the exact-SHA deploy gate, shared by the deploy harnesses.

The real gate (scripts/deploy_ci_gate.py) runs unmodified. It only talks to two
external programs, and both are faked here on PATH:

* ``docker buildx imagetools inspect`` returns remote metadata: a manifest whose
  digest is the harness's NEW digest, and a config whose revision label is
  ``GATE_SHA``. Every other docker call is forwarded to the harness's own
  stateful fake docker.
* ``gh api --method GET --paginate --slurp`` returns paginated arrays holding
  one successful push run of ``GATE_SHA`` with the six required jobs.

Nothing here touches the network or the host Docker daemon.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE_HELPER = REPO / "scripts" / "deploy_ci_gate.py"
GATE_SHA = "a" * 40

_FRONT_DOCKER = """\
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:3] == ["buildx", "imagetools", "inspect"]:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate-calls.log"), "a") as f:
        f.write(json.dumps(args) + "\\n")
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    if ".Manifest" in fmt:
        print(json.dumps({"digest": "sha256:" + DIGEST_HEX}))
    else:
        print(json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": GATE_SHA}}}))
    sys.exit(0)
os.execv(BACKEND, [BACKEND, *args])
"""

_FAKE_GH = """\
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate-calls.log"), "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:5] != ["api", "--method", "GET", "--paginate", "--slurp"] or len(args) != 6:
    print("fake gh refuses: " + " ".join(args), file=sys.stderr)
    sys.exit(42)
endpoint = args[5]
SHA = GATE_SHA
if "/actions/workflows/test.yml/runs" in endpoint:
    runs = [{
        "id": 100, "run_attempt": 1, "event": "push", "head_sha": SHA,
        "head_branch": "main", "status": "completed", "conclusion": "success",
        "name": "Test", "path": ".github/workflows/test.yml",
        "created_at": "2026-11-02T00:00:00Z", "updated_at": "2026-11-02T00:10:00Z",
    }]
    print(json.dumps([{"total_count": 1, "workflow_runs": runs}]))
elif "/actions/runs/100/attempts/1/jobs" in endpoint:
    names = ["lint", "taskiq-smoke", "test (3.13, 1)", "test (3.13, 2)", "test (3.13, 3)", "test (3.13, 4)"]
    jobs = [{"id": 200 + i, "name": n, "run_attempt": 1, "status": "completed", "conclusion": "success"}
            for i, n in enumerate(names)]
    print(json.dumps([{"total_count": len(jobs), "jobs": jobs}]))
else:
    print("fake gh refuses endpoint: " + endpoint, file=sys.stderr)
    sys.exit(42)
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_gate_fakes(bindir: Path, docker_script: str, digest: str) -> None:
    """Install fake ``docker`` (wrapping ``docker_script``) and fake ``gh`` in bindir.

    ``digest`` is the NEW repo digest (``repo@sha256:<64 hex>``) the remote tag
    resolves to; the same value the harness's fake docker reports after a pull.
    """
    hex_part = digest.rsplit("sha256:", 1)[1]
    backend = bindir / "_docker_backend"
    _write_exec(backend, docker_script)
    front = (
        _FRONT_DOCKER.replace("DIGEST_HEX", repr(hex_part))
        .replace("GATE_SHA", repr(GATE_SHA))
        .replace("BACKEND", repr(str(backend)))
    )
    _write_exec(bindir / "docker", front)
    _write_exec(bindir / "gh", _FAKE_GH.replace("GATE_SHA", repr(GATE_SHA)))


def install_gate_helper(script_directory: Path) -> Path:
    """Copy the standalone gate helper beside an installed/mutated deploy script."""
    target = script_directory / GATE_HELPER.name
    shutil.copy2(GATE_HELPER, target)
    return target
