"""hk 3487 — static and behavioural contract for the any-ref CI.

mergewake dispatches `.github/workflows/test-any-ref.yml` for one bisect commit
whose push run GitHub cancelled, and reads the commit statuses it posts. These
tests pin what must stay true so that path can be trusted:

1. `test.yml` cancels with the literal true, and its concurrency groups keep a
   dispatch out of every push's group (what cancels what, per event kind).
2. The any-ref run validates its ref before any checkout, and only then calls
   `test.yml` for that exact commit (every checkout there reads `inputs.ref`).
3. Each watched check is posted as a commit status by a job whose name is not a
   watched check, and the policy names the workflow file, which exists.
4. The any-ref workflow posts nothing else (no image, no deploy-ok, no ci-ok), and
   only its status job can write statuses.

Expressions are evaluated with `tests/ci/gh_expr.py`, and the validation step is run
under bash, so the tests check behaviour rather than spelling.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci import gh_expr

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_WORKFLOW = REPO_ROOT / ".github/workflows/test.yml"
ANY_REF_WORKFLOW = REPO_ROOT / ".github/workflows/test-any-ref.yml"
POLICY_PATH = REPO_ROOT / ".mergewake/policy.yaml"
HELPER_PATH = REPO_ROOT / ".github/scripts/any_ref_statuses.py"

STATUS_JOB = "post-status"
VALIDATE_JOB = "validate"
SUITE_JOB = "suite"
WATCH_ARG = re.compile(r"--watch '([^']*)'")
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("any_ref_statuses", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


@pytest.fixture(scope="module")
def test_wf() -> dict[str, Any]:
    return yaml.safe_load(TEST_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def any_ref() -> dict[str, Any]:
    return yaml.safe_load(ANY_REF_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow.get("on", workflow.get(True))


def _group(workflow: dict[str, Any], event_name: str, **github: Any) -> tuple[str, bool]:
    block = workflow["concurrency"]
    context = {"github": {"workflow": "Test", "event_name": event_name, **github}}
    return (
        str(gh_expr.evaluate(block["group"], context)),
        gh_expr.truthy(gh_expr.evaluate(block["cancel-in-progress"], context)),
    )


def _checkouts(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]


# --------------------------------------------------------------------------
# 1. cancel-in-progress and the per-ref / per-dispatch groups
# --------------------------------------------------------------------------


def test_test_workflow_cancels_with_the_literal_true(test_wf: dict[str, Any]) -> None:
    # Literal, not an expression: mergewake ci-policy cannot verify an expression.
    assert test_wf["concurrency"]["cancel-in-progress"] is True


def test_a_push_group_is_per_ref(test_wf: dict[str, Any]) -> None:
    main = _group(test_wf, "push", ref="refs/heads/main", run_id=1, event={})
    develop = _group(test_wf, "push", ref="refs/heads/develop", run_id=2, event={})
    main_again = _group(test_wf, "push", ref="refs/heads/main", run_id=3, event={})
    assert main[0] != develop[0]
    assert main[0] == main_again[0]
    assert main == ("Test-refs/heads/main", True)


def test_a_dispatch_group_is_its_own_run_and_attempt(test_wf: dict[str, Any]) -> None:
    first = _group(
        test_wf, "workflow_dispatch", ref="refs/heads/main", run_id=7, run_attempt=1, event={}
    )
    second_run = _group(
        test_wf, "workflow_dispatch", ref="refs/heads/main", run_id=8, run_attempt=1, event={}
    )
    rerun = _group(
        test_wf, "workflow_dispatch", ref="refs/heads/main", run_id=7, run_attempt=2, event={}
    )
    assert first[0] == "Test-dispatch-7-1"
    assert len({first[0], second_run[0], rerun[0]}) == 3


def test_a_workflow_call_from_any_ref_has_its_own_dispatch_group(
    test_wf: dict[str, Any],
) -> None:
    """The called run inherits event_name workflow_dispatch from test-any-ref.yml."""

    called = _group(
        test_wf, "workflow_dispatch", ref="refs/heads/main", run_id=55, run_attempt=1, event={}
    )
    push_main = _group(test_wf, "push", ref="refs/heads/main", run_id=56, event={})
    assert called[0] != push_main[0]


@pytest.mark.parametrize(
    "event_kind, kwargs",
    [
        ("push", {"ref": "refs/heads/main", "run_id": 9, "event": {}}),
        ("pull_request", {"ref": "refs/pull/4/merge", "run_id": 9, "event": {"pull_request": {"number": 4}}}),
        ("workflow_dispatch", {"ref": "refs/heads/main", "run_id": 9, "run_attempt": 1, "event": {}}),
    ],
)
def test_no_push_group_collides_with_a_dispatch_group(
    test_wf: dict[str, Any], event_kind: str, kwargs: dict[str, Any]
) -> None:
    """A dispatch is never cancelled by a push, a PR, or another dispatch."""

    dispatch = _group(
        test_wf, "workflow_dispatch", ref="refs/heads/main", run_id=100, run_attempt=1, event={}
    )
    other = _group(test_wf, event_kind, **kwargs)
    assert other[0] != dispatch[0]


def test_every_test_checkout_reads_the_called_ref(test_wf: dict[str, Any]) -> None:
    """A called run must check out the exact commit, not the caller's branch."""

    checkouts = [
        (job_id, step)
        for job_id, job in test_wf["jobs"].items()
        for step in _checkouts(job)
    ]
    assert len(checkouts) >= 13
    for job_id, step in checkouts:
        assert step["with"]["ref"] == "${{ inputs.ref }}", job_id


