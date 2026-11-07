"""hk 3445: the main tier's `ci-ok` and the deploy tier's workflow wiring.

decision/2026-11-07/fleet-ci-three-tiers-r2: Test on a main push stays the main tier
and ends in `ci-ok`; `deploy.yml` calls `deploy-checks.yml` for the same SHA after a
successful main-push Test run, on the digest it just pushed; the deploy tier is never
cancelled and ends in `deploy-ok`. This PR only publishes `deploy-ok`: the six gate
jobs and their full-suite `if:` stay exactly as they are.

Expressions are evaluated with `tests/ci/gh_expr.py`; shell steps are extracted from
the YAML and run, so the tests pin behaviour rather than spelling.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci import gh_expr
from tests.ci.test_pr_gate_workflow_contract import (
    FULL_MAIN_CHECK_NAMES,
    FULL_SUITE_IF,
    _displayed_names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TEST_YML = WORKFLOWS / "test.yml"
DEPLOY_YML = WORKFLOWS / "deploy.yml"
CHECKS_YML = WORKFLOWS / "deploy-checks.yml"

#: Test jobs that are not in the main tier's verdict, and why.
NOT_MAIN_TIER = {
    "ci-ok": "is the verdict",
    "gate-verdict": "PR verdict; aggregates the children ci-ok lists",
    "pr-gate": "PR gate; aggregates the verdict",
    "affected-tests": "pull_request only",
    "write-fence-guard": "one PR branch only",
}
DEPLOY_TIER_JOBS = {"plan", "migration-path", "install-smoke"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def test_wf() -> dict[str, Any]:
    return _load(TEST_YML)


@pytest.fixture(scope="module")
def deploy_wf() -> dict[str, Any]:
    return _load(DEPLOY_YML)


@pytest.fixture(scope="module")
def checks_wf() -> dict[str, Any]:
    return _load(CHECKS_YML)


def _truthy(expression: Any, context: dict[str, Any]) -> bool:
    return gh_expr.truthy(gh_expr.evaluate(expression, context))


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


# ------------------------------------------------------------ gate unchanged


def test_the_six_gate_jobs_keep_their_names_and_full_suite_condition(
    test_wf: dict[str, Any],
) -> None:
    names = [
        name
        for job_id in ("lint", "taskiq-smoke", "test")
        for name in _displayed_names(job_id, test_wf["jobs"][job_id])
    ]
    assert names == list(FULL_MAIN_CHECK_NAMES)
    assert test_wf["jobs"]["test"]["if"] == FULL_SUITE_IF
    assert test_wf["jobs"]["taskiq-smoke"]["if"] == FULL_SUITE_IF
    assert "if" not in test_wf["jobs"]["lint"]


def test_the_pr_gate_still_reads_exactly_its_five_children(
    test_wf: dict[str, Any],
) -> None:
    assert test_wf["jobs"]["gate-verdict"]["needs"] == [
        "lint",
        "change-classifier",
        "affected-tests",
        "test",
        "taskiq-smoke",
    ]
    assert test_wf["jobs"]["pr-gate"]["needs"] == ["gate-verdict"]


def test_the_main_tier_does_not_build_or_publish_an_image(
    test_wf: dict[str, Any],
) -> None:
    text = TEST_YML.read_text(encoding="utf-8")
    assert "deploy-ok" not in text
    assert "build-push-action" not in text
    assert "statuses: write" not in text


# --------------------------------------------------------------------- ci-ok


def test_ci_ok_covers_every_main_tier_job(test_wf: dict[str, Any]) -> None:
    ci_ok = test_wf["jobs"]["ci-ok"]
    assert ci_ok["name"] == "ci-ok"
    expected = set(test_wf["jobs"]) - set(NOT_MAIN_TIER)
    assert set(ci_ok["needs"]) == expected
    assert len(ci_ok["needs"]) == len(set(ci_ok["needs"]))
    # The six gate jobs sit under three job ids.
    assert {"lint", "taskiq-smoke", "test"} <= set(ci_ok["needs"])


def _ci_ok_runs(test_wf: dict[str, Any], event: str, results: list[str]) -> bool:
    needs = {f"job{i}": {"result": result} for i, result in enumerate(results)}
    context = {"github": {"event_name": event}, "needs": needs}
    return _truthy(test_wf["jobs"]["ci-ok"]["if"], context)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize(
    ("results", "runs"),
    [
        (["success", "success", "success"], True),
        (["success", "failure", "success"], True),
        (["failure", "cancelled", "success"], True),
        (["success", "skipped", "success"], True),
        (["success", "cancelled", "success"], False),
        (["cancelled", "cancelled"], False),
    ],
)
def test_ci_ok_runs_unless_the_run_was_only_cancelled(
    test_wf: dict[str, Any], event: str, results: list[str], runs: bool
) -> None:
    assert _ci_ok_runs(test_wf, event, results) is runs


def test_ci_ok_never_runs_on_a_pull_request(test_wf: dict[str, Any]) -> None:
    assert not _ci_ok_runs(test_wf, "pull_request", ["success", "success"])
    assert not _ci_ok_runs(test_wf, "pull_request", ["failure"])


def _ci_ok_step_exit(test_wf: dict[str, Any], results: list[str]) -> int:
    step = test_wf["jobs"]["ci-ok"]["steps"][0]
    assert step["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    return subprocess.run(
        ["bash", "-c", step["run"]],
        env={"RESULTS": " ".join(results), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def test_ci_ok_passes_only_when_every_main_tier_job_succeeded(
    test_wf: dict[str, Any],
) -> None:
    assert _ci_ok_step_exit(test_wf, ["success"] * 10) == 0


@pytest.mark.parametrize("bad", ["failure", "skipped", "cancelled", "neutral"])
def test_ci_ok_fails_when_any_main_tier_job_did_not_succeed(
    test_wf: dict[str, Any], bad: str
) -> None:
    for position in (0, 4, 9):
        results = ["success"] * 10
        results[position] = bad
        assert _ci_ok_step_exit(test_wf, results) == 1, (bad, position)


def test_ci_ok_with_a_failed_job_is_red_end_to_end(test_wf: dict[str, Any]) -> None:
    """A failure runs the job (the `if`) and the job goes red (the step)."""

    results = ["success", "failure", "success"]
    assert _ci_ok_runs(test_wf, "push", results)
    assert _ci_ok_step_exit(test_wf, results) == 1


# ----------------------------------------------------- deploy tier triggering


def test_deploy_checks_is_only_called_or_dispatched(checks_wf: dict[str, Any]) -> None:
    assert set(_on(checks_wf)) == {"workflow_call", "workflow_dispatch"}
    inputs = _on(checks_wf)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"ref", "suite"}
    assert inputs["suite"]["options"] == ["deploy"]
    assert inputs["suite"]["type"] == "choice"


def test_deploy_tier_starts_only_from_a_successful_main_push_test_run(
    deploy_wf: dict[str, Any],
) -> None:
    trigger = _on(deploy_wf)["workflow_run"]
    assert trigger["workflows"] == ["Test"]
    assert trigger["branches"] == ["main"]
    assert trigger["types"] == ["completed"]

    job = deploy_wf["jobs"]["deploy-checks"]
    assert job["needs"] == "build-and-push"
    assert job["uses"] == "./.github/workflows/deploy-checks.yml"

    def runs(event: str, conclusion: str, build_result: str) -> bool:
        context = {
            "github": {
                "event_name": event,
                "event": {"workflow_run": {"conclusion": conclusion}},
            },
            "needs": {"build-and-push": {"result": build_result}},
        }
        return _truthy(job["if"], context)

    assert runs("workflow_run", "success", "success")
    # A cancelled Test run (superseded by a newer main push) gets no deploy tier.
    assert not runs("workflow_run", "cancelled", "skipped")
    assert not runs("workflow_run", "cancelled", "success")
    # A red main tier gets none either, even if the image gate let a build through.
    assert not runs("workflow_run", "failure", "success")
    assert not runs("workflow_run", "success", "failure")
    assert not runs("workflow_run", "success", "skipped")
    # deploy.yml's own dispatch and release paths are unchanged: no deploy tier.
    assert not runs("workflow_dispatch", "", "success")
    assert not runs("release", "", "success")


def test_deploy_tier_is_called_for_the_same_sha_on_the_pushed_digest(
    deploy_wf: dict[str, Any],
) -> None:
    build = deploy_wf["jobs"]["build-and-push"]
    assert build["outputs"]["sha"] == "${{ steps.sha.outputs.sha }}"
    assert build["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert any(step.get("id") == "build" for step in build["steps"])

    with_ = deploy_wf["jobs"]["deploy-checks"]["with"]
    assert with_["sha"] == "${{ needs.build-and-push.outputs.sha }}"
    assert with_["image"] == (
        "ghcr.io/${{ github.repository }}@${{ needs.build-and-push.outputs.digest }}"
    )


def test_a_red_deploy_tier_cannot_touch_images_or_notifications(
    deploy_wf: dict[str, Any],
) -> None:
    jobs = deploy_wf["jobs"]
    for job_id, job in jobs.items():
        if job_id == "deploy-checks":
            continue
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert "deploy-checks" not in needs, job_id
    assert jobs["notify"]["needs"] == ["build-and-push", "promote-moving-tags"]
    assert jobs["promote-moving-tags"]["needs"] == "build-and-push"


# --------------------------------------------------------------- never cancelled


def test_the_deploy_tier_is_one_run_per_repository_and_never_cancelled(
    checks_wf: dict[str, Any], deploy_wf: dict[str, Any]
) -> None:
    block = checks_wf["concurrency"]
    assert block["cancel-in-progress"] is False
    context = {"github": {"repository": "mgh3326/auto_trader", "run_id": "1"}}
    first = gh_expr.evaluate(block["group"], context)
    context["github"]["run_id"] = "2"
    assert gh_expr.evaluate(block["group"], context) == first
    assert "mgh3326/auto_trader" in first
    text = CHECKS_YML.read_text(encoding="utf-8")
    assert not re.search(r"cancel-in-progress:\s*(true|\$\{\{)", text)
    # The caller adds no group of its own: the same group on both sides deadlocks.
    assert "concurrency" not in deploy_wf["jobs"]["deploy-checks"]
    assert "concurrency" not in deploy_wf


# --------------------------------------------------------------- digest, not rebuild


def _step_runs(step: dict[str, Any], mode: str) -> bool:
    condition = step.get("if")
    if condition is None:
        return True
    return _truthy(condition, {"needs": {"plan": {"outputs": {"mode": mode}}}})


def test_the_pushed_image_is_pulled_by_digest_and_never_rebuilt(
    checks_wf: dict[str, Any],
) -> None:
    steps = checks_wf["jobs"]["install-smoke"]["steps"]
    building = [
        step
        for step in steps
        if "build-push-action" in step.get("uses", "")
        or re.search(r"docker\s+(buildx\s+)?build\b", step.get("run", ""))
    ]
    assert building, "the any-ref path must still build locally"
    for step in building:
        assert not _step_runs(step, "pushed"), step.get("name")
        assert _step_runs(step, "local"), step.get("name")

    pull = next(
        step for step in steps if "Pull the pushed image" in step.get("name", "")
    )
    assert _step_runs(pull, "pushed")
    assert not _step_runs(pull, "local")
    assert pull["env"]["IMAGE"] == "${{ inputs.image }}"
    assert "@sha256:" in pull["run"]  # refuses a tag reference
    assert "docker pull" in pull["run"]
    assert "org.opencontainers.image.revision" in pull["run"]  # same commit as sha

    smoke = next(step for step in steps if step.get("name") == "Install smoke")
    assert 'install_smoke.sh "$SMOKE_IMAGE"' in smoke["run"]
    assert "SMOKE_IMAGE=$IMAGE" in pull["run"]


def test_the_local_build_serves_only_the_any_ref_path(
    checks_wf: dict[str, Any],
) -> None:
    plan = checks_wf["jobs"]["plan"]
    resolve = next(step for step in plan["steps"] if step.get("id") == "candidate")
    assert (
        'if [ -n "$INPUT_IMAGE" ]; then mode=pushed; else mode=local; fi'
        in (resolve["run"])
    )
    assert resolve["env"]["INPUT_IMAGE"] == "${{ inputs.image }}"


# ------------------------------------------------------------- dispatch never pushes


def test_deploy_checks_never_pushes_tags_or_writes_packages(
    checks_wf: dict[str, Any],
) -> None:
    text = CHECKS_YML.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in (
        "packages: write",
        "push: true",
        "cache-to",
        "imagetools",
        "docker push",
        "docker tag",
        "docker/metadata-action",
        "secrets.",
        "contents: write",
        "id-token",
    ):
        assert forbidden not in code, forbidden
    assert re.findall(r"^\s+push:\s*(\S+)", code, flags=re.M) == ["false"]
    assert re.findall(r"^\s+load:\s*(\S+)", code, flags=re.M) == ["true"]
    for job_id, job in checks_wf["jobs"].items():
        for level, value in job.get("permissions", {}).items():
            allowed = {"contents": "read", "packages": "read", "statuses": "read"}
            if job_id == "deploy-ok" and level == "statuses":
                allowed["statuses"] = "write"
            assert value == allowed.get(level), (job_id, level, value)
    assert checks_wf["permissions"] == {"contents": "read"}


def test_the_registry_login_exists_only_for_the_pushed_image(
    checks_wf: dict[str, Any],
) -> None:
    for step in checks_wf["jobs"]["install-smoke"]["steps"]:
        if "docker/login-action" in step.get("uses", ""):
            assert _step_runs(step, "pushed")
            assert not _step_runs(step, "local")


def test_the_caller_grants_the_called_workflow_only_what_it_asks_for(
    deploy_wf: dict[str, Any],
) -> None:
    assert deploy_wf["jobs"]["deploy-checks"]["permissions"] == {
        "contents": "read",
        "packages": "read",
        "statuses": "write",
    }


# ------------------------------------------------------------ migration path


def test_the_migration_path_runs_base_then_candidate_on_one_database(
    checks_wf: dict[str, Any],
) -> None:
    text = CHECKS_YML.read_text(encoding="utf-8")
    assert "0c416a3df" in text
    assert "deploy/auto_trader/20261106T230040Z" in text  # the bootstrap's why

    job = checks_wf["jobs"]["migration-path"]
    steps = job["steps"]
    base_checkout, cand_checkout = steps[0], steps[1]
    assert base_checkout["with"]["ref"] == "${{ needs.plan.outputs.base_sha }}"
    assert base_checkout["with"]["path"] == "base"
    assert cand_checkout["with"]["ref"] == "${{ needs.plan.outputs.sha }}"
    assert cand_checkout["with"]["path"] == "candidate"

    upgrades = [
        (step["working-directory"], step["run"])
        for step in steps
        if "alembic upgrade head" in step.get("run", "")
    ]
    assert [where for where, _ in upgrades] == ["base", "candidate"]
    # One service database, one DATABASE_URL for both upgrades.
    assert set(job["services"]) == {"postgres"}
    assert "DATABASE_URL" not in "".join(
        step.get("run", "") + str(step.get("env", "")) for step in steps
    )
    assert checks_wf["env"]["DATABASE_URL"].endswith("/deploy_check")


def test_the_base_is_selected_by_the_helper_from_first_parent_main(
    checks_wf: dict[str, Any],
) -> None:
    plan = checks_wf["jobs"]["plan"]
    checkout = plan["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    select = next(step for step in plan["steps"] if step.get("id") == "base")
    assert "scripts/ci/select_deploy_base.py" in select["run"]
    assert '--bootstrap "$BOOTSTRAP_BASE_SHA"' in select["run"]
    assert "--main-ref origin/main" in select["run"]
    assert plan["permissions"]["statuses"] == "read"


# --------------------------------------------------------------- install smoke


def test_the_postgres_services_are_timescaledb(checks_wf: dict[str, Any]) -> None:
    """Migrations raise `timescaledb extension is not installed` on plain postgres."""

    for job_id in ("migration-path", "install-smoke"):
        image = checks_wf["jobs"][job_id]["services"]["postgres"]["image"]
        assert image.startswith("timescale/timescaledb:"), (job_id, image)


def test_install_smoke_has_postgres_and_redis_and_no_secrets(
    checks_wf: dict[str, Any],
) -> None:
    job = checks_wf["jobs"]["install-smoke"]
    assert set(job["services"]) == {"postgres", "redis"}
    env = checks_wf["env"]
    for key in (
        "SECRET_KEY",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "OPENDART_API_KEY",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
    ):
        assert key in env, key
    for key in ("KIS_APP_KEY", "KIS_APP_SECRET", "OPENDART_API_KEY"):
        assert "${{" not in str(env[key]) and str(env[key]).startswith("DUMMY"), key


# ----------------------------------------------------------------- deploy-ok


def test_deploy_ok_waits_for_every_other_deploy_tier_job(
    checks_wf: dict[str, Any],
) -> None:
    jobs = checks_wf["jobs"]
    assert set(jobs) - {"deploy-ok"} == DEPLOY_TIER_JOBS
    assert set(jobs["deploy-ok"]["needs"]) == DEPLOY_TIER_JOBS


def test_deploy_ok_runs_after_a_failed_check_and_judges_the_same_jobs(
    checks_wf: dict[str, Any],
) -> None:
    job = checks_wf["jobs"]["deploy-ok"]
    for results in (
        {"plan": "success", "migration-path": "failure", "install-smoke": "success"},
        {"plan": "success", "migration-path": "success", "install-smoke": "skipped"},
        {"plan": "failure", "migration-path": "skipped", "install-smoke": "skipped"},
    ):
        needs = {name: {"result": result} for name, result in results.items()}
        needs["plan"]["outputs"] = {"sha": "a" * 40}
        assert _truthy(job["if"], {"needs": needs, "inputs": {}}), results

    publish = next(
        step for step in job["steps"] if step.get("name") == "Publish deploy-ok"
    )
    required = set(re.findall(r"--required (\S+)", publish["run"]))
    assert required == set(job["needs"])
    assert publish["env"]["DEPLOY_NEEDS"] == "${{ toJSON(needs) }}"
    assert publish["env"]["IMAGE_DIGEST"] == (
        "${{ needs.install-smoke.outputs.image_digest }}"
    )
    assert job["permissions"] == {"contents": "read", "statuses": "write"}


def test_deploy_ok_uploads_its_evidence(checks_wf: dict[str, Any]) -> None:
    upload = checks_wf["jobs"]["deploy-ok"]["steps"][-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "deploy-evidence.json"
