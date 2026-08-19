"""ROB-1294 — the change classifier must never shrink coverage by accident.

The failure this suite exists to prevent is silent: a classifier that answers
"only docs changed" for a change set it did not actually understand, and a
later PR that wires job ``if:`` conditions to that answer. Every ambiguity
here has exactly two legal outcomes -- ``run_all`` or a red job -- and the
tests pin which one applies to which input.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci.classify_changes import (
    ALL_JOBS,
    ChangeEntry,
    Classification,
    ClassifierError,
    build_outputs,
    classify_entries,
    classify_path,
    main,
    parse_name_status,
)

# --------------------------------------------------------------------------
# Pure lane classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_lane"),
    [
        # docs
        ("docs/runbooks/ci-required-aggregator.md", "docs"),
        ("README.md", "docs"),
        ("CLAUDE.md", "docs"),
        # app / tests / research / scripts
        ("app/services/halt_detection.py", "app"),
        ("tests/test_symbol_conversion.py", "tests"),
        ("research/nautilus_scalping/rob974_features.py", "research"),
        ("scripts/policy_table/adapters/kr.py", "scripts"),
        ("frontend/invest/src/desktop/screener/x.tsx", "frontend"),
        ("alembic/versions/1a2b3c4d5e6f_x.py", "migrations"),
        ("config/trading_policy.yaml", "config"),
        # shared CI/config/test infrastructure -> forces run_all
        (".github/workflows/test.yml", "ci_shared"),
        (".github/workflows/taskiq-smoke.sh", "ci_shared"),
        ("scripts/ci/classify_changes.py", "ci_shared"),
        ("scripts/setup-test-env.sh", "ci_shared"),
        ("pyproject.toml", "ci_shared"),
        ("uv.lock", "ci_shared"),
        ("Makefile", "ci_shared"),
        (".test_durations", "ci_shared"),
        ("tests/conftest.py", "ci_shared"),
        ("tests/_socket_guard_plugin.py", "ci_shared"),
        ("env.example", "ci_shared"),
        # unrecognised -> unknown, which also forces run_all
        (".gitignore", "unknown"),
        ("some_new_top_level_dir/thing.py", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_path_lane_table(path: str, expected_lane: str) -> None:
    assert classify_path(path) == expected_lane


def test_shared_infra_rules_win_over_the_directory_lane_they_live_in() -> None:
    """`scripts/ci/**` and `tests/conftest.py` must not fall into their dirs."""

    assert classify_path("scripts/ci/aggregate_required.py") == "ci_shared"
    assert classify_path("scripts/other_tool.py") == "scripts"
    assert classify_path("tests/conftest.py") == "ci_shared"
    assert classify_path("tests/services/test_x.py") == "tests"


# --------------------------------------------------------------------------
# Change-set classification
# --------------------------------------------------------------------------


def _added(*paths: str) -> list[ChangeEntry]:
    return [ChangeEntry(status="A", path=path) for path in paths]


def test_docs_only_change_selects_no_ci_job() -> None:
    result = classify_entries(_added("docs/a.md", "README.md"))
    assert result.run_all is False
    assert result.result == "classified"
    assert result.lanes == ("docs",)
    assert result.jobs == ()


def test_app_only_change_selects_the_app_lane_jobs() -> None:
    result = classify_entries(_added("app/services/x.py", "app/models/y.py"))
    assert result.run_all is False
    assert result.lanes == ("app",)
    assert result.jobs == ("lint", "security", "taskiq-smoke", "test")


def test_tests_only_change_selects_lint_and_test() -> None:
    result = classify_entries(_added("tests/test_a.py"))
    assert result.run_all is False
    assert result.lanes == ("tests",)
    assert result.jobs == ("lint", "test")


def test_mixed_lanes_union_their_jobs() -> None:
    result = classify_entries(_added("docs/a.md", "tests/test_a.py"))
    assert result.run_all is False
    assert result.lanes == ("docs", "tests")
    assert result.jobs == ("lint", "test")


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/test.yml",
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "scripts/ci/classify_changes.py",
    ],
)
def test_shared_infrastructure_change_forces_run_all(path: str) -> None:
    result = classify_entries(_added("docs/a.md", path))
    assert result.run_all is True
    assert result.reason == "forcing_path"
    assert path in result.forcing_paths
    assert result.jobs == tuple(sorted(ALL_JOBS))


def test_unknown_path_forces_run_all_even_beside_a_known_docs_path() -> None:
    result = classify_entries(_added("docs/a.md", "brand_new_dir/file.py"))
    assert result.run_all is True
    assert result.reason == "forcing_path"
    assert result.forcing_paths == ("brand_new_dir/file.py",)


@pytest.mark.parametrize("status", ["R", "C", "D", "T", "U", "X", "B"])
def test_non_add_modify_statuses_force_run_all(status: str) -> None:
    """A rename/delete's pre-image is part of the blast radius."""

    entries = [ChangeEntry(status=status, path="docs/renamed.md", old_path="docs/a.md")]
    result = classify_entries(entries)
    assert result.run_all is True, status
    assert result.reason == "forcing_path"
    assert result.path_lanes["docs/renamed.md"] == "unknown"


