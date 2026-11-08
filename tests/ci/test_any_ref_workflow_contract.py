"""hk 3487 — static and behavioural contract for the any-ref CI.

mergewake dispatches `.github/workflows/test-any-ref.yml` for one bisect commit whose
push run was cancelled, and reads the commit statuses it gets. These tests pin what
must stay true so that path can be trusted:

1. `test.yml` cancels with the literal true, and its concurrency groups keep a dispatch
   out of every push's group (what cancels what, per event kind).
2. The any-ref run validates its ref before any checkout, then calls `test.yml` for that
   exact commit (every checkout there reads `inputs.ref`), then records the commit and the
   watched check names.
3. Statuses are posted only by `post-any-ref-status.yml`, a `workflow_run` workflow that
   GitHub loads from the default branch, and only for a dispatch from `main`. No branch-run
   workflow holds `statuses: write`.
4. Each watched check is posted by a job whose name is not a watched check, the policy
   names the any-ref workflow, and that file exists.
5. Nothing else is posted (no image, no deploy-ok, no ci-ok).

Expressions are evaluated with `tests/ci/gh_expr.py`, and the validation step is run under
bash, so the tests check behaviour rather than spelling.
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
POST_WORKFLOW = REPO_ROOT / ".github/workflows/post-any-ref-status.yml"
POLICY_PATH = REPO_ROOT / ".mergewake/policy.yaml"
HELPER_PATH = REPO_ROOT / ".github/scripts/any_ref_statuses.py"

ANY_REF_NAME = "Test (any ref)"
VALIDATE_JOB = "validate"
SUITE_JOB = "suite"
RECORD_JOB = "record"
POST_JOB = "post"
RESOLVE_JOB = "resolve"
REFUSE_STEP = "Refuse a called ref on a pull_request"
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("any_ref_statuses", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow.get("on", workflow.get(True))


@pytest.fixture(scope="module")
def test_wf() -> dict[str, Any]:
    return _load(TEST_WORKFLOW)


@pytest.fixture(scope="module")
def any_ref() -> dict[str, Any]:
    return _load(ANY_REF_WORKFLOW)


@pytest.fixture(scope="module")
def post() -> dict[str, Any]:
    return _load(POST_WORKFLOW)


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return _load(POLICY_PATH)


def _group(
    workflow: dict[str, Any], event_name: str, **github: Any
) -> tuple[str, bool]:
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


def _step_run(job: dict[str, Any], marker: str) -> str:
    return next(
        str(step["run"]) for step in job["steps"] if marker in str(step.get("run", ""))
    )


def _watched_array(run: str) -> list[str]:
    block = re.search(r"watched=\((.*?)\n\s*\)", run, flags=re.DOTALL)
    assert block is not None, "post step must declare its watched list in one array"
    return re.findall(r"'([^']*)'", block.group(1))


def _recorded_names(run: str) -> list[str]:
    # The printf format is '%s\n'; every other single-quoted token is a check name.
    return [name for name in re.findall(r"'([^']*)'", run) if not name.startswith("%")]


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
        test_wf,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=7,
        run_attempt=1,
        event={},
    )
    second_run = _group(
        test_wf,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=8,
        run_attempt=1,
        event={},
    )
    rerun = _group(
        test_wf,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=7,
        run_attempt=2,
        event={},
    )
    assert first[0] == "Test-dispatch-7-1"
    assert len({first[0], second_run[0], rerun[0]}) == 3


def test_a_workflow_call_from_any_ref_has_its_own_dispatch_group(
    test_wf: dict[str, Any],
) -> None:
    """The called run inherits event_name workflow_dispatch from test-any-ref.yml."""

    called = _group(
        test_wf,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=55,
        run_attempt=1,
        event={},
    )
    push_main = _group(test_wf, "push", ref="refs/heads/main", run_id=56, event={})
    assert called[0] != push_main[0]


@pytest.mark.parametrize(
    "event_kind, kwargs",
    [
        ("push", {"ref": "refs/heads/main", "run_id": 9, "event": {}}),
        (
            "pull_request",
            {
                "ref": "refs/pull/4/merge",
                "run_id": 9,
                "event": {"pull_request": {"number": 4}},
            },
        ),
        (
            "workflow_dispatch",
            {"ref": "refs/heads/main", "run_id": 9, "run_attempt": 1, "event": {}},
        ),
    ],
)
def test_no_push_group_collides_with_a_dispatch_group(
    test_wf: dict[str, Any], event_kind: str, kwargs: dict[str, Any]
) -> None:
    """A dispatch is never cancelled by a push, a PR, or another dispatch."""

    dispatch = _group(
        test_wf,
        "workflow_dispatch",
        ref="refs/heads/main",
        run_id=100,
        run_attempt=1,
        event={},
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
# 2. the any-ref run: validate, then call test.yml, then record
# --------------------------------------------------------------------------


def _validation_script(any_ref: dict[str, Any]) -> str:
    return any_ref["jobs"][VALIDATE_JOB]["steps"][0]["run"]


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
    [
        "",
        "main",
        "abc",
        VALID_SHA[:39],
        VALID_SHA + "0",
        "g" * 40,
        "../" + VALID_SHA[:37],
    ],
)
def test_ref_is_refused_unless_it_is_an_exact_40_hex_sha(
    any_ref: dict[str, Any], value: str
) -> None:
    assert _run_validation(_validation_script(any_ref), value).returncode != 0


def test_a_40_hex_sha_passes_the_format_check(any_ref: dict[str, Any]) -> None:
    assert _run_validation(_validation_script(any_ref), VALID_SHA).returncode == 0
    assert (
        _run_validation(_validation_script(any_ref), VALID_SHA.upper()).returncode == 0
    )


def test_validation_is_the_first_step_and_runs_before_any_checkout(
    any_ref: dict[str, Any],
) -> None:
    validate = any_ref["jobs"][VALIDATE_JOB]
    assert "actions/checkout" not in str(validate.get("steps"))
    assert "[[" in validate["steps"][0]["run"]


def test_every_other_job_waits_for_validation(any_ref: dict[str, Any]) -> None:
    jobs = any_ref["jobs"]
    assert jobs[SUITE_JOB]["needs"] == VALIDATE_JOB
    assert jobs[RECORD_JOB]["needs"] == VALIDATE_JOB


def test_suite_calls_test_yml_for_the_exact_ref(any_ref: dict[str, Any]) -> None:
    suite = any_ref["jobs"][SUITE_JOB]
    assert suite["uses"] == "./.github/workflows/test.yml"
    assert suite["with"] == {"ref": "${{ inputs.ref }}"}
    # Not `inherit`: a branch can edit this file, so only the one secret the suite reads.
    assert suite["secrets"] == {"CODECOV_TOKEN": "${{ secrets.CODECOV_TOKEN }}"}


def _any_ref_group(any_ref: dict[str, Any], run_id: int, attempt: int) -> str:
    context = {"github": {"run_id": run_id, "run_attempt": attempt}}
    return str(gh_expr.evaluate(any_ref["concurrency"]["group"], context))


def test_any_ref_group_is_per_dispatch_and_attempt(any_ref: dict[str, Any]) -> None:
    """Two dispatches of one commit must not share a group: the second must not cancel the first.

    A cancelled run's workflow_run poster could otherwise post failures after a newer
    run's success on the same commit (PR #2317, r4506345303).
    """

    assert any_ref["concurrency"]["cancel-in-progress"] is True
    same_commit_first = _any_ref_group(any_ref, 10, 1)
    same_commit_second = _any_ref_group(any_ref, 11, 1)
    rerun = _any_ref_group(any_ref, 10, 2)
    assert len({same_commit_first, same_commit_second, rerun}) == 3


def test_any_ref_workflow_is_not_named_test(any_ref: dict[str, Any]) -> None:
    """deploy.yml's workflow_run and deploy_ci_gate.py select runs named Test."""

    assert any_ref["name"] == ANY_REF_NAME


