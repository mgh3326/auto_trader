"""hk 6037: promote_deploy.py — mergewake's deploy_approval rule, ported, plus the promote steps.

Range reviews run against real throwaway git repositories; the registry and the CI gate are a
fake runner, so nothing here touches the network, docker or GitHub.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "ci"))
import promote_deploy as pd  # noqa: E402

POLICY = (REPO / ".mergewake" / "policy.yaml").read_text()
FIXTURE = json.loads(
    (REPO / "tests" / "fixtures" / "mergewake_deploy_approval_paths.json").read_text()
)
IMAGE = pd.IMAGE_REPOSITORY
DIGEST = "sha256:" + "d" * 64
PINNED = f"{IMAGE}@{DIGEST}"
OLD_DIGEST = "sha256:" + "0" * 64
OLD_PINNED = f"{IMAGE}@{OLD_DIGEST}"


# ---------------------------------------------------------------------------- git


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit(root: Path, files: dict[str, str | None], message: str = "c") -> str:
    for name, body in files.items():
        target = root / name
        if body is None:
            git(root, "rm", "-q", "--", name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        git(root, "add", "--", name)
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


def base(repo: Path, policy: str | None = POLICY) -> str:
    files: dict[str, str | None] = {"README.md": "x\n", "docs/a.md": "a\n"}
    if policy is not None:
        files[".mergewake/policy.yaml"] = policy
    return commit(repo, files, "base")


def review(repo: Path, previous: str, release: str) -> pd.Review:
    return pd.review_range(repo, previous, release)


# ------------------------------------------------------------- the policy reader


def test_the_reader_agrees_with_a_real_yaml_parser_on_the_checked_in_policy() -> None:
    expected = [
        {"paths": rule["paths"], "reason": rule["reason"]}
        for rule in yaml.safe_load(POLICY)["deploy_approval"]
    ]
    assert pd.parse_deploy_approval(POLICY) == expected
    assert len(expected) >= 5


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\n",  # no key
        "deploy_approval:\n",  # nothing under it
        "deploy_approval: {}\n",  # unsupported shape
        "deploy_approval:\n  - paths: ['a/**']\n",  # no reason
        "deploy_approval:\n  - paths: ['a/**']\n    reason: r\n    surprise: 1\n",
        "deploy_approval:\n  - paths: ['a/**']\n    reason: r\ndeploy_approval:\n",
    ],
)
def test_the_reader_fails_closed_on_anything_it_does_not_understand(text: str) -> None:
    with pytest.raises(pd.PolicyError):
        pd.parse_deploy_approval(text)


def test_an_explicit_empty_list_is_an_empty_policy() -> None:
    assert (
        pd.parse_deploy_approval("version: 1\ndeploy_approval: []\nenabled: true\n")
        == []
    )


@pytest.mark.parametrize(
    ("glob", "name", "expected"),
    [
        ("alembic/versions/**", "alembic/versions/x.py", True),
        ("alembic/versions/**", "alembic/versions", True),  # ** may match nothing
        ("alembic/versions/**", "alembic/env.py", False),
        ("**/prefect.yaml", "prefect.yaml", True),
        ("**/prefect.yaml", "a/b/prefect.yaml", True),
        ("app/services/kis_trading*.py", "app/services/kis_trading_service.py", True),
        ("app/services/kis_trading*.py", "app/services/x/kis_trading_a.py", False),
        (
            "ops/ncp/systemd/job-kickoff-[0-9]*.timer",
            "ops/ncp/systemd/job-kickoff-0905.timer",
            True,
        ),
        (
            "ops/ncp/systemd/job-kickoff-[0-9]*.timer",
            "ops/ncp/systemd/job-kickoff-us-x.timer",
            False,
        ),
    ],
)
def test_globs_follow_mergewakes_segment_rules(
    glob: str, name: str, expected: bool
) -> None:
    assert pd.match_glob(glob, name) is expected


@pytest.mark.parametrize("glob", ["", "/abs", "a/../b", "a//b", "a\\b"])
def test_invalid_globs_are_errors_not_matches(glob: str) -> None:
    with pytest.raises(pd.PolicyError):
        pd.match_glob(glob, "a/b")


# ------------------------------------------------------- the fixture of mergewake


def test_every_row_of_the_mergewake_fixture_holds_or_passes_the_same_way(
    repo: Path,
) -> None:
    """tests/fixtures/mergewake_deploy_approval_paths.json is the rule's own evidence."""
    start = base(repo)
    wrong = []
    for row in FIXTURE:
        git(repo, "checkout", "-q", "-B", "t", start)
        release = commit(repo, {row["path"]: "x\n"}, "change")
        result = review(repo, start, release)
        if result.held != row["hold"]:
            wrong.append((row["path"], row["hold"], result.status, result.reasons))
    assert wrong == []
    assert sum(1 for row in FIXTURE if row["hold"]) > 10  # the fixture really has holds


