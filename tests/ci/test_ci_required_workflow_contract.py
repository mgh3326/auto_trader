"""ROB-1294 — static contract on `.github/workflows/test.yml`.

Two things must stay true and are easy to break by accident:

1. The six checks branch protection names today -- ``lint``,
   ``taskiq-smoke``, ``test (3.13, 1..4)`` -- keep their displayed names,
   their matrix shape and their (absent) ``if:`` conditions. Renaming any of
   them silently turns a required check into "expected -- waiting for status"
   forever.
2. The new classifier stays **shadow**: no existing job's execution may
   depend on it in this PR.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"

#: Branch-protection snapshot (verified 2026-07-28, unchanged by ROB-1294).
REQUIRED_CHECK_NAMES = (
    "lint",
    "taskiq-smoke",
    "test (3.13, 1)",
    "test (3.13, 2)",
    "test (3.13, 3)",
    "test (3.13, 4)",
)
REQUIRED_JOB_IDS = ("lint", "taskiq-smoke", "test")

AGGREGATE_JOB_ID = "ci-required"
CLASSIFIER_JOB_ID = "change-classifier"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


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


# --------------------------------------------------------------------------
# The six required checks are untouched
# --------------------------------------------------------------------------


def test_required_check_names_are_exactly_the_protected_six(
    workflow: dict[str, Any],
) -> None:
    names: list[str] = []
    for job_id in REQUIRED_JOB_IDS:
        names += _displayed_names(job_id, workflow["jobs"][job_id])
    assert sorted(names) == sorted(REQUIRED_CHECK_NAMES)


@pytest.mark.parametrize("job_id", REQUIRED_JOB_IDS)
def test_required_jobs_carry_no_name_override(
    workflow: dict[str, Any], job_id: str
) -> None:
    """A `name:` key would decouple the displayed check from the job id."""

    assert "name" not in workflow["jobs"][job_id]


@pytest.mark.parametrize("job_id", REQUIRED_JOB_IDS)
def test_required_jobs_carry_no_if_condition(
    workflow: dict[str, Any], job_id: str
) -> None:
    """They must run unconditionally; a conditional required check can hang."""

    assert "if" not in workflow["jobs"][job_id]


@pytest.mark.parametrize("job_id", REQUIRED_JOB_IDS)
def test_required_jobs_declare_no_needs(workflow: dict[str, Any], job_id: str) -> None:
    """ROB-1294 must not sequence the required jobs behind the classifier."""

    assert "needs" not in workflow["jobs"][job_id]


def test_test_job_matrix_shape_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"]["test"]["strategy"]["matrix"] == {
        "python-version": ["3.13"],
        "group": [1, 2, 3, 4],
    }


# --------------------------------------------------------------------------
# The aggregate exists and cannot disappear on failure
# --------------------------------------------------------------------------


def test_aggregate_job_exists_with_a_constant_displayed_name(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    assert job["name"] == AGGREGATE_JOB_ID
    assert _displayed_names(AGGREGATE_JOB_ID, job) == [AGGREGATE_JOB_ID]
    assert "matrix" not in job.get("strategy", {})


def test_aggregate_job_runs_even_when_children_fail(
    workflow: dict[str, Any],
) -> None:
    """Without `always()` the aggregate is skipped on any child failure."""

    assert workflow["jobs"][AGGREGATE_JOB_ID]["if"] == "always()"


def test_aggregate_job_needs_every_currently_required_job_and_the_classifier(
    workflow: dict[str, Any],
) -> None:
    needs = workflow["jobs"][AGGREGATE_JOB_ID]["needs"]
    assert set(needs) == {*REQUIRED_JOB_IDS, CLASSIFIER_JOB_ID}


def test_aggregate_required_flags_match_its_needs_list(
    workflow: dict[str, Any],
) -> None:
    """A `needs:` entry with no `--required` flag would be silently ignored."""

    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    tokens = script.replace("\\\n", " ").split()
    declared = [
        tokens[index + 1]
        for index, token in enumerate(tokens)
        if token == "--required" and index + 1 < len(tokens)
    ]
    assert sorted(declared) == sorted(job["needs"])


def test_aggregate_authorizes_no_skips(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert "--authorize-skip" not in script
    assert "--allow-undeclared" not in script


def test_aggregate_invokes_the_checked_in_aggregator_script(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert "scripts/ci/aggregate_required.py" in script
    assert (WORKFLOW_PATH.parents[2] / "scripts/ci/aggregate_required.py").is_file()


# --------------------------------------------------------------------------
# The classifier is wired, and wired to nothing
# --------------------------------------------------------------------------


def test_classifier_job_exists_and_checks_out_full_history(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][CLASSIFIER_JOB_ID]
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_classifier_job_invokes_the_checked_in_classifier_script(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][CLASSIFIER_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert "scripts/ci/classify_changes.py" in script
    assert (WORKFLOW_PATH.parents[2] / "scripts/ci/classify_changes.py").is_file()


def test_no_job_other_than_the_aggregate_depends_on_the_classifier(
    workflow: dict[str, Any],
) -> None:
    for job_id, job in workflow["jobs"].items():
        if job_id == AGGREGATE_JOB_ID:
            continue
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        assert CLASSIFIER_JOB_ID not in needs, job_id


def test_no_job_condition_references_the_classifier(
    workflow: dict[str, Any],
) -> None:
    """The shadow guarantee: classifier outputs drive no skip in this PR."""

    for job_id, job in workflow["jobs"].items():
        condition = str(job.get("if", ""))
        assert CLASSIFIER_JOB_ID not in condition, job_id
        for step in job["steps"]:
            assert CLASSIFIER_JOB_ID not in str(step.get("if", "")), job_id


def test_classifier_outputs_are_never_read_by_an_expression(
    workflow_text: str,
) -> None:
    assert f"needs.{CLASSIFIER_JOB_ID}.outputs" not in workflow_text


# --------------------------------------------------------------------------
# Nothing else about the workflow moved
# --------------------------------------------------------------------------


def test_workflow_triggers_are_unchanged(workflow: dict[str, Any]) -> None:
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
    }


def test_concurrency_block_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number "
        "|| github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
