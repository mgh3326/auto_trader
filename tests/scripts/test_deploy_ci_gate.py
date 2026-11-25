"""ROB-1456: exact commit evidence for the deployment gate."""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts import deploy_ci_gate as gate

SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + "1" * 64
PINNED = f"{gate.IMAGE_REPOSITORY}@{DIGEST}"
# Deliberately independent of the implementation's list: omissions must be red.
CHECKS = ("lint", "taskiq-smoke", *(f"test (3.13, {n})" for n in range(1, 5)))


def green_jobs():
    return [
        {"id": n, "name": name, "status": "completed", "conclusion": "success"}
        for n, name in enumerate(CHECKS, 1)
    ]


def run_record(**changes):
    return {"id": 100, "run_attempt": 1, "head_sha": SHA, "event": "push", **changes}


def api(monkeypatch, *, runs=None, jobs=None, extra_job_pages=()):
    calls = []
    runs = [run_record()] if runs is None else runs
    jobs = green_jobs() if jobs is None else jobs

    def command(argv):
        calls.append(argv)
        if "/workflows/test.yml/runs?" in argv[-1]:
            assert f"head_sha={SHA}" in argv[-1]
            return [{"workflow_runs": runs}]
        return [{"jobs": jobs}, *({"jobs": page} for page in extra_job_pages)]

    monkeypatch.setattr(gate, "_json_command", command)
    return calls


def test_green_suite_accepts_exact_sha(monkeypatch, capsys):
    calls = api(monkeypatch)
    assert gate.main(["--sha", SHA]) == 0
    assert f"PASS SHA {SHA}" in capsys.readouterr().err
    assert "/runs/100/attempts/1/jobs?" in calls[-1][-1]


@pytest.mark.parametrize("missing", CHECKS)
def test_each_missing_check_refuses(monkeypatch, capsys, missing):
    api(monkeypatch, jobs=[j for j in green_jobs() if j["name"] != missing])
    assert gate.main(["--sha", SHA]) == 78
    assert f"{missing}: missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "skipped", "neutral", "timed_out", None]
)
def test_non_success_conclusions_refuse(monkeypatch, capsys, conclusion):
    jobs = green_jobs()
    jobs[-1]["conclusion"] = conclusion
    api(monkeypatch, jobs=jobs)
    assert gate.main(["--sha", SHA]) == 78
    assert "test (3.13, 4): not successful" in capsys.readouterr().err


def test_running_check_cannot_reuse_an_old_success(monkeypatch):
    jobs = green_jobs()
    jobs.append({**jobs[0], "id": 99, "status": "in_progress", "conclusion": None})
    api(monkeypatch, jobs=jobs)
    assert gate.main(["--sha", SHA]) == 78


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_pr_checks_are_not_full_suite_authority(monkeypatch, capsys, event):
    api(monkeypatch, runs=[run_record(event=event)])
    assert gate.main(["--sha", SHA]) == 78
    err = capsys.readouterr().err
    assert all(f"{name}: missing" in err for name in CHECKS)


def test_different_commit_success_does_not_count(monkeypatch, capsys):
    calls = api(monkeypatch, runs=[run_record(head_sha=OTHER_SHA)])
    assert gate.main(["--sha", SHA]) == 78
    err = capsys.readouterr().err
    assert all(f"{name}: missing" in err for name in CHECKS)
    assert len(calls) == 1


def test_newest_run_attempt_is_authority_not_an_old_green(monkeypatch):
    calls = api(
        monkeypatch, runs=[run_record(), run_record(id=101, run_attempt=2)], jobs=[]
    )
    assert gate.main(["--sha", SHA]) == 78
    assert "/runs/101/attempts/2/jobs?" in calls[-1][-1]


def test_paginated_jobs_can_complete_the_suite(monkeypatch):
    jobs = green_jobs()
    api(monkeypatch, jobs=jobs[:2], extra_job_pages=[jobs[2:]])
    assert gate.main(["--sha", SHA]) == 0


def test_dispatch_full_suite_is_accepted(monkeypatch):
    api(monkeypatch, runs=[run_record(event="workflow_dispatch")])
    assert gate.main(["--sha", SHA]) == 0


def test_job_for_another_sha_is_not_evidence(monkeypatch):
    jobs = green_jobs()
    jobs[0]["head_sha"] = OTHER_SHA
    api(monkeypatch, jobs=jobs)
    assert gate.main(["--sha", SHA]) == 78


@pytest.mark.parametrize("payload", [None, {}, [], [{}], [{"workflow_runs": [None]}]])
def test_malformed_api_fails_closed(monkeypatch, capsys, payload):
    monkeypatch.setattr(gate, "_json_command", lambda argv: payload)
    assert gate.main(["--sha", SHA]) == 78
    assert "lint: unverified" in capsys.readouterr().err


def test_command_failure_does_not_leak_stderr(monkeypatch, capsys):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 1, "", "secret must not appear"
        ),
    )
    assert gate.main(["--sha", SHA]) == 78
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("reason", ["", "  ", "\n"])
def test_override_requires_a_reason_before_api(monkeypatch, reason):
    calls = api(monkeypatch)
    assert gate.main(["--sha", SHA, "--allow-unverified", reason]) == 78
    assert calls == []


def test_override_logs_reason_and_missing_checks(monkeypatch, capsys):
    api(monkeypatch, jobs=[])
    reason = "incident 2586\noperator decision"
    assert gate.main(["--sha", SHA, "--allow-unverified", reason]) == 0
    err = capsys.readouterr().err
    assert f"reason={json.dumps(reason)}" in err
    assert all(f"{name}: missing" in err for name in CHECKS)


def test_registry_is_pinned_before_platform_revision_reads(monkeypatch, capsys):
    calls = []
    children = ["sha256:" + n * 64 for n in ("2", "3")]

    def command(argv):
        calls.append(argv)
        if argv[0] == "gh":
            if "/workflows/" in argv[-1]:
                return [{"workflow_runs": [run_record()]}]
            return [{"jobs": green_jobs()}]
        if argv[-1] == "{{json .Manifest}}":
            return {"digest": DIGEST, "manifests": [{"digest": d} for d in children]}
        assert argv[4] in [f"{gate.IMAGE_REPOSITORY}@{d}" for d in children]
        return {"config": {"Labels": {"org.opencontainers.image.revision": SHA}}}

    monkeypatch.setattr(gate, "_json_command", command)
    assert gate.main(["--image", f"{gate.IMAGE_REPOSITORY}:main"]) == 0
    assert capsys.readouterr().out == PINNED + "\n"
    assert sum(":main" in arg for cmd in calls for arg in cmd) == 1
    assert not any(cmd[:2] == ["docker", "pull"] for cmd in calls)


def test_inconsistent_platform_revisions_refuse_even_with_override(monkeypatch):
    revisions = iter([SHA, OTHER_SHA])
    monkeypatch.setattr(gate, "resolve_image", lambda _: (PINNED, ["child1", "child2"]))
    monkeypatch.setattr(
        gate,
        "_inspect",
        lambda *a: {"config": {"Labels": {gate._REVISION: next(revisions)}}},
    )
    assert (
        gate.main(
            [
                "--image",
                f"{gate.IMAGE_REPOSITORY}:main",
                "--allow-unverified",
                "incident",
            ]
        )
        == 78
    )