# ----------------------------------------------------------------- range reviews


def test_a_routine_range_promotes(repo: Path) -> None:
    start = base(repo)
    release = commit(
        repo, {"docs/b.md": "b\n", "app/services/mock_strategy/x.py": "x\n"}
    )
    result = review(repo, start, release)
    assert result.status == "routine" and result.paths == [] and result.reasons == []


@pytest.mark.parametrize(
    "path",
    [
        "alembic/versions/20261122_new_table.py",
        "alembic/env.py",
        "app/services/brokers/kis/domestic_orders.py",
        "app/core/scheduler.py",
        "deploy/dagu/paper-forward.yaml",
        "config/trading_policy.yaml",
    ],
)
def test_ranges_touching_approval_paths_hold_and_name_them(
    repo: Path, path: str
) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n", path: "x\n"})
    result = review(repo, start, release)
    assert result.held and result.paths == [path] and result.reasons


def test_a_new_migration_holds_even_when_the_deployed_policy_has_no_rules(
    repo: Path,
) -> None:
    start = base(repo, policy="version: 1\ndeploy_approval: []\n")
    routine = commit(repo, {"docs/b.md": "b\n"})
    assert review(repo, start, routine).status == "routine"
    release = commit(repo, {"alembic/versions/20261122_x.py": "x\n"})
    result = review(repo, start, release)
    assert result.held and "always held" in " ".join(result.reasons)


def test_a_release_cannot_relax_its_own_gate_by_editing_the_policy(repo: Path) -> None:
    start = base(repo)
    release = commit(
        repo,
        {
            ".mergewake/policy.yaml": "version: 1\ndeploy_approval: []\n",
            "app/services/brokers/kis/domestic_orders.py": "x\n",
        },
    )
    result = review(repo, start, release)
    assert result.held
    assert (
        ".mergewake/policy.yaml" in result.paths
    )  # the policy file itself always holds
    assert (
        "app/services/brokers/kis/domestic_orders.py" in result.paths
    )  # old rules still apply


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "deploy/.env.api",
        "env.example",
        "config/secrets/token.txt",
        "keys/server.pem",
        "a/credentials.json",
    ],
)
def test_environment_and_secret_paths_always_hold(repo: Path, path: str) -> None:
    start = base(repo, policy="version: 1\ndeploy_approval: []\n")
    release = commit(repo, {path: "x\n"})
    assert review(repo, start, release).held


def test_a_rename_is_checked_by_both_names(repo: Path) -> None:
    start = base(repo)
    commit(
        repo,
        {
            "app/services/brokers/kis/domestic_orders.py": "def f():\n    return 1\n"
            * 20
        },
        "add",
    )
    start = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "app/services/brokers/kis/domestic_orders.py", "docs/moved.md")
    git(repo, "commit", "-q", "-m", "rename out of a held path")
    release = git(repo, "rev-parse", "HEAD")
    result = review(repo, start, release)
    assert result.held
    assert "app/services/brokers/kis/domestic_orders.py" in result.paths


def test_a_comparison_at_the_file_limit_holds(repo: Path) -> None:
    start = base(repo)
    release = commit(repo, {f"docs/bulk/{n}.md": "x\n" for n in range(pd.FILE_LIMIT)})
    result = review(repo, start, release)
    assert result.held and "file comparison limit" in result.reasons[0]
    under = commit(repo, {f"docs/more/{n}.md": "x\n" for n in range(pd.FILE_LIMIT - 5)})
    assert review(repo, release, under).status == "routine"


