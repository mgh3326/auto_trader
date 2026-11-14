"""ROB-1454 — static contract on what `.github/workflows/test.yml` runs where.

Four things must stay true and are easy to break by accident:

1. A push to main or a dispatch always runs the full suite. The `if:` on the
   full-suite jobs may only ever narrow a pull_request, and must keep running
   when the classifier job itself failed.
2. `PR gate` runs even when its children fail, and declares every job it needs.
3. `.mergewake/policy.yaml` names checks this workflow really displays: a name
   nothing reports would leave a required check unknown forever.
4. hk 2939, three tiers: T-PR (a pull_request) is light and never resolves a job to
   a runner variable or a self-hosted label; T-POST (push, dispatch) runs the rest
   on the newest main head only, and a superseded run ends with `PR gate` skipped.

Expressions are evaluated with `tests/ci/gh_expr.py` instead of compared as text,
so the tests pin what a workflow does, not how it is spelled.
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
    RETRO_CONSUMER_GATE_JOB,
)
from tests.ci import gh_expr

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
VERDICT_JOB_ID = "gate-verdict"
GATE_CHECK_NAME = "PR gate"

RUNNER_VAR = "AUTO_TRADER_POST_LINUX_RUNNER"
HOSTED = "ubuntu-latest"
SENTINEL_RUNNER = "desk-linux-sentinel"

#: Jobs that exist only in T-POST: a pull_request must never run them.
POST_ONLY_JOBS = (
    "security",
    "alpaca-track-fast-tests",
    "intraday-harness-v2-tests",
    "kiwoom-dual-surface-smoke",
    "alpaca-track-walkforward-tests",
)
#: Jobs a pull_request may run (some only on a classifier verdict or one branch).
PR_JOBS = frozenset(
    {
        "lint",
        "write-fence-guard",
        CLASSIFIER_JOB,
        AFFECTED_JOB,
        *FULL_SUITE_ONLY_JOBS,
        "frontend",
        "retro-consumer-gate",
        VERDICT_JOB_ID,
        GATE_JOB_ID,
    }
)
#: PR-only jobs: they cannot run on push or dispatch, so they never read the runner var.
PR_ONLY_JOBS = frozenset({"write-fence-guard", AFFECTED_JOB})

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


def test_workflow_triggers_add_only_workflow_dispatch_and_the_any_ref_call(
    workflow: dict[str, Any],
) -> None:
    # PyYAML parses the bare key `on` as the boolean True.
    # hk 3487: `workflow_call` is the one addition, so test-any-ref.yml can run this
    # exact definition for a bisect commit. Its `ref` defaults to empty (event ref).
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
        "workflow_dispatch": None,
        "workflow_call": {
            "inputs": {
                "ref": {
                    "description": (
                        "Exact 40-hex commit SHA to check out; empty uses the "
                        "event ref."
                    ),
                    "required": False,
                    "type": "string",
                    "default": "",
                }
            },
            "secrets": {"CODECOV_TOKEN": {"required": False}},
        },
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


def test_expand_contract_lint_runs_for_pull_requests(workflow: dict[str, Any]) -> None:
    lint = workflow["jobs"]["lint"]
    checkout = next(
        step
        for step in lint["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    steps = [
        step
        for step in lint["steps"]
        if step.get("name") == "Expand-contract migration lint"
    ]
    assert len(steps) == 1
    step = steps[0]
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "env" not in step
    assert 'event["pull_request"]["base"]["sha"]' in step["run"]
    assert 'git fetch --no-tags --depth=1 origin "${base_sha}"' in step["run"]
    assert (
        "python3 scripts/ci/migration_expand_contract_lint.py "
        "--base-ref FETCH_HEAD --head-ref HEAD --task-id"
    ) in step["run"]
    assert "GITHUB_EVENT_PATH" in step["run"]
    assert "Mergewake-Task" in step["run"]


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
    """Without `always()` the verdict job is skipped on a child failure: unknown, not red."""

    assert workflow["jobs"][VERDICT_JOB_ID]["if"] == "always()"
    assert "always()" in workflow["jobs"][GATE_JOB_ID]["if"]


def test_verdict_needs_every_job_that_decides_the_verdict(
    workflow: dict[str, Any],
) -> None:
    needs = set(workflow["jobs"][VERDICT_JOB_ID]["needs"])
    assert needs == {
        "lint",
        CLASSIFIER_JOB,
        AFFECTED_JOB,
        *FULL_SUITE_ONLY_JOBS,
        RETRO_CONSUMER_GATE_JOB,
    }


def test_gate_needs_only_the_verdict(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"][GATE_JOB_ID]["needs"] == [VERDICT_JOB_ID]


def test_verdict_required_flags_match_its_needs_list(workflow: dict[str, Any]) -> None:
    """A `needs:` entry with no `--required` flag would be silently ignored."""

    job = workflow["jobs"][VERDICT_JOB_ID]
    tokens = _script(job).replace("\\\n", " ").split()
    declared = [
        tokens[index + 1]
        for index, token in enumerate(tokens)
        if token == "--required" and index + 1 < len(tokens)
    ]
    assert sorted(declared) == sorted(job["needs"])


def test_verdict_derives_skips_only_from_the_classifier(
    workflow: dict[str, Any],
) -> None:
    script = _script(workflow["jobs"][VERDICT_JOB_ID])
    assert "scripts/ci/aggregate_required.py" in script
    assert "--pr-gate" in script
    assert "--allow-superseded" in script
    assert "--authorize-skip" not in script
    assert "--allow-undeclared" not in script


def test_verdict_publishes_the_superseded_output(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][VERDICT_JOB_ID]
    step = next(s for s in job["steps"] if s.get("id") == "verdict")
    assert "aggregate_required.py" in step["run"]
    assert job["outputs"]["superseded"] == "${{ steps.verdict.outputs.superseded }}"


def _gate_context(
    event: str, *, superseded: str = "", verdict_result: str = "success"
) -> dict[str, Any]:
    return {
        "github": {"event_name": event},
        "needs": {
            VERDICT_JOB_ID: {
                "result": verdict_result,
                "outputs": {"superseded": superseded},
            }
        },
    }


def _gate_runs(workflow: dict[str, Any], context: dict[str, Any]) -> bool:
    return gh_expr.truthy(
        gh_expr.evaluate(workflow["jobs"][GATE_JOB_ID]["if"], context)
    )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_gate_is_skipped_when_a_t_post_run_is_superseded(
    workflow: dict[str, Any], event: str
) -> None:
    """Skipped, not failed: mergewake reads a skipped check as unknown and holds."""

    assert not _gate_runs(workflow, _gate_context(event, superseded="true"))


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize("verdict_result", ["failure", "skipped"])
def test_gate_runs_when_a_failed_verdict_still_wrote_superseded(
    workflow: dict[str, Any], event: str, verdict_result: str
) -> None:
    """A verdict step that wrote superseded=true before a later step failed must
    not turn the gate into a skip; the gate runs and goes red."""

    assert _gate_runs(
        workflow,
        _gate_context(event, superseded="true", verdict_result=verdict_result),
    )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_gate_is_skipped_when_the_verdict_job_was_cancelled_with_its_run(
    workflow: dict[str, Any], event: str
) -> None:
    assert not _gate_runs(workflow, _gate_context(event, verdict_result="cancelled"))


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize("verdict_result", ["success", "failure", "skipped"])
def test_gate_runs_on_t_post_whenever_the_run_was_not_superseded(
    workflow: dict[str, Any], event: str, verdict_result: str
) -> None:
    assert _gate_runs(
        workflow,
        _gate_context(event, superseded="false", verdict_result=verdict_result),
    )
    assert _gate_runs(workflow, _gate_context(event, verdict_result=verdict_result))


@pytest.mark.parametrize("superseded", ["", "false", "true"])
@pytest.mark.parametrize(
    "verdict_result", ["success", "failure", "cancelled", "skipped"]
)
def test_gate_never_skips_on_a_pull_request(
    workflow: dict[str, Any], superseded: str, verdict_result: str
) -> None:
    """A skipped required check is a green merge button to GitHub."""

    assert _gate_runs(
        workflow,
        _gate_context(
            "pull_request", superseded=superseded, verdict_result=verdict_result
        ),
    )


def test_gate_goes_red_unless_the_verdict_succeeded(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][GATE_JOB_ID]
    step = job["steps"][0]
    assert "needs.gate-verdict.result" in step["env"]["VERDICT_RESULT"]
    assert '[ "$VERDICT_RESULT" != "success" ]' in step["run"]
    assert "exit 1" in step["run"]


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


def _concurrency(
    workflow: dict[str, Any], event_name: str, **github: Any
) -> tuple[str, bool]:
    block = workflow["concurrency"]
    context = {"github": {"workflow": "Test", "event_name": event_name, **github}}
    return (
        str(gh_expr.evaluate(block["group"], context)),
        gh_expr.truthy(gh_expr.evaluate(block["cancel-in-progress"], context)),
    )


def _pr_concurrency(workflow: dict[str, Any], number: int = 7) -> tuple[str, bool]:
    return _concurrency(
        workflow,
        "pull_request",
        ref=f"refs/pull/{number}/merge",
        run_id=1,
        event={"pull_request": {"number": number}},
    )


def _push_concurrency(
    workflow: dict[str, Any], ref: str = "refs/heads/main"
) -> tuple[str, bool]:
    return _concurrency(workflow, "push", ref=ref, run_id=1, event={})


def _dispatch_concurrency(workflow: dict[str, Any], run_id: int) -> tuple[str, bool]:
    return _concurrency(
        workflow, "workflow_dispatch", ref="refs/heads/main", run_id=run_id, event={}
    )


def test_pull_request_concurrency_is_unchanged(workflow: dict[str, Any]) -> None:
    """One group per PR number, newest head wins."""

    assert _pr_concurrency(workflow, 7) == ("Test-7", True)
    assert _pr_concurrency(workflow, 8)[0] == "Test-8"


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/develop"])
def test_a_push_cancels_the_previous_push_run_of_the_same_ref(
    workflow: dict[str, Any], ref: str
) -> None:
    """T-POST tests only the newest head: same group per ref, cancel in progress."""

    assert _push_concurrency(workflow, ref) == (f"Test-{ref}", True)


def test_dispatch_is_never_cancelled_by_a_push_or_another_dispatch(
    workflow: dict[str, Any],
) -> None:
    """Deploy-SHA and bisect dispatches must survive a newer push to main.

    hk 3487: `cancel-in-progress` is the literal true now (mergewake ci-policy cannot
    verify an expression). The protection moved into the group: a dispatch's group
    is its own run id and attempt, so nothing else ever shares it.
    """

    first = _dispatch_concurrency(workflow, 101)
    second = _dispatch_concurrency(workflow, 102)
    assert first[1] is True and second[1] is True
    groups = {
        first[0],
        second[0],
        _push_concurrency(workflow)[0],
        _pr_concurrency(workflow)[0],
    }
    assert len(groups) == 4


def test_a_dispatch_rerun_attempt_does_not_cancel_the_attempt_it_reruns(
    workflow: dict[str, Any],
) -> None:
    """A re-run keeps the run id; the attempt number keeps it out of the old group."""

    first_attempt = _concurrency(
        workflow,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=101,
        run_attempt=1,
        event={},
    )
    second_attempt = _concurrency(
        workflow,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=101,
        run_attempt=2,
        event={},
    )
    assert first_attempt[0] != second_attempt[0]


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


# --------------------------------------------------------------------------
# hk 2939: tiers and runner selection
# --------------------------------------------------------------------------


def _job_context(
    event: str,
    *,
    run_all: str = "false",
    run_affected: str = "true",
    lanes: str = "app",
    head_ref: str = "feature/x",
) -> dict[str, Any]:
    needs = {
        name: {"result": "success", "outputs": {}}
        for name in ("lint", AFFECTED_JOB, *FULL_SUITE_ONLY_JOBS)
    }
    needs[CLASSIFIER_JOB] = {
        "result": "success",
        "outputs": {
            "run_all": run_all,
            "run_affected_tests": run_affected,
            "lanes": lanes,
        },
    }
    return {
        "github": {
            "event_name": event,
            "event": {"pull_request": {"head": {"ref": head_ref}}},
        },
        "needs": needs,
    }


def _classifier_verdicts(event: str) -> list[dict[str, Any]]:
    return [
        _job_context(
            event,
            run_all=run_all,
            run_affected=run_affected,
            lanes=lanes,
            head_ref=head_ref,
        )
        for run_all in ("true", "false")
        for run_affected in ("true", "false")
        for lanes in ("app", "frontend", "docs,frontend", "ci_shared")
        for head_ref in ("feature/x", "fix/j3a-write-fence-test")
    ]


def _runs_on_event(job: dict[str, Any], event: str) -> bool:
    """Can this job run at all on `event`, under some classifier verdict?"""

    if "if" not in job:
        return True
    return any(
        gh_expr.truthy(gh_expr.evaluate(job["if"], context))
        for context in _classifier_verdicts(event)
    )


def _runs_on_label(job: dict[str, Any], event: str, runner_var: str | None) -> Any:
    context = _job_context(event)
    context["vars"] = {} if runner_var is None else {RUNNER_VAR: runner_var}
    runs_on = job["runs-on"]
    if "${{" not in runs_on:
        return runs_on  # a literal label
    return gh_expr.evaluate(runs_on, context)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_every_job_runs_on_t_post_except_the_pr_only_ones(
    workflow: dict[str, Any], event: str
) -> None:
    runs = {
        job_id for job_id, job in workflow["jobs"].items() if _runs_on_event(job, event)
    }
    assert runs == set(workflow["jobs"]) - PR_ONLY_JOBS


def test_t_pr_runs_only_the_light_jobs(workflow: dict[str, Any]) -> None:
    """The five T-POST research/smoke/security jobs are unreachable from a PR."""

    on_pr = {
        job_id
        for job_id, job in workflow["jobs"].items()
        if _runs_on_event(job, "pull_request")
    }
    assert on_pr == PR_JOBS
    assert not on_pr & set(POST_ONLY_JOBS)


@pytest.mark.parametrize("job_id", POST_ONLY_JOBS)
def test_post_only_job_condition_is_a_non_pull_request_event(
    workflow: dict[str, Any], job_id: str
) -> None:
    job = workflow["jobs"][job_id]
    assert not _runs_on_event(job, "pull_request")
    assert gh_expr.truthy(
        gh_expr.evaluate(job["if"], _job_context("push"))
    ) and gh_expr.truthy(gh_expr.evaluate(job["if"], _job_context("workflow_dispatch")))


def test_frontend_runs_on_a_pull_request_when_frontend_changed_or_on_full_suite(
    workflow: dict[str, Any],
) -> None:
    condition = workflow["jobs"]["frontend"]["if"]
    assert workflow["jobs"]["frontend"]["needs"] == CLASSIFIER_JOB
    for lanes, expected in (
        ("app,tests", False),
        ("docs", False),
        ("", False),
        ("frontend", True),
        ("app,frontend", True),
    ):
        context = _job_context("pull_request", lanes=lanes)
        assert gh_expr.truthy(gh_expr.evaluate(condition, context)) is expected, lanes
    # A full-suite fallback (rename/delete under frontend/, unknown path, a
    # crashed classifier) runs the frontend job too, as it runs the shards.
    for run_all in ("true", ""):
        context = _job_context("pull_request", run_all=run_all, lanes="ci_shared")
        assert gh_expr.truthy(gh_expr.evaluate(condition, context)), run_all
    for event in ("push", "workflow_dispatch"):
        assert gh_expr.truthy(
            gh_expr.evaluate(condition, _job_context(event, lanes=""))
        )


@pytest.mark.parametrize("runner_var", [None, "", SENTINEL_RUNNER, "self-hosted"])
def test_no_pull_request_job_resolves_to_a_variable_or_a_self_hosted_label(
    workflow: dict[str, Any], runner_var: str | None
) -> None:
    """The repo is public: PR code must never reach the desk's runners."""

    for job_id, job in workflow["jobs"].items():
        assert _runs_on_label(job, "pull_request", runner_var) == HOSTED, job_id