def test_called_ref_defaults_to_the_event_ref(test_wf: dict[str, Any]) -> None:
    ref = _triggers(test_wf)["workflow_call"]["inputs"]["ref"]
    assert ref["default"] == "" and ref["required"] is False


# --------------------------------------------------------------------------
# 2. the any-ref run: validate first, then call test.yml for that commit
# --------------------------------------------------------------------------


def _validation_script(any_ref: dict[str, Any]) -> str:
    steps = any_ref["jobs"][VALIDATE_JOB]["steps"]
    return steps[0]["run"]


def _run_validation(script: str, value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        env={"TARGET_SHA": value, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "value",
    ["", "main", "abc", VALID_SHA[:39], VALID_SHA + "0", "g" * 40, "../" + VALID_SHA[:37]],
)
def test_ref_is_refused_unless_it_is_an_exact_40_hex_sha(
    any_ref: dict[str, Any], value: str
) -> None:
    assert _run_validation(_validation_script(any_ref), value).returncode != 0


def test_a_40_hex_sha_passes_the_format_check(any_ref: dict[str, Any]) -> None:
    assert _run_validation(_validation_script(any_ref), VALID_SHA).returncode == 0
    assert _run_validation(_validation_script(any_ref), VALID_SHA.upper()).returncode == 0


def test_validation_is_the_first_step_and_runs_before_any_checkout(
    any_ref: dict[str, Any],
) -> None:
    validate = any_ref["jobs"][VALIDATE_JOB]
    assert "actions/checkout" not in str(validate.get("steps"))
    assert "[[" in validate["steps"][0]["run"]


def test_every_other_job_waits_for_validation(any_ref: dict[str, Any]) -> None:
    jobs = any_ref["jobs"]
    assert jobs[SUITE_JOB]["needs"] == VALIDATE_JOB
    assert VALIDATE_JOB in jobs[STATUS_JOB]["needs"]


def test_suite_calls_test_yml_for_the_exact_ref(any_ref: dict[str, Any]) -> None:
    suite = any_ref["jobs"][SUITE_JOB]
    assert suite["uses"] == "./.github/workflows/test.yml"
    assert suite["with"] == {"ref": "${{ inputs.ref }}"}
    assert suite["secrets"] == "inherit"


def test_any_ref_group_is_per_ref_and_cancels(any_ref: dict[str, Any]) -> None:
    concurrency = any_ref["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "inputs.ref" in concurrency["group"]


def test_any_ref_workflow_is_not_named_test(any_ref: dict[str, Any]) -> None:
    """deploy.yml's workflow_run and deploy_ci_gate.py select runs named Test."""

    assert any_ref["name"] != "Test"


def test_any_ref_input_is_the_only_dispatch_input(any_ref: dict[str, Any]) -> None:
    """mergewake sends only `ref`; an extra required input would refuse every dispatch."""

    inputs = _triggers(any_ref)["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["ref"]
    assert inputs["ref"]["required"] is True


# --------------------------------------------------------------------------
# 3. statuses: watched names, a differently named poster, policy wiring
# --------------------------------------------------------------------------


def test_policy_names_the_any_ref_workflow_and_the_file_exists(
    policy: dict[str, Any],
) -> None:
    assert policy["any_ref_workflow"] == "test-any-ref.yml"
    assert (REPO_ROOT / ".github/workflows" / policy["any_ref_workflow"]).is_file()


def test_status_job_posts_exactly_the_watched_checks(
    any_ref: dict[str, Any], policy: dict[str, Any]
) -> None:
    run = next(
        step["run"]
        for step in any_ref["jobs"][STATUS_JOB]["steps"]
        if "any_ref_statuses.py" in str(step.get("run", ""))
    )
    posted = WATCH_ARG.findall(run)
    assert sorted(posted) == sorted(policy["watch_checks"])


def test_status_job_name_is_not_a_watched_check(
    any_ref: dict[str, Any], policy: dict[str, Any]
) -> None:
    """mergewake reads a same-name check run and status as ambiguous when they disagree."""

    name = any_ref["jobs"][STATUS_JOB]["name"]
    assert name not in policy["watch_checks"]
    assert not any(name.endswith(" / " + watched) for watched in policy["watch_checks"])


def test_each_watched_check_is_judged_from_the_suite_job(
    policy: dict[str, Any],
) -> None:
    """The suite jobs appear as `suite / <check>`; the helper matches that form."""

    jobs = [
        {"name": f"suite / {name}", "status": "completed", "conclusion": "success"}
        for name in policy["watch_checks"]
    ]
    plan = helper.verdicts(jobs, policy["watch_checks"])
    assert set(plan) == set(policy["watch_checks"])
    assert all(state == "success" for state, _ in plan.values())


@pytest.mark.parametrize(
    "conclusion, state",
    [("success", "success"), ("failure", "failure"), ("cancelled", "failure"), ("skipped", "failure")],
)
def test_a_watched_job_is_green_only_on_success(conclusion: str, state: str) -> None:
    jobs = [{"name": "suite / lint", "status": "completed", "conclusion": conclusion}]
    assert helper.verdicts(jobs, ["lint"])["lint"][0] == state


def test_a_missing_or_duplicated_watched_job_is_never_green() -> None:
    assert helper.verdicts([], ["lint"])["lint"][0] == "failure"
    twice = [
        {"name": "suite / lint", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    assert helper.verdicts(twice, ["lint"])["lint"][0] == "failure"


def test_a_job_that_only_shares_a_suffix_is_not_the_check() -> None:
    jobs = [{"name": "suite / xlint", "status": "completed", "conclusion": "success"}]
    assert helper.verdicts(jobs, ["lint"])["lint"][0] == "failure"


def test_an_unfinished_job_is_not_green() -> None:
    jobs = [{"name": "lint", "status": "in_progress", "conclusion": None}]
    assert helper.verdicts(jobs, ["lint"])["lint"][0] == "failure"


@pytest.mark.parametrize(
    "compare_status, ancestor",
    [("behind", True), ("identical", True), ("ahead", False), ("diverged", False), ("", False)],
)
def test_only_commits_on_mains_history_are_posted(compare_status: str, ancestor: bool) -> None:
    assert helper.ancestor_of_main(compare_status) is ancestor


def test_status_description_stays_within_the_github_limit() -> None:
    assert len(helper.description("test (3.13, 4)", "x" * 500)) <= 140


def test_post_status_job_is_the_guarded_status_job(any_ref: dict[str, Any]) -> None:
    job = any_ref["jobs"][STATUS_JOB]
    assert job["if"] == "${{ always() && needs.validate.result == 'success' }}"


# --------------------------------------------------------------------------
# 4. nothing else is posted; only the status job writes statuses
# --------------------------------------------------------------------------


def test_top_level_permissions_are_read_only(any_ref: dict[str, Any]) -> None:
    assert any_ref["permissions"] == {"contents": "read"}


def test_only_the_status_job_can_write_statuses(any_ref: dict[str, Any]) -> None:
    writers = {
        job_id
        for job_id, job in any_ref["jobs"].items()
        if str((job.get("permissions") or {}).get("statuses", "")) == "write"
    }
    assert writers == {STATUS_JOB}


def test_any_ref_workflow_has_no_image_or_deploy_step(any_ref: dict[str, Any]) -> None:
    for job_id, job in any_ref["jobs"].items():
        uses = [str(step["uses"]) for step in job.get("steps", []) if "uses" in step]
        if "uses" in job:
            uses.append(str(job["uses"]))
        for value in uses:
            assert value.startswith(("actions/checkout", "./.github/workflows/test.yml")), (job_id, value)
        runs = " ".join(str(step.get("run", "")) for step in job.get("steps", []))
        for forbidden in ("docker", "ghcr", "deploy-ok", "deploy-partial", "ci-ok", "deploy.yml"):
            assert forbidden not in runs, (job_id, forbidden)


def test_helper_is_stdlib_only() -> None:
    text = HELPER_PATH.read_text(encoding="utf-8")
    imports = re.findall(r"^(?:import|from) (\w+)", text, flags=re.MULTILINE)
    assert set(imports) <= {"__future__", "argparse", "json", "subprocess", "sys"}