def test_a_missing_or_unreadable_deployed_policy_holds(repo: Path) -> None:
    start = base(repo, policy=None)
    release = commit(repo, {"docs/b.md": "b\n"})
    assert review(repo, start, release).held
    other = base(repo, policy="version: 1\nenabled: true\n")
    release2 = commit(repo, {"docs/c.md": "c\n"})
    assert review(repo, other, release2).held  # no deploy_approval key at all


def test_unknown_previous_holds(repo: Path) -> None:
    base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    result = review(repo, "unknown", release)
    assert result.held and "previous deployment" in result.reasons[0]


def test_history_that_is_not_in_the_checkout_holds(repo: Path) -> None:
    base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    result = review(repo, "f" * 40, release)
    assert result.held and "not in the checkout" in result.reasons[0]


def test_divergent_history_holds(repo: Path) -> None:
    start = base(repo)
    git(repo, "checkout", "-q", "-b", "side", start)
    side = commit(repo, {"docs/side.md": "s\n"})
    git(repo, "checkout", "-q", "main")
    release = commit(repo, {"docs/main.md": "m\n"})
    assert review(repo, side, release).held


def test_same_commit_and_an_older_build_are_not_holds(repo: Path) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    assert review(repo, release, release).status == "same"
    assert review(repo, release, start).status == "stale"  # older build finishing late


# ------------------------------------------------------------------- promote steps


class Registry:
    """Fake gate/registry/docker: records every external command, never touches anything."""

    def __init__(
        self,
        *,
        gate_rc: int = 0,
        gate_stdout: str = PINNED,
        deploy_tag: tuple[str, str] | None = None,
        create_rc: int = 0,
    ) -> None:
        self.gate_rc, self.gate_stdout = gate_rc, gate_stdout
        self.deploy_tag = deploy_tag
        self.create_rc = create_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "docker":
            if self.create_rc == 0:
                self.deploy_tag = (PINNED, self.candidate)
            return subprocess.CompletedProcess(argv, self.create_rc, "", "")
        if "--resolve-only" in argv:
            if self.deploy_tag is None:
                return subprocess.CompletedProcess(argv, 78, "", "no such tag\n")
            return subprocess.CompletedProcess(
                argv, 0, f"{self.deploy_tag[0]}\t{self.deploy_tag[1]}\n", ""
            )
        return subprocess.CompletedProcess(
            argv, self.gate_rc, self.gate_stdout + "\n", "gate\n"
        )

    candidate = ""

    def created(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if call[:4] == ["docker", "buildx", "imagetools", "create"]
        ]


def args(repo: Path, sha: str, approval_ref: str = "") -> argparse.Namespace:
    return argparse.Namespace(
        sha=sha,
        repo="mgh3326/auto_trader",
        repo_root=str(repo),
        approval_ref=approval_ref,
    )


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)


def summary(tmp_path: Path) -> str:
    path = tmp_path / "summary.md"
    return path.read_text() if path.exists() else ""


def test_a_routine_range_moves_the_deploy_tag_onto_the_verified_digest(
    repo: Path, tmp_path: Path
) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    fake = Registry(deploy_tag=(OLD_PINNED, start))
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 0
    assert fake.created() == [
        ["docker", "buildx", "imagetools", "create", "--tag", f"{IMAGE}:deploy", PINNED]
    ]
    assert "승격" in summary(tmp_path)
    # the gate ran first and asked for the exact commit, main ancestry and the image identity
    first = fake.calls[0]
    assert first[1].endswith("deploy_ci_gate.py")
    assert {"--main-ancestor", "--expect-sha", release}.issubset(first)
    assert first[first.index("--image") + 1] == f"{IMAGE}:sha-{release[:7]}"


def test_a_held_range_moves_nothing_and_says_what_to_approve(
    repo: Path, tmp_path: Path
) -> None:
    start = base(repo)
    release = commit(repo, {"alembic/versions/20261122_x.py": "x\n"})
    fake = Registry(deploy_tag=(OLD_PINNED, start))
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 0  # a hold is not a red job
    assert fake.created() == []
    text = summary(tmp_path)
    assert "보류" in text and "alembic/versions/20261122_x.py" in text
    assert f"promote_sha={release}" in text and "approval_ref" in text