def test_pull_request_reachable_jobs_name_no_self_hosted_label_or_runner_variable(
    workflow: dict[str, Any],
) -> None:
    for job_id, job in workflow["jobs"].items():
        runs_on = job["runs-on"]
        assert isinstance(runs_on, str), job_id
        assert "self-hosted" not in runs_on.lower(), job_id
        assert "macos" not in runs_on.lower(), job_id
        if job_id in PR_ONLY_JOBS:
            assert runs_on == HOSTED, job_id
        else:
            assert f"vars.{RUNNER_VAR}" in runs_on, job_id
            assert "github.event_name != 'pull_request'" in runs_on, job_id


def test_runner_variable_is_read_only_by_runs_on(workflow: dict[str, Any]) -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    runs_on_lines = [line for line in text.splitlines() if "runs-on:" in line]
    for line in text.splitlines():
        if "vars." in line and not line.lstrip().startswith("#"):
            assert line in runs_on_lines, line


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_t_post_jobs_use_the_runner_variable_and_fall_back_to_hosted(
    workflow: dict[str, Any], event: str
) -> None:
    for job_id, job in workflow["jobs"].items():
        if job_id in PR_ONLY_JOBS:
            continue
        assert _runs_on_label(job, event, SENTINEL_RUNNER) == SENTINEL_RUNNER, job_id
        assert _runs_on_label(job, event, None) == HOSTED, job_id
        assert _runs_on_label(job, event, "") == HOSTED, job_id
