"""ROB-1454 — static contract on what `.github/workflows/test.yml` runs where.

Three things must stay true and are easy to break by accident:

1. A push to main or a dispatch always runs the full suite. The `if:` on the
   full-suite jobs may only ever narrow a pull_request, and must keep running
   when the classifier job itself failed.
2. `PR gate` runs even when its children fail, and declares every job it needs.
3. `.mergewake/policy.yaml` names checks this workflow really displays: a name
   nothing reports would leave a required check unknown forever.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.aggregate_required import (
    AFFECTED_JOB,
    CLASSIFIER_JOB,
    FULL_SUITE_ONLY_JOBS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/test.yml"
POLICY_PATH = REPO_ROOT / ".mergewake/policy.yaml"

FULL_MAIN_CHECK_NAMES = (
    "lint",
    "taskiq-smoke",
    "test (3.13, 1)",
    "test (3.13, 2)",
    "test (3.13, 3)",
    "test (3.13, 4)",
)
GATE_JOB_ID = "pr-gate"
GATE_CHECK_NAME = "PR gate"

#: The exact condition on both full-suite jobs. Pinned as a string because a
#: reworded `if:` is exactly how main silently loses coverage.
FULL_SUITE_IF = (
    "${{ !cancelled() && (github.event_name != 'pull_request' || "
    "needs.change-classifier.outputs.run_all != 'false') }}"
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _displayed_names(job_id: str, job: dict[str, Any]) -> list[str]:
    """Reproduce the check name GitHub displays for a job."""

    label = job.get("name", job_id)
    matrix = job.get("strategy", {}).get("matrix")
    if not matrix:
        return [label]
    keys = list(matrix.keys())
    return [
        f"{label} ({', '.join(str(value) for value in combo)})"
        for combo in itertools.product(*(matrix[key] for key in keys))
    ]


def _script(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))


# --------------------------------------------------------------------------
# Full suite: main pushes and dispatch
# --------------------------------------------------------------------------


def test_workflow_triggers_add_only_workflow_dispatch(
    workflow: dict[str, Any],
) -> None:
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
        "workflow_dispatch": None,
    }


@pytest.mark.parametrize("job_id", FULL_SUITE_ONLY_JOBS)
def test_full_suite_jobs_only_narrow_on_a_pull_request(
    workflow: dict[str, Any], job_id: str
) -> None:
    job = workflow["jobs"][job_id]
    assert job["if"] == FULL_SUITE_IF
    assert job["needs"] == CLASSIFIER_JOB


def test_full_main_check_names_are_unchanged(workflow: dict[str, Any]) -> None:
    names: list[str] = []
    for job_id in ("lint", *FULL_SUITE_ONLY_JOBS):
        names += _displayed_names(job_id, workflow["jobs"][job_id])
    assert sorted(names) == sorted(FULL_MAIN_CHECK_NAMES)


def test_test_job_matrix_shape_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"]["test"]["strategy"]["matrix"] == {
        "python-version": ["3.13"],
        "group": [1, 2, 3, 4],
    }


def test_lint_stays_unconditional(workflow: dict[str, Any]) -> None:
    lint = workflow["jobs"]["lint"]
    assert "if" not in lint
    assert "needs" not in lint


def test_taskiq_smoke_still_owns_the_exact_cover_check(
    workflow: dict[str, Any],
) -> None:
    assert "file_shard_plan check" in _script(workflow["jobs"]["taskiq-smoke"])


def test_shard_job_still_runs_its_manifest(workflow: dict[str, Any]) -> None:
    script = _script(workflow["jobs"]["test"])
    assert (
        'mapfile -t SHARD_FILES < "ci_shards/shard-${{ matrix.group }}.txt"' in script
    )


# --------------------------------------------------------------------------
# Affected tests: pull requests only
# --------------------------------------------------------------------------


def test_affected_job_runs_only_on_a_narrow_pull_request(
    workflow: dict[str, Any],
) -> None:
    condition = workflow["jobs"][AFFECTED_JOB]["if"]
    assert "github.event_name == 'pull_request'" in condition
    assert "needs.change-classifier.outputs.run_all == 'false'" in condition
    assert "needs.change-classifier.outputs.run_affected_tests == 'true'" in condition


def test_affected_job_holds_no_token_while_running_pr_test_code(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AFFECTED_JOB]
    assert job["permissions"] == {"contents": "read"}
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] is False


def test_affected_job_excludes_live_tests_like_a_shard(
    workflow: dict[str, Any],
) -> None:
    script = _script(workflow["jobs"][AFFECTED_JOB])
    assert '-m "not live"' in script
    assert "-n 4 --dist=loadfile" in script


def test_affected_list_is_passed_through_the_environment(
    workflow: dict[str, Any],
) -> None:
    """It comes from the PR's tree, so it must never be interpolated into a script."""

    job = workflow["jobs"][AFFECTED_JOB]
    step = next(s for s in job["steps"] if s.get("name") == "Run affected tests")
    assert "outputs.affected_tests" in step["env"]["AFFECTED_TESTS"]
    assert "${{" not in step["run"]