def test_any_ref_input_is_the_only_dispatch_input(any_ref: dict[str, Any]) -> None:
    """mergewake sends only `ref`; an extra required input would refuse every dispatch."""

    inputs = _triggers(any_ref)["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["ref"]
    assert inputs["ref"]["required"] is True


def test_record_job_names_the_watched_checks_it_expects_published(
    any_ref: dict[str, Any], policy: dict[str, Any]
) -> None:
    run = _step_run(any_ref["jobs"][RECORD_JOB], "expected-checks.txt")
    assert sorted(_recorded_names(run)) == sorted(policy["watch_checks"])


def test_record_job_uploads_the_target_record(any_ref: dict[str, Any]) -> None:
    upload = next(
        step
        for step in any_ref["jobs"][RECORD_JOB]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )
    assert upload["with"]["name"] == "any-ref-target"


# --------------------------------------------------------------------------
# 3. the post workflow: default-branch code, main-only gate, pinned checkout
# --------------------------------------------------------------------------


def test_post_workflow_is_a_workflow_run_of_the_any_ref_run(
    post: dict[str, Any],
) -> None:
    triggers = _triggers(post)
    assert set(triggers) == {"workflow_run"}
    assert triggers["workflow_run"] == {
        "workflows": [ANY_REF_NAME],
        "types": ["completed"],
    }


def test_post_workflow_gate_passes_only_a_dispatch_from_main(
    post: dict[str, Any],
) -> None:
    expression = post["jobs"][RESOLVE_JOB]["if"]
    base = {
        "github": {
            "event": {
                "workflow_run": {
                    "event": "workflow_dispatch",
                    "head_branch": "main",
                    "head_repository": {"full_name": "mgh3326/auto_trader"},
                }
            },
            "repository": "mgh3326/auto_trader",
        }
    }

    def gate(**changes: Any) -> bool:
        context = {"github": {**base["github"]}}
        run = dict(base["github"]["event"]["workflow_run"])
        for key, value in changes.items():
            run[key] = value
        context["github"]["event"] = {"workflow_run": run}
        return gh_expr.truthy(gh_expr.evaluate(expression, context))

    assert gate() is True
    assert gate(head_branch="feature/x") is False
    assert gate(event="push") is False
    assert gate(head_repository={"full_name": "someone/fork"}) is False


def test_post_checkout_pins_the_default_branch(post: dict[str, Any]) -> None:
    checkouts = _checkouts(post["jobs"][POST_JOB])
    assert len(checkouts) == 1
    assert (
        checkouts[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    )


def _run_resolve(
    tmp_path: Path,
    post: dict[str, Any],
    sha: str,
    validate: str = "success",
    download_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the resolve step against a stub `gh`.

    The stub answers the triggering run's jobs query with `validate` (the validate job's
    conclusion, or "none" when absent) and answers the artifact download with `sha` when
    `download_ok`, else fails the way a missing or unreadable artifact does.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    gh = stub / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = api ]; then printf \'%s\' "$FAKE_VALIDATE"; exit 0; fi\n'
        'if [ "$FAKE_DOWNLOAD_OK" != 1 ]; then exit 1; fi\n'
        'mkdir -p "$9"\n'
        'printf \'%s\\n\' "$FAKE_SHA" > "$9/sha.txt"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    script = _step_run(post["jobs"][RESOLVE_JOB], "any-ref-target")
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{stub}:/usr/bin:/bin",
            "FAKE_SHA": sha,
            "FAKE_VALIDATE": validate,
            "FAKE_DOWNLOAD_OK": "1" if download_ok else "0",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(output),
            "RUN_ID": "1",
            "RUN_ATTEMPT": "1",
            "REPO": "mgh3326/auto_trader",
            "GH_TOKEN": "x",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _outputs(tmp_path: Path) -> str:
    return (tmp_path / "github_output").read_text(encoding="utf-8")


def test_resolve_accepts_a_recorded_sha_and_publishes_it(
    post: dict[str, Any], tmp_path: Path
) -> None:
    result = _run_resolve(tmp_path, post, VALID_SHA)
    assert result.returncode == 0, result.stderr
    assert f"sha={VALID_SHA}" in _outputs(tmp_path)
    assert "has_target=true" in _outputs(tmp_path)


@pytest.mark.parametrize("validate", ["failure", "cancelled", "skipped", "none"])
def test_a_run_whose_validation_did_not_pass_posts_nothing_quietly(
    post: dict[str, Any], tmp_path: Path, validate: str
) -> None:
    """No target was recorded because validation did not pass: a successful no-op."""

    result = _run_resolve(
        tmp_path, post, VALID_SHA, validate=validate, download_ok=False
    )
    assert result.returncode == 0, result.stderr
    assert "has_target=false" in _outputs(tmp_path)


def test_a_passed_validation_whose_target_cannot_be_read_fails(
    post: dict[str, Any], tmp_path: Path
) -> None:
    """A retrieval error after a passed validation is a failure, so the run can be retried."""

    result = _run_resolve(
        tmp_path, post, VALID_SHA, validate="success", download_ok=False
    )
    assert result.returncode != 0
    assert "has_target" not in _outputs(tmp_path)


@pytest.mark.parametrize("sha", ["main", "abc", "g" * 40, "0" * 39])
def test_resolve_refuses_a_recorded_value_that_is_not_a_sha(
    post: dict[str, Any], tmp_path: Path, sha: str
) -> None:
    result = _run_resolve(tmp_path, post, sha)
    assert result.returncode != 0
    assert "sha=" not in _outputs(tmp_path)


def test_resolve_holds_no_statuses_permission(post: dict[str, Any]) -> None:
    assert "statuses" not in post["jobs"][RESOLVE_JOB].get("permissions", {})


def test_post_waits_for_resolve_and_serialises_per_commit(post: dict[str, Any]) -> None:
    job = post["jobs"][POST_JOB]
    assert job["needs"] == RESOLVE_JOB
    assert job["concurrency"]["cancel-in-progress"] is False

    def group(sha: str) -> str:
        context = {"needs": {"resolve": {"outputs": {"sha": sha}}}}
        return str(gh_expr.evaluate(job["concurrency"]["group"], context))

    assert group(VALID_SHA) == group(VALID_SHA)
    assert group(VALID_SHA) != group("f" * 40)


def test_post_watch_list_matches_policy_and_the_record(
    post: dict[str, Any], policy: dict[str, Any], any_ref: dict[str, Any]
) -> None:
    run = _step_run(post["jobs"][POST_JOB], "any_ref_statuses.py")
    watched = _watched_array(run)
    assert sorted(watched) == sorted(policy["watch_checks"])
    recorded = _recorded_names(
        _step_run(any_ref["jobs"][RECORD_JOB], "expected-checks.txt")
    )
    assert sorted(watched) == sorted(recorded)


def test_post_job_name_is_not_a_watched_check(
    post: dict[str, Any], policy: dict[str, Any]
) -> None:
    """mergewake reads a same-name check run and status as ambiguous when they disagree."""

    name = post["jobs"][POST_JOB]["name"]
    assert name not in policy["watch_checks"]
    assert not any(name.endswith(" / " + watched) for watched in policy["watch_checks"])


def test_post_workflow_is_not_named_test(post: dict[str, Any]) -> None:
    assert post["name"] != "Test"


@pytest.mark.parametrize(
    "conclusion, state",
    [
        ("success", "success"),
        ("failure", "failure"),
        ("cancelled", "failure"),
        ("skipped", "failure"),
    ],
)
def test_a_watched_job_is_green_only_on_success(conclusion: str, state: str) -> None:
    jobs = [{"name": "suite / lint", "status": "completed", "conclusion": conclusion}]
    assert helper.verdicts(jobs, ["lint"])["lint"][0] == state


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
    [
        ("behind", True),
        ("identical", True),
        ("ahead", False),
        ("diverged", False),
        ("", False),
    ],
)
def test_only_commits_on_mains_history_are_posted(
    compare_status: str, ancestor: bool
) -> None:
    assert helper.ancestor_of_main(compare_status) is ancestor