def test_rename_within_docs_still_forces_run_all() -> None:
    """Both sides look like docs, and it is *still* run_all. Intentional."""

    result = classify_entries(
        [ChangeEntry(status="R", path="docs/new.md", old_path="docs/old.md")]
    )
    assert result.run_all is True


def test_delete_of_an_app_file_forces_run_all() -> None:
    result = classify_entries([ChangeEntry(status="D", path="app/services/gone.py")])
    assert result.run_all is True


def test_empty_change_set_is_run_all_not_nothing_to_do() -> None:
    result = classify_entries([])
    assert result.run_all is True
    assert result.reason == "empty_change_set"
    assert result.jobs == tuple(sorted(ALL_JOBS))


# --------------------------------------------------------------------------
# --name-status parsing
# --------------------------------------------------------------------------


def test_parse_name_status_handles_adds_modifies_and_renames() -> None:
    payload = "A\0app/new.py\0M\0docs/a.md\0R100\0docs/old.md\0docs/new.md\0"
    entries = parse_name_status(payload)
    assert entries == [
        ChangeEntry("A", "app/new.py"),
        ChangeEntry("M", "docs/a.md"),
        ChangeEntry("R", "docs/new.md", "docs/old.md"),
    ]


def test_parse_name_status_of_empty_payload_is_empty() -> None:
    assert parse_name_status("") == []


def test_parse_name_status_rejects_a_truncated_rename_record() -> None:
    with pytest.raises(ClassifierError, match="malformed rename/copy record"):
        parse_name_status("R100\0docs/old.md\0")


def test_parse_name_status_rejects_a_status_with_no_path() -> None:
    with pytest.raises(ClassifierError, match="malformed record"):
        parse_name_status("A\0app/new.py\0M\0")


# --------------------------------------------------------------------------
# GitHub output surface
# --------------------------------------------------------------------------


def test_build_outputs_marks_shadow_and_emits_every_job_flag() -> None:
    outputs = build_outputs(classify_entries(_added("docs/a.md")))
    assert outputs["shadow"] == "true"
    assert outputs["result"] == "classified"
    assert outputs["run_all"] == "false"
    for job in ALL_JOBS:
        assert outputs["run_" + job.replace("-", "_")] == "false"


def test_build_outputs_for_run_all_turns_every_job_flag_on() -> None:
    outputs = build_outputs(
        Classification(
            run_all=True,
            reason="forcing_path",
            lanes=("unknown",),
            jobs=tuple(sorted(ALL_JOBS)),
        )
    )
    assert outputs["run_all"] == "true"
    for job in ALL_JOBS:
        assert outputs["run_" + job.replace("-", "_")] == "true"


