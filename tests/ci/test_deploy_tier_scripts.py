"""hk 3445: behaviour of the deploy tier's three helpers, run for real.

`scripts/ci/select_deploy_base.py`, `scripts/ci/deploy_ok.py` and
`scripts/ci/install_smoke.sh` are executed against a throwaway git repository and
fake `gh`, `docker` and `curl` executables on PATH. Nothing touches the network or
a container runtime.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "ci"
SELECT = SCRIPTS / "select_deploy_base.py"
DEPLOY_OK = SCRIPTS / "deploy_ok.py"
SMOKE = SCRIPTS / "install_smoke.sh"
REPO = "mgh3326/auto_trader"


def _executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _commit(cwd: Path, message: str, files: dict[str, str] | None = None) -> str:
    for name, content in (files or {}).items():
        target = cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(cwd, "add", name)
    _git(cwd, "commit", "--allow-empty", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(bin_dir.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.update(extra)
    return env


# ------------------------------------------------------- select_deploy_base


def _fake_gh_status(bin_dir: Path, statuses: dict[str, list[dict[str, str]]]) -> Path:
    """`gh api --method GET repos/R/commits/<sha>/status` from a table."""

    table = bin_dir / "statuses.json"
    table.write_text(json.dumps(statuses), encoding="utf-8")
    log = bin_dir / "gh.log"
    _executable(
        bin_dir / "gh",
        f"""\
        #!{sys.executable}
        import json, os, sys
        open({str(log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
        if os.environ.get("FAKE_GH_FAIL"):
            sys.stderr.write("HTTP 502")
            sys.exit(1)
        sha = sys.argv[-1].split("/")[-2]
        table = json.load(open({str(table)!r}))
        print(json.dumps({{"statuses": table.get(sha, [])}}))
        """,
    )
    return log


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """main: root <- boot <- a <- b <- c <- d, plus branch commit f on top of c."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    commits = {"root": _commit(repo, "root")}
    for name in ("boot", "a", "b", "c", "d"):
        commits[name] = _commit(repo, name)
    _git(repo, "update-ref", "refs/remotes/origin/main", commits["d"])
    _git(repo, "checkout", "-q", "-b", "feature", commits["c"])
    commits["f"] = _commit(repo, "f")
    _git(repo, "checkout", "-q", "main")
    commits["repo"] = str(repo)
    return commits


def _select(
    history: dict[str, str], bin_dir: Path, sha: str, **env: str
) -> subprocess.CompletedProcess[str]:
    output = bin_dir / "github_output"
    output.write_text("", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SELECT),
            "--repo",
            REPO,
            "--sha",
            sha,
            "--bootstrap",
            history["boot"][:9],
            "--main-ref",
            "origin/main",
        ],
        cwd=history["repo"],
        env=_env(bin_dir, GITHUB_OUTPUT=str(output), **env),
        capture_output=True,
        text=True,
        check=False,
    )


def _outputs(bin_dir: Path) -> dict[str, str]:
    lines = (bin_dir / "github_output").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


OK = [{"context": "deploy-ok", "state": "success"}]


def test_base_is_the_newest_first_parent_ancestor_with_deploy_ok(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(bin_dir, {history["a"]: OK, history["b"]: OK})
    done = _select(history, bin_dir, history["d"])
    assert done.returncode == 0, done.stderr
    assert _outputs(bin_dir) == {"base_sha": history["b"], "base_source": "deploy-ok"}


def test_the_candidate_itself_is_never_its_own_base(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(bin_dir, {history["d"]: OK})
    done = _select(history, bin_dir, history["d"])
    assert done.returncode == 0, done.stderr
    assert _outputs(bin_dir)["base_source"] == "bootstrap"
    assert _outputs(bin_dir)["base_sha"] == history["boot"]


def test_until_a_deploy_ok_exists_the_base_is_the_pinned_bootstrap(
    history: dict[str, str], bin_dir: Path
) -> None:
    log = _fake_gh_status(bin_dir, {})
    done = _select(history, bin_dir, history["d"])
    assert done.returncode == 0, done.stderr
    assert _outputs(bin_dir) == {
        "base_sha": history["boot"],
        "base_source": "bootstrap",
    }
    # It stops at the bootstrap instead of walking the whole history.
    probed = [line.split("/")[-2] for line in log.read_text().splitlines()]
    assert probed == [history["c"], history["b"], history["a"], history["boot"]]


def test_only_a_successful_deploy_ok_counts(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(
        bin_dir,
        {
            history["c"]: [{"context": "deploy-ok", "state": "failure"}],
            history["b"]: [{"context": "ci-ok", "state": "success"}],
            history["a"]: [{"context": "deploy-ok", "state": "pending"}],
        },
    )
    done = _select(history, bin_dir, history["d"])
    assert done.returncode == 0, done.stderr
    assert _outputs(bin_dir)["base_source"] == "bootstrap"


def test_a_branch_candidate_walks_main_from_its_merge_base(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(bin_dir, {history["c"]: OK, history["d"]: OK})
    done = _select(history, bin_dir, history["f"])
    assert done.returncode == 0, done.stderr
    # c is the merge-base and counts; d is not an ancestor of the branch.
    assert _outputs(bin_dir) == {"base_sha": history["c"], "base_source": "deploy-ok"}


def test_an_unreadable_status_is_an_error_not_a_bootstrap_fallback(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(bin_dir, {})
    done = _select(history, bin_dir, history["d"], FAKE_GH_FAIL="1")
    assert done.returncode == 1
    assert "cannot read the commit status" in done.stderr
    assert not _outputs(bin_dir)


def test_a_candidate_older_than_the_bootstrap_has_no_base(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_status(bin_dir, {})
    done = _select(history, bin_dir, history["root"])
    assert done.returncode == 1
    assert "is not its ancestor" in done.stderr


# ----------------------------------------------------------------- deploy_ok


def _fake_gh_post(bin_dir: Path) -> Path:
    log = bin_dir / "gh.log"
    _executable(
        bin_dir / "gh",
        f"""\
        #!/bin/sh
        echo "$@" >> {log}
        """,
    )
    return log


def _deploy_ok(
    history: dict[str, str],
    bin_dir: Path,
    needs: object,
    *,
    sha: str,
    required: tuple[str, ...] = ("plan", "migration-path", "install-smoke"),
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(DEPLOY_OK),
        "--repo",
        REPO,
        "--sha",
        sha,
        "--needs-env",
        "DEPLOY_NEEDS",
        "--run-id",
        "4242",
        "--run-url",
        "https://github.invalid/run/4242",
        "--environment",
        "github-actions/Linux-X64",
        "--image-digest",
        "sha256:" + "d" * 64,
        "--main-ref",
        "origin/main",
        "--evidence-out",
        str(bin_dir / "deploy-evidence.json"),
    ]
    for name in required:
        argv += ["--required", name]
    return subprocess.run(
        argv,
        cwd=history["repo"],
        env=_env(bin_dir, DEPLOY_NEEDS=json.dumps(needs)),
        capture_output=True,
        text=True,
        check=False,
    )


def _all(result: str) -> dict[str, dict[str, str]]:
    return {
        name: {"result": result} for name in ("plan", "migration-path", "install-smoke")
    }


def _posted(log: Path) -> list[dict[str, str]]:
    """One dict per `gh api --method POST repos/R/statuses/SHA -f k=v ...` call."""

    posts = []
    for line in log.read_text().splitlines() if log.exists() else []:
        head, *fields = line.split(" -f ")
        assert head.startswith("api --method POST ")
        post = dict(field.split("=", 1) for field in fields)
        post["path"] = head.split()[-1]
        posts.append(post)
    return posts


def test_deploy_ok_is_success_only_when_every_deploy_tier_job_succeeded(
    history: dict[str, str], bin_dir: Path
) -> None:
    log = _fake_gh_post(bin_dir)
    done = _deploy_ok(history, bin_dir, _all("success"), sha=history["d"])
    assert done.returncode == 0, done.stderr
    [post] = _posted(log)
    assert post["path"] == f"repos/{REPO}/statuses/{history['d']}"
    assert post["state"] == "success"
    assert post["context"] == "deploy-ok"
    assert post["target_url"] == "https://github.invalid/run/4242"


@pytest.mark.parametrize("bad", ["failure", "cancelled", "skipped", "neutral"])
@pytest.mark.parametrize("which", ["plan", "migration-path", "install-smoke"])
def test_deploy_ok_is_failure_after_any_failed_deploy_tier_job(
    history: dict[str, str], bin_dir: Path, bad: str, which: str
) -> None:
    log = _fake_gh_post(bin_dir)
    needs = _all("success")
    needs[which] = {"result": bad}
    done = _deploy_ok(history, bin_dir, needs, sha=history["d"])
    assert done.returncode == 0, done.stderr
    [post] = _posted(log)
    assert post["state"] == "failure"
    assert which in post["description"]
    evidence = json.loads((bin_dir / "deploy-evidence.json").read_text())
    assert evidence["result"] == "failure"


@pytest.mark.parametrize(
    "needs",
    [{}, {"plan": {"result": "success"}}, "garbage", None],
    ids=["empty", "missing-jobs", "not-an-object", "null"],
)
def test_deploy_ok_is_failure_when_a_required_job_is_absent_or_unreadable(
    history: dict[str, str], bin_dir: Path, needs: object
) -> None:
    log = _fake_gh_post(bin_dir)
    done = _deploy_ok(history, bin_dir, needs, sha=history["d"])
    assert done.returncode == 0, done.stderr
    assert [post["state"] for post in _posted(log)] == ["failure"]


def test_deploy_ok_with_nothing_required_is_failure(
    history: dict[str, str], bin_dir: Path
) -> None:
    log = _fake_gh_post(bin_dir)
    done = _deploy_ok(history, bin_dir, _all("success"), sha=history["d"], required=())
    assert done.returncode == 0, done.stderr
    assert [post["state"] for post in _posted(log)] == ["failure"]


def test_a_branch_commit_gets_evidence_but_no_status(
    history: dict[str, str], bin_dir: Path
) -> None:
    log = _fake_gh_post(bin_dir)
    done = _deploy_ok(history, bin_dir, _all("success"), sha=history["f"])
    assert done.returncode == 0, done.stderr
    assert not log.exists()
    assert json.loads((bin_dir / "deploy-evidence.json").read_text())["result"] == (
        "success"
    )


def test_the_evidence_records_what_the_decision_requires(
    history: dict[str, str], bin_dir: Path
) -> None:
    repo = Path(history["repo"])
    policy = "deploy_approval: {}\n"
    with_policy = _commit(repo, "policy", {".mergewake/policy.yaml": policy})
    _git(repo, "update-ref", "refs/remotes/origin/main", with_policy)
    blob = _git(repo, "rev-parse", f"{with_policy}:.mergewake/policy.yaml")
    _fake_gh_post(bin_dir)
    done = _deploy_ok(history, bin_dir, _all("success"), sha=with_policy)
    assert done.returncode == 0, done.stderr
    evidence = json.loads((bin_dir / "deploy-evidence.json").read_text())
    assert evidence == {
        "repo": REPO,
        "sha": with_policy,
        "policy_revision": blob,
        "suite": "deploy",
        "environment": "github-actions/Linux-X64",
        "run_id": "4242",
        "result": "success",
        "image_digest": "sha256:" + "d" * 64,
    }


def test_a_commit_without_a_policy_file_says_so(
    history: dict[str, str], bin_dir: Path
) -> None:
    _fake_gh_post(bin_dir)
    _deploy_ok(history, bin_dir, _all("success"), sha=history["d"])
    evidence = json.loads((bin_dir / "deploy-evidence.json").read_text())
    assert evidence["policy_revision"] == "absent"


# --------------------------------------------------------------- install_smoke


def _fake_runtime(bin_dir: Path) -> Path:
    """Fake `docker` and `curl`. Behaviour comes from FAKE_* env vars."""

    log = bin_dir / "docker.log"
    _executable(
        bin_dir / "docker",
        f"""\
        #!/bin/sh
        echo "$@" >> {log}
        case "$1" in
          logs) echo "container log line"; exit 0 ;;
          run)
            case "$*" in
              *"${{FAKE_DOCKER_FAIL:-@@never@@}}"*) exit 1 ;;
            esac
            exit 0 ;;
        esac
        exit 0
        """,
    )
    counter = bin_dir / "curl.count"
    _executable(
        bin_dir / "curl",
        f"""\
        #!/bin/sh
        n=$(cat {counter} 2>/dev/null || echo 0)
        n=$((n + 1))
        echo "$n" > {counter}
        echo "curl $@" >> {log}
        [ "$n" -gt "${{FAKE_CURL_FAILS:-0}}" ]
        """,
    )
    return log


SMOKE_ENV = dict.fromkeys(
    (
        "DATABASE_URL",
        "REDIS_URL",
        "SECRET_KEY",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "OPENDART_API_KEY",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
    ),
    "placeholder",
)


def _smoke(bin_dir: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = _env(bin_dir, SMOKE_SLEEP="0", **{**SMOKE_ENV, **extra})
    return subprocess.run(
        ["bash", str(SMOKE), "registry.invalid/app@sha256:" + "a" * 64],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _runs(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line.startswith("run ")]


def test_install_smoke_migrates_boots_and_imports_the_entrypoints_in_order(
    bin_dir: Path,
) -> None:
    log = _fake_runtime(bin_dir)
    done = _smoke(bin_dir)
    assert done.returncode == 0, done.stdout + done.stderr
    runs = _runs(log)
    assert len(runs) == 5
    assert "alembic upgrade head" in runs[0]
    assert "-d" in runs[1].split() and "app.main:app" in runs[1]
    assert "uvicorn" in runs[1]
    assert "app.core.scheduler" in runs[2]
    assert "app.core.taskiq_broker" in runs[3]
    assert "websocket_monitor" in runs[4]
    for line in runs:
        assert "registry.invalid/app@sha256:" + "a" * 64 in line  # the given digest
        assert "--network host" in line
    assert "curl -fsS -o /dev/null http://127.0.0.1:8000/healthz" in log.read_text()


def test_install_smoke_passes_settings_by_name_only(bin_dir: Path) -> None:
    log = _fake_runtime(bin_dir)
    _smoke(bin_dir)
    text = log.read_text()
    assert "-e SECRET_KEY" in text and "placeholder" not in text


def test_install_smoke_waits_for_healthz_then_passes(bin_dir: Path) -> None:
    log = _fake_runtime(bin_dir)
    done = _smoke(bin_dir, FAKE_CURL_FAILS="29")
    assert done.returncode == 0, done.stdout + done.stderr
    assert log.read_text().count("healthz") == 30
    assert "answered 200 on attempt 30" in done.stdout


def test_install_smoke_fails_after_thirty_attempts_and_prints_the_log_tail(
    bin_dir: Path,
) -> None:
    log = _fake_runtime(bin_dir)
    done = _smoke(bin_dir, FAKE_CURL_FAILS="1000")
    assert done.returncode == 1
    text = log.read_text()
    assert text.count("healthz") == 30
    assert "logs --tail 60 smoke-api" in text
    assert "container log line" in done.stdout
    # Failing before the entrypoint imports means they never ran.
    assert not any("websocket_monitor" in run for run in _runs(log))


@pytest.mark.parametrize(
    ("fail", "container"),
    [
        ("alembic upgrade head", "smoke-migrate"),
        ("app.core.scheduler", "smoke-import-scheduler"),
        ("app.core.taskiq_broker", "smoke-import-worker"),
        ("websocket_monitor", "smoke-import-websocket"),
    ],
)
def test_a_failing_step_prints_its_containers_last_sixty_lines(
    bin_dir: Path, fail: str, container: str
) -> None:
    log = _fake_runtime(bin_dir)
    done = _smoke(bin_dir, FAKE_DOCKER_FAIL=fail)
    assert done.returncode == 1
    assert f"logs --tail 60 {container}" in log.read_text()
    assert "container log line" in done.stdout


def test_a_failed_migration_never_starts_the_app(bin_dir: Path) -> None:
    log = _fake_runtime(bin_dir)
    done = _smoke(bin_dir, FAKE_DOCKER_FAIL="alembic upgrade head")
    assert done.returncode == 1
    assert not any("uvicorn" in run for run in _runs(log))


def test_install_smoke_refuses_a_missing_placeholder_setting(bin_dir: Path) -> None:
    log = _fake_runtime(bin_dir)
    env = _env(bin_dir, SMOKE_SLEEP="0", **SMOKE_ENV)
    del env["SECRET_KEY"]
    done = subprocess.run(
        ["bash", str(SMOKE), "registry.invalid/app@sha256:" + "a" * 64],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "SECRET_KEY" in done.stderr
    assert not log.exists()  # docker was never invoked


def test_install_smoke_defaults_are_thirty_attempts_two_seconds_apart() -> None:
    text = SMOKE.read_text(encoding="utf-8")
    assert 'attempts="${SMOKE_ATTEMPTS:-30}"' in text
    assert 'pause="${SMOKE_SLEEP:-2}"' in text
    assert os.access(SMOKE, os.X_OK)