def test_status_description_stays_within_the_github_limit() -> None:
    assert len(helper.description("test (3.13, 4)", "x" * 500)) <= 140


# --------------------------------------------------------------------------
# 4. the policy, and the rest of what each file may do
# --------------------------------------------------------------------------


def test_policy_names_the_any_ref_workflow_and_the_file_exists(
    policy: dict[str, Any],
) -> None:
    assert policy["any_ref_workflow"] == "test-any-ref.yml"
    assert (REPO_ROOT / ".github/workflows" / policy["any_ref_workflow"]).is_file()


def test_top_level_permissions_are_read_only(
    any_ref: dict[str, Any], post: dict[str, Any]
) -> None:
    assert any_ref["permissions"] == {"contents": "read"}
    assert post["permissions"] == {"contents": "read"}


def test_no_branch_run_workflow_can_write_statuses(any_ref: dict[str, Any]) -> None:
    """A dispatch from any branch runs this file, so it must not be able to post."""

    writers = [
        job_id
        for job_id, job in any_ref["jobs"].items()
        if str((job.get("permissions") or {}).get("statuses", "")) == "write"
    ]
    assert writers == []


def test_only_the_post_job_can_write_statuses(post: dict[str, Any]) -> None:
    writers = {
        job_id
        for job_id, job in post["jobs"].items()
        if str((job.get("permissions") or {}).get("statuses", "")) == "write"
    }
    assert writers == {POST_JOB}