def test_an_approval_waives_the_range_hold_only(repo: Path, tmp_path: Path) -> None:
    start = base(repo)
    release = commit(repo, {"alembic/versions/20261122_x.py": "x\n"})
    fake = Registry(deploy_tag=(OLD_PINNED, start))
    fake.candidate = release
    assert (
        pd.promote(args(repo, release, "decision/2026-11-22/at-migration-ok"), fake)
        == 0
    )
    assert len(fake.created()) == 1
    assert "decision/2026-11-22/at-migration-ok" in summary(tmp_path)

    # the same approval does not help a commit the gate refuses
    refused = Registry(gate_rc=78, deploy_tag=(OLD_PINNED, start))
    refused.candidate = release
    with pytest.raises(SystemExit) as exit_info:
        pd.promote(args(repo, release, "decision/2026-11-22/at-migration-ok"), refused)
    assert exit_info.value.code == 78
    assert refused.created() == []


def test_a_malformed_approval_reference_is_refused_before_anything_runs(
    repo: Path,
) -> None:
    release = base(repo)
    fake = Registry()
    assert pd.promote(args(repo, release, "ok; rm -rf /"), fake) == pd.EXIT_REFUSED
    assert fake.calls == []


def test_a_refused_gate_stops_before_the_registry_is_touched(repo: Path) -> None:
    release = base(repo)
    fake = Registry(gate_rc=78)
    with pytest.raises(SystemExit) as exit_info:
        pd.promote(args(repo, release), fake)
    assert exit_info.value.code == 78
    assert fake.created() == []
    assert all("--resolve-only" not in call for call in fake.calls)


def test_a_gate_answer_that_is_not_a_digest_is_refused(repo: Path) -> None:
    release = base(repo)
    with pytest.raises(SystemExit):
        pd.promote(args(repo, release), Registry(gate_stdout=f"{IMAGE}:main"))


def test_no_deploy_tag_yet_holds_for_bootstrap_approval(
    repo: Path, tmp_path: Path
) -> None:
    release = base(repo)
    fake = Registry(deploy_tag=None)
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 0
    assert fake.created() == [] and "보류" in summary(tmp_path)
    assert pd.promote(args(repo, release, "decision/2026-11-22/bootstrap"), fake) == 0
    assert len(fake.created()) == 1


def test_an_older_build_never_rewinds_the_tag_and_the_current_one_is_a_no_op(
    repo: Path,
) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    fake = Registry(deploy_tag=(PINNED, release))
    fake.candidate = start
    older = Registry(gate_stdout=OLD_PINNED, deploy_tag=(PINNED, release))
    assert pd.promote(args(repo, start), older) == 0
    assert older.created() == []
    assert pd.promote(args(repo, release), fake) == 0
    assert fake.created() == []


def test_a_tag_that_does_not_read_back_as_the_digest_is_a_failure(repo: Path) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})

    class Lying(Registry):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            done = super().__call__(argv)
            if argv[0] == "docker":
                self.deploy_tag = (OLD_PINNED, start)  # the registry kept the old tag
            return done

    fake = Lying(deploy_tag=(OLD_PINNED, start))
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 1


def test_a_failed_tag_move_is_a_failure(repo: Path) -> None:
    start = base(repo)
    release = commit(repo, {"docs/b.md": "b\n"})
    fake = Registry(deploy_tag=(OLD_PINNED, start), create_rc=1)
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 1


def test_discord_is_best_effort(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = base(repo)
    release = commit(repo, {"alembic/versions/20261122_x.py": "x\n"})
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.example/hook")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("network down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    fake = Registry(deploy_tag=(OLD_PINNED, start))
    fake.candidate = release
    assert pd.promote(args(repo, release), fake) == 0  # a failed notice changes nothing


def test_evaluate_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    start = base(repo)
    routine = commit(repo, {"docs/b.md": "b\n"})
    held = commit(repo, {"alembic/versions/20261122_x.py": "x\n"})

    def ns(prev: str, sha: str) -> argparse.Namespace:
        return argparse.Namespace(previous=prev, sha=sha, repo_root=str(repo))

    assert pd.evaluate(ns(start, routine)) == 0
    assert pd.evaluate(ns(routine, held)) == pd.EXIT_HOLD
    assert (
        json.loads(capsys.readouterr().out.split("\n}\n")[1] + "\n}")["status"]
        == "hold"
    )
