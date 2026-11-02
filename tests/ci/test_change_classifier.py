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
    AFFECTED_JOB,
    ALL_JOBS,
    MAX_AFFECTED_TEST_FILES,
    ChangeEntry,
    Classification,
    ClassifierError,
    TestUniverse,
    build_outputs,
    classify_entries,
    classify_path,
    main,
    parse_name_status,
    select_affected_tests,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

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
        # a one-line manifest entry is how every new test file lands
        ("ci_shards/shard-1.txt", "manifests"),
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
        ("ci_shards/weights.json", "unknown"),
        ("ci_shards/shard-1.txt.orig", "unknown"),
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
    assert result.jobs == (AFFECTED_JOB, "lint")


def test_tests_only_change_selects_lint_and_test() -> None:
    result = classify_entries(_added("tests/test_a.py"))
    assert result.run_all is False
    assert result.lanes == ("tests",)
    assert result.jobs == (AFFECTED_JOB, "lint")


def test_mixed_lanes_union_their_jobs() -> None:
    result = classify_entries(_added("docs/a.md", "tests/test_a.py"))
    assert result.run_all is False
    assert result.lanes == ("docs", "tests")
    assert result.jobs == (AFFECTED_JOB, "lint")


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


def test_build_outputs_emits_every_job_flag_and_the_selection() -> None:
    outputs = build_outputs(classify_entries(_added("docs/a.md")))
    assert "shadow" not in outputs
    assert outputs["result"] == "classified"
    assert outputs["run_all"] == "false"
    assert outputs["run_affected_tests"] == "false"
    assert outputs["check_manifests"] == "false"
    assert outputs["affected_tests"] == "[]"
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
    assert outputs["run_affected_tests"] == "false"
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
    assert report["affected_tests"] == []


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
    code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--name-status-file",
            str(payload),
            "--json-out",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    # No manifests under this root, so nothing can map: the full suite.
    assert report["lanes"] == ["app"]
    assert report["run_all"] is True
    assert report["reason"] == "unmapped_path"


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


# --------------------------------------------------------------------------
# ROB-1294 verifier P1 — laundering a malformed/odd change set into a narrow
# green classification. Every case below produced `classified, jobs=()` before
# the fix; each must now be run_all or red.
# --------------------------------------------------------------------------


def test_a_literal_leading_space_path_is_not_normalized_into_the_docs_lane() -> None:
    """git tracks `" docs/only.md"` as a file in a directory named `" docs"`.

    Stripping it first turned an unrecognised path into a green docs-only
    classification with zero jobs.
    """

    assert classify_path(" docs/only.md") == "unknown"
    result = classify_entries(parse_name_status("M\0 docs/only.md\0"))
    assert result.run_all is True
    assert result.forcing_paths == (" docs/only.md",)


def test_a_trailing_space_path_inside_docs_stays_in_the_docs_lane() -> None:
    """The literal path really is under `docs/`, so narrowing is honest."""

    assert classify_path("docs/only.md ") == "docs"


@pytest.mark.parametrize(
    "path",
    ["snapshots/expected.md", "some_new_dir/notes.md", " docs/only.md"],
)
def test_a_nested_markdown_path_in_an_unknown_directory_is_unknown(path: str) -> None:
    """The `*.md` suffix rule is top-level only.

    A bare suffix rule would swallow any unmatched nested markdown path --
    including a fixture a test reads -- and answer "docs, no jobs needed".
    """

    assert classify_path(path) == "unknown"


@pytest.mark.parametrize("path", ["README.md", "CLAUDE.md", "AGENTS.md"])
def test_top_level_markdown_is_still_docs(path: str) -> None:
    assert classify_path(path) == "docs"


def test_a_scored_modify_is_a_rewrite_and_forces_run_all() -> None:
    """`M100` is git's dissimilarity score for a rewrite (emitted under -B).

    It is valid git output, so it is not red -- but it is not an ordinary
    modify either, and truncating it to "M" classified it by path.
    """

    entries = parse_name_status("M100\0docs/only.md\0")
    assert entries == [ChangeEntry("M100", "docs/only.md")]
    result = classify_entries(entries)
    assert result.run_all is True
    assert result.reason == "forcing_path"


def test_an_embedded_extra_nul_is_red_not_silently_dropped() -> None:
    """A dropped empty field resynchronises the parser onto wrong pairings."""

    with pytest.raises(ClassifierError, match="empty field at position"):
        parse_name_status("A\0docs/one.md\0\0M\0docs/two.md\0")