def test_any_ref_workflow_has_no_image_or_deploy_step(any_ref: dict[str, Any]) -> None:
    allowed = (
        "actions/checkout",
        "actions/upload-artifact",
        "./.github/workflows/test.yml",
    )
    for job_id, job in any_ref["jobs"].items():
        uses = [str(step["uses"]) for step in job.get("steps", []) if "uses" in step]
        if "uses" in job:
            uses.append(str(job["uses"]))
        for value in uses:
            assert value.startswith(allowed), (job_id, value)
        runs = " ".join(str(step.get("run", "")) for step in job.get("steps", []))
        for forbidden in (
            "docker",
            "ghcr",
            "deploy-ok",
            "deploy-partial",
            "ci-ok",
            "deploy.yml",
        ):
            assert forbidden not in runs, (job_id, forbidden)


def test_helper_is_stdlib_only() -> None:
    text = HELPER_PATH.read_text(encoding="utf-8")
    imports = re.findall(r"^(?:import|from) (\w+)", text, flags=re.MULTILINE)
    assert set(imports) <= {"__future__", "argparse", "json", "subprocess", "sys"}


# --------------------------------------------------------------------------
# 5. newest run per commit, the run title, secret narrowing, the PR refusal
# --------------------------------------------------------------------------


def test_run_title_is_the_commit_the_run_tests(any_ref: dict[str, Any]) -> None:
    """The poster finds runs by this title, so the caller's run-name must match the helper."""

    assert any_ref["run-name"] == "any-ref ${{ inputs.ref }}"
    assert helper.run_title(VALID_SHA) == f"any-ref {VALID_SHA}"


