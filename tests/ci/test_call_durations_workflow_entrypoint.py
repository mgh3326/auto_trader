"""ROB-1308 — module-safe entrypoint contract for `scripts/call_durations.py`.

`scripts/call_durations.py` does ``from scripts.merge_test_durations import
...`` at import time (as of ROB-1312: ``load_node_manifest``,
``validate_disjoint_shard_collections``, and
``_check_shard_measured_matches_collected`` -- the disjoint-shard-collection
/ independent-authoritative-collection contract those functions implement).
That only resolves when ``scripts`` is importable as a package, which
requires the invoking process to have the repo root on ``sys.path`` (e.g.
``python -m scripts.call_durations``).
Direct-script invocation (``python scripts/call_durations.py ...``) puts the
script's own directory on ``sys.path[0]`` instead, so the import raises
``ModuleNotFoundError: No module named 'scripts'`` — this is exactly what
broke the "Merge duration shards" job's `build`/`validate` steps in
https://github.com/mgh3326/auto_trader/actions/runs/32321164500.

This is a static contract on the raw `run:` shell blocks in the ``merge``
job of ``.github/workflows/test-durations-refresh.yml``: both the
"Build call-phase duration artifact" and "Validate call-phase duration
freshness" steps must invoke the module-safe form, and direct-script
invocation must never reappear.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github/workflows/test-durations-refresh.yml"
)

MERGE_JOB_ID = "merge"
BUILD_STEP_NAME = "Build call-phase duration artifact"
VALIDATE_STEP_NAME = "Validate call-phase duration freshness"

DIRECT_SCRIPT_INVOCATION = "python scripts/call_durations.py"
MODULE_SAFE_INVOCATION = "python -m scripts.call_durations"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(workflow: dict[str, Any], job_id: str, step_name: str) -> dict[str, Any]:
    job = workflow["jobs"][job_id]
    for step in job["steps"]:
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"step {step_name!r} not found in job {job_id!r}")


@pytest.mark.parametrize("step_name", [BUILD_STEP_NAME, VALIDATE_STEP_NAME])
def test_call_durations_step_uses_module_safe_invocation(
    workflow: dict[str, Any], step_name: str
) -> None:
    run = _step(workflow, MERGE_JOB_ID, step_name)["run"]
    assert DIRECT_SCRIPT_INVOCATION not in run, (
        f"{step_name!r} regressed to direct-script invocation "
        f"({DIRECT_SCRIPT_INVOCATION!r}), which raises "
        "ModuleNotFoundError: No module named 'scripts' when scripts/ is "
        "not installed as an editable package (see ROB-1308)."
    )
    assert MODULE_SAFE_INVOCATION in run, (
        f"{step_name!r} must invoke {MODULE_SAFE_INVOCATION!r} so "
        "`from scripts.merge_test_durations import ...` resolves without "
        "relying on an editable install."
    )


def test_no_direct_script_invocation_anywhere_in_workflow() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert DIRECT_SCRIPT_INVOCATION not in text
