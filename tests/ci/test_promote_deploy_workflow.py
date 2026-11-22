"""hk 6037: the promote job's wiring (static). Behaviour is in test_promote_deploy.py."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / ".github" / "workflows" / "deploy.yml"
PROMOTE = REPO / ".github" / "workflows" / "promote-deploy.yml"


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads a bare `on` as True
    return data


def test_deploy_yml_calls_the_promote_workflow_only_after_a_successful_main_push_build() -> (
    None
):
    job = load(DEPLOY)["jobs"]["promote"]
    assert job["uses"] == "./.github/workflows/promote-deploy.yml"
    assert job["needs"] == "build-and-push"
    condition = " ".join(job["if"].split())
    for needle in (
        "needs.build-and-push.result == 'success'",
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert needle in condition, needle
    assert job["with"]["sha"] == "${{ needs.build-and-push.outputs.sha }}"
    assert job["permissions"] == {
        "contents": "read",
        "packages": "write",
        "actions": "read",
    }


def test_promote_runs_one_at_a_time_and_is_never_cancelled() -> None:
    job = load(PROMOTE)["jobs"]["promote"]
    assert job["concurrency"]["cancel-in-progress"] is False
    assert "${{ github.repository }}" in job["concurrency"]["group"]
    assert "cancel-in-progress: true" not in PROMOTE.read_text()


def test_dispatch_is_main_only_and_takes_the_sha_and_the_approval_reference() -> None:
    workflow = load(PROMOTE)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"promote_sha", "approval_ref"}
    assert inputs["promote_sha"]["required"] is True
    assert inputs["approval_ref"]["required"] is False
    assert "github.ref == 'refs/heads/main'" in workflow["jobs"]["promote"]["if"]
    assert set(workflow["on"]["workflow_call"]["inputs"]) == {"sha"}


def executable_text(job: dict) -> str:
    """Only what runs: `run:` bodies and `uses:` references (comments may say anything)."""
    parts = [job.get("uses", "")]
    for step in job.get("steps", []):
        parts += [step.get("run", ""), step.get("uses", "")]
    return "\n".join(parts)


def test_the_logic_lives_in_the_script_and_the_tag_is_moved_nowhere_else() -> None:
    steps = load(PROMOTE)["jobs"]["promote"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert any("python3 scripts/ci/promote_deploy.py promote" in run for run in runs)
    assert "imagetools create" not in executable_text(load(PROMOTE)["jobs"]["promote"])
    assert "imagetools create" not in executable_text(load(DEPLOY)["jobs"]["promote"])
    checkout = next(
        s for s in steps if s.get("uses", "").startswith("actions/checkout@")
    )
    assert (
        checkout["with"]["fetch-depth"] == 0
    )  # the range review needs the deployed commit
    assert checkout["with"]["persist-credentials"] is False
    # approval is an input to the script, never an expression that skips a step
    assert all("approval" not in str(step.get("if", "")) for step in steps)


def test_there_is_no_path_into_the_pi() -> None:
    jobs = [load(PROMOTE)["jobs"]["promote"], load(DEPLOY)["jobs"]["promote"]]
    for job in jobs:
        text = executable_text(job)
        assert not re.search(
            r"\b(ssh|scp|rsync|tailscale|cloudflared|nc)\b", text, re.I
        )
    secrets = set(re.findall(r"secrets\.([A-Z_]+)", PROMOTE.read_text()))
    assert secrets == {
        "GITHUB_TOKEN",
        "DISCORD_WEBHOOK_URL",
    }  # no deploy key, no ssh secret


def test_the_build_job_is_unchanged_in_what_it_gates() -> None:
    build = load(DEPLOY)["jobs"]["build-and-push"]
    assert "github.event.workflow_run.conclusion != 'cancelled'" in " ".join(
        build["if"].split()
    )


def test_the_approval_waiver_runs_only_in_the_environment_gated_job() -> None:
    jobs = load(PROMOTE)["jobs"]
    gated, plain = jobs["promote-approved"], jobs["promote"]
    assert gated["environment"] == "at-deploy-approval"
    assert "environment" not in plain
    gated_run = executable_text(gated)
    plain_run = executable_text(plain)
    assert "--approval-gated" in gated_run and "--approval-ref" in gated_run
    assert "--approval" not in plain_run  # the ungated job can never carry an approval
    assert "APPROVAL_REF" not in str(plain)
    # exactly one of the two jobs runs for a given dispatch
    assert "inputs.approval_ref == ''" in " ".join(plain["if"].split())
    assert "inputs.approval_ref != ''" in " ".join(gated["if"].split())
    # a workflow_call (the automatic path) can only reach the ungated job
    assert "workflow_dispatch" in gated["if"]