@pytest.mark.parametrize(
    "runs, run_id, attempt, expected",
    [
        ([], 5, 1, []),
        (
            [{"id": 9, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 1}],
            5,
            1,
            [(9, 1)],
        ),
        (
            [{"id": 4, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 1}],
            5,
            1,
            [],
        ),
        (
            [{"id": 9, "display_title": "any-ref " + "f" * 40, "run_attempt": 1}],
            5,
            1,
            [],
        ),
        (
            [
                {"id": 7, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 1},
                {"id": 12, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 1},
                {"id": 20, "display_title": "any-ref " + "f" * 40, "run_attempt": 1},
            ],
            5,
            1,
            [(12, 1), (7, 1)],
        ),
        # A re-run keeps the run id; the later attempt is newer than the earlier one.
        (
            [{"id": 5, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 2}],
            5,
            1,
            [(5, 2)],
        ),
        (
            [{"id": 5, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 2}],
            5,
            2,
            [],
        ),
        (
            [{"id": 5, "display_title": f"any-ref {VALID_SHA}", "run_attempt": 1}],
            5,
            2,
            [],
        ),
    ],
)
def test_newer_runs_for_the_commit_are_listed_by_id_and_attempt(
    runs: list[dict[str, Any]],
    run_id: int,
    attempt: int,
    expected: list[tuple[int, int]],
) -> None:
    assert (
        helper.newer_runs(runs, helper.run_title(VALID_SHA), run_id, attempt)
        == expected
    )


# A newer run or attempt suppresses an older poster only when it recorded a target. These
# tests run the real `main` with the two gh seams stubbed: `_gh_json` (every read) and
# `_post` (every status write). Any unrouted read fails the test.

OUR_RUN = 5
OUR_ATTEMPT = 1
WATCHED = [
    "lint",
    "taskiq-smoke",
    "test (3.13, 1)",
    "test (3.13, 2)",
    "test (3.13, 3)",
    "test (3.13, 4)",
]
REPO = "mgh3326/auto_trader"