def test_affected_job_refuses_an_empty_pytest_argv(workflow: dict[str, Any]) -> None:
    """pytest with no paths runs its default testpaths, not nothing."""

    script = _script(workflow["jobs"][AFFECTED_JOB])
    assert 'test "${#AFFECTED_FILES[@]}" -gt 0' in script


def test_manifest_edit_runs_exact_cover_on_the_pull_request(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AFFECTED_JOB]
    step = next(
        s
        for s in job["steps"]
        if s.get("name") == "Validate file-shard manifests (exact-cover)"
    )
    assert "outputs.check_manifests == 'true'" in step["if"]


# --------------------------------------------------------------------------
# PR gate
# --------------------------------------------------------------------------


def test_gate_has_a_constant_displayed_name(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][GATE_JOB_ID]
    assert _displayed_names(GATE_JOB_ID, job) == [GATE_CHECK_NAME]


def test_gate_runs_even_when_children_fail(workflow: dict[str, Any]) -> None:
    """Without `always()` the gate is skipped on a child failure: unknown, not red."""

    assert workflow["jobs"][GATE_JOB_ID]["if"] == "always()"


def test_gate_needs_every_job_that_decides_the_verdict(
    workflow: dict[str, Any],
) -> None:
    needs = set(workflow["jobs"][GATE_JOB_ID]["needs"])
    assert needs == {"lint", CLASSIFIER_JOB, AFFECTED_JOB, *FULL_SUITE_ONLY_JOBS}


def test_gate_required_flags_match_its_needs_list(workflow: dict[str, Any]) -> None:
    """A `needs:` entry with no `--required` flag would be silently ignored."""

    job = workflow["jobs"][GATE_JOB_ID]
    tokens = _script(job).replace("\\\n", " ").split()
    declared = [
        tokens[index + 1]
        for index, token in enumerate(tokens)
        if token == "--required" and index + 1 < len(tokens)
    ]
    assert sorted(declared) == sorted(job["needs"])


def test_gate_derives_skips_only_from_the_classifier(
    workflow: dict[str, Any],
) -> None:
    script = _script(workflow["jobs"][GATE_JOB_ID])
    assert "scripts/ci/aggregate_required.py" in script
    assert "--pr-gate" in script
    assert "--authorize-skip" not in script
    assert "--allow-undeclared" not in script


def test_classifier_job_checks_out_full_history(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][CLASSIFIER_JOB]
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_classifier_job_publishes_every_output_the_workflow_reads(
    workflow: dict[str, Any],
) -> None:
    outputs = workflow["jobs"][CLASSIFIER_JOB]["outputs"]
    for key in ("run_all", "run_affected_tests", "check_manifests", "affected_tests"):
        assert key in outputs


def test_concurrency_block_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number "
        "|| github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


# --------------------------------------------------------------------------
# mergewake policy
# --------------------------------------------------------------------------


def test_policy_requires_lint_and_the_gate(policy: dict[str, Any]) -> None:
    assert policy["required_checks"] == ["lint", GATE_CHECK_NAME]


def test_policy_watches_the_full_main_set(policy: dict[str, Any]) -> None:
    assert sorted(policy["watch_checks"]) == sorted(FULL_MAIN_CHECK_NAMES)


def test_every_policy_check_is_a_name_the_workflow_displays(
    workflow: dict[str, Any], policy: dict[str, Any]
) -> None:
    displayed = {
        name
        for job_id, job in workflow["jobs"].items()
        for name in _displayed_names(job_id, job)
    }
    assert set(policy["required_checks"]) | set(policy["watch_checks"]) <= displayed


@pytest.mark.parametrize(
    "path",
    [
        "app/services/brokers/**",
        "app/services/order_proposals/**",
        "app/services/execution_ledger/**",
        "alembic/versions/**",
        "config/trading_policy.yaml",
        "app/tasks/**/*.py",
        ".github/workflows/**",
        "scripts/ci/**",
    ],
)
def test_policy_keeps_its_blocking_paths(policy: dict[str, Any], path: str) -> None:
    """They still mean two review passes and no auto-revert."""

    blocking = {p for entry in policy["blocking"] for p in entry["paths"]}
    assert path in blocking