def test_an_unterminated_stream_is_red() -> None:
    with pytest.raises(ClassifierError, match="not NUL-terminated"):
        parse_name_status("A\0docs/one.md")


@pytest.mark.parametrize(
    "token", ["AA", "Z", "A1", "R1000", "M1234", "1", "-", "m", "RR100"]
)
def test_a_status_token_outside_gits_grammar_is_red(token: str) -> None:
    """Truncating an unrecognised token to its first character invented a
    status git never emitted."""

    with pytest.raises(ClassifierError, match="unrecognised git status token"):
        parse_name_status(f"{token}\0docs/one.md\0")


@pytest.mark.parametrize(
    "token", ["A", "D", "M", "T", "U", "X", "B", "R", "C", "R100", "C75", "R9"]
)
def test_every_token_git_actually_emits_is_accepted(token: str) -> None:
    payload = (
        f"{token}\0docs/old.md\0docs/new.md\0"
        if token[0] in {"R", "C"}
        else f"{token}\0docs/one.md\0"
    )
    assert parse_name_status(payload)


def test_a_real_leading_space_filename_from_git_forces_run_all(
    repo: Path, tmp_path: Path
) -> None:
    """End-to-end against real `git diff --name-status -z` output."""

    odd_dir = repo / " docs"
    odd_dir.mkdir()
    (odd_dir / "only.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "odd dir")
    base = _git(repo, "rev-parse", "HEAD")
    (odd_dir / "only.md").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "modify odd dir")
    head = _git(repo, "rev-parse", "HEAD")

    raw = subprocess.run(
        ["git", "diff", "--name-status", "-z", f"{base}..{head}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert raw == "M\0 docs/only.md\0", repr(raw)

    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is True
    assert report["forcing_paths"] == [" docs/only.md"]
    assert report["jobs"] == sorted(ALL_JOBS)


def test_a_real_trailing_space_filename_from_git_is_classified_literally(
    repo: Path, tmp_path: Path
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "docs" / "only.md ").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "trailing space")
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["path_lanes"] == {"docs/only.md ": "docs"}
    assert report["run_all"] is False


# --------------------------------------------------------------------------
# ROB-1294 re-verifier P2 — an absent base must not suppress an invalid head.
#
# `main()` used to take the missing-base short-circuit before resolving the
# supplied head, so an unresolvable head was reported as a conservative green
# run_all and was indistinguishable from a legitimate first-push head.
#
# These repos deliberately have no `origin` remote, so `_resolve_commit`'s
# fetch fallbacks fail locally: the tests stay network-0.
# --------------------------------------------------------------------------

UNRESOLVABLE_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_repo_fixture_has_no_remote_so_resolution_stays_offline(repo: Path) -> None:
    """Pins the precondition the two reproducers below rely on."""

    assert (
        subprocess.run(
            ["git", "remote"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )


def test_an_unresolvable_head_with_no_base_is_red_not_green_run_all(
    repo: Path, tmp_path: Path
) -> None:
    code, report = _run_cli(repo, tmp_path, "--head-sha", UNRESOLVABLE_SHA)
    assert code == 1
    assert report["result"] == "error"
    assert report["run_all"] is True
    assert report["reason"] == "classifier_error"
    assert "not reachable" in str(report["error"])


def test_an_unresolvable_head_with_an_all_zero_base_is_red(
    repo: Path, tmp_path: Path
) -> None:
    """`github.event.before` is 0*40 on a first push; that is exactly the
    situation in which the invalid head used to be hidden."""

    code, report = _run_cli(
        repo, tmp_path, "--base-sha", "0" * 40, "--head-sha", UNRESOLVABLE_SHA
    )
    assert code == 1
    assert report["result"] == "error"
    assert report["run_all"] is True
    assert "not reachable" in str(report["error"])


def test_a_valid_head_with_no_base_is_still_conservative_green_run_all(
    repo: Path, tmp_path: Path
) -> None:
    """The positive half of the contract must not regress."""

    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--head-sha", head)
    assert code == 0
    assert report["result"] == "run_all"
    assert report["reason"] == "base_sha_missing"
    assert report["error"] is None


def test_a_valid_head_with_an_all_zero_base_is_still_green_run_all(
    repo: Path, tmp_path: Path
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    code, report = _run_cli(repo, tmp_path, "--base-sha", "0" * 40, "--head-sha", head)
    assert code == 0
    assert report["result"] == "run_all"
    assert report["reason"] == "base_sha_missing"


def test_a_head_that_resolves_to_a_non_commit_object_is_red(
    repo: Path, tmp_path: Path
) -> None:
    """A blob SHA exists locally but is not a commit; `cat-file -e <sha>^{commit}`
    must reject it rather than accepting any resolvable object."""

    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input="not a commit\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert _git(repo, "cat-file", "-t", blob) == "blob"

    code, report = _run_cli(repo, tmp_path, "--head-sha", blob)
    assert code == 1
    assert report["result"] == "error"
    assert report["run_all"] is True


def test_the_head_is_resolved_before_the_on_missing_base_fail_switch(
    repo: Path, tmp_path: Path
) -> None:
    """Both paths are red; the head diagnosis is the more specific one."""

    code, report = _run_cli(
        repo,
        tmp_path,
        "--head-sha",
        UNRESOLVABLE_SHA,
        "--on-missing-base",
        "fail",
    )
    assert code == 1
    assert "not reachable" in str(report["error"])


def test_an_offline_name_status_file_still_needs_no_sha_resolution(
    tmp_path: Path,
) -> None:
    """`--name-status-file` remains a pure offline path."""

    payload = tmp_path / "diff.txt"
    payload.write_text("A\0docs/a.md\0", encoding="utf-8")
    out = tmp_path / "report.json"
    assert main(["--name-status-file", str(payload), "--json-out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["lanes"] == ["docs"]


# --------------------------------------------------------------------------
# ROB-1454 — affected-test selection
# --------------------------------------------------------------------------


def _universe(tree: dict[str, str]) -> TestUniverse:
    return TestUniverse(files=frozenset(tree), read=tree.__getitem__)


def _modified(*paths: str) -> list[ChangeEntry]:
    return [ChangeEntry(status="M", path=path) for path in paths]


def _select(tree: dict[str, str], *paths: str) -> Classification:
    return select_affected_tests(classify_entries(_modified(*paths)), _universe(tree))


def test_a_docs_only_change_runs_no_tests() -> None:
    result = _select({"tests/test_a.py": "import app.a\n"}, "docs/a.md", "README.md")
    assert result.run_all is False
    assert result.affected_tests == ()
    assert result.run_affected_tests is False
    assert result.jobs == ()


def test_a_research_only_change_with_no_suite_here_runs_no_tests() -> None:
    result = _select({"tests/test_a.py": "import app.a\n"}, "research/foo/run.py")
    assert result.run_all is False
    assert result.affected_tests == ()
    assert result.jobs == ("lint",)


def test_a_research_change_runs_its_mirror_directory() -> None:
    tree = {"tests/research/foo/test_run.py": "x = 1\n", "tests/test_a.py": "x = 2\n"}
    result = _select(tree, "research/foo/run.py")
    assert result.affected_tests == ("tests/research/foo/test_run.py",)


def test_a_frontend_only_change_runs_no_python_tests() -> None:
    result = _select({"tests/test_a.py": "x = 1\n"}, "frontend/invest/src/a.tsx")
    assert result.run_all is False
    assert result.affected_tests == ()


def test_an_app_change_selects_only_the_tests_that_reference_it() -> None:
    tree = {
        "tests/test_imports_it.py": "from app.services.halt import classify\n",
        "tests/test_from_parent.py": "from app.services import (\n    halt,\n)\n",
        "tests/test_patches_it.py": 'patch("app.services.halt.classify")\n',
        "tests/test_reads_it.py": 'Path("app/services/halt.py").read_text()\n',
        "tests/services/halt/test_mirror.py": "x = 1\n",
        "tests/test_longer_name.py": "from app.services.halt_extra import y\n",
        "tests/test_other_parent.py": "from app.services import halt_extra\n",
        "tests/test_unrelated.py": "from app.services.other import halt\n",
    }
    result = _select(tree, "app/services/halt.py")
    assert result.run_all is False
    assert result.jobs == (AFFECTED_JOB, "lint")
    assert result.affected_tests == (
        "tests/services/halt/test_mirror.py",
        "tests/test_from_parent.py",
        "tests/test_imports_it.py",
        "tests/test_patches_it.py",
        "tests/test_reads_it.py",
    )


def test_a_package_change_selects_its_mirror_directory_but_not_the_whole_tree() -> None:
    tree = {
        "tests/services/foo/test_bar.py": "x = 1\n",
        "tests/services/test_foo_flat.py": "x = 1\n",
        "tests/services/other/test_x.py": "x = 1\n",
    }
    result = _select(tree, "app/services/foo/bar.py")
    assert result.affected_tests == ("tests/services/foo/test_bar.py",)


def test_a_top_level_service_does_not_select_all_of_tests_services() -> None:
    tree = {
        "tests/services/test_other.py": "x = 1\n",
        "tests/test_flat.py": "import app.services.flat\n",
    }
    result = _select(tree, "app/services/flat.py")
    assert result.affected_tests == ("tests/test_flat.py",)


def test_a_broker_change_is_narrow_like_any_other_path() -> None:
    """Operator 2026-11-02: no blocking-path exception; merging is not deploying."""

    tree = {"tests/test_kis.py": "from app.services.brokers.kis.client import C\n"}
    result = _select(tree, "app/services/brokers/kis/client.py")
    assert result.run_all is False
    assert result.affected_tests == ("tests/test_kis.py",)


def test_a_changed_test_file_selects_itself() -> None:
    tree = {"tests/test_a.py": "x = 1\n", "tests/test_b.py": "x = 1\n"}
    assert _select(tree, "tests/test_a.py").affected_tests == ("tests/test_a.py",)


def test_a_nested_helper_selects_its_directory() -> None:
    tree = {
        "tests/services/foo/test_x.py": "x = 1\n",
        "tests/services/test_y.py": "x = 1\n",
    }
    result = _select(tree, "tests/services/foo/conftest.py")
    assert result.affected_tests == ("tests/services/foo/test_x.py",)


def test_a_root_helper_selects_its_importers_only() -> None:
    tree = {
        "tests/test_x.py": "from tests._helpers import a\n",
        "tests/test_y.py": "x = 1\n",
    }
    assert _select(tree, "tests/_helpers.py").affected_tests == ("tests/test_x.py",)


def test_a_test_file_missing_from_the_manifests_forces_run_all() -> None:
    """A forgotten manifest entry reaches taskiq-smoke's exact-cover check."""

    result = _select({"tests/test_a.py": "x = 1\n"}, "tests/test_new.py")
    assert result.run_all is True
    assert result.reason == "unmapped_path"
    assert result.forcing_paths == ("tests/test_new.py",)


@pytest.mark.parametrize(
    "path",
    [
        "app/services/untested.py",
        "scripts/untested.py",
        "config/untested.yaml",
        "alembic/versions/untested.py",
        "tests/fixtures/untested.json",
    ],
)
def test_a_path_no_test_names_forces_run_all(path: str) -> None:
    result = _select({"tests/test_a.py": "x = 1\n"}, path)
    assert result.run_all is True
    assert result.reason == "unmapped_path"
    assert result.forcing_paths == (path,)
    assert result.jobs == tuple(sorted(ALL_JOBS))
    assert result.affected_tests == ()


def test_one_unmapped_path_widens_a_change_set_that_also_has_mapped_paths() -> None:
    tree = {"tests/test_a.py": "import app.a\n"}
    result = _select(tree, "app/a.py", "app/untested.py")
    assert result.run_all is True
    assert result.forcing_paths == ("app/untested.py",)


def test_an_empty_universe_forces_run_all() -> None:
    assert _select({}, "app/a.py").run_all is True


def test_a_selection_bigger_than_a_shard_forces_run_all() -> None:
    fits = {f"tests/t{i}/test_x.py": "import app.a\n" for i in range(500)}
    assert MAX_AFFECTED_TEST_FILES == 500
    assert _select(fits, "app/a.py").run_all is False
    over = fits | {"tests/t500/test_x.py": "import app.a\n"}
    result = _select(over, "app/a.py")
    assert result.run_all is True
    assert result.reason == "selection_too_broad"
    assert result.affected_tests == ()


def test_a_manifest_edit_asks_for_the_exact_cover_check_without_forcing_run_all() -> (
    None
):
    tree = {"tests/test_new.py": "x = 1\n"}
    result = select_affected_tests(
        classify_entries(
            [
                ChangeEntry("A", "tests/test_new.py"),
                ChangeEntry("M", "ci_shards/shard-2.txt"),
            ]
        ),
        _universe(tree),
    )
    assert result.run_all is False
    assert result.check_manifests is True
    assert result.affected_tests == ("tests/test_new.py",)


def test_a_manifest_only_edit_still_needs_the_job() -> None:
    result = _select({"tests/test_a.py": "x = 1\n"}, "ci_shards/shard-2.txt")
    assert result.run_all is False
    assert result.affected_tests == ()
    assert result.run_affected_tests is True
    assert result.jobs == (AFFECTED_JOB, "lint")


def test_selection_leaves_a_run_all_verdict_alone() -> None:
    forced = classify_entries(_modified(".github/workflows/test.yml"))
    assert select_affected_tests(forced, _universe({})) is forced


def test_an_unparseable_test_file_counts_as_importing_the_module() -> None:
    tree = {"tests/test_broken.py": "from app.services import halt (\n"}
    assert _select(tree, "app/services/halt.py").affected_tests == (
        "tests/test_broken.py",
    )


def test_a_manifest_naming_a_missing_file_is_red(tmp_path: Path) -> None:
    (tmp_path / "ci_shards").mkdir()
    (tmp_path / "ci_shards/shard-1.txt").write_text("tests/test_gone.py\n")
    payload = tmp_path / "diff.txt"
    payload.write_text("M\0app/a.py\0", encoding="utf-8")
    out = tmp_path / "report.json"
    code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--name-status-file",
            str(payload),
            "--json-out",
            str(out),
        ]
    )
    assert code == 1
    assert json.loads(out.read_text(encoding="utf-8"))["result"] == "error"


# Acceptance 1, against this repository's real manifests and test files.
@pytest.mark.parametrize(
    "path", ["docs/runbooks/ci-required-aggregator.md", "CLAUDE.md"]
)
def test_real_repo_docs_change_runs_no_shards_and_no_tests(path: str) -> None:
    result = select_affected_tests(
        classify_entries(_modified(path)), TestUniverse.from_manifests(REPO_ROOT)
    )
    assert result.run_all is False
    assert result.affected_tests == ()
    assert "test" not in result.jobs


def test_real_repo_app_service_change_runs_only_its_lane() -> None:
    universe = TestUniverse.from_manifests(REPO_ROOT)
    result = select_affected_tests(
        classify_entries(_modified("app/services/halt_detection.py")), universe
    )
    assert result.run_all is False
    assert "test" not in result.jobs
    assert "taskiq-smoke" not in result.jobs
    assert any("halt" in path for path in result.affected_tests)
    assert len(result.affected_tests) < len(universe.files) / 10


def test_real_repo_workflow_change_runs_the_full_suite() -> None:
    result = select_affected_tests(
        classify_entries(_modified(".github/workflows/test.yml")),
        TestUniverse.from_manifests(REPO_ROOT),
    )
    assert result.run_all is True
    assert set(result.jobs) == set(ALL_JOBS)


# --------------------------------------------------------------------------
# ROB-1454 — the diff is what the PR changed, not what main gained since
# --------------------------------------------------------------------------


def test_cli_ignores_what_the_base_branch_gained_after_the_branch_point(
    repo: Path, tmp_path: Path
) -> None:
    """pull_request.base.sha is the base tip, so a two-dot diff reports main's
    new file as a delete and forces run_all on nearly every PR."""

    _git(repo, "branch", "-M", "main")
    _git(repo, "switch", "-q", "-c", "feature")
    head = _commit(repo, "docs/feature.md", "x\n", "feature")
    _git(repo, "switch", "-q", "main")
    base = _commit(repo, "app/added_on_main.py", "x = 1\n", "main moved on")

    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["run_all"] is False
    assert report["path_lanes"] == {"docs/feature.md": "docs"}


def test_cli_sees_a_pr_merge_of_main_as_no_change_of_its_own(
    repo: Path, tmp_path: Path
) -> None:
    _git(repo, "branch", "-M", "main")
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "docs/feature.md", "x\n", "feature")
    _git(repo, "switch", "-q", "main")
    base = _commit(repo, "app/added_on_main.py", "x = 1\n", "main moved on")
    _git(repo, "switch", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    head = _git(repo, "rev-parse", "HEAD")

    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 0
    assert report["path_lanes"] == {"docs/feature.md": "docs"}


def test_cli_is_red_when_base_and_head_share_no_history(
    repo: Path, tmp_path: Path
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "--orphan", "stray")
    head = _commit(repo, "docs/stray.md", "x\n", "stray")

    code, report = _run_cli(repo, tmp_path, "--base-sha", base, "--head-sha", head)
    assert code == 1
    assert report["result"] == "error"