def _job(
    name: str, conclusion: str | None, status: str = "completed"
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def _green_suite(names: list[str]) -> list[dict[str, Any]]:
    return [_job(name, "success") for name in names]


def _listed(run_id: int, attempt: int) -> dict[str, Any]:
    return {
        "id": run_id,
        "display_title": f"any-ref {VALID_SHA}",
        "run_attempt": attempt,
    }


class _StubGh:
    """Answers the helper's gh reads from a table; records the status writes it makes."""

    def __init__(
        self,
        runs: list[dict[str, Any]],
        attempts: dict[tuple[int, int], list[dict[str, Any]] | Exception],
    ) -> None:
        self.runs = runs
        self.attempts = attempts
        self.posted: list[tuple[str, str]] = []

    def gh_json(self, args: list[str]) -> dict[str, Any]:
        path = args[0]
        if "/compare/main..." in path:
            return {"status": "behind"}
        if "/actions/workflows/test-any-ref.yml/runs" in path:
            return {"workflow_runs": self.runs}
        match = re.search(r"/actions/runs/(\d+)/attempts/(\d+)/jobs", path)
        if match:
            key = (int(match.group(1)), int(match.group(2)))
            if key == (OUR_RUN, OUR_ATTEMPT):
                return {"jobs": _green_suite(WATCHED)}
            if key not in self.attempts:
                raise AssertionError(f"unrouted jobs read for run {key}")
            answer = self.attempts[key]
            if isinstance(answer, Exception):
                raise answer
            return {"jobs": answer}
        raise AssertionError(f"unrouted gh read: {path}")

    def post(
        self, repo: str, sha: str, context: str, state: str, text: str, url: str
    ) -> bool:
        self.posted.append((context, state))
        return True


def _run_main(
    monkeypatch: pytest.MonkeyPatch, stub: _StubGh
) -> tuple[int, list[tuple[str, str]]]:
    monkeypatch.setattr(helper, "_gh_json", stub.gh_json)
    monkeypatch.setattr(helper, "_post", stub.post)
    argv = [
        "--repo",
        REPO,
        "--sha",
        VALID_SHA,
        "--run-id",
        str(OUR_RUN),
        "--run-attempt",
        str(OUR_ATTEMPT),
        "--target-url",
        "https://example.invalid/run",
    ]
    for name in WATCHED:
        argv += ["--watch", name]
    return helper.main(argv), stub.posted


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ([_job("validate ref", "success")], True),
        ([_job("validate ref", None, status="queued")], False),
        ([_job("validate ref", None, status="in_progress")], False),
        ([_job("validate ref", "cancelled")], False),
        ([_job("validate ref", "failure")], False),
        ([_job("validate ref", "skipped")], False),
        ([], False),
        ([_job("validate ref", "success"), _job("validate ref", "success")], False),
        ([_job("lint", "success")], False),
    ],
)
def test_only_a_successful_validate_ref_job_marks_a_run_as_publishing(
    jobs: list[dict[str, Any]], expected: bool
) -> None:
    assert helper.validate_ref_green(jobs) is expected


@pytest.mark.parametrize(
    "newer",
    [
        # A newer attempt that will never publish must not suppress this poster.
        pytest.param(
            {"attempt": 2, "validate": [_job("validate ref", None, status="queued")]},
            id="attempt-2-queued",
        ),
        pytest.param(
            {"attempt": 2, "validate": [_job("validate ref", "cancelled")]},
            id="attempt-2-cancelled",
        ),
        pytest.param(
            {"attempt": 2, "validate": [_job("validate ref", "failure")]},
            id="attempt-2-validate-failed",
        ),
        pytest.param(
            {"attempt": 2, "validate": []},
            id="attempt-2-never-recorded-a-target",
        ),
    ],
)
def test_a_newer_attempt_that_will_not_publish_does_not_suppress_this_poster(
    monkeypatch: pytest.MonkeyPatch, newer: dict[str, Any]
) -> None:
    stub = _StubGh(
        runs=[_listed(OUR_RUN, newer["attempt"])],
        attempts={(OUR_RUN, newer["attempt"]): newer["validate"]},
    )
    status, posted = _run_main(monkeypatch, stub)
    assert status == 0
    assert sorted(posted) == sorted((name, "success") for name in WATCHED)