# --------------------------------------------------------------------------
# git-backed CLI behaviour (self-contained temp repos; never this repo's own
# history, which CI clones shallowly)
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "ci@example.invalid")
    _git(path, "config", "user.name", "ROB-1294 test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "docs").mkdir()
    (path / "docs" / "seed.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def _commit(repo_path: Path, relative: str, body: str, message: str) -> str:
    target = repo_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)
    return _git(repo_path, "rev-parse", "HEAD")


def _run_cli(repo_path: Path, tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / f"report-{len(list(tmp_path.iterdir()))}.json"
    code = main(["--repo-root", str(repo_path), "--json-out", str(out), *extra])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_cli_classifies_a_real_docs_only_diff(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "docs/new.md", "hello\n", "docs")
    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["result"] == "classified"
    assert report["run_all"] is False
    assert report["lanes"] == ["docs"]
    assert report["jobs"] == []
    assert report["shadow"] is True


def test_cli_run_alls_on_a_real_workflow_change(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, ".github/workflows/test.yml", "name: t\n", "ci")
    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True
    assert report["reason"] == "forcing_path"
    assert report["forcing_paths"] == [".github/workflows/test.yml"]


def test_cli_run_alls_on_a_real_rename(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "docs/seed.md", "docs/renamed.md")
    _git(repo, "commit", "-q", "-m", "rename")
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True


def test_cli_run_alls_on_a_real_delete(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "docs/seed.md")
    _git(repo, "commit", "-q", "-m", "delete")
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True


def test_cli_run_alls_when_base_equals_head(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", head, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True
    assert report["reason"] == "empty_change_set"


def test_cli_run_alls_when_the_base_sha_is_absent(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True
    assert report["reason"] == "base_sha_missing"


def test_cli_treats_the_all_zero_push_sentinel_as_an_absent_base(
    repo: Path, tmp_path: Path
) -> None:
    """`github.event.before` is 0*40 on the first push of a new branch."""

    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", "0" * 40, "--head-sha", head)
    assert code == 0
    assert report["reason"] == "base_sha_missing"


def test_cli_can_be_told_to_fail_instead_on_an_absent_base(
    repo: Path, tmp_path: Path
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(
        repo, tmp_path, "--head-sha", head, "--on-missing-base", "fail"
    )
    assert code == 1
    assert report["result"] == "error"


def test_cli_is_red_when_no_head_sha_is_given(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLASSIFY_HEAD_SHA", raising=False)
    monkeypatch.delenv("CLASSIFY_BASE_SHA", raising=False)
    code, report = _run_cli(repo, tmp_path)
    assert code == 1
    assert report["result"] == "error"
    assert "head commit" in str(report["error"])


def test_cli_is_red_when_a_supplied_sha_is_unresolvable(
    repo: Path, tmp_path: Path
) -> None:
    """Incomplete/shallow history is a hard failure, never a narrower run."""

    head = _git(repo, "rev-parse", "HEAD")
    missing = "0123456789abcdef0123456789abcdef01234567"
    code, report = _run_cli(repo, tmp_path, "--base-sha", missing, "--head-sha", head)
    assert code == 1
    assert report["result"] == "error"
    assert "not reachable" in str(report["error"])


def test_cli_error_path_never_publishes_a_narrower_run(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the red path writes run_all=true outputs, so no consumer can
    read a partially written $GITHUB_OUTPUT and infer reduced coverage."""

    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    head = _git(repo, "rev-parse", "HEAD")
    code, _ = _run_cli(
        repo,
        tmp_path,
        "--base-sha",
        "0123456789abcdef0123456789abcdef01234567",
        "--head-sha",
        head,
        "--github-output",
    )
    assert code == 1
    emitted = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
        if line
    )
    assert emitted["result"] == "error"
    assert emitted["run_all"] == "true"
    assert emitted["run_test"] == "true"


def test_cli_reads_a_name_status_file_without_touching_git(tmp_path: Path) -> None:
    payload = tmp_path / "diff.txt"
    payload.write_text("A\0app/x.py\0", encoding="utf-8")
    out = tmp_path / "report.json"
    code = main(["--name-status-file", str(payload), "--json-out", str(out)])
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["lanes"] == ["app"]


def test_cli_is_red_on_a_malformed_name_status_file(tmp_path: Path) -> None:
    payload = tmp_path / "diff.txt"
    payload.write_text("R100\0only/one/path\0", encoding="utf-8")
    out = tmp_path / "report.json"
    code = main(["--name-status-file", str(payload), "--json-out", str(out)])
    assert code == 1
    assert json.loads(out.read_text(encoding="utf-8"))["result"] == "error"


def test_cli_is_red_on_a_missing_name_status_file(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    code = main(
        ["--name-status-file", str(tmp_path / "nope.txt"), "--json-out", str(out)]
    )
    assert code == 1
    assert json.loads(out.read_text(encoding="utf-8"))["result"] == "error"
