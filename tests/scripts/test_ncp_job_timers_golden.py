from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts import ncp_job_timers_golden as golden

PREFECT_REPO = Path("/Users/mgh3326/services/prefect")


def _fake_prefect_repo(tmp_path: Path) -> Path:
    """Copy the real helper and three real task modules into a tiny checkout."""
    repo = tmp_path / "prefect"
    helper_target = repo / "src/robin_automation/auto_trader_execution.py"
    helper_target.parent.mkdir(parents=True)
    shutil.copy2(
        PREFECT_REPO / "src/robin_automation/auto_trader_execution.py", helper_target
    )
    for filename in (
        "investor_flow_snapshots.py",
        "toss_warnings_sync.py",
        "invest_screener_snapshots_us.py",
    ):
        target = repo / "flows/auto_trader" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PREFECT_REPO / "flows/auto_trader" / filename, target)
    support = repo / "src/robin_automation"
    (support / "news_ingestor.py").write_text(
        "def redact_secrets(value):\n    return value\n"
    )
    (support / "placement.py").write_text(
        "def deployment_source_root(*_): return None\n"
        "def placement_job_variables(*_): return {}\n"
        "def placement_work_pool(*_): return ''\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return repo


def test_check_matches_generated_fixture_and_detects_one_token_mutant(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _fake_prefect_repo(tmp_path)
    fixture = tmp_path / "ncp_job_timers_prefect_argv.json"
    monkeypatch.setattr(golden, "FIXTURE", fixture)

    assert golden.run_cli(["--prefect-repo", str(repo), "--write"]) == 0
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 0

    fixture.write_text(fixture.read_text().replace("--commit", "--mutant", 1))
    # Assertion mutant: removing the diff check makes this assertion red.
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 1


def test_missing_prefect_repo_is_explicit_exit_two(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing-prefect"
    assert golden.run_cli(["--prefect-repo", str(missing), "--check"]) == 2
    assert f"Prefect repo is unavailable: {missing}" in capsys.readouterr().err