@pytest.mark.parametrize(
    "newer_runs_jobs, expected_posts",
    [
        pytest.param(
            [(OUR_RUN + 1, 1, [_job("validate ref", None, status="queued")])],
            len(WATCHED),
            id="newer-run-queued",
        ),
        pytest.param(
            [(OUR_RUN + 1, 1, [_job("validate ref", "cancelled")])],
            len(WATCHED),
            id="newer-run-cancelled",
        ),
        pytest.param(
            [(OUR_RUN + 1, 1, [_job("validate ref", "failure")])],
            len(WATCHED),
            id="newer-run-validate-failed",
        ),
    ],
)
def test_a_newer_run_that_will_not_publish_does_not_suppress_this_poster(
    monkeypatch: pytest.MonkeyPatch,
    newer_runs_jobs: list[tuple[int, int, list[dict[str, Any]]]],
    expected_posts: int,
) -> None:
    runs = [_listed(run_id, attempt) for run_id, attempt, _ in newer_runs_jobs]
    attempts = {(run_id, attempt): jobs for run_id, attempt, jobs in newer_runs_jobs}
    stub = _StubGh(runs=runs, attempts=attempts)
    status, posted = _run_main(monkeypatch, stub)
    assert status == 0
    assert len(posted) == expected_posts


def test_a_newer_run_that_recorded_a_target_supersedes_this_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubGh(
        runs=[_listed(OUR_RUN + 1, 1)],
        attempts={(OUR_RUN + 1, 1): [_job("validate ref", "success")]},
    )
    status, posted = _run_main(monkeypatch, stub)
    assert status == 0
    assert posted == []


def test_a_newer_attempt_that_recorded_a_target_supersedes_this_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubGh(
        runs=[_listed(OUR_RUN, 2)],
        attempts={(OUR_RUN, 2): [_job("validate ref", "success")]},
    )
    status, posted = _run_main(monkeypatch, stub)
    assert status == 0
    assert posted == []


def test_an_unreadable_newer_run_fails_the_poster_instead_of_going_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _StubGh(
        runs=[_listed(OUR_RUN + 1, 1)],
        attempts={(OUR_RUN + 1, 1): subprocess.CalledProcessError(1, "gh")},
    )
    status, posted = _run_main(monkeypatch, stub)
    assert status == 1
    assert posted == []
    assert "failing so it can be retried" in capsys.readouterr().err


def test_a_proven_newer_run_still_wins_over_an_unreadable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubGh(
        runs=[_listed(OUR_RUN + 2, 1), _listed(OUR_RUN + 1, 1)],
        attempts={
            (OUR_RUN + 2, 1): subprocess.CalledProcessError(1, "gh"),
            (OUR_RUN + 1, 1): [_job("validate ref", "success")],
        },
    )
    status, posted = _run_main(monkeypatch, stub)
    assert status == 0
    assert posted == []


def test_suite_secret_is_declared_by_test_yml(test_wf: dict[str, Any]) -> None:
    declared = set(_triggers(test_wf)["workflow_call"].get("secrets", {}))
    read = set(
        re.findall(r"secrets\.([A-Z_]+)", TEST_WORKFLOW.read_text(encoding="utf-8"))
    )
    assert read <= declared


def test_pull_request_refuses_a_called_ref(test_wf: dict[str, Any]) -> None:
    step = next(
        step
        for step in test_wf["jobs"]["change-classifier"]["steps"]
        if step.get("name") == REFUSE_STEP
    )
    script = step["run"]

    def run(ref: str, event: str) -> int:
        return subprocess.run(
            ["bash", "-c", script],
            env={"CALLED_REF": ref, "EVENT_NAME": event, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    assert run("", "pull_request") == 0
    assert run("", "push") == 0
    assert run(VALID_SHA, "workflow_dispatch") == 0
    assert run(VALID_SHA, "pull_request") != 0


def test_coverage_upload_is_skipped_for_a_called_ref() -> None:
    """The upload attributes coverage to the workflow's commit; a dispatch's is main's head."""

    workflow = _load(TEST_WORKFLOW)
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("codecov/codecov-action")
    ]
    assert steps
    for step in steps:
        assert step["if"] == "${{ !inputs.ref }}"
